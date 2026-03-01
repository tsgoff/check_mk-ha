"""Sensor platform for Checkmk Metrics."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_METRICS
from .coordinator import CheckmkMetricsCoordinator, merged_config


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Checkmk metric sensors from config entry."""
    config = merged_config(entry)

    coordinator = CheckmkMetricsCoordinator(hass, config)
    await coordinator.async_config_entry_first_refresh()

    entities = [
        CheckmkMetricSensor(coordinator, entry, metric) for metric in config[CONF_METRICS]
    ]
    async_add_entities(entities)


class CheckmkMetricSensor(CoordinatorEntity[CheckmkMetricsCoordinator], SensorEntity):
    """Representation of one Checkmk metric."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CheckmkMetricsCoordinator,
        entry: ConfigEntry,
        metric: dict[str, str],
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._metric = metric

        self._attr_unique_id = f"{entry.entry_id}_{metric['id']}"
        self._attr_name = metric["name"]

    @property
    def native_value(self) -> float | None:
        """Return the current metric value."""
        current = self.coordinator.data.get(self._metric["id"], {})
        value = current.get("value")
        if isinstance(value, (int, float)):
            return float(value)
        return None

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        current = self.coordinator.data.get(self._metric["id"], {})
        return self.coordinator.last_update_success and "value" in current

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return preferred unit of measurement."""
        if self._metric.get("unit"):
            return self._metric["unit"]

        current = self.coordinator.data.get(self._metric["id"], {})
        unit = current.get("unit")
        if isinstance(unit, str) and unit:
            return unit

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes for debugging and traceability."""
        current = self.coordinator.data.get(self._metric["id"], {})
        attrs: dict[str, Any] = {
            "host": self._metric["host"],
            "service": self._metric["service"],
            "metric": self._metric["metric"],
        }

        if "error" in current:
            attrs["api_error"] = current["error"]

        return attrs
