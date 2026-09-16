# Lennox M30 DDS sidecar — OpenDDS pinned to the DEVICE's version (3.22.0 / ACE
# 6.5.24, from the app's libOpenDDS_Dcps.so "This is OpenDDS 3.22.0-dev using
# ACE 6.5.24"). Matching the version fixes the XTypes TypeLookup wire-format skew
# that blocked type matching against a from-source HEAD build.
#
# Build context = repo root:
#   docker build -f research/dds/docker/Dockerfile -t lennox-dds:3.22 .
# Run (needs outbound UDP to the relay 4.151.164.12:4445-4447):
#   docker run --rm --network host \
#     -v $PWD/research/security/bundle:/security:ro \
#     -e LENNOX_PARTITION=5212374 \
#     lennox-dds:3.22
#
# (On Docker Desktop/mac there is no real host networking; use the default bridge
#  — outbound UDP still works. --network host matters on the Linux HA server.)

# OpenDDS lives in a separate, cached base image (Dockerfile.base). Build it once:
#   docker build -f research/dds/docker/Dockerfile.base -t lennox-opendds-base:3.22 .
# Then this app image compiles only our subscriber (seconds).
ARG BASE=lennox-opendds-base:3.22
FROM ${BASE} AS build

# --- build the zoneStatus subscriber from the recovered IDL ---
WORKDIR /src
COPY idl/lennox_m30.idl        /src/idl/lennox_m30.idl
COPY sub/lennox_zone_status_sub.cpp /src/sub/
COPY sub/lennox_sub.mpc    /src/sub/
COPY sub/lennox_sub.mwc    /src/sub/
COPY build_in_container.sh /src/
RUN bash /src/build_in_container.sh

# Gather the runtime .so files into one dir, DEREFERENCING symlinks (the versioned
# libs in /opt/OpenDDS/lib + ACE_wrappers/lib are symlinks into dds/, ace/, TAO/).
RUN mkdir -p /opt/dds-libs \
    && cp -Lv /opt/OpenDDS/lib/*.so* /opt/dds-libs/ 2>/dev/null; \
       cp -Lv /opt/OpenDDS/ACE_wrappers/lib/*.so* /opt/dds-libs/ 2>/dev/null; \
       ls /opt/dds-libs | wc -l

# --- runtime image ---
FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
      libssl3 libxerces-c3.2 python3 python3-pip \
    && pip3 install --no-cache-dir --break-system-packages \
         websockets aiomqtt lennoxs30api \
    && apt-get purge -y python3-pip && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
ENV DDS_ROOT=/opt/OpenDDS
# OpenDDS + ACE/TAO shared libs (dereferenced into one dir in the build stage).
COPY --from=build /opt/dds-libs /opt/dds-libs
COPY --from=build /src/sub/lennox_zone_status_sub /app/lennox_zone_status_sub
COPY opendds_rtps.ini /config/opendds_rtps.ini
COPY entrypoint.sh /app/entrypoint.sh
COPY bridge_server.py /app/bridge_server.py
# credentialed provisioning (login -> mint identity -> download the DDS-Security
# bundle); imported/run by bridge_server when lennox_email/password are set.
COPY m30_prod4.py /app/m30_prod4.py
COPY fetch_security_docs.py /app/fetch_security_docs.py
RUN chmod +x /app/entrypoint.sh
ENV LD_LIBRARY_PATH=/opt/dds-libs
# Default = long-running bridge (DDS -> WebSocket + optional MQTT). For the
# one-shot connection test, override: --entrypoint /app/entrypoint.sh
ENTRYPOINT ["python3", "/app/bridge_server.py"]
