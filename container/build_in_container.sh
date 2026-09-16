#!/bin/bash
# Build the zoneStatus subscriber inside the OpenDDS container (invoked by the
# Dockerfile). Mirrors research/dds/sub/build.sh but for the container layout.
set -e
: "${DDS_ROOT:=/opt/OpenDDS}"
# shellcheck disable=SC1091
source "$DDS_ROOT/setenv.sh"

cd /src/sub
# copy the canonical recovered IDL + append the DCPS topic pragma (same as the
# host build.sh; keeps the model file itself clean).
cp /src/idl/lennox_m30.idl ./lennox_m30.idl
cat >> ./lennox_m30.idl <<'IDL'

#pragma DCPS_DATA_TYPE "LxZoneStatusIDL::zoneStatus"
#pragma DCPS_DATA_TYPE "LxScheduleUpdateIDL::scheduleUpdate"
IDL

"$ACE_ROOT/bin/mwc.pl" -type gnuace lennox_sub.mwc
make
test -x lennox_zone_status_sub
echo "container subscriber built OK"
