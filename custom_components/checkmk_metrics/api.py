"""API client for Checkmk Metrics."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientSession

_LOGGER = logging.getLogger(__name__)

# Columns to request from the monitoring services endpoint so that
# perf_data and plugin_output are always included in the response.
_SERVICE_COLUMNS = [
    "host_name",
    "description",
    "state",
    "plugin_output",
    "perf_data",
    "long_plugin_output",
    "metrics",
]


class CheckmkApiError(Exception):
    """Raised when the Checkmk API returns an error or unexpected data."""


@dataclass(slots=True)
class MetricResult:
    """Normalized metric result."""

    value: float
    unit: str | None
    raw: dict[str, Any] | list[Any] | int | float


class CheckmkApiClient:
    """Small API client for Checkmk REST API."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        site: str,
        username: str,
        password: str,
        verify_ssl: bool,
    ) -> None:
        self._session = session
        self._base_url = self._build_base_url(base_url, site)
        self._headers = {
            "Authorization": f"Bearer {username} {password}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._ssl = verify_ssl

    @staticmethod
    def _build_base_url(base_url: str, site: str) -> str:
        base = base_url.rstrip("/")
        if base.endswith("/check_mk/api/1.0"):
            return base
        return f"{base}/{site}/check_mk/api/1.0"

    async def validate_connection(self) -> None:
        """Validate credentials and endpoint."""
        await self._request("GET", "/version")

    async def list_hosts(self) -> list[str]:
        """Return a sorted list of available hosts."""
        hosts: list[str] = []
        last_error: CheckmkApiError | None = None

        # Setup endpoint (requires configuration permissions)
        try:
            data = await self._request("GET", "/domain-types/host_config/collections/all")
            hosts.extend(
                self._extract_names_from_collection(
                    data, preferred=("name", "host_name")
                )
            )
        except CheckmkApiError as err:
            last_error = err

        # Monitoring endpoint fallback (works with read-only monitoring roles)
        try:
            data = await self._request(
                "POST",
                "/domain-types/host/collections/all",
                json_payload={},
            )
            hosts.extend(
                self._extract_names_from_collection(
                    data, preferred=("name", "host_name")
                )
            )
        except CheckmkApiError as err:
            last_error = err

        deduped = sorted(set(hosts), key=str.casefold)
        if deduped:
            return deduped

        if last_error is not None:
            raise last_error

        return []

    async def list_services(self, host: str) -> list[str]:
        """Return a sorted list of services for a host."""
        encoded_host = quote(host, safe="")

        # Try host-scoped services first.
        try:
            data = await self._request(
                "POST",
                f"/objects/host/{encoded_host}/collections/services",
                json_payload={},
            )
            services = self._extract_names_from_collection(
                data,
                preferred=("description", "service_description", "name"),
            )
            if services:
                return sorted(set(services), key=str.casefold)
        except CheckmkApiError:
            pass

        # Fallback: query all services and filter by host.
        data = await self._request(
            "POST",
            "/domain-types/service/collections/all",
            json_payload={
                "query": {
                    "op": "=",
                    "left": "host_name",
                    "right": host,
                }
            },
        )
        services = self._extract_names_from_collection(
            data,
            preferred=("description", "service_description", "name"),
        )
        return sorted(set(services), key=str.casefold)

    async def list_metrics(self, host: str, service: str) -> list[str]:
        """Return discovered metric names for a host/service."""
        encoded_host = quote(host, safe="")

        # Fetch a single service status object and inspect common fields.
        candidates: list[str] = []
        params_variants = [
            {"service_description": service},
            {"service": service},
            {},
        ]

        for params in params_variants:
            try:
                data = await self._request(
                    "GET",
                    f"/objects/host/{encoded_host}/actions/show_service/invoke",
                    query_params=params,
                )
            except CheckmkApiError:
                continue

            candidates.extend(self._extract_metric_names(data))
            if candidates:
                break

        # Fallback via service collection query for monitoring-only roles/setups.
        if not candidates:
            try:
                data = await self._request(
                    "POST",
                    "/domain-types/service/collections/all",
                    json_payload={
                        "query": {
                            "op": "and",
                            "expr": [
                                {"op": "=", "left": "host_name", "right": host},
                                {"op": "=", "left": "description", "right": service},
                            ],
                        },
                        "columns": _SERVICE_COLUMNS,
                    },
                )
                _LOGGER.debug("list_metrics collection response: %s", data)
                candidates.extend(self._extract_metric_names(data))
            except CheckmkApiError:
                pass

        return sorted(set(candidates), key=str.casefold)

    async def fetch_metric(
        self,
        host: str,
        service: str,
        metric: str,
    ) -> MetricResult:
        """Fetch a single metric by trying a few payload variants."""
        if metric == "__service_value__":
            snapshot = await self._get_service_snapshot(host, service)
            # Unwrap collection structure to get flat service data dict.
            unwrapped = self._unwrap_service_data(snapshot)
            _LOGGER.debug(
                "fetch_metric __service_value__ unwrapped=%s snapshot_type=%s",
                unwrapped,
                type(snapshot).__name__,
            )
            target = unwrapped if unwrapped is not None else snapshot
            parsed = self._extract_first_numeric(target)
            if parsed is not None:
                return parsed
            raise CheckmkApiError(
                f"No numeric service value found for {host}/{service}."
            )

        payload_variants = [
            {
                "host_name": host,
                "service_description": service,
                "metric_name": metric,
            },
            {"host": host, "service": service, "metric": metric},
            {"host": host, "service_description": service, "metric_name": metric},
        ]

        last_error: CheckmkApiError | None = None
        for payload in payload_variants:
            try:
                data = await self._request(
                    "POST",
                    "/domain-types/metric/actions/get/invoke",
                    json_payload=payload,
                )
                parsed = self._parse_metric_response(data, metric)
                if parsed is not None:
                    return parsed
                last_error = CheckmkApiError(
                    f"Metric response had no numeric value for '{metric}'."
                )
            except CheckmkApiError as err:
                last_error = err

        # Fallback: extract metric from service status payload.
        try:
            snapshot = await self._get_service_snapshot(host, service)
            # Try the unwrapped extensions dict first, then the raw snapshot.
            unwrapped = self._unwrap_service_data(snapshot)
            for target in (t for t in (unwrapped, snapshot) if t is not None):
                parsed = self._parse_metric_response(target, metric)
                if parsed is not None:
                    return parsed
            last_error = CheckmkApiError(
                f"Metric '{metric}' not found in service snapshot for {host}/{service}."
            )
        except CheckmkApiError as err:
            last_error = err

        raise last_error or CheckmkApiError("Unknown metric lookup error")

    async def _get_service_snapshot(self, host: str, service: str) -> Any:
        """Get one service status payload for metric fallback parsing."""
        encoded_host = quote(host, safe="")

        for params in (
            {"service_description": service},
            {"service": service},
            {},
        ):
            try:
                return await self._request(
                    "GET",
                    f"/objects/host/{encoded_host}/actions/show_service/invoke",
                    query_params=params,
                )
            except CheckmkApiError:
                continue

        data = await self._request(
            "POST",
            "/domain-types/service/collections/all",
            json_payload={
                "query": {
                    "op": "and",
                    "expr": [
                        {"op": "=", "left": "host_name", "right": host},
                        {"op": "=", "left": "description", "right": service},
                    ],
                },
                "columns": _SERVICE_COLUMNS,
            },
        )
        _LOGGER.debug("_get_service_snapshot collection response: %s", data)
        return data

    @staticmethod
    def _unwrap_service_data(data: Any) -> dict[str, Any] | None:
        """Extract the first service extensions dict from a collection response.

        Checkmk REST API collection responses wrap the actual service data
        inside  response -> value[0] -> extensions.  This helper unwraps that
        so callers get the flat dict with perf_data, plugin_output, etc.
        """
        if not isinstance(data, dict):
            return None

        # Already a flat service dict (from show_service or similar).
        for key in ("perf_data", "service_perf_data", "plugin_output", "service_plugin_output"):
            if key in data:
                return data

        # Check extensions at top level.
        extensions = data.get("extensions")
        if isinstance(extensions, dict):
            for key in ("perf_data", "service_perf_data", "plugin_output", "service_plugin_output"):
                if key in extensions:
                    return extensions

        # Unwrap collection: value -> [item] -> extensions.
        value = data.get("value")
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                item_ext = item.get("extensions")
                if isinstance(item_ext, dict):
                    return item_ext
                # Some endpoints put data directly in the item.
                for key in ("perf_data", "service_perf_data", "plugin_output", "service_plugin_output"):
                    if key in item:
                        return item

        return None

    async def _request(
        self,
        method: str,
        path: str,
        json_payload: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"

        try:
            request_json = json_payload if method in ("POST", "PUT", "PATCH") else None
            async with self._session.request(
                method,
                url,
                headers=self._headers,
                json=request_json,
                params=query_params,
                ssl=self._ssl,
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise CheckmkApiError(
                        f"HTTP {response.status} from {path}: {text[:300]}"
                    )

                if not text:
                    return {}

                try:
                    return json.loads(text)
                except json.JSONDecodeError as err:
                    raise CheckmkApiError(
                        f"Invalid JSON from {path}: {text[:200]}"
                    ) from err

        except ClientError as err:
            raise CheckmkApiError(str(err)) from err

    @staticmethod
    def _extract_names_from_collection(
        data: Any,
        preferred: tuple[str, ...],
    ) -> list[str]:
        out: list[str] = []
        values: list[Any] = []

        if isinstance(data, dict):
            if isinstance(data.get("value"), list):
                values = data["value"]
            elif isinstance(data.get("members"), list):
                values = data["members"]
            else:
                # Fallback for uncommon collection wrappers.
                for value in data.values():
                    if isinstance(value, list):
                        values = value
                        break

        for item in values:
            if not isinstance(item, dict):
                continue

            sources = [item]
            extensions = item.get("extensions")
            if isinstance(extensions, dict):
                sources.append(extensions)

            found: str | None = None
            for source in sources:
                for key in preferred:
                    value = source.get(key)
                    if isinstance(value, str) and value.strip():
                        found = value.strip()
                        break
                if found:
                    break

            if not found and isinstance(item.get("title"), str):
                found = item["title"].strip()
            if not found and isinstance(item.get("instanceId"), str):
                found = item["instanceId"].strip()

            if found:
                out.append(found)

        return out

    @staticmethod
    def _extract_metric_names(data: Any) -> list[str]:
        names: set[str] = set()

        def add_from_mapping(mapping: dict[str, Any]) -> None:
            metrics = mapping.get("metrics")
            if isinstance(metrics, list):
                for m in metrics:
                    if isinstance(m, str) and m.strip():
                        names.add(m.strip())
                    elif isinstance(m, dict):
                        for key in ("name", "metric", "metric_name", "id"):
                            v = m.get(key)
                            if isinstance(v, str) and v.strip():
                                names.add(v.strip())

            for perf_map_key in ("performance_data", "service_performance_data"):
                perf_map = mapping.get(perf_map_key)
                if isinstance(perf_map, dict):
                    for key in perf_map:
                        if isinstance(key, str) and key.strip():
                            names.add(key.strip())

            for perf_raw_key in ("perf_data", "service_perf_data"):
                perf_raw = mapping.get(perf_raw_key)
                if isinstance(perf_raw, str):
                    for part in perf_raw.split():
                        key = part.split("=", 1)[0].strip()
                        if key:
                            names.add(key)

            for output_key in ("plugin_output", "service_plugin_output"):
                output = mapping.get(output_key)
                if isinstance(output, str):
                    names.update(CheckmkApiClient._extract_metric_labels_from_output(output))

        if isinstance(data, dict):
            add_from_mapping(data)
            extensions = data.get("extensions")
            if isinstance(extensions, dict):
                add_from_mapping(extensions)
            members = data.get("members")
            if isinstance(members, dict):
                add_from_mapping(members)
            value = data.get("value")
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        add_from_mapping(item)
                        # Checkmk nests service data inside extensions.
                        item_ext = item.get("extensions")
                        if isinstance(item_ext, dict):
                            add_from_mapping(item_ext)

        return sorted(names)

    @staticmethod
    def _parse_metric_response(data: Any, metric_name: str) -> MetricResult | None:
        direct = CheckmkApiClient._extract_value(data, metric_name)
        if direct is not None:
            return direct

        if isinstance(data, dict):
            for key in ("extensions", "members", "value", "result", "data"):
                nested = data.get(key)
                parsed = CheckmkApiClient._extract_value(nested, metric_name)
                if parsed is not None:
                    return parsed

        return None

    @staticmethod
    def _extract_value(container: Any, metric_name: str) -> MetricResult | None:
        if container is None:
            return None

        if isinstance(container, (int, float)):
            return MetricResult(float(container), None, container)

        if isinstance(container, dict):
            if metric_name in container and isinstance(container[metric_name], (int, float)):
                return MetricResult(float(container[metric_name]), None, container)
            # Case-insensitive direct key lookup.
            for key, value in container.items():
                if (
                    isinstance(key, str)
                    and key.strip().lower() == metric_name.strip().lower()
                    and isinstance(value, (int, float))
                ):
                    return MetricResult(float(value), None, container)

            for perf_map_key in ("performance_data", "service_performance_data"):
                perf_map = container.get(perf_map_key)
                if isinstance(perf_map, dict) and metric_name in perf_map:
                    metric_value = perf_map.get(metric_name)
                    if isinstance(metric_value, (int, float)):
                        return MetricResult(float(metric_value), None, container)
                    if isinstance(metric_value, dict):
                        for key in ("value", "current", "last"):
                            value = metric_value.get(key)
                            if isinstance(value, (int, float)):
                                unit = (
                                    metric_value.get("unit")
                                    if isinstance(metric_value.get("unit"), str)
                                    else None
                                )
                                return MetricResult(float(value), unit, metric_value)

            for perf_raw_key in ("perf_data", "service_perf_data"):
                perf_raw = container.get(perf_raw_key)
                if isinstance(perf_raw, str):
                    parsed = CheckmkApiClient._extract_from_perf_data_string(
                        perf_raw, metric_name
                    )
                    if parsed is not None:
                        return parsed

            for output_key in ("plugin_output", "service_plugin_output"):
                output = container.get(output_key)
                if isinstance(output, str):
                    parsed = CheckmkApiClient._extract_from_plugin_output(
                        output, metric_name
                    )
                    if parsed is not None:
                        return parsed

            for key in ("value", "metric_value", "current", "last"):
                value = container.get(key)
                if isinstance(value, (int, float)):
                    unit = container.get("unit") if isinstance(container.get("unit"), str) else None
                    return MetricResult(float(value), unit, container)

            metrics = container.get("metrics")
            if isinstance(metrics, list):
                result = CheckmkApiClient._extract_value(metrics, metric_name)
                if result is not None:
                    return result

        if isinstance(container, list):
            for item in container:
                if not isinstance(item, dict):
                    continue

                nested_result = CheckmkApiClient._extract_value(item, metric_name)
                if nested_result is not None:
                    return nested_result

                name_candidates = [
                    item.get("name"),
                    item.get("metric"),
                    item.get("metric_name"),
                    item.get("id"),
                ]
                if metric_name in name_candidates:
                    for key in ("value", "metric_value", "current", "last"):
                        value = item.get(key)
                        if isinstance(value, (int, float)):
                            unit = item.get("unit") if isinstance(item.get("unit"), str) else None
                            return MetricResult(float(value), unit, item)

        return None

    @staticmethod
    def _extract_from_perf_data_string(
        perf_data: str,
        metric_name: str,
    ) -> MetricResult | None:
        """Parse perf_data like 'temp=48.1;80;90;0;100'."""
        parsed_candidates: list[MetricResult] = []
        for chunk in perf_data.split():
            if "=" not in chunk:
                continue
            name, raw_value = chunk.split("=", 1)
            name_clean = name.strip()

            value_part = raw_value.split(";", 1)[0].strip()
            numeric = ""
            unit = ""
            for char in value_part:
                if char.isdigit() or char in ".-":
                    numeric += char
                else:
                    unit += char

            try:
                value = float(numeric)
            except ValueError:
                continue

            result = MetricResult(value=value, unit=unit or None, raw={"perf_data": chunk})
            parsed_candidates.append(result)

            metric_lower = metric_name.strip().lower()
            name_lower = name_clean.lower()
            if (
                name_lower == metric_lower
                or name_lower.startswith(metric_lower)
                or metric_lower.startswith(name_lower)
            ):
                return result

        # If this service exposes only one metric, use it as fallback.
        if len(parsed_candidates) == 1:
            return parsed_candidates[0]
        return None

    @staticmethod
    def _extract_metric_labels_from_output(output: str) -> set[str]:
        """Extract labels from output like 'Temperature: 49.9 °C'."""
        labels: set[str] = set()
        for line in output.splitlines():
            match = re.match(r"^\s*([A-Za-z0-9 _./-]+)\s*:\s*[-+]?\d", line)
            if match:
                label = match.group(1).strip()
                if label:
                    labels.add(label)
        return labels

    @staticmethod
    def _extract_from_plugin_output(output: str, metric_name: str) -> MetricResult | None:
        """Parse values from plugin output lines like 'Temperature: 49.9 °C'."""
        metric_lower = metric_name.strip().lower()
        pattern = re.compile(
            r"^\s*([A-Za-z0-9 _./-]+)\s*:\s*([-+]?\d+(?:\.\d+)?)\s*([^\s]*)"
        )
        parsed_candidates: list[MetricResult] = []
        for line in output.splitlines():
            match = pattern.match(line)
            if not match:
                continue

            label, number, unit = match.group(1).strip(), match.group(2), match.group(3).strip()
            label_lower = label.lower()

            try:
                value = float(number)
            except ValueError:
                continue

            result = MetricResult(value=value, unit=unit or None, raw={"plugin_output": line})
            parsed_candidates.append(result)
            if (
                label_lower == metric_lower
                or label_lower.startswith(metric_lower)
                or metric_lower.startswith(label_lower)
            ):
                return result

        if len(parsed_candidates) == 1:
            return parsed_candidates[0]
        return None

    @staticmethod
    def _extract_first_numeric(data: Any) -> MetricResult | None:
        """Best-effort fallback: return first numeric metric found in snapshot."""
        if isinstance(data, dict):
            # 1) Prefer explicit performance maps.
            for perf_key in ("service_performance_data", "performance_data"):
                perf_map = data.get(perf_key)
                if isinstance(perf_map, dict):
                    for metric_value in perf_map.values():
                        if isinstance(metric_value, (int, float)):
                            return MetricResult(float(metric_value), None, {perf_key: perf_map})
                        if isinstance(metric_value, dict):
                            for key in ("value", "current", "last"):
                                value = metric_value.get(key)
                                if isinstance(value, (int, float)):
                                    unit = (
                                        metric_value.get("unit")
                                        if isinstance(metric_value.get("unit"), str)
                                        else None
                                    )
                                    return MetricResult(float(value), unit, {perf_key: metric_value})

            # 2) Prefer explicit perf_data strings.
            for perf_raw_key in ("service_perf_data", "perf_data"):
                perf_raw = data.get(perf_raw_key)
                if isinstance(perf_raw, str):
                    for chunk in perf_raw.split():
                        if "=" not in chunk:
                            continue
                        _, raw_value = chunk.split("=", 1)
                        value_part = raw_value.split(";", 1)[0].strip()
                        numeric = ""
                        unit = ""
                        for char in value_part:
                            if char.isdigit() or char in ".-":
                                numeric += char
                            else:
                                unit += char
                        try:
                            value = float(numeric)
                        except ValueError:
                            continue
                        return MetricResult(value=value, unit=unit or None, raw={"perf_data": chunk})

            # 3) Then plugin output lines like "Temperature: 49.9 °C".
            for output_key in ("service_plugin_output", "plugin_output"):
                output = data.get(output_key)
                if isinstance(output, str):
                    for line in output.splitlines():
                        match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*([^\s]*)", line)
                        if not match:
                            continue
                        try:
                            value = float(match.group(1))
                        except ValueError:
                            continue
                        unit = match.group(2).strip() or None
                        return MetricResult(value=value, unit=unit, raw={"plugin_output": line})

            # 4) Recursive fallback, but skip status-like numeric fields.
            skip_numeric_keys = {
                "state",
                "service_state",
                "host_state",
                "state_type",
                "service_state_type",
                "host_state_type",
                "hard_state",
                "service_hard_state",
                "host_hard_state",
                "last_state",
                "current_attempt",
                "current_notification_number",
                "is_service",
                "is_pending",
                "fixed",
                "type",
                "id",
            }
            for key, value in data.items():
                if key in skip_numeric_keys and isinstance(value, (int, float)):
                    continue
                result = CheckmkApiClient._extract_first_numeric(value)
                if result is not None:
                    return result

        if isinstance(data, list):
            for item in data:
                result = CheckmkApiClient._extract_first_numeric(item)
                if result is not None:
                    return result

        if isinstance(data, (int, float)):
            return MetricResult(float(data), None, data)

        return None
