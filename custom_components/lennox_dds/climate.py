"""Climate entity mapping the M30 zoneStatus (via the DDS bridge)."""
from __future__ import annotations

import time

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    FAN_MODE_TO_STR,
    HVAC_TO_SYSTEM_MODE,
    STR_TO_FAN_MODE,
    SYSTEM_MODE_TO_HVAC,
    TEMP_OPERATION_TO_ACTION,
)
from .coordinator import M30BridgeCoordinator

# How long to show an optimistically-set value before reverting to the device's
# reported value. The M30 cloud/relay path can take a few minutes to apply and
# echo a setpoint/mode/fan change, so without this the card shows the OLD value
# the whole time and users assume the change failed (and retry).
_OPTIMISTIC_TTL = 300  # seconds


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: M30BridgeCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _discover() -> None:
        new = [M30Climate(coordinator, k) for k in (coordinator.data or {}) if k not in known]
        for e in new:
            known.add(e._key)
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_discover))
    _discover()


class M30Climate(CoordinatorEntity[M30BridgeCoordinator], ClimateEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]
    _attr_fan_modes = list(STR_TO_FAN_MODE)

    def __init__(self, coordinator: M30BridgeCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        sys_id, _, zone = key.partition(":")
        self._sys_id = sys_id
        self._zone_id = int(zone) if zone.isdigit() else 0
        self._attr_unique_id = f"lennox_dds_{sys_id}_{zone}_climate"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, sys_id)},
            "name": f"Lennox iComfort {sys_id[:8]}",
            "manufacturer": "Lennox",
            "model": "iComfort",
        }
        # optimistic overlay: field -> (value, monotonic_ts) set on control writes,
        # cleared once the device echoes the value back or the TTL expires.
        self._pending: dict[str, tuple] = {}

    @property
    def _z(self) -> dict:
        return (self.coordinator.data or {}).get(self._key, {})

    @property
    def _period(self) -> dict:
        return self._z.get("period", {})

    def _eff(self, field: str, device_value):
        """Return the optimistic pending value for a period field if we're still
        waiting for the device to catch up; otherwise the device's value."""
        p = self._pending.get(field)
        if p is None:
            return device_value
        val, ts = p
        # reconciled: device now reports what we asked for (float-tolerant)
        try:
            if device_value is not None and abs(float(device_value) - float(val)) < 0.5:
                del self._pending[field]
                return device_value
        except (TypeError, ValueError):
            if device_value == val:
                del self._pending[field]
                return device_value
        # expired: stop pretending, show the device's real value again
        if time.monotonic() - ts > _OPTIMISTIC_TTL:
            del self._pending[field]
            return device_value
        return val

    def _set_pending(self, **fields) -> None:
        now = time.monotonic()
        for k, v in fields.items():
            self._pending[k] = (v, now)
        self.async_write_ha_state()  # reflect immediately in the UI

    @property
    def available(self) -> bool:
        return bool(self._z)

    @property
    def current_temperature(self):
        return self._z.get("temperature")

    @property
    def current_humidity(self):
        return self._z.get("humidity")

    @property
    def supported_features(self):
        # A range (two setpoints) only makes sense in heat_cool; heat/cool/off use
        # a single target. Advertising both at once makes the card show a range in
        # cool mode (heat setpoint bleeds in), so pick per mode.
        features = ClimateEntityFeature.FAN_MODE
        if self.hvac_mode == HVACMode.HEAT_COOL:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        else:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        return features

    @property
    def hvac_mode(self):
        return SYSTEM_MODE_TO_HVAC.get(self._eff("systemMode", self._period.get("systemMode")))

    @property
    def hvac_action(self):
        return TEMP_OPERATION_TO_ACTION.get(self._z.get("tempOperation"))

    @property
    def fan_mode(self):
        return FAN_MODE_TO_STR.get(self._eff("fanMode", self._period.get("fanMode")))

    @property
    def target_temperature(self):
        # single target for heat/cool/off; None in heat_cool (uses low/high)
        mode = self.hvac_mode
        if mode == HVACMode.HEAT:
            return self._eff("hsp", self._period.get("hsp"))
        if mode == HVACMode.COOL:
            return self._eff("csp", self._period.get("csp"))
        if mode == HVACMode.HEAT_COOL:
            return None
        return self._eff("sp", self._period.get("sp"))

    @property
    def target_temperature_low(self):
        if self.hvac_mode != HVACMode.HEAT_COOL:
            return None
        return self._eff("hsp", self._period.get("hsp"))

    @property
    def target_temperature_high(self):
        if self.hvac_mode != HVACMode.HEAT_COOL:
            return None
        return self._eff("csp", self._period.get("csp"))

    # --- control: send a command over the bridge WebSocket -> DDS scheduleUpdate ---
    def _base_command(self) -> dict:
        return {"sysID": self._sys_id, "zoneId": self._zone_id}

    async def async_set_temperature(self, **kwargs) -> None:
        cmd = self._base_command()
        pending: dict = {}
        if ATTR_TARGET_TEMP_LOW in kwargs or ATTR_TARGET_TEMP_HIGH in kwargs:
            # heat_cool range: low = heat setpoint (hsp), high = cool setpoint (csp)
            if (low := kwargs.get(ATTR_TARGET_TEMP_LOW)) is not None:
                cmd["hsp"] = low; pending["hsp"] = low
            if (high := kwargs.get(ATTR_TARGET_TEMP_HIGH)) is not None:
                cmd["csp"] = high; pending["csp"] = high
        elif (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            mode = self.hvac_mode
            if mode == HVACMode.HEAT:
                cmd["hsp"] = temp; pending["hsp"] = temp
            elif mode == HVACMode.COOL:
                cmd["csp"] = temp; pending["csp"] = temp
            else:
                cmd["sp"] = temp; pending["sp"] = temp
        if len(cmd) > 2:  # something to write beyond sysID/zoneId
            self._set_pending(**pending)  # optimistic: show it immediately
            await self.coordinator.async_send_command(cmd)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        mode = HVAC_TO_SYSTEM_MODE.get(hvac_mode)
        if mode is None:
            return
        self._set_pending(systemMode=mode)
        await self.coordinator.async_send_command({**self._base_command(), "mode": mode})

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        fan = STR_TO_FAN_MODE.get(fan_mode)
        if fan is None:
            return
        self._set_pending(fanMode=fan)
        await self.coordinator.async_send_command({**self._base_command(), "fan": fan})
