"""API client for Checkmk Metrics."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientSession


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
        data = await self._request("GET", "/domain-types/host_config/collections/all")
        hosts = self._extract_names_from_collection(data, preferred=("name", "host_name"))
        return sorted(set(hosts), key=str.casefold)

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

        return sorted(set(candidates), key=str.casefold)

    async def fetch_metric(
        self,
        host: str,
        service: str,
        metric: str,
    ) -> MetricResult:
        """Fetch a single metric by trying a few payload variants."""
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

        raise last_error or CheckmkApiError("Unknown metric lookup error")

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
        values = []

        if isinstance(data, dict):
            if isinstance(data.get("value"), list):
                values = data["value"]
            elif isinstance(data.get("members"), list):
                values = data["members"]

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

            perf_map = mapping.get("performance_data")
            if isinstance(perf_map, dict):
                for key in perf_map:
                    if isinstance(key, str) and key.strip():
                        names.add(key.strip())

            perf_raw = mapping.get("perf_data")
            if isinstance(perf_raw, str):
                for part in perf_raw.split():
                    key = part.split("=", 1)[0].strip()
                    if key:
                        names.add(key)

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
