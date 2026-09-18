#!/bin/sh
# Container liveness probe for the Lennox DDS bridge.
#
# The bridge process can be alive yet receiving nothing — the RtpsRelay evicted us
# (a 3rd participant joined), the DDS-Security cert expired, or the thermostat went
# offline. In all those cases stdout goes quiet but the process lingers, so a plain
# "is the process running" check looks healthy while HA data is frozen.
#
# bridge_server.py touches $LENNOX_HEARTBEAT_FILE on every zoneStatus sample. We
# report UNHEALTHY when that file is missing or older than HEALTH_MAX_AGE seconds,
# so the problem shows up in HA (and Supervisor's watchdog can restart the add-on).
#
# The M30 publishes zoneStatus periodically and on change; gaps of a few minutes are
# normal, so the default threshold is generous (300s). The Dockerfile HEALTHCHECK
# --start-period covers the initial connect/provision before the first sample.
set -eu

FILE="${LENNOX_HEARTBEAT_FILE:-/tmp/lennox_last_sample}"
# Threshold: explicit env wins; else the value bridge_server wrote from the
# `health_max_age` add-on option; else the default.
AGE_FILE="${LENNOX_HEALTH_MAX_AGE_FILE:-/tmp/lennox_health_max_age}"
MAX_AGE="${HEALTH_MAX_AGE:-}"
if [ -z "$MAX_AGE" ] && [ -f "$AGE_FILE" ]; then
  MAX_AGE=$(cat "$AGE_FILE" 2>/dev/null)
fi
MAX_AGE="${MAX_AGE:-300}"
case "$MAX_AGE" in ''|*[!0-9]*) MAX_AGE=300 ;; esac  # guard non-numeric

if [ ! -f "$FILE" ]; then
  echo "unhealthy: no DDS sample received yet ($FILE missing)"
  exit 1
fi

now=$(date +%s)
mtime=$(stat -c %Y "$FILE" 2>/dev/null || echo 0)
age=$(( now - mtime ))

if [ "$age" -gt "$MAX_AGE" ]; then
  echo "unhealthy: last DDS sample ${age}s ago (> ${MAX_AGE}s threshold)"
  exit 1
fi

echo "healthy: last DDS sample ${age}s ago"
exit 0
