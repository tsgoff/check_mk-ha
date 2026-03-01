"""Config flow for Checkmk Metrics."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .api import CheckmkApiClient, CheckmkApiError
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
from .coordinator import InvalidMetricFormat, merged_config

FIELD_HOST = "host"
FIELD_SERVICE = "service"
FIELD_METRIC_CHOICES = "metric_choices"
FIELD_METRICS_MANUAL = "metrics_manual"
FIELD_ADD_MORE = "add_more"


def _make_metric_id(host: str, service: str, metric: str) -> str:
    return f"{host}__{service}__{metric}".replace(" ", "_").lower()


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

        metrics.append(
            {
                "id": _make_metric_id(host, service, metric),
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


def _parse_manual_metric_list(raw: str) -> list[str]:
    if not raw.strip():
        return []

    values = [v.strip() for v in raw.replace("\n", ",").split(",")]
    return [v for v in values if v]


class CheckmkMetricsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Checkmk Metrics."""

    VERSION = 2

    def __init__(self) -> None:
        self._base_config: dict[str, Any] = {}
        self._selected_metrics: list[dict[str, str]] = []
        self._api: CheckmkApiClient | None = None
        self._hosts: list[str] = []
        self._services: list[str] = []
        self._metric_candidates: list[str] = []
        self._current_host = ""
        self._current_service = ""

    async def _ensure_api(self) -> CheckmkApiClient:
        if self._api is not None:
            return self._api

        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        self._api = CheckmkApiClient(
            async_get_clientsession(self.hass),
            self._base_config[CONF_BASE_URL],
            self._base_config[CONF_SITE],
            self._base_config[CONF_USERNAME],
            self._base_config[CONF_PASSWORD],
            bool(self._base_config.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
        )
        return self._api

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 1: credentials and base config."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._base_config = {
                CONF_NAME: user_input[CONF_NAME],
                CONF_BASE_URL: user_input[CONF_BASE_URL],
                CONF_SITE: user_input[CONF_SITE],
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_VERIFY_SSL: bool(user_input[CONF_VERIFY_SSL]),
                CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
            }

            try:
                api = await self._ensure_api()
                await api.validate_connection()
                self._hosts = await api.list_hosts()
                if not self._hosts:
                    errors["base"] = "no_hosts"
                else:
                    await self.async_set_unique_id(
                        f"{self._base_config[CONF_BASE_URL]}::"
                        f"{self._base_config[CONF_SITE]}::"
                        f"{self._base_config[CONF_USERNAME]}"
                    )
                    self._abort_if_unique_id_configured()
                    return await self.async_step_select_host()
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
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_select_host(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: choose host."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._current_host = user_input[FIELD_HOST]
            try:
                api = await self._ensure_api()
                self._services = await api.list_services(self._current_host)
                if not self._services:
                    errors["base"] = "no_services"
                else:
                    return await self.async_step_select_service()
            except CheckmkApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"

        host_selector = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=self._hosts,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
        schema = vol.Schema(
            {
                vol.Required(FIELD_HOST, default=self._hosts[0] if self._hosts else ""): host_selector,
            }
        )

        return self.async_show_form(
            step_id="select_host",
            data_schema=schema,
            errors=errors,
            description_placeholders={"selected_count": str(len(self._selected_metrics))},
        )

    async def async_step_select_service(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3: choose service."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._current_service = user_input[FIELD_SERVICE]
            try:
                api = await self._ensure_api()
                self._metric_candidates = await api.list_metrics(
                    self._current_host, self._current_service
                )
                return await self.async_step_select_metrics()
            except CheckmkApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"

        service_selector = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=self._services,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

        schema = vol.Schema(
            {
                vol.Required(
                    FIELD_SERVICE,
                    default=self._services[0] if self._services else "",
                ): service_selector,
            }
        )

        return self.async_show_form(
            step_id="select_service",
            data_schema=schema,
            errors=errors,
            description_placeholders={"host": self._current_host},
        )

    async def async_step_select_metrics(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 4: choose one or more metrics for current host/service."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected: list[str] = list(user_input.get(FIELD_METRIC_CHOICES, []))
            selected.extend(_parse_manual_metric_list(user_input.get(FIELD_METRICS_MANUAL, "")))

            deduped = sorted(set([s.strip() for s in selected if s.strip()]), key=str.casefold)
            if not deduped:
                errors["base"] = "no_metrics_selected"
            else:
                for metric_name in deduped:
                    metric = {
                        "id": _make_metric_id(
                            self._current_host,
                            self._current_service,
                            metric_name,
                        ),
                        "host": self._current_host,
                        "service": self._current_service,
                        "metric": metric_name,
                        "name": f"{self._current_host} {self._current_service} {metric_name}",
                        "unit": "",
                    }
                    if metric["id"] not in {m["id"] for m in self._selected_metrics}:
                        self._selected_metrics.append(metric)

                if user_input.get(FIELD_ADD_MORE, False):
                    return await self.async_step_select_host()

                return self._create_final_entry()

        metric_options = self._metric_candidates or []
        metrics_selector = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=metric_options,
                mode=selector.SelectSelectorMode.DROPDOWN,
                multiple=True,
            )
        )

        schema = vol.Schema(
            {
                vol.Optional(FIELD_METRIC_CHOICES, default=[]): metrics_selector,
                vol.Optional(FIELD_METRICS_MANUAL, default=""): str,
                vol.Optional(FIELD_ADD_MORE, default=False): bool,
            }
        )

        return self.async_show_form(
            step_id="select_metrics",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "host": self._current_host,
                "service": self._current_service,
                "discovered": str(len(metric_options)),
                "selected_count": str(len(self._selected_metrics)),
            },
        )

    def _create_final_entry(self) -> FlowResult:
        data = dict(self._base_config)
        data[CONF_METRICS] = self._selected_metrics
        return self.async_create_entry(
            title=self._base_config[CONF_NAME],
            data=data,
        )

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
