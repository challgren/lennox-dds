"""Diagnostics: the one-file bug report.

Settings -> Devices & Services -> Lennox iComfort (DDS) -> ⋮ -> Download
diagnostics gives us (nearly) everything needed to debug an issue or spot a field
we don't handle yet on another model:

- integration version + connection info,
- per-zone raw samples (sysID redacted),
- decoded status flags / validFlag bits, and
- an explicit `unrecognized_fields` list so NEW fields on other hardware jump out.

Nothing secret is included: sysID is redacted, and the add-on holds all
credentials/tokens (they never reach the integration).
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN, STATUS_BINARY_SENSORS
from .coordinator import M30BridgeCoordinator

_REDACT = {"sysID"}

# top-level zoneStatus keys we currently understand (used to flag new ones)
_KNOWN_KEYS = {
    "zoneId", "sysID", "temperature", "temperatureC", "humidity", "tempStatus",
    "humidityStatus", "tempOperation", "humOperation", "fan", "balancePoint",
    "validFlag", "period", "scheduleExceptionIds",
} | {f for f, *_ in STATUS_BINARY_SENSORS}

_KNOWN_PERIOD_KEYS = {
    "systemMode", "fanMode", "sp", "spC", "hsp", "hspC", "csp", "cspC",
    "husp", "desp", "away",
}


def _redact(sample: dict) -> dict:
    return {k: ("**REDACTED**" if k in _REDACT else v) for k, v in sample.items()}


def _decode(sample: dict) -> dict:
    """Human-readable decode of the bitmask + flags, so a report is legible."""
    valid = int(sample.get("validFlag", 0) or 0)
    return {
        "validFlag_bits": {name: bool(valid & bit)
                           for field, name, bit, _ in STATUS_BINARY_SENSORS},
        "active_flags": [f for f, *_ in STATUS_BINARY_SENSORS if sample.get(f)],
    }


def _unrecognized(sample: dict) -> dict:
    """Fields present on THIS device that we don't model yet -> please report."""
    out: dict[str, Any] = {}
    extra = sorted(set(sample) - _KNOWN_KEYS)
    if extra:
        out["top_level"] = extra
    period = sample.get("period")
    if isinstance(period, dict):
        pextra = sorted(set(period) - _KNOWN_PERIOD_KEYS)
        if pextra:
            out["period"] = pextra
    return out


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: M30BridgeCoordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data or {}

    try:
        version = str((await async_get_integration(hass, DOMAIN)).version)
    except Exception:  # pragma: no cover - defensive
        version = "unknown"

    zones: dict[str, Any] = {}
    all_unrecognized: dict[str, Any] = {}
    for key, sample in data.items():
        zk = f"zone-{key.split(':')[-1]}"
        zones[zk] = {
            "raw": _redact(sample),
            "decoded": _decode(sample),
        }
        if (u := _unrecognized(sample)):
            all_unrecognized[zk] = u

    return {
        "integration": {
            "version": version,
            "ws_url": entry.data.get("ws_url"),
            "zone_count": len(data),
        },
        # If this is non-empty, your model exposes fields we don't handle yet —
        # please include this diagnostics file in a GitHub issue.
        "unrecognized_fields": all_unrecognized,
        "zones": zones,
    }
