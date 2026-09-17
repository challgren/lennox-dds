# Changelog

## 0.1.11

Better logging + bug reporting for everyone:

- **`debug` option** (default `false`): logs every raw sample (`[raw] {…}`) to the
  add-on log — turn it on to capture what your model emits for a bug report.
  (`debug_away` still adds the away-topic echo reader on top.)
- **Richer diagnostics:** Download diagnostics now includes the integration
  version, decoded status flags, and an **`unrecognized_fields`** section that
  flags data your hardware exposes but the integration doesn't handle yet. Still
  fully redacted (no sysID, no credentials).
- **Issue templates:** Bug report + "New/unrecognized device fields" forms that
  point you at the diagnostics file.
- The raw stream now includes `scheduleExceptionIds` (zoneStatus field 18) — the
  candidate for reliable away-state detection.

## 0.1.10

- **Away switch disabled by default (experimental).** The away *write* works, but
  the M30 doesn't echo manual-away in `period.away`, so the switch couldn't show
  true state and could silently leave the system in Away. It's now off by default
  until the state-readback path is found; enable it manually if you want to test.
- **`debug_away` option** (default `false`): logs each raw sample and subscribes
  read-only to the `Owner Manual Away` topic, to discover how the thermostat
  reports away state back. Turn on only for diagnostics — it's noisy.

## 0.1.9

- **Away control**: Manual Away is now writable, not just visible. The bridge
  publishes a `manualAwayUpdate` on the `Owner Manual Away` topic, and the
  integration adds an **Away switch** per thermostat (Settings → Devices &
  Services → Lennox iComfort → *Away*). Also controllable over MQTT via
  `lennox_dds/<sysID>/<zone>/set/away` (`on`/`off`).
- The climate card and the switch stay in sync: flipping Away shows up in
  `period.away` across zones.

## 0.1.8

- **Allergen Defender + more status flags**: the bridge now streams the full
  `zoneStatus` (allergen defender, ventilation, aux heat, defrost, smooth-setback
  recovery, heat/cool coast). The integration exposes each as a **binary sensor**,
  created only when your device marks the flag valid — so unsupported models (e.g.
  the base M30) show nothing.
- **Diagnostics**: Settings → Devices & Services → Lennox iComfort (DDS) → ⋮ →
  **Download diagnostics** exports the raw samples (sysID redacted), so
  unrecognized fields on other models are easy to spot and report.
- **Icons**: proper add-on icon + logo; `brands/` has ready-to-submit files for a
  home-assistant/brands PR (integration icon in Devices & Services).

## 0.1.7

- The companion **`lennox_dds` integration is now in this repo** (under
  `custom_components/`) and installable via **HACS** as a custom repository — no
  more manual copy. Add-on image unchanged from 0.1.5.

## 0.1.6

- Fix the thermostat card showing a range (e.g. `66–76`) in **cool** mode — the
  heat setpoint was bleeding in as the low. The `lennox_dds` integration now shows
  a single setpoint in heat/cool and a range only in heat_cool.
- _Integration change:_ the add-on image is unchanged from 0.1.5; update the
  companion `lennox_dds` custom integration to get this fix.

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
