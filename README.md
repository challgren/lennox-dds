# Lennox iComfort DDS Bridge

Home Assistant support for **Lennox iComfort thermostats that have been migrated to
the Lennox "prod4"/v4 cloud** (notably the **iComfort M30**), which the classic
[`lennoxs30`](https://github.com/PeteRager/lennoxs30) /
[`lennoxs30api`](https://github.com/PeteRager/lennoxs30api) message-bus path can no
longer reach.

- ✅ Live climate + temperature/humidity, **local push**
- ✅ Control: setpoints, HVAC mode, fan, and **Away** (true state, reflected back)
- ✅ **Outdoor temperature + weather** (humidity, wind), **HVAC fault alerts**,
  **filter/maintenance reminders**, and **Smart Away** — as HA sensors
- ✅ Read-only status flags (allergen defender, ventilation, aux heat, defrost, …)
  surfaced as binary sensors when your model reports them
- ✅ No manual certificates — the add-on provisions itself from your Lennox login

## Why this exists

Lennox migrated some accounts to a new backend. On prod4 the mobile **message
bus** only delivers to an `applicationid` Lennox has already registered — a
self-generated client subscribes, gets `Ok`, and then `retrieve` returns
`{"messages":[]}` forever (see
[lennoxs30api#101](https://github.com/PeteRager/lennoxs30api/issues/101)).

This project takes the **other data plane the app uses**: the thermostat's realtime
state flows over **OpenDDS (DDS-RTPS + DDS-Security) through Lennox's RtpsRelay**,
and a DDS participant authenticates with **its own minted identity** — so there's
no app-id allowlist to get past.

## How it works

```
HA ── WebSocket ──▶ DDS bridge (this add-on) ── DDS-RTPS/Security ──▶ Lennox RtpsRelay ──▶ M30
        ▲                                                                   
   lennox_dds integration (climate + sensors, control)                     
```

1. Log in (prod4 gateway) → `registerLCCOwner` → a plant token with `DDS.LCC_OWNER`.
2. Provision a DDS-Security bundle from `plantdevices/v1/dds-im/` — CA + governance,
   a minted participant identity (`newcert?nonce=`), and permissions
   (`/permissions/DDS.LCC_OWNER?nonce=`, same nonce).
3. Join **domain 0**, partition = your homeId, over the RtpsRelay; subscribe the
   **`LCC Zone Status`** topic → live `zoneStatus`. Control = writes to the
   **`Owner Schedule Update`** topic (setpoints / mode / fan) and
   **`Owner Manual Away`** (`manualAwayUpdate`, the Away switch).

The bridge is a tiny C++ OpenDDS subscriber + a Python server that streams JSON to a
local WebSocket; the companion **`lennox_dds`** HA integration (a thin `local_push`
client, auto-wired by Supervisor discovery) turns that into entities.

## Install

**1. Integration (via HACS)** — HACS → ⋮ → **Custom repositories** → add
`https://github.com/challgren/lennox-dds` as category **Integration** → install
**Lennox iComfort (DDS)** → **restart Home Assistant**.

**2. Add-on** — Settings → Add-ons → Add-on Store → ⋮ → **Repositories** → add
`https://github.com/challgren/lennox-dds` → install **Lennox iComfort DDS Bridge**,
set **`lennox_email`** + **`lennox_password`**, and start it. It provisions the
DDS-Security bundle and auto-derives your `home_id`. (Advanced/offline: leave those
blank and drop your own bundle — see [`lennox_dds/README.md`](./lennox_dds).)

**3. Done** — the add-on announces itself and the integration auto-configures via
Supervisor discovery; your climate + temperature/humidity entities appear.

## Entities

All are created automatically per system/zone; ones your model doesn't report
simply don't appear.

| Entity | Type | Source |
|--------|------|--------|
| Thermostat | `climate` | setpoints, HVAC mode, fan, current temp/humidity |
| Away | `switch` | true state from `LCC Manual Away Status`, writable |
| Temperature / Humidity | `sensor` | per-zone `zoneStatus` |
| Outdoor Temperature | `sensor` | weather-service value (falls back to the outdoor-unit sensor) |
| Outdoor Humidity / Wind Speed | `sensor` | `LCC Weather Status` |
| Alert | `binary_sensor` (problem) | active faults from `LCC Alert Active/Cleared` (codes + messages) |
| Maintenance Due | `binary_sensor` (problem) | filter/maintenance reminders + % remaining |
| Smart Away | `binary_sensor` | geofence away enabled |
| Demand Response Event | `binary_sensor` | utility OpenADR/AHRI-1380 peak event (pending/opt-out/start/end) |
| Allergen Defender, Ventilation, Aux Heat, … | `binary_sensor` | `zoneStatus` flags, only when your model marks them valid |

Control is also available over MQTT (`mqtt_enabled`) for external/non-HA use — see
the add-on docs. Download diagnostics (⋮ on the device) for a full, redacted dump.

> This one repo serves three things: the **integration** (`custom_components/`, via
> HACS), the **add-on** (`lennox_dds/`, via the Add-on Store), and the **image build
> source** (`container/`, via CI).

## Repository layout

```
lennox_dds/     # the HA add-on (prebuilt-image manifest -> ghcr.io/challgren/lennox-dds)
container/      # the Docker image build source (Dockerfiles, C++ subscriber, IDL,
                #   XCDR2 interop patch, bridge server, credentialed bundle fetch)
.github/        # CI: build-image.yml (per-commit) + build-base.yml (manual OpenDDS base)
```

The add-on pulls a prebuilt image (fast install, no on-server compile); CI builds and
publishes it to GHCR. See [`container/`](./container) for the internals and the
one interop note (the device runs an OCI-proprietary OpenDDS 3.22 `pkg-21`, so a
from-source OpenDDS needs the `skip_sequence_dheader` XTypes patch).

## Reporting a bug

Please [open an issue](https://github.com/challgren/lennox-dds/issues/new/choose)
and attach a **diagnostics** file — it's the fastest way to a fix:

1. **Settings → Devices & Services → Lennox iComfort (DDS) → ⋮ → Download
   diagnostics.** It's auto-redacted (no sysID, no credentials) and includes the
   integration version, decoded status flags, and an `unrecognized_fields` section
   that flags data your model exposes but the integration doesn't handle yet.
2. For control or data problems, set **`debug: true`** in the add-on config and
   restart — the add-on log then prints every raw sample (`[raw] {…}`). Paste the
   relevant lines into the issue.

Different hardware (S30/E30, PureAir, humidifiers, multi-zone) reports fields the
base M30 doesn't; a diagnostics file from your device is how we add support.

## Status / credits

Reverse-engineered from the Lennox Home app against a live M30; shared upstream at
[lennoxs30api#101](https://github.com/PeteRager/lennoxs30api/issues/101). Not
affiliated with Lennox. Use at your own risk.
