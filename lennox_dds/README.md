# Lennox iComfort DDS Bridge

OpenDDS 3.22 sidecar that speaks the Lennox iComfort (M30 / prod4) realtime data
plane — DDS-RTPS + DDS-Security over the RtpsRelay — and exposes it to Home
Assistant. It streams `zoneStatus` samples over a local WebSocket and accepts
control commands (setpoint / mode / fan) that it writes back as schedule
overrides, plus **Away** (`manualAwayUpdate` on the `Owner Manual Away` topic). The
companion `lennox_dds` integration consumes this and is auto-wired via Supervisor
discovery.

### MQTT control topics (external / non-HA)

With `mqtt_enabled: true`, publish to `lennox_dds/<sysID>/<zone>/set/<field>`:
`temperature`, `temperature_low`, `temperature_high`, `mode`, `fan_mode`, and
`away` (`on`/`off`). State is on `lennox_dds/<sysID>/<zone>/state`.

## Setup — two ways to supply credentials

### A) Lennox login (recommended, zero cert handling)

Set **`lennox_email`** and **`lennox_password`** to your Lennox account. On start
the add-on logs in, mints its own DDS participant identity, downloads the full
DDS-Security bundle, caches it in `/data`, and **auto-derives `home_id`**. Leave
`home_id` and the security files blank. That's it.

### B) Bring your own bundle

Leave the credentials blank and drop the 6 DDS-Security files into
`/config/lennox_dds/security/` (`identity_ca.pem`, `identity.pem`, `identity.key`,
`permissions_ca.pem`, `governance.xml.p7s`, `permissions.xml.p7s`), and set
`home_id` to your login homeId (the DDS partition). The folder is mounted
read-only into the add-on (see `security_dir`).

## Options

| Option           | Description                                                        |
|------------------|--------------------------------------------------------------------|
| `lennox_email`    | (A) Lennox account email — auto-provisions the bundle + `home_id`. |
| `lennox_password` | (A) Lennox account password.                                      |
| `home_id`         | (B only) login homeId = DDS partition (e.g. `5212374`). Auto with creds. |
| `domain`          | DDS domain id (default `0`).                                       |
| `topic`           | Topic name (default `LCC Zone Status`).                            |
| `security_dir`    | (B) where the add-on reads the bundle (default `/homeassistant_config/lennox_dds/security`). |
| `dcps_debug`      | OpenDDS debug level (default `0`).                                 |
| `debug`           | Log every raw sample to the add-on log — turn on for bug reports (default `false`). |
| `mqtt_enabled`    | Also publish state via MQTT discovery (default `false`).          |

## Notes

- Nothing secret is baked into the image; the identity private key is minted/held
  only in the add-on's own `/data`.
- The minted identity cert starts ~now, so if the add-on's clock is briefly behind
  the issuer the add-on waits for it to become valid (logged) rather than failing.
  Keep host time synced (NTP).

## Networking

Only outbound UDP to the Lennox relay is needed. The WebSocket is on port 8099,
**internal to the Supervisor network** (no host port), reachable by the
integration at `ws://<add-on-hostname>:8099` — wired automatically via discovery.
