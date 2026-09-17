"""Temperature + humidity sensors from the M30 zoneStatus."""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import M30BridgeCoordinator

# (suffix, name, device_class, unit, value_fn)
SENSORS: list[tuple] = [
    ("temperature", "Temperature", SensorDeviceClass.TEMPERATURE,
     UnitOfTemperature.FAHRENHEIT, lambda z: z.get("temperature")),
    ("humidity", "Humidity", SensorDeviceClass.HUMIDITY,
     PERCENTAGE, lambda z: z.get("humidity")),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: M30BridgeCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _discover() -> None:
        new = []
        for key in (coordinator.data or {}):
            for spec in SENSORS:
                uid = f"{key}:{spec[0]}"
                if uid not in known:
                    known.add(uid)
                    new.append(M30Sensor(coordinator, key, spec))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_discover))
    _discover()


class M30Sensor(CoordinatorEntity[M30BridgeCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: M30BridgeCoordinator, key: str, spec: tuple) -> None:
        super().__init__(coordinator)
        self._key = key
        self._value_fn: Callable[[dict], object] = spec[4]
        sys_id, _, zone = key.partition(":")
        self._attr_name = spec[1]
        self._attr_device_class = spec[2]
        self._attr_native_unit_of_measurement = spec[3]
        self._attr_unique_id = f"lennox_dds_{sys_id}_{zone}_{spec[0]}"
        self._attr_device_info = {"identifiers": {(DOMAIN, sys_id)}}

    @property
    def native_value(self):
        return self._value_fn((self.coordinator.data or {}).get(self._key, {}))

    @property
    def available(self) -> bool:
        return bool((self.coordinator.data or {}).get(self._key))
