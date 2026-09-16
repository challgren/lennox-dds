#!/bin/bash
# DDS sidecar entrypoint. Runs the zoneStatus subscriber against the relay.
#
# Required:
#   /security/            mount with the 6 DDS-Security docs (identity_ca.pem,
#                         permissions_ca.pem, identity.pem, identity.key,
#                         governance.xml.p7s, permissions.xml.p7s)
#   LENNOX_PARTITION      login homeId (e.g. 5212374) -- the DDS partition
# Optional:
#   LENNOX_DOMAIN         DDS domain id (default 0)
#   LENNOX_TOPIC          topic name (default "LCC Zone Status")
#   LENNOX_TIMEOUT        seconds to wait for a sample (default 60; 0 = forever*)
#   DCPS_DEBUG            OpenDDS debug level (default 0)
set -e

: "${LENNOX_DOMAIN:=0}"
: "${LENNOX_TOPIC:=LCC Zone Status}"
: "${LENNOX_TIMEOUT:=60}"
: "${DCPS_DEBUG:=0}"
SEC_DIR="${LENNOX_SECURITY_DIR:-/security}"
INI="${LENNOX_CONFIG:-/config/opendds_rtps.ini}"

if [ -z "$LENNOX_PARTITION" ]; then
  echo "ERROR: LENNOX_PARTITION (login homeId) is required (else zero samples)." >&2
  exit 2
fi
for f in identity_ca.pem permissions_ca.pem identity.pem identity.key governance.xml.p7s permissions.xml.p7s; do
  [ -f "$SEC_DIR/$f" ] || { echo "ERROR: missing $SEC_DIR/$f (mount the security bundle at $SEC_DIR)." >&2; exit 3; }
done

echo "[sidecar] domain=$LENNOX_DOMAIN topic='$LENNOX_TOPIC' partition=$LENNOX_PARTITION sec=$SEC_DIR"
exec /app/lennox_zone_status_sub \
  -DCPSConfigFile "$INI" \
  --domain "$LENNOX_DOMAIN" \
  --topic "$LENNOX_TOPIC" \
  --partition "$LENNOX_PARTITION" \
  --security-dir "$SEC_DIR" \
  --timeout "$LENNOX_TIMEOUT" \
  -DCPSDebugLevel "$DCPS_DEBUG"
