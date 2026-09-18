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
# Setpoints come straight from the active period (csp/hsp are ALWAYS published, in
# every HVAC mode), so both show as dedicated sensors regardless of mode — unlike the
# climate entity, which only exposes the mode-relevant target. Declared °F with a
# temperature device_class, so HA auto-converts them for a °C household. Exposing them
# as sensors also puts setpoint changes in normal history/logbook (the climate
# logbook only records HVAC-mode changes, not setpoint changes).
SENSORS: list[tuple] = [
    ("temperature", "Temperature", SensorDeviceClass.TEMPERATURE,
     UnitOfTemperature.FAHRENHEIT, lambda z: z.get("temperature")),
    ("humidity", "Humidity", SensorDeviceClass.HUMIDITY,
     PERCENTAGE, lambda z: z.get("humidity")),
    ("cool_setpoint", "Cool Setpoint", SensorDeviceClass.TEMPERATURE,
     UnitOfTemperature.FAHRENHEIT, lambda z: (z.get("period") or {}).get("csp")),
    ("heat_setpoint", "Heat Setpoint", SensorDeviceClass.TEMPERATURE,
     UnitOfTemperature.FAHRENHEIT, lambda z: (z.get("period") or {}).get("hsp")),
]

# System-wide sensors (per sysID), sourced from the merged system/weather objects.
# (suffix, name, device_class, unit, gate_key, value_fn)
def _outdoor_temp(s: dict):
    # Prefer the weather-service value (what the M30 actually displays); fall back
    # to systemStatus.outdoorTemperature (outdoor-unit sensor, tends to lag/read low).
    w = (s.get("weather") or {}).get("temperature")
    return w if w is not None else (s.get("system") or {}).get("outdoorTemperature")


SYSTEM_SENSORS: list[tuple] = [
    ("outdoor_temperature", "Outdoor Temperature", SensorDeviceClass.TEMPERATURE,
     UnitOfTemperature.FAHRENHEIT, "system", _outdoor_temp),
    ("outdoor_humidity", "Outdoor Humidity", SensorDeviceClass.HUMIDITY,
     PERCENTAGE, "weather", lambda s: (s.get("weather") or {}).get("humidity")),
    ("wind_speed", "Wind Speed", SensorDeviceClass.WIND_SPEED,
     "mph", "weather", lambda s: (s.get("weather") or {}).get("windSpeed")),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: M30BridgeCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()
    known_sys: set[str] = set()

    @callback
    def _discover() -> None:
        new = []
        for key, sample in (coordinator.data or {}).items():
            for spec in SENSORS:
                uid = f"{key}:{spec[0]}"
                if uid not in known:
                    known.add(uid)
                    new.append(M30Sensor(coordinator, key, spec))
            # per-system sensors, created once the source object is present
            sys_id = key.partition(":")[0]
            for spec in SYSTEM_SENSORS:
                uid = f"{sys_id}:{spec[0]}"
                if uid not in known_sys and sample.get(spec[4]) is not None:
                    known_sys.add(uid)
                    new.append(M30SystemSensor(coordinator, sys_id, spec))
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


class M30SystemSensor(CoordinatorEntity[M30BridgeCoordinator], SensorEntity):
    """A per-system sensor sourced from the merged system/weather objects."""
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: M30BridgeCoordinator, sys_id: str, spec: tuple) -> None:
        super().__init__(coordinator)
        self._sys_id = sys_id
        self._value_fn: Callable[[dict], object] = spec[5]
        self._attr_name = spec[1]
        self._attr_device_class = spec[2]
        self._attr_native_unit_of_measurement = spec[3]
        self._attr_unique_id = f"lennox_dds_{sys_id}_{spec[0]}"
        self._attr_device_info = {"identifiers": {(DOMAIN, sys_id)}}

    def _sample(self) -> dict:
        for key, sample in (self.coordinator.data or {}).items():
            if key.partition(":")[0] == self._sys_id:
                return sample
        return {}

    @property
    def native_value(self):
        return self._value_fn(self._sample())

    @property
    def available(self) -> bool:
        return self._value_fn(self._sample()) is not None
