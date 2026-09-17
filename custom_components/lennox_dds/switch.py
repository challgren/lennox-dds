"""Manual Away switch.

Away is a system-wide (per-sysID) setting on the M30, so one switch is created
per device. State is read from any zone's period.away; toggling writes a
manualAwayUpdate over the bridge -> DDS ("Owner Manual Away").
"""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import M30BridgeCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: M30BridgeCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _discover() -> None:
        new = []
        for key in (coordinator.data or {}):
            sys_id = key.partition(":")[0]
            if sys_id in known:
                continue
            known.add(sys_id)
            new.append(M30AwaySwitch(coordinator, sys_id))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_discover))
    _discover()


class M30AwaySwitch(CoordinatorEntity[M30BridgeCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Away"
    _attr_icon = "mdi:home-export-outline"

    def __init__(self, coordinator: M30BridgeCoordinator, sys_id: str) -> None:
        super().__init__(coordinator)
        self._sys_id = sys_id
        self._attr_unique_id = f"lennox_dds_{sys_id}_away"
        self._attr_device_info = {"identifiers": {(DOMAIN, sys_id)}}

    def _zones(self):
        for key, sample in (self.coordinator.data or {}).items():
            if key.partition(":")[0] == self._sys_id:
                yield sample

    @property
    def available(self) -> bool:
        return any(True for _ in self._zones())

    @property
    def is_on(self):
        # Away is system-wide; any zone reflecting it means we're away.
        return any(z.get("period", {}).get("away") for z in self._zones())

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_send_command({"sysID": self._sys_id, "away": True})

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_send_command({"sysID": self._sys_id, "away": False})
