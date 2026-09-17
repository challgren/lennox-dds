"""Zone status flags (allergen defender, ventilation, aux, defrost, ...).

Each flag is created for a zone only once the device marks it valid via the
zoneStatus validFlag, so hardware without the feature shows nothing.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, STATUS_BINARY_SENSORS
from .coordinator import M30BridgeCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: M30BridgeCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()
    known_alerts: set[str] = set()

    @callback
    def _discover() -> None:
        new = []
        for key, sample in (coordinator.data or {}).items():
            valid = sample.get("validFlag", 0) or 0
            for field, name, bit, icon in STATUS_BINARY_SENSORS:
                uid = f"{key}:{field}"
                # only create once the device says this flag is meaningful
                if uid in known or not (valid & bit):
                    continue
                known.add(uid)
                new.append(M30StatusBinarySensor(coordinator, key, field, name, icon))
            # per-system sensors, created once their source field streams
            sys_id = key.partition(":")[0]
            if (sys_id, "alert") not in known_alerts and "alerts" in sample:
                known_alerts.add((sys_id, "alert"))
                new.append(M30AlertBinarySensor(coordinator, sys_id))
            if (sys_id, "reminder") not in known_alerts and "reminders" in sample:
                known_alerts.add((sys_id, "reminder"))
                new.append(M30ReminderBinarySensor(coordinator, sys_id))
            if (sys_id, "smartaway") not in known_alerts and sample.get("smartAwayEnabled") is not None:
                known_alerts.add((sys_id, "smartaway"))
                new.append(M30SmartAwayBinarySensor(coordinator, sys_id))
            if (sys_id, "dr") not in known_alerts and sample.get("drEvent") is not None:
                known_alerts.add((sys_id, "dr"))
                new.append(M30DemandResponseBinarySensor(coordinator, sys_id))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_discover))
    _discover()


class M30StatusBinarySensor(CoordinatorEntity[M30BridgeCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, key, field, name, icon):
        super().__init__(coordinator)
        self._key = key
        self._field = field
        self._attr_name = name
        self._attr_icon = icon
        sys_id, _, zone = key.partition(":")
        self._attr_unique_id = f"lennox_dds_{sys_id}_{zone}_{field}"
        self._attr_device_info = {"identifiers": {(DOMAIN, sys_id)}}

    @property
    def _z(self) -> dict:
        return (self.coordinator.data or {}).get(self._key, {})

    @property
    def available(self) -> bool:
        return bool(self._z)

    @property
    def is_on(self):
        return bool(self._z.get(self._field))


class M30AlertBinarySensor(CoordinatorEntity[M30BridgeCoordinator], BinarySensorEntity):
    """Active HVAC fault alerts for a system (from LCC Alert Active/Cleared)."""
    _attr_has_entity_name = True
    _attr_name = "Alert"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: M30BridgeCoordinator, sys_id: str) -> None:
        super().__init__(coordinator)
        self._sys_id = sys_id
        self._attr_unique_id = f"lennox_dds_{sys_id}_alert"
        self._attr_device_info = {"identifiers": {(DOMAIN, sys_id)}}

    def _alerts(self) -> list:
        # alerts are per-sysID; identical across zones, so take the first zone's.
        for key, sample in (self.coordinator.data or {}).items():
            if key.partition(":")[0] == self._sys_id:
                return sample.get("alerts") or []
        return []

    @property
    def available(self) -> bool:
        return any(k.partition(":")[0] == self._sys_id
                   for k in (self.coordinator.data or {}))

    @property
    def is_on(self):
        return len(self._alerts()) > 0

    @property
    def extra_state_attributes(self):
        alerts = self._alerts()
        return {
            "active_count": len(alerts),
            "codes": [a.get("code") for a in alerts],
            "messages": [a.get("message") for a in alerts],
            "alerts": alerts,
        }


def _first_sample_for(coordinator, sys_id: str) -> dict:
    for key, sample in (coordinator.data or {}).items():
        if key.partition(":")[0] == sys_id:
            return sample
    return {}


class M30ReminderBinarySensor(CoordinatorEntity[M30BridgeCoordinator], BinarySensorEntity):
    """Filter/maintenance reminders (from LCC Reminder Status)."""
    _attr_has_entity_name = True
    _attr_name = "Maintenance Due"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:air-filter"

    def __init__(self, coordinator: M30BridgeCoordinator, sys_id: str) -> None:
        super().__init__(coordinator)
        self._sys_id = sys_id
        self._attr_unique_id = f"lennox_dds_{sys_id}_maintenance"
        self._attr_device_info = {"identifiers": {(DOMAIN, sys_id)}}

    def _reminders(self) -> list:
        # timer-based (reminders) + sensor-based (reminderSensors)
        s = _first_sample_for(self.coordinator, self._sys_id)
        return (s.get("reminders") or []) + (s.get("reminderSensors") or [])

    @property
    def available(self) -> bool:
        return bool(_first_sample_for(self.coordinator, self._sys_id))

    @property
    def is_on(self):
        return any(r.get("expired") for r in self._reminders())

    @property
    def extra_state_attributes(self):
        rems = self._reminders()
        pcts = [r.get("remainingPct") for r in rems if r.get("remainingPct") is not None]
        return {
            "reminder_count": len(rems),
            "min_remaining_pct": min(pcts) if pcts else None,
            "reminders": rems,
        }


class M30SmartAwayBinarySensor(CoordinatorEntity[M30BridgeCoordinator], BinarySensorEntity):
    """Smart Away (geofence) enabled state (from LCC Smart Away Status)."""
    _attr_has_entity_name = True
    _attr_name = "Smart Away"
    _attr_icon = "mdi:map-marker-radius"

    def __init__(self, coordinator: M30BridgeCoordinator, sys_id: str) -> None:
        super().__init__(coordinator)
        self._sys_id = sys_id
        self._attr_unique_id = f"lennox_dds_{sys_id}_smart_away"
        self._attr_device_info = {"identifiers": {(DOMAIN, sys_id)}}

    @property
    def available(self) -> bool:
        return _first_sample_for(self.coordinator, self._sys_id).get("smartAwayEnabled") is not None

    @property
    def is_on(self):
        return bool(_first_sample_for(self.coordinator, self._sys_id).get("smartAwayEnabled"))


class M30DemandResponseBinarySensor(CoordinatorEntity[M30BridgeCoordinator], BinarySensorEntity):
    """Utility demand-response (OCST/OpenADR) event: on during an active event."""
    _attr_has_entity_name = True
    _attr_name = "Demand Response Event"
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, coordinator: M30BridgeCoordinator, sys_id: str) -> None:
        super().__init__(coordinator)
        self._sys_id = sys_id
        self._attr_unique_id = f"lennox_dds_{sys_id}_dr_event"
        self._attr_device_info = {"identifiers": {(DOMAIN, sys_id)}}

    def _dr(self) -> dict:
        return _first_sample_for(self.coordinator, self._sys_id).get("drEvent") or {}

    @property
    def available(self) -> bool:
        return _first_sample_for(self.coordinator, self._sys_id).get("drEvent") is not None

    @property
    def is_on(self):
        return bool(self._dr().get("active"))

    @property
    def extra_state_attributes(self):
        dr = self._dr()
        enroll = _first_sample_for(self.coordinator, self._sys_id).get("drEnrollment") or {}
        return {
            "pending": dr.get("pending"),
            "allow_opt_out": dr.get("allowOptOut"),
            "start": dr.get("start"),
            "end": dr.get("end"),
            "enrolled": enroll.get("enrolled"),
        }
