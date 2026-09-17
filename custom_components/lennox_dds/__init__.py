"""Lennox iComfort (DDS) integration.

Consumes zoneStatus streamed by the DDS sidecar (add-on) over a local WebSocket
and exposes climate + sensor entities. The heavy lifting (DDS-Security, RtpsRelay,
XTypes) lives in the sidecar; this integration is a thin local_push client.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_WS_URL, DOMAIN
from .coordinator import M30BridgeCoordinator

PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = M30BridgeCoordinator(hass, entry.data[CONF_WS_URL])
    await coordinator.async_start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: M30BridgeCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unloaded
