# Changelog

## 0.1.5

- **MQTT control**: publish HA-climate command topics
  (`.../set/temperature`, `temperature_low`, `temperature_high`, `mode`,
  `fan_mode`) and route them through the same DDS schedule-override writer as the
  WebSocket path.
- **`mqtt_discovery`** option (default `false`): external/non-HA use — raw state on
  `lennox_dds/<sysID>/<zone>/state` + `set/…` control only, and it removes any
  previously-published HA MQTT entity so there's no duplicate. Set `true` to also
  expose a controllable HA MQTT climate.

## 0.1.4

- Fix MQTT: the Supervisor broker lookup was dropped in the provisioning refactor,
  so `mqtt_enabled` silently did nothing. Restored, with clearer logging.
- Add optional explicit broker options: `mqtt_host`, `mqtt_port`, `mqtt_user`,
  `mqtt_password` (used if set; otherwise the Supervisor MQTT service / Mosquitto).

## 0.1.3

- Tolerate clock skew on the freshly-minted identity cert: wait (bounded) until
  the cert is valid instead of crash-looping, and re-mint a not-yet-valid cached
  cert. Keep host time synced (NTP).

## 0.1.2

- **Credentialed auto-provisioning**: set `lennox_email` + `lennox_password` and
  the add-on logs in, mints its own DDS identity, downloads the full DDS-Security
  bundle (cached in `/data`), and auto-derives `home_id`. No manual certs.

## 0.1.1

- Resolve the DDS-Security bundle mount path across Supervisor versions (the
  `homeassistant_config` map mounts at `/homeassistant` on some, not
  `/homeassistant_config`).

## 0.1.0

- Initial release: OpenDDS 3.22 sidecar that speaks the Lennox iComfort (M30 /
  prod4) realtime plane (DDS-RTPS + DDS-Security over the RtpsRelay), streams
  `zoneStatus` over a local WebSocket, and accepts setpoint / mode / fan control.
