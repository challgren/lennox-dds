# `container/` — DDS bridge image (build source)

This directory is the **Docker image build source** for the Lennox iComfort DDS
Bridge add-on. CI (`.github/workflows/build-image.yml`) builds it and publishes it
to `ghcr.io/challgren/lennox-dds:<version>`; the add-on
(`../lennox_dds/config.yaml`) runs that prebuilt image. Nothing here is installed
directly by Home Assistant — it all ships inside the image.

## What the image does

A small **OpenDDS 3.22 sidecar** that speaks the M30's realtime plane
(DDS-RTPS + DDS-Security over Lennox's RtpsRelay), plus a thin Python supervisor:

```
 M30 ⇄ RtpsRelay ⇄  lennox_zone_status_sub (C++, --stream)
                          │  newline-delimited JSON zoneStatus on stdout
                          ▼
                    bridge_server.py  ──►  local WebSocket (HA integration)
                                      ──►  optional MQTT
                          ▲  "SET …" / "AWAY …" control lines on stdin
```

## Files

| File | Role |
|------|------|
| `Dockerfile` | Two-stage build: compile the subscriber against the OpenDDS base image, then a slim runtime. Defines the container `HEALTHCHECK`. |
| `Dockerfile.base` / `Dockerfile.patched` | Build the cached OpenDDS 3.22 base (pinned to the device's version; carries the `patch/` XCDR2 fix). |
| `sub/lennox_zone_status_sub.cpp` | The DDS subscriber/writer. Streams `zoneStatus`, reads the status topics, and writes control (`Owner Schedule Update`, `Owner Manual Away`). |
| `sub/lennox_sub.mpc` / `.mwc` | MPC project files for the subscriber. |
| `idl/lennox_m30.idl` | Reverse-engineered IDL for every type we touch. |
| `build_in_container.sh` | Runs `opendds_idl` + compiles the subscriber inside the build stage. |
| `bridge_server.py` | Python supervisor: spawns the subscriber, fans samples to WebSocket/MQTT, routes control back, provisions security, writes the liveness heartbeat. |
| `m30_prod4.py` / `fetch_security_docs.py` | Credentialed provisioning: log in → mint a DDS identity → download the DDS-Security bundle. |
| `entrypoint.sh` | One-shot connection-test entrypoint (override `--entrypoint`); the default entrypoint is `bridge_server.py`. |
| `healthcheck.sh` | Liveness probe used by `HEALTHCHECK` — see below. |
| `opendds_rtps.ini` | OpenDDS transport/discovery config (relay addresses, XCDR2, security). |
| `patch/` | `skip_sequence_dheader` XCDR2 patch a from-source OpenDDS needs to interop with the device's `pkg-21`. |
| `requirements.txt` | Pinned Python deps (Dependabot-tracked). |

## Health check

The bridge process can stay up while receiving **nothing** — the relay evicted us,
the cert expired, or the thermostat went offline. `bridge_server.py` touches
`$LENNOX_HEARTBEAT_FILE` (default `/tmp/lennox_last_sample`) on every zoneStatus
sample; `healthcheck.sh` reports **unhealthy** when that file is missing or older
than the threshold (default 300s, set by the `health_max_age` add-on option, which
bridge_server writes to `/tmp/lennox_health_max_age`; env `HEALTH_MAX_AGE` overrides).
Supervisor surfaces this automatically; enable the add-on's **Watchdog** toggle to
auto-restart on unhealthy.

## Build locally (verify a change before committing)

The subscriber + generated TypeSupport compile at `-O3` (slow, a few minutes). From
the repo root:

```
docker build --platform linux/amd64 \
  --build-arg BASE=ghcr.io/challgren/lennox-opendds-base:322dev-amd64-patched \
  -f container/Dockerfile -t lennox-dds:<ver>-test container
```

(The plain local base tag isn't pullable — always pass the `BASE` build-arg.)

## Gotcha: relay participant cap (~2 per home)

Lennox's RtpsRelay admits only ~2 DDS participants per home (the phone app + this
add-on). A 3rd participant is starved and can **evict** the app (it shows
"Offline"). So the add-on runs a **single** DDS participant and the HA integration
is a plain WebSocket client, not its own DDS endpoint. Never run an extra DDS
subscriber against the relay while the app + add-on are both up.

> The C++/IDL/bridge sources here are vendored copies kept in sync with the
> research workspace (`research/dds/…`). Edit there first, then sync into
> `container/` — see the top-level `CLAUDE.md`.
