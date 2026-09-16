# Lennox iComfort DDS Add-ons

Home Assistant add-on repository for the **Lennox iComfort DDS Bridge** — an
OpenDDS 3.22 sidecar that bridges the Lennox iComfort (M30 / prod4 cloud)
realtime data plane (DDS-RTPS + DDS-Security over RtpsRelay) to Home Assistant.

It pairs with the companion **`lennox_dds`** custom integration (installed
separately into `config/custom_components/`), which it auto-wires via Supervisor
discovery.

## Install

1. Settings → Add-ons → Add-on Store → ⋮ → **Repositories** → add
   `https://github.com/challgren/lennox-dds-addon`
2. Install **Lennox iComfort DDS Bridge**.
3. Drop your DDS-Security bundle into `/config/lennox_dds/security/` (6 files:
   `identity_ca.pem`, `identity.pem`, `identity.key`, `permissions_ca.pem`,
   `governance.xml.p7s`, `permissions.xml.p7s`).
4. Set the add-on option `home_id` to your login homeId (the DDS partition).
5. Start the add-on. The `lennox_dds` integration will be discovered and offered
   for one-click setup.

## Add-ons

- **[Lennox iComfort DDS Bridge](./lennox_dds)** — the bridge sidecar.

The prebuilt image is published to `ghcr.io/challgren/lennox-dds`.
