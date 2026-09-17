"""Diagnostics: dump the latest raw zoneStatus per zone.

Lets anyone hit Settings -> Devices & Services -> Lennox iComfort (DDS) -> ⋮ ->
Download diagnostics and share the full samples, so fields we don't surface yet
(new/other-model flags) are easy to spot. sysID is redacted.
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import M30BridgeCoordinator

_REDACT = {"sysID"}


def _redact(sample: dict) -> dict:
    return {k: ("**REDACTED**" if k in _REDACT else v) for k, v in sample.items()}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: M30BridgeCoordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data or {}
    return {
        "entry": {"ws_url": entry.data.get("ws_url")},
        "zone_count": len(data),
        # raw samples keyed by "zone-N" (sysID stripped from the key + body)
        "samples": {
            f"zone-{k.split(':')[-1]}": _redact(v) for k, v in data.items()
        },
    }
