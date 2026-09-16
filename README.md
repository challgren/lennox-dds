# Lennox iComfort DDS Add-ons

Home Assistant add-on repository for the **Lennox iComfort DDS Bridge** — an
OpenDDS 3.22 sidecar that bridges the Lennox iComfort (M30 / prod4 cloud) realtime
data plane (DDS-RTPS + DDS-Security over RtpsRelay) to Home Assistant. It pairs
with the companion **`lennox_dds`** custom integration (HACS / `custom_components`),
which it auto-wires via Supervisor discovery.

## Install (add-on)

1. Settings → Add-ons → Add-on Store → ⋮ → **Repositories** → add
   `https://github.com/challgren/lennox-dds`
2. Install **Lennox iComfort DDS Bridge**, set `lennox_email` + `lennox_password`
   (it provisions everything itself), and start it. See
   [`lennox_dds/README.md`](./lennox_dds) for the options.

## Repository layout

```
repository.yaml          # HA add-on repository marker
lennox_dds/              # the add-on (thin: a manifest that points at the image)
  config.yaml            #   image: ghcr.io/challgren/lennox-dds  (prebuilt, pulled)
container/               # the Docker image BUILD SOURCE (built by CI -> GHCR)
  Dockerfile             #   app image: compiles the subscriber + Python bridge
  Dockerfile.base        #   OpenDDS 3.22 from source (heavy, prebuilt+cached)
  Dockerfile.patched     #   + XCDR2 skip_sequence_dheader interop patch
  bridge_server.py, m30_prod4.py, fetch_security_docs.py, sub/, idl/, patch/, ...
.github/workflows/       # CI: build-image.yml (per-commit) + build-base.yml (manual)
```

The add-on is a **prebuilt-image** add-on: `lennox_dds/config.yaml` sets
`image: ghcr.io/challgren/lennox-dds`, so Supervisor just pulls the published image
— it does not build on the server. `container/` has no `config.yaml`, so Supervisor
ignores it; it exists so the image is reproducible and CI-built.

## Images / CI

- `ghcr.io/challgren/lennox-dds` — the add-on image. Built + pushed by
  **`build-image.yml`** on every change to `container/**` (fast: it compiles only
  the small subscriber + bridge on top of the prebuilt base).
- `ghcr.io/challgren/lennox-opendds-base:322dev-amd64-patched` — the heavy OpenDDS
  base. Built by **`build-base.yml`** (manual, ~1-2h); rerun only when OpenDDS
  (`OPENDDS_REF`, default `98248232b` = the device's 3.22.0-dev) or the patch
  changes.

### One-time GHCR setup (for CI pushes)

The workflows push with the built-in `GITHUB_TOKEN`, so each GHCR package must be
linked to this repo with Actions write access, and public so the add-on/base pull
without auth:

1. github.com/users/challgren/packages → `lennox-dds` and `lennox-opendds-base`
2. Package settings → **Manage Actions access** → add repo `challgren/lennox-dds`
   (role: Write); and **Change visibility → Public**.

## Local build (optional)

```bash
# app image on top of the prebuilt base:
docker build --platform linux/amd64 \
  --build-arg BASE=ghcr.io/challgren/lennox-opendds-base:322dev-amd64-patched \
  -f container/Dockerfile -t ghcr.io/challgren/lennox-dds:dev container
```
