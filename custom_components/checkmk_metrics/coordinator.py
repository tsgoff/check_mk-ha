"""Data coordinator for Checkmk Metrics."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CheckmkApiClient, CheckmkApiError
from .const import (
    CONF_BASE_URL,
    CONF_METRICS,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SITE,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DOMAIN,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_VERIFY_SSL,
)

_LOGGER = logging.getLogger(__name__)


class CheckmkMetricsCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator that fetches all selected Checkmk metrics."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self._metrics: list[dict[str, str]] = config[CONF_METRICS]

        scan_interval = int(config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=max(10, scan_interval)),
        )

        self._client = CheckmkApiClient(
            async_get_clientsession(hass),
            config[CONF_BASE_URL],
            config[CONF_SITE],
            config[CONF_USERNAME],
            config[CONF_PASSWORD],
            bool(config.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
        )

    async def async_validate(self) -> None:
        """Validate API connection once."""
        await self._client.validate_connection()

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        data: dict[str, dict[str, Any]] = {}
        successful = 0

        for metric in self._metrics:
            metric_id = metric["id"]
            try:
                result = await self._client.fetch_metric(
                    metric["host"], metric["service"], metric["metric"]
                )
                data[metric_id] = {
                    "value": result.value,
                    "unit": result.unit,
                    "raw": result.raw,
                }
                successful += 1
            except CheckmkApiError as err:
                data[metric_id] = {
                    "error": str(err),
                }

        if successful == 0:
            raise UpdateFailed("No configured metric could be fetched")

        return data


def merged_config(entry: Any) -> dict[str, Any]:
    """Merge config entry data and options."""
    merged = dict(entry.data)
    merged.update(entry.options)
    return merged


class InvalidMetricFormat(HomeAssistantError):
    """Raised for invalid metric definition string."""
