"""Constants + zoneStatus enum maps for the Lennox iComfort (DDS) integration.

The DDS sidecar (add-on) streams zoneStatus JSON over a local WebSocket; this
integration consumes it. Enum values are the recovered IDL constants
(research/idl/lennox_m30.idl).
"""
from __future__ import annotations

from homeassistant.components.climate import HVACAction, HVACMode

DOMAIN = "lennox_dds"

CONF_WS_URL = "ws_url"
DEFAULT_WS_URL = "ws://localhost:8099"

# Lx_PeriodIDL::systemModeEnum  ->  HA HVACMode
SYSTEM_MODE_TO_HVAC = {
    0: HVACMode.OFF,
    1: HVACMode.HEAT,
    2: HVACMode.COOL,
    3: HVACMode.HEAT_COOL,
    4: HVACMode.HEAT,  # emergency heat -> heat (+ preset later)
}
HVAC_TO_SYSTEM_MODE = {
    HVACMode.OFF: 0,
    HVACMode.HEAT: 1,
    HVACMode.COOL: 2,
    HVACMode.HEAT_COOL: 3,
}

# LxZoneStatusIDL::tempOperationEnum  ->  HA HVACAction
TEMP_OPERATION_TO_ACTION = {
    0: HVACAction.IDLE,      # off
    1: HVACAction.HEATING,
    2: HVACAction.COOLING,
    3: HVACAction.IDLE,      # waiting
    4: HVACAction.IDLE,      # error
}

# Lx_PeriodIDL::fanmodeEnum  ->  HA fan mode strings
FAN_MODE_TO_STR = {1: "auto", 2: "circulate", 3: "on", 4: "auto_circulate"}
STR_TO_FAN_MODE = {v: k for k, v in FAN_MODE_TO_STR.items()}

# LxZoneStatusIDL zoneStatus boolean status flags -> binary sensors. Each is only
# created for a zone once its ZoneStatus_Valid_* bit is seen set, so devices
# without the feature (e.g. the base M30) don't get a phantom sensor.
# (json key, friendly name, ZoneStatus_Valid_* bit, mdi icon)
STATUS_BINARY_SENSORS = [
    ("allergenDefender", "Allergen Defender", 256, "mdi:air-filter"),
    ("ventilation", "Ventilation", 512, "mdi:fan"),
    ("aux", "Aux Heat", 1024, "mdi:heating-coil"),
    ("ssr", "Smooth Setback Recovery", 2048, "mdi:clock-outline"),
    ("defrost", "Defrost", 4096, "mdi:snowflake-melt"),
    ("heatCoast", "Heat Coast", 8192, "mdi:radiator"),
    ("coolCoast", "Cool Coast", 16384, "mdi:snowflake"),
]
