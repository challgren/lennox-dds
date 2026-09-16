# Lennox iComfort DDS Bridge

OpenDDS 3.22 sidecar that speaks the Lennox iComfort (M30 / prod4) realtime data
plane — DDS-RTPS + DDS-Security over the RtpsRelay — and exposes it to Home
Assistant. It streams `zoneStatus` samples over a local WebSocket and accepts
control commands (setpoint / mode / fan) that it writes back as schedule
overrides. The companion `lennox_dds` integration consumes this.

## Configuration

| Option         | Description                                                        |
|----------------|--------------------------------------------------------------------|
| `home_id`      | Your login homeId — the DDS partition (e.g. `5212374`). **Required.** |
| `domain`       | DDS domain id (default `0`).                                       |
| `topic`        | Topic name (default `LCC Zone Status`).                            |
| `security_dir` | Where the add-on reads the DDS-Security bundle (default `/homeassistant_config/lennox_dds/security`). |
| `dcps_debug`   | OpenDDS debug level (default `0`).                                 |
| `mqtt_enabled` | Also publish state via MQTT discovery (default `false`).          |

## Security bundle

Place these 6 files in `/config/lennox_dds/security/` (mapped read-only into the
add-on): `identity_ca.pem`, `identity.pem`, `identity.key`, `permissions_ca.pem`,
`governance.xml.p7s`, `permissions.xml.p7s`.

## Networking

The bridge needs only outbound UDP to the Lennox relay. It exposes a WebSocket on
port 8099 **internal to the Supervisor network** (no host port), reachable by the
integration at `ws://<add-on-hostname>:8099` — wired automatically via discovery.
