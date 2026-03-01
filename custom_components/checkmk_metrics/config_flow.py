"""Config flow for Checkmk Metrics."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .api import CheckmkApiError
from .const import (
    CONF_BASE_URL,
    CONF_METRICS,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SITE,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)
from .coordinator import CheckmkMetricsCoordinator, InvalidMetricFormat, merged_config


def _metrics_to_text(metrics: list[dict[str, str]]) -> str:
    return "\n".join(
        [
            ";".join(
                [
                    m["host"],
                    m["service"],
                    m["metric"],
                    m.get("name", m["metric"]),
                    m.get("unit", ""),
                ]
            ).rstrip(";")
            for m in metrics
        ]
    )


def _parse_metrics(text: str) -> list[dict[str, str]]:
    metrics: list[dict[str, str]] = []

    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = [part.strip() for part in line.split(";")]
        if len(parts) < 3:
            raise InvalidMetricFormat(
                f"Line {index} is invalid. Use: host;service;metric;name(optional);unit(optional)"
            )

        host, service, metric = parts[0], parts[1], parts[2]
        name = parts[3] if len(parts) > 3 and parts[3] else metric
        unit = parts[4] if len(parts) > 4 and parts[4] else ""

        metric_id = f"{host}__{service}__{metric}".replace(" ", "_").lower()
        metrics.append(
            {
                "id": metric_id,
                "host": host,
                "service": service,
                "metric": metric,
                "name": name,
                "unit": unit,
            }
        )

    if not metrics:
        raise InvalidMetricFormat("Please define at least one metric")

    return metrics


async def _validate_input(hass: HomeAssistant, data: dict[str, Any]) -> None:
    coordinator = CheckmkMetricsCoordinator(hass, data)
    await coordinator.async_validate()


class CheckmkMetricsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Checkmk Metrics."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                parsed = dict(user_input)
                parsed[CONF_METRICS] = _parse_metrics(user_input[CONF_METRICS])

                await _validate_input(self.hass, parsed)
                await self.async_set_unique_id(
                    f"{parsed[CONF_BASE_URL]}::{parsed[CONF_SITE]}::{parsed[CONF_USERNAME]}"
                )
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data=parsed,
                )
            except InvalidMetricFormat:
                errors["base"] = "invalid_metrics"
            except CheckmkApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_BASE_URL): str,
                vol.Required(CONF_SITE): str,
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=10)
                ),
                vol.Required(
                    CONF_METRICS,
                    default="host01;CPU load;load15;CPU Load;\n",
                ): str,
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return CheckmkMetricsOptionsFlow(config_entry)


class CheckmkMetricsOptionsFlow(config_entries.OptionsFlow):
    """Handle options for Checkmk Metrics."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        current = merged_config(self._config_entry)

        if user_input is not None:
            try:
                options = {
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_VERIFY_SSL: bool(user_input[CONF_VERIFY_SSL]),
                    CONF_METRICS: _parse_metrics(user_input[CONF_METRICS]),
                }
                return self.async_create_entry(title="", data=options)
            except InvalidMetricFormat:
                errors["base"] = "invalid_metrics"
            except Exception:
                errors["base"] = "unknown"

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=10)),
                vol.Required(
                    CONF_VERIFY_SSL,
                    default=current.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
                ): bool,
                vol.Required(
                    CONF_METRICS,
                    default=_metrics_to_text(current[CONF_METRICS]),
                ): str,
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
