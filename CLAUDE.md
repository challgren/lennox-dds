# CLAUDE.md — lennox-dds

Guidance for contributors (human or AI) working in this repo.

## What this is

One repo serving three things for Home Assistant support of **Lennox iComfort
thermostats on the prod4/v4 cloud** (notably the **M30**):

- `custom_components/lennox_dds/` — the HA **integration** (installable via HACS)
- `lennox_dds/` — the HA **add-on** (prebuilt-image manifest)
- `container/` — the **Docker image build source** (CI publishes it to GHCR)

The add-on is an OpenDDS 3.22 sidecar that speaks the thermostat's realtime plane
(DDS-RTPS + DDS-Security over Lennox's RtpsRelay), streams `zoneStatus` over a
local WebSocket, and writes control back. The integration is a thin `local_push`
client, auto-wired via Supervisor discovery.

## Release discipline (every version bump)

1. Bump **both** versions and keep them equal:
   - `lennox_dds/config.yaml` → `version:`
   - `custom_components/lennox_dds/manifest.json` → `version`
2. **Always** add a `lennox_dds/CHANGELOG.md` entry — HA shows it in the update
   dialog.
3. Commit, push, then tag `vX.Y.Z`. CI: `build-image.yml` publishes the GHCR
   image per commit; `release.yml` cuts a GitHub Release from the CHANGELOG on a
   `v*` tag.

## Building the image locally to verify a container/ change

The C++ subscriber + generated TypeSupport compile at `-O3` (slow). Verify it
links before committing:

```
docker build --platform linux/amd64 \
  --build-arg BASE=ghcr.io/challgren/lennox-opendds-base:322dev-amd64-patched \
  -f container/Dockerfile -t lennox-dds:<ver>-test container
```

## Layout notes

- **⚠ Relay participant cap (~2 per home).** Lennox's RtpsRelay admits only ~2 DDS
  participants per home — the phone **app** + our **add-on**. A **3rd** participant
  is starved and can **evict** the app (it shows "Offline" until relaunched), so
  never run an extra DDS participant (a debug subscriber/eavesdropper) while both
  are up — stop the add-on first. This is why the add-on runs a single participant
  and the integration is a plain WebSocket client, not its own DDS endpoint.
- DDS **topics**: `LCC Zone Status` (read), `Owner Schedule Update`
  (setpoint/mode/fan write — for a setpoint we write the **manual slot**
  `scheduleId = 16 + zoneId` with a **complete period**: both heat+cool setpoints
  incl. Celsius, `period.validFlag = 241`, mirroring the app so the M30 applies it
  promptly; `bridge_server.py` `SCHEDULE_OVERRIDE_BASE`), `Owner Manual Away` (Away
  write). Status topics the
  bridge also reads: `LCC Manual Away Status` (true away state), `LCC Alert
  Active/Cleared`, `LCC Reminder Status`, `LCC Weather Status`, `LCC Smart Away
  Status`, `LCC System Status`, `LCC Ocst Event/Enrollment Status`. New types use
  `@mutable @autoid(HASH)` subsets — declare only the fields you need with EXACT
  names (member IDs are name hashes); the reader skips the rest. The IDL is
  `container/idl/lennox_m30.idl`; the subscriber is `container/sub/`.
- **MQTT** (external/non-HA): with `mqtt_enabled`, control topics are
  `lennox_dds/<sysID>/<zone>/set/{temperature,temperature_low,temperature_high,mode,fan_mode,away}`.
- Credentials: `lennox_email` + `lennox_password` auto-provision the full
  DDS-Security bundle (cached in the add-on's `/data`) and derive `home_id`.
  Nothing secret is baked into the image.
- Interop: the device runs an OCI-proprietary OpenDDS 3.22 `pkg-21`; a
  from-source OpenDDS needs the `skip_sequence_dheader` XCDR2 patch
  (`container/patch/`).

## Don't

- Don't commit any DDS-Security bundle / private key or account credentials.
- Don't use Lennox's trademarked logo — icons are original art only.
- Don't rapid-switch HVAC state in control writes.
