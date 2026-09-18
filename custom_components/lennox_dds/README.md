# `custom_components/lennox_dds/` — Home Assistant integration

The Home Assistant **integration** (custom component) for Lennox iComfort
thermostats on the prod4/v4 cloud (M30). Installable via HACS or by copying this
folder into `<config>/custom_components/`.

It is a thin **`local_push`** client: it connects to the **Lennox iComfort DDS
Bridge add-on** over a local WebSocket, receives `zoneStatus` samples, and exposes
them as entities. It does **not** talk DDS itself — all the OpenDDS / RtpsRelay work
lives in the add-on (see `../../container/`). Keeping DDS in the add-on also honors
the relay's ~2-participant-per-home cap (the integration is not a second endpoint).

## Setup

Auto-discovered via Supervisor when the add-on is running — accept the discovery
prompt. Manual setup asks for the bridge host/port (default the add-on's WebSocket).
No Lennox credentials live here; those are add-on options.

## Files

| File | Role |
|------|------|
| `__init__.py` | Entry setup/teardown; starts the coordinator, registers the device. |
| `coordinator.py` | WebSocket client to the bridge: receives samples, dispatches updates, sends control commands. |
| `config_flow.py` | UI/discovery config flow (host/port). |
| `climate.py` | The thermostat entity — target temperature(s), HVAC mode, fan. Includes the **optimistic overlay** (a set value shows immediately, then reconciles with the device, which can echo back slowly). |
| `sensor.py` | Read-only sensors (temperature, humidity, outdoor temp, weather, reminders, …). |
| `binary_sensor.py` | Binary sensors (alerts / maintenance-due / away state, …). |
| `switch.py` | Controllable switches (e.g. Away). |
| `diagnostics.py` | Redacted diagnostics download for bug reports. |
| `const.py` | Domain, defaults, enum maps (systemMode/fanMode ↔ HA). |
| `strings.json` / `translations/` | UI strings. |
| `manifest.json` | Integration metadata + version (keep equal to `../../lennox_dds/config.yaml`). |

## Control latency note

Setpoint/mode/fan writes go out over DDS via the add-on and the M30 echoes the new
value back over the cloud/relay after a short delay. `climate.py`'s optimistic
overlay hides that delay in the UI (reverting after 5 min if the device never
confirms). As of add-on 0.1.18 the write mirrors the Lennox app's own command
(manual-hold slot, complete period), so the device applies it promptly. When
debugging "control didn't work", check the add-on log for `WROTE scheduleUpdate …
rc=0` first — the write almost certainly landed.

## Versioning

Bump `manifest.json` `version` together with `../../lennox_dds/config.yaml` and add
a `../../lennox_dds/CHANGELOG.md` entry — see the top-level `CLAUDE.md`.
