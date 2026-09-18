# Changelog

## 0.1.21

- **System config reader (groundwork for air/comfort settings).** The bridge now
  subscribes to `LCC System Config Status` and surfaces the system parameters —
  **fan circulate time**, **dehumidification overcooling**, the **allergen-defender
  setting**, ventilation/humidity modes, temperature unit — in the sample as a
  `config` object. HA entities for these follow in a later release. (On firmware
  where this config type is XTypes-incompatible it's simply absent — no entities,
  no errors.)

## 0.1.20

- **Humidify / Dehumidify setpoint controls.** Two new **number** entities let you
  set the humidify and dehumidify targets (%RH) from Home Assistant, like the old
  lennoxs30 integration. The bridge writes them through the same manual-slot
  scheduleUpdate period as the temperature setpoints. They're **capability-gated** —
  only created when your system actually publishes those fields, so homes without
  humidity control won't see them.

## 0.1.19

_Integration-only — update via HACS; the add-on/bridge image is unchanged._

- **Cool Setpoint & Heat Setpoint sensors.** Two new sensors expose the cool and
  heat setpoints directly (read from the zone's active period, so **both are shown in
  every HVAC mode** — not just the mode-relevant one the climate card shows). They're
  °F with a temperature device_class, so Home Assistant auto-converts them for a °C
  household. Because they're sensors, setpoint changes now appear in normal history
  and the logbook — the climate entity's logbook only records HVAC-*mode* changes,
  never setpoint (attribute) changes, which is why setpoint edits looked like "no
  activity."

## 0.1.18

- **Faster setpoint changes.** Setpoint writes now mirror exactly what the Lennox
  app sends: a **complete period** on the **manual hold slot** (schedule id `16`
  instead of `32`) carrying **both** the heat and cool setpoints (with their
  Celsius values) and the `ID` bit — `period.validFlag = 241`. The old write sent a
  single cool-setpoint on the scheduled-override slot, which the M30 applied slowly
  and variably (minutes, sometimes reverting). The unspecified setpoint and the
  mode/fan are filled from the latest zone status so nothing else changes. This was
  reverse-engineered from a live capture of the app's own DDS write. Writes still
  land the same way (`WROTE scheduleUpdate … rc=0`); they should now take effect
  much sooner.
- **Health check.** The container now reports **unhealthy** when it stops receiving
  zoneStatus samples (relay eviction, cert expiry, or the thermostat going offline)
  — previously the process stayed "up" while the data silently froze. Home Assistant
  surfaces this automatically; enable the add-on's **Watchdog** toggle to have
  Supervisor auto-restart it. The staleness threshold defaults to 300s (the M30 has
  multi-minute quiet gaps) and is tunable via the new **`health_max_age`** option
  (30–3600s).

## 0.1.17

_Integration-only — update via HACS; the add-on/bridge image is unchanged._

- **Optimistic setpoint/mode/fan.** The M30 can take a few minutes to apply and
  echo a change back over the cloud/relay, during which the card kept showing the
  *old* value — so it looked like the change failed and users retried. The climate
  entity now reflects a set temperature / mode / fan **immediately**, then
  reconciles with the device when it catches up (or reverts after 5 min if the
  device never confirms). The underlying writes were already succeeding; this is
  purely the missing UI feedback.

## 0.1.16

- **Stability: clean shutdown.** The bridge no longer prints a
  `RuntimeError: Event loop stopped before Future completed` traceback (and churn)
  every time the add-on stops/restarts — SIGTERM now cancels the bridge and unwinds
  gracefully, terminating the DDS subprocess.
- **Sensor-based reminders**: also read `LCC Reminder Sensor Status` and fold them
  into the **Maintenance Due** sensor alongside the timer-based reminders.

## 0.1.15

Four new data sources from the thermostat's status topics (all validated live):

- **Outdoor temperature + weather**: a new **Outdoor Temperature** sensor (from
  the weather-service value the M30 displays, falling back to the outdoor-unit
  sensor), plus **Outdoor Humidity** and **Wind Speed**, and city/state/condition.
- **Filter/maintenance reminders**: a **Maintenance Due** binary sensor (on when a
  reminder has expired), with each reminder's % life remaining as attributes.
  Empty reminder slots are filtered out.
- **Smart Away**: a **Smart Away** binary sensor (geofence away enabled).
- **Demand response** (utility OpenADR / AHRI 1380): a **Demand Response Event**
  binary sensor — on during an active utility peak event, with pending/opt-out/
  start/end and enrollment as attributes.
- _Config:_ removed the `topic` and `domain` options — they're fixed by the
  protocol now that the bridge reads many hardcoded status topics.

## 0.1.14

- **HVAC alerts.** The bridge now subscribes to the `LCC Alert Active` /
  `LCC Alert Cleared` topics and streams active faults. The integration adds an
  **Alert** binary sensor (device class `problem`) per system — `on` when a fault
  is active, with the alert `codes`, `messages`, and full details as attributes.
  Great for automations that notify you when the system reports a problem.

## 0.1.13

- **Away switch works for real.** The bridge now subscribes to the dedicated
  **`LCC Manual Away Status`** topic and streams the true `manualAway` state, so
  the Away switch reflects reality — including away set on the thermostat or the
  Lennox app — instead of snapping back. The switch is **re-enabled** (no longer
  experimental/disabled-by-default).
- **Timestamped logs.** Every add-on log line is now prefixed with the date/time
  (the C++ bridge's output is routed through the same timestamped logger), so
  `debug` captures show exactly when each event happened.

## 0.1.12

- **Diagnostic:** with `debug_away: true`, the add-on now enumerates every topic
  the thermostat publishes (via the DCPSPublication built-in topic) and logs each
  as `[debug-topics] publishes topic='…' type='…'`. Since `zoneStatus` carries no
  clean away flag, this locates the system/status topic that does — the next step
  toward true Away-state detection.

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
