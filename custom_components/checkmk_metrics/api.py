"""API client for Checkmk Metrics."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

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
    ) -> Any:
        url = f"{self._base_url}{path}"

        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers,
                json=json_payload,
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
