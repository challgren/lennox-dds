"""Humidify / dehumidify setpoint numbers from the M30 zoneStatus period.

Settable analogues of lennoxs30's humidity setpoint numbers. Only created when the
device actually publishes the field (capability-gated) — a system without humidity
control won't get them. Writes go back through the same scheduleUpdate period as the
temperature setpoints (manual hold slot), so the M30 applies them promptly.
"""
from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import M30BridgeCoordinator

# (suffix, name, period_key/command_field, min, max)
NUMBERS: list[tuple] = [
    ("humidify_setpoint", "Humidify Setpoint", "husp", 15, 45),
    ("dehumidify_setpoint", "Dehumidify Setpoint", "desp", 40, 60),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: M30BridgeCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _discover() -> None:
        new = []
        for key, sample in (coordinator.data or {}).items():
            period = sample.get("period") or {}
            for spec in NUMBERS:
                uid = f"{key}:{spec[0]}"
                if uid in known or period.get(spec[2]) is None:
                    continue  # capability gate: only if the device publishes it
                known.add(uid)
                new.append(M30SetpointNumber(coordinator, key, spec))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_discover))
    _discover()


class M30SetpointNumber(CoordinatorEntity[M30BridgeCoordinator], NumberEntity):
    _attr_has_entity_name = True
    _attr_device_class = NumberDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: M30BridgeCoordinator, key: str, spec: tuple) -> None:
        super().__init__(coordinator)
        self._key = key
        sys_id, _, zone = key.partition(":")
        self._sys_id = sys_id
        self._zone_id = int(zone) if zone.isdigit() else 0
        self._field = spec[2]  # both the zoneStatus period key and the SET field
        self._attr_name = spec[1]
        self._attr_native_min_value = spec[3]
        self._attr_native_max_value = spec[4]
        self._attr_unique_id = f"lennox_dds_{sys_id}_{zone}_{spec[0]}"
        self._attr_device_info = {"identifiers": {(DOMAIN, sys_id)}}

    @property
    def _period(self) -> dict:
        return ((self.coordinator.data or {}).get(self._key) or {}).get("period") or {}

    @property
    def native_value(self):
        v = self._period.get(self._field)
        return float(v) if v is not None else None

    @property
    def available(self) -> bool:
        return self._period.get(self._field) is not None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_send_command({
            "sysID": self._sys_id,
            "zoneId": self._zone_id,
            self._field: int(value),
        })
