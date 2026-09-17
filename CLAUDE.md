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

- DDS **topics**: `LCC Zone Status` (read), `Owner Schedule Update`
  (setpoint/mode/fan), `Owner Manual Away` (Away). The IDL is
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
