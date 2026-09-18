#!/usr/bin/env python3
"""Lennox iComfort DDS sidecar bridge server.

Runs inside the add-on/container. Spawns the compiled OpenDDS bridge
(`lennox_zone_status_sub --stream`), reads its newline-delimited JSON zoneStatus
samples from stdout, and fans them out to:

  * a local WebSocket (the HA integration connects here) -- always on
  * MQTT with Home Assistant discovery -- optional, only if MQTT_HOST is set

Keeping WebSocket + MQTT here (Python) keeps the C++ bridge tiny (DDS -> stdout).

The C++ bridge does the DDS-Security handshake using the mounted bundle; this
server does no DDS itself. Control (setpoint/mode) flows the other way: the HA
integration sends a JSON `{"type":"command", ...}` frame over the WebSocket,
which we translate to a `SET ...` line on the bridge's stdin (the bridge writes
a scheduleUpdate). As an HA add-on we also load /data/options.json and post
Supervisor discovery so the integration auto-wires this WebSocket.

Env:
  LENNOX_PARTITION   login homeId (required) -- the DDS partition
  LENNOX_DOMAIN      DDS domain id (default 0)
  LENNOX_TOPIC       topic name (default "LCC Zone Status")
  LENNOX_SECURITY_DIR  security bundle dir (default /security)
  LENNOX_CONFIG      OpenDDS ini (default /config/opendds_rtps.ini)
  BRIDGE_BIN         path to the C++ bridge (default /app/lennox_zone_status_sub)
  WS_HOST/WS_PORT    WebSocket bind (default 0.0.0.0:8099)
  MQTT_HOST/MQTT_PORT/MQTT_USER/MQTT_PASS  optional MQTT (discovery under
                     homeassistant/ + state topics under lennox_dds/<sysID>/<zoneId>)
  DCPS_DEBUG         OpenDDS debug level (default 0)
"""
from __future__ import annotations

import asyncio
import builtins
import calendar
import datetime
import json
import os
import re
import signal
import socket
import time
import urllib.request
from contextlib import suppress


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def print(*args, **kwargs):  # noqa: A001 - deliberately timestamp every log line
    builtins.print(f"[{_ts()}]", *args, **kwargs)

# --------------------------------------------------------------------------- #
# Home Assistant add-on integration (options + Supervisor discovery)
#
# When run as an HA add-on, Supervisor writes the user's options to
# /data/options.json and exposes its API at http://supervisor with the token in
# SUPERVISOR_TOKEN. We map options -> the env vars the rest of this server reads,
# pull MQTT creds from the Supervisor `mqtt` service if enabled, and announce
# ourselves via Supervisor discovery so the companion integration auto-wires the
# WebSocket URL (no manual host/port typing). Outside an add-on these are no-ops.
# --------------------------------------------------------------------------- #
SUPERVISOR = "http://supervisor"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
DISCOVERY_SERVICE = "lennox_dds"  # must equal the integration domain


def _supervisor_request(method: str, path: str, body: dict | None = None) -> dict | None:
    if not SUPERVISOR_TOKEN:
        return None
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{SUPERVISOR}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {SUPERVISOR_TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (trusted host)
            return json.loads(resp.read() or b"{}")
    except Exception as err:  # noqa: BLE001
        print(f"[bridge-server] supervisor {method} {path} failed: {err}", flush=True)
        return None


def _load_addon_options() -> None:
    """Map /data/options.json (+ Supervisor MQTT service) onto our env vars.

    Env already set in the container wins (os.environ.setdefault), so a manual
    `docker run -e ...` still overrides add-on options.
    """
    try:
        with open("/data/options.json", encoding="utf-8") as fh:
            opts = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return  # not running as an add-on
    print("[bridge-server] loaded add-on options", flush=True)
    if opts.get("topic"):
        os.environ.setdefault("LENNOX_TOPIC", str(opts["topic"]))
    os.environ.setdefault("LENNOX_DOMAIN", str(opts.get("domain", 0)))
    os.environ.setdefault("DCPS_DEBUG", str(opts.get("dcps_debug", 0)))
    # Health check: max seconds without a sample before the container is UNHEALTHY.
    os.environ.setdefault("HEALTH_MAX_AGE", str(opts.get("health_max_age", 300)))
    # Debug: `debug` logs every raw sample (any model) for bug reports. `debug_away`
    # additionally subscribes read-only to the away topic (away-state RE).
    if opts.get("debug") or opts.get("debug_away"):
        os.environ.setdefault("LENNOX_RAW_DUMP", "1")
        print("[bridge-server] debug ON: raw sample logging", flush=True)
    if opts.get("debug_away"):
        os.environ.setdefault("LENNOX_DEBUG_AWAY", "1")
        print("[bridge-server] debug_away ON: away echo reader", flush=True)

    # MQTT (optional): publish state + accept control. Broker comes from an
    # explicit mqtt_host option, else the Supervisor `mqtt` service (Mosquitto).
    # mqtt_discovery=false (default) = external/non-HA use: raw state + set/ topics
    # only, no HA entity (and it removes one if previously published).
    if opts.get("mqtt_enabled"):
        os.environ.setdefault("MQTT_DISCOVERY", "1" if opts.get("mqtt_discovery") else "0")
        if opts.get("mqtt_host"):
            os.environ.setdefault("MQTT_HOST", str(opts["mqtt_host"]))
            os.environ.setdefault("MQTT_PORT", str(opts.get("mqtt_port", 1883)))
            if opts.get("mqtt_user"):
                os.environ.setdefault("MQTT_USER", str(opts["mqtt_user"]))
            if opts.get("mqtt_password"):
                os.environ.setdefault("MQTT_PASS", str(opts["mqtt_password"]))
            print(f"[bridge-server] MQTT: using configured broker {opts['mqtt_host']}", flush=True)
        else:
            svc = _supervisor_request("GET", "/services/mqtt")
            creds = (svc or {}).get("data") or {}
            if creds.get("host"):
                os.environ.setdefault("MQTT_HOST", str(creds["host"]))
                os.environ.setdefault("MQTT_PORT", str(creds.get("port", 1883)))
                if creds.get("username"):
                    os.environ.setdefault("MQTT_USER", str(creds["username"]))
                if creds.get("password"):
                    os.environ.setdefault("MQTT_PASS", str(creds["password"]))
                print(f"[bridge-server] MQTT: broker from Supervisor = "
                      f"{creds['host']}:{creds.get('port', 1883)}", flush=True)
            else:
                print("[bridge-server] MQTT enabled but no broker found: Supervisor "
                      "/services/mqtt returned none. Add the MQTT integration / Mosquitto "
                      "add-on, or set mqtt_host in the add-on options.", flush=True)

    # Option A -- credentials: fetch the whole DDS-Security bundle from the
    # Lennox cloud (login -> mint identity -> download docs) and auto-derive the
    # homeId/partition. Zero manual cert handling. Falls through to Option B on
    # absence/failure.
    provisioned = _provision_from_creds(opts)
    if provisioned:
        os.environ.setdefault("LENNOX_SECURITY_DIR", provisioned)
    else:
        # Option B -- user-supplied bundle in HA's /config (mapped read-only). The
        # exact mount path of a `homeassistant_config` map varies by Supervisor
        # version, so resolve it by searching candidates for the files.
        if opts.get("home_id"):
            os.environ.setdefault("LENNOX_PARTITION", str(opts["home_id"]))
        src = _resolve_security_dir(str(opts.get("security_dir",
                                                 "/homeassistant_config/lennox_dds/security")))
        os.environ.setdefault("LENNOX_SECURITY_DIR", _materialize_bundle(src))


def _bundle_complete(d: str) -> bool:
    need = ("identity_ca.pem", "identity.pem", "identity.key", "permissions_ca.pem",
            "governance.xml.p7s", "permissions.xml.p7s")
    return all(os.path.isfile(os.path.join(d, f)) and os.path.getsize(os.path.join(d, f)) > 0
               for f in need)


def _cert_not_before(path: str) -> float | None:
    """UTC epoch of a PEM cert's notBefore, via stdlib (no openssl/cryptography)."""
    try:
        import ssl
        nb = ssl._ssl._test_decode_cert(path)["notBefore"]  # e.g. 'Sep 16 22:03:00 2026 GMT'
        return calendar.timegm(time.strptime(nb, "%b %d %H:%M:%S %Y %Z"))
    except Exception:  # noqa: BLE001
        return None


def await_identity_valid() -> None:
    """OpenDDS-Security validates our own identity cert's notBefore at participant
    creation with zero skew tolerance. Freshly-minted Lennox certs start ~now, so
    if the container clock lags the issuer the cert reads 'not yet valid' and the
    bridge crash-loops. Log the skew and wait (bounded) until the cert is valid."""
    secdir = os.environ.get("LENNOX_SECURITY_DIR")
    if not secdir:
        return
    nb = _cert_not_before(os.path.join(secdir, "identity.pem"))
    now = time.time()
    print(f"[bridge-server] container UTC={int(now)} identity.notBefore="
          f"{int(nb) if nb else None} (skew={int(nb - now) if nb else 'n/a'}s)", flush=True)
    if nb and nb > now:
        wait = min(int(nb - now) + 3, 900)
        print(f"[bridge-server] identity cert not valid for {int(nb - now)}s "
              f"(container clock behind issuer); waiting {wait}s before starting DDS. "
              f"Fix the host/add-on clock (NTP) to avoid this.", flush=True)
        time.sleep(wait)


def _provision_from_creds(opts: dict) -> str | None:
    """If lennox_email/lennox_password are set, fetch the DDS-Security bundle into
    the add-on's persistent /data/security (cached across restarts) and set
    LENNOX_PARTITION from the derived homeId. Returns the bundle dir or None."""
    import subprocess
    email, password = opts.get("lennox_email"), opts.get("lennox_password")
    if not (email and password):
        return None
    cache = "/data/security"
    os.makedirs(cache, exist_ok=True)
    # Reuse the cache only if the identity cert is not in the future (a cached cert
    # minted while the clock was skewed can read 'not yet valid' forever -> re-mint).
    nb = _cert_not_before(os.path.join(cache, "identity.pem"))
    cache_ok = _bundle_complete(cache) and (nb is None or nb <= time.time() + 60)
    if cache_ok:
        print("[bridge-server] using cached provisioned bundle (/data/security)", flush=True)
    else:
        if _bundle_complete(cache) and nb and nb > time.time() + 60:
            print(f"[bridge-server] cached identity not-yet-valid ({int(nb - time.time())}s "
                  "in the future); re-provisioning a fresh one", flush=True)
            for f in os.listdir(cache):
                with suppress(OSError):
                    os.remove(os.path.join(cache, f))
        print("[bridge-server] provisioning DDS-Security bundle from Lennox login...", flush=True)
        env = {**os.environ, "LENNOX_EMAIL": str(email), "LENNOX_PASSWORD": str(password),
               "LENNOX_BUNDLE_DIR": cache, "PYTHONPATH": "/app"}
        try:
            r = subprocess.run(["python3", "/app/fetch_security_docs.py"], env=env, cwd="/app",
                               capture_output=True, text=True, timeout=180)
            out = re.sub(r"(?i)bearer\s+[A-Za-z0-9._-]+", "Bearer <redacted>", r.stdout + r.stderr)
            for line in out.strip().splitlines()[-25:]:
                print("  [provision] " + line, flush=True)
        except Exception as err:  # noqa: BLE001
            print(f"[bridge-server] provisioning error: {err}", flush=True)
    if not _bundle_complete(cache):
        print("[bridge-server] provisioning did not yield a complete bundle; "
              "falling back to a user-supplied bundle", flush=True)
        return None
    hp = os.path.join(cache, "home_id.txt")
    if os.path.isfile(hp):
        hid = open(hp, encoding="utf-8").read().strip()
        if hid:
            os.environ.setdefault("LENNOX_PARTITION", hid)
            print(f"[bridge-server] auto homeId/partition = {hid}", flush=True)
    return cache


def _resolve_security_dir(configured: str) -> str:
    """Find the dir that actually holds the DDS-Security bundle. A
    `homeassistant_config` map may mount HA's /config at /homeassistant_config,
    /homeassistant, or /config depending on Supervisor version, so try the
    configured path first then the same 'lennox_dds/security' tail under each
    known base. Logs each candidate's listing to aid diagnosis."""
    tail = "lennox_dds/security"
    candidates = [configured]
    for base in ("/homeassistant_config", "/homeassistant", "/config", "/data"):
        c = f"{base}/{tail}"
        if c not in candidates:
            candidates.append(c)
    for c in candidates:
        try:
            names = sorted(os.listdir(c))
        except OSError:
            print(f"[bridge-server] security dir not present: {c}", flush=True)
            continue
        has_bundle = any(n.startswith(("identity_ca.pem", "identity.pem")) for n in names)
        print(f"[bridge-server] security dir {c}: {names} "
              f"({'has bundle' if has_bundle else 'no bundle'})", flush=True)
        if has_bundle:
            return c
    print(f"[bridge-server] WARNING: no bundle dir found; using {configured}", flush=True)
    return configured


def _materialize_bundle(src: str) -> str:
    """If the security dir holds base64 (.b64) files, decode them into a writable
    runtime dir (alongside the plain files) and return that dir; else return src.

    Rationale: the S/MIME governance/permissions docs are CRLF-signed, and some
    provisioning channels (e.g. text-only file APIs) can't carry raw CR bytes.
    Shipping those as <name>.b64 preserves them exactly; a .b64 twin overrides a
    same-named plain file. The read-only bundle mount also means we must copy to
    a writable location to present a clean dir to OpenDDS."""
    import base64
    try:
        names = os.listdir(src)
    except OSError:
        return src
    if not any(n.endswith(".b64") for n in names):
        return src  # nothing to decode; use the dir as-is
    b64set = {n for n in names if n.endswith(".b64")}
    runtime = "/tmp/lennox_security"  # noqa: S108 (container-local, ephemeral)
    os.makedirs(runtime, exist_ok=True)
    for n in names:
        sp = os.path.join(src, n)
        if not os.path.isfile(sp):
            continue
        if not n.endswith(".b64") and (n + ".b64") in b64set:
            continue  # the .b64 twin is authoritative
        try:
            with open(sp, "rb") as fh:
                raw = fh.read()
            data = base64.b64decode(raw) if n.endswith(".b64") else raw
            with open(os.path.join(runtime, n[:-4] if n.endswith(".b64") else n), "wb") as out:
                out.write(data)
        except Exception as err:  # noqa: BLE001
            print(f"[bridge-server] bundle materialize failed for {n}: {err}", flush=True)
    print(f"[bridge-server] materialized security bundle -> {runtime}", flush=True)
    return runtime


def _post_discovery(host: str, port: int) -> None:
    """Announce this add-on to HA via Supervisor discovery so the companion
    integration's async_step_hassio fires with our WebSocket host/port."""
    resp = _supervisor_request(
        "POST", "/discovery",
        {"service": DISCOVERY_SERVICE, "config": {"host": host, "port": port}},
    )
    if resp is not None:
        print(f"[bridge-server] posted discovery ({host}:{port})", flush=True)


_load_addon_options()

BRIDGE_BIN = os.environ.get("BRIDGE_BIN", "/app/lennox_zone_status_sub")
INI = os.environ.get("LENNOX_CONFIG", "/config/opendds_rtps.ini")
SEC_DIR = os.environ.get("LENNOX_SECURITY_DIR", "/security")
DOMAIN = os.environ.get("LENNOX_DOMAIN", "0")
TOPIC = os.environ.get("LENNOX_TOPIC", "LCC Zone Status")
PARTITION = os.environ.get("LENNOX_PARTITION", "")
DCPS_DEBUG = os.environ.get("DCPS_DEBUG", "0")
WS_HOST = os.environ.get("WS_HOST", "0.0.0.0")
WS_PORT = int(os.environ.get("WS_PORT", "8099"))
MQTT_HOST = os.environ.get("MQTT_HOST")
MQTT_DISCOVERY = os.environ.get("MQTT_DISCOVERY", "0") == "1"  # publish an HA entity?
RAW_DUMP = os.environ.get("LENNOX_RAW_DUMP", "0") == "1"  # debug: log each raw sample

# Liveness heartbeat: we touch this file every time a zoneStatus sample arrives from
# the DDS subprocess. The container HEALTHCHECK (healthcheck.sh) reports UNHEALTHY
# when it goes stale, so "DDS connected but receiving nothing" (relay eviction, cert
# expiry, device offline) surfaces in HA instead of looking fine. mtime = last
# sample time; freshness threshold is HEALTH_MAX_AGE seconds (default 300).
HEARTBEAT_FILE = os.environ.get("LENNOX_HEARTBEAT_FILE", "/tmp/lennox_last_sample")
# healthcheck.sh runs as a separate process (docker exec) and can't see our env, so
# we persist the resolved staleness threshold (the `health_max_age` add-on option)
# to this file for it to read. Env HEALTH_MAX_AGE still wins if set on the container.
HEALTH_MAX_AGE_FILE = os.environ.get("LENNOX_HEALTH_MAX_AGE_FILE",
                                     "/tmp/lennox_health_max_age")


def _touch_heartbeat() -> None:
    """Record that a sample was just received (best-effort; never raises)."""
    try:
        with open(HEARTBEAT_FILE, "w") as fh:
            fh.write(str(int(time.time())))
    except Exception:
        pass


def _persist_health_max_age() -> None:
    """Write the configured health staleness threshold where healthcheck.sh reads it."""
    try:
        with open(HEALTH_MAX_AGE_FILE, "w") as fh:
            fh.write(str(int(os.environ.get("HEALTH_MAX_AGE", "300"))))
    except Exception:
        pass


# latest sample per (sysID, zoneId); newly-connected WS clients get a snapshot.
_latest: dict[str, dict] = {}
_ws_clients: set = set()
_mqtt = None  # set if MQTT enabled
_discovery_sent: set = set()
_bridge_proc = None  # current asyncio subprocess (for writing control commands)

# HA maps a zone to a schedule slot. scheduleId = 16 + zoneId is the MANUAL hold
# slot the Lennox app uses for setpoint changes -> the device applies it promptly.
# (The 32 + zoneId "override"/scheduled slot applies slowly/variably; verified by
# a native frida capture of the app's scheduleUpdate -- see memory
# control-write-latency.) Control writes target the manual slot.
SCHEDULE_OVERRIDE_BASE = 16


def _build_set_line(cmd: dict) -> str | None:
    """Translate a control command dict into a bridge stdin line:
        SET <sysID> <scheduleId> [mode=<int>] [csp=<F>] [hsp=<F>] [sp=<F>]
                                  [husp=<%>] [desp=<%>]
    Returns None if the command has no sysID or no writable field."""
    sys_id = cmd.get("sysID")
    if not sys_id:
        return None
    # Manual Away is a per-system toggle on its own topic (not a schedule field).
    if cmd.get("away") is not None:
        return f"AWAY {sys_id} {1 if cmd['away'] else 0}"
    schedule_id = SCHEDULE_OVERRIDE_BASE + int(cmd.get("zoneId", 0))
    parts = ["SET", str(sys_id), str(schedule_id)]
    for field in ("mode", "fan"):  # integer enum fields
        if cmd.get(field) is not None:
            parts.append(f"{field}={int(cmd[field])}")
    for field in ("csp", "hsp", "sp"):  # float setpoints (Fahrenheit)
        if cmd.get(field) is not None:
            parts.append(f"{field}={float(cmd[field])}")
    for field in ("husp", "desp"):  # humidify / dehumidify setpoints (%RH)
        if cmd.get(field) is not None:
            parts.append(f"{field}={int(cmd[field])}")
    return " ".join(parts) if len(parts) > 3 else None


async def _send_command(cmd: dict) -> None:
    """Write one control line to the DDS bridge's stdin."""
    line = _build_set_line(cmd)
    if line is None:
        print(f"[bridge-server] ignoring control cmd (no target/field): {cmd}", flush=True)
        return
    proc = _bridge_proc
    if proc is None or proc.stdin is None or proc.returncode is not None:
        print("[bridge-server] control cmd dropped: bridge not running", flush=True)
        return
    print(f"[bridge-server] control -> bridge: {line}", flush=True)
    proc.stdin.write((line + "\n").encode())
    with suppress(Exception):
        await proc.stdin.drain()


# --------------------------------------------------------------------------- #
# WebSocket fan-out (uses the `websockets` library)
# --------------------------------------------------------------------------- #
async def _ws_handler(ws):
    _ws_clients.add(ws)
    try:
        # snapshot current state to the new client
        for sample in _latest.values():
            await ws.send(json.dumps(sample))
        # inbound = control commands from the HA integration
        async for raw in ws:
            try:
                cmd = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(cmd, dict) and cmd.get("type") == "command":
                await _send_command(cmd)
    finally:
        _ws_clients.discard(ws)


async def _ws_broadcast(sample: dict) -> None:
    if not _ws_clients:
        return
    msg = json.dumps(sample)
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send(msg)
        except Exception:  # noqa: BLE001
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


# --------------------------------------------------------------------------- #
# Optional MQTT publish with Home Assistant discovery
# --------------------------------------------------------------------------- #
async def _mqtt_connect():
    global _mqtt
    try:
        import aiomqtt  # type: ignore
    except ImportError:
        print("[bridge-server] MQTT_HOST set but aiomqtt not installed; skipping MQTT",
              flush=True)
        return None
    client = aiomqtt.Client(
        hostname=MQTT_HOST,
        port=int(os.environ.get("MQTT_PORT", "1883")),
        username=os.environ.get("MQTT_USER"),
        password=os.environ.get("MQTT_PASS"),
    )
    await client.__aenter__()
    _mqtt = client
    print(f"[bridge-server] MQTT connected to {MQTT_HOST}", flush=True)
    return client


async def _mqtt_publish(sample: dict) -> None:
    if _mqtt is None:
        return
    sys_id = str(sample.get("sysID", "sys"))
    zone = str(sample.get("zoneId", "0"))
    base = f"lennox_dds/{sys_id}/{zone}"
    key = base
    # one-time HA MQTT discovery for a controllable climate (state + command
    # topics) -- only when mqtt_discovery is on; otherwise remove any stale entity
    # so external-only mode doesn't leave a duplicate in HA.
    if key not in _discovery_sent:
        uid = f"lennox_dds_{sys_id}_{zone}"
        cfg_topic = f"homeassistant/climate/{uid}/config"
        if not MQTT_DISCOVERY:
            with suppress(Exception):
                await _mqtt.publish(cfg_topic, "", retain=True)  # remove HA entity
            _discovery_sent.add(key)
            with suppress(Exception):
                await _mqtt.publish(f"{base}/state", json.dumps(sample), retain=True)
            return
        st = f"{base}/state"
        disc = {
            "name": f"Lennox iComfort zone {zone}",
            "unique_id": uid,
            "temperature_unit": "F",
            "min_temp": 45, "max_temp": 95, "temp_step": 1,
            "modes": ["off", "heat", "cool", "heat_cool"],
            "fan_modes": ["auto", "circulate", "on", "auto_circulate"],
            # --- current readings ---
            "current_temperature_topic": st,
            "current_temperature_template": "{{ value_json.temperature }}",
            "current_humidity_topic": st,
            "current_humidity_template": "{{ value_json.humidity }}",
            "action_topic": st,
            "action_template":
                "{{ ['off','heating','cooling','idle','idle'][value_json.tempOperation] }}",
            # --- hvac mode (state + command) ---
            "mode_state_topic": st,
            "mode_state_template":
                "{{ ['off','heat','cool','heat_cool','heat','off','off','off','off']"
                "[value_json.period.systemMode] }}",
            "mode_command_topic": f"{base}/set/mode",
            # --- fan (state + command) ---
            "fan_mode_state_topic": st,
            "fan_mode_state_template":
                "{{ ['auto','auto','circulate','on','auto_circulate','auto']"
                "[value_json.period.fanMode] }}",
            "fan_mode_command_topic": f"{base}/set/fan_mode",
            # --- single setpoint (heat->hsp, cool->csp, else sp) ---
            "temperature_state_topic": st,
            "temperature_state_template":
                "{{ value_json.period.hsp if value_json.period.systemMode == 1 else "
                "(value_json.period.csp if value_json.period.systemMode == 2 else "
                "value_json.period.sp) }}",
            "temperature_command_topic": f"{base}/set/temperature",
            # --- heat_cool range (low=hsp, high=csp) ---
            "temperature_low_state_topic": st,
            "temperature_low_state_template": "{{ value_json.period.hsp }}",
            "temperature_low_command_topic": f"{base}/set/temperature_low",
            "temperature_high_state_topic": st,
            "temperature_high_state_template": "{{ value_json.period.csp }}",
            "temperature_high_command_topic": f"{base}/set/temperature_high",
            "device": {"identifiers": [f"lennox_dds_{sys_id}"], "name": f"Lennox iComfort {sys_id}",
                       "manufacturer": "Lennox", "model": "iComfort"},
        }
        with suppress(Exception):
            await _mqtt.publish(f"homeassistant/climate/{uid}/config",
                                json.dumps(disc), retain=True)
        _discovery_sent.add(key)
    with suppress(Exception):
        await _mqtt.publish(f"{base}/state", json.dumps(sample), retain=True)


# incoming MQTT control: lennox_dds/<sysID>/<zone>/set/<field>  (values: HA-style
# mode/fan strings, or a °F number). Routed through the same DDS control path.
_MQTT_MODE = {"off": 0, "heat": 1, "cool": 2, "heat_cool": 3}
_MQTT_FAN = {"auto": 1, "circulate": 2, "on": 3, "auto_circulate": 4}


async def _handle_mqtt_command(topic: str, payload: str) -> None:
    parts = topic.split("/")
    if len(parts) != 5 or parts[0] != "lennox_dds" or parts[3] != "set":
        return
    sys_id, zone_s, field = parts[1], parts[2], parts[4]
    payload = payload.strip()
    cmd: dict = {"sysID": sys_id, "zoneId": int(zone_s) if zone_s.isdigit() else 0}
    try:
        if field == "mode":
            m = _MQTT_MODE.get(payload.lower())
            if m is None:
                return
            cmd["mode"] = m
        elif field == "fan_mode":
            f = _MQTT_FAN.get(payload.lower())
            if f is None:
                return
            cmd["fan"] = f
        elif field == "temperature_low":
            cmd["hsp"] = float(payload)
        elif field == "temperature_high":
            cmd["csp"] = float(payload)
        elif field == "temperature":
            t = float(payload)
            mode = _latest.get(f"{sys_id}:{cmd['zoneId']}", {}).get("period", {}).get("systemMode")
            cmd["hsp" if mode == 1 else "csp" if mode == 2 else "sp"] = t
        elif field == "away":
            cmd["away"] = payload.lower() in ("on", "true", "1")
        else:
            return
    except ValueError:
        return
    print(f"[bridge-server] MQTT command {topic} = {payload!r}", flush=True)
    await _send_command(cmd)


async def _mqtt_command_loop() -> None:
    """Subscribe to the set/# topics and route each to the DDS control path."""
    if _mqtt is None:
        return
    with suppress(Exception):
        await _mqtt.subscribe("lennox_dds/+/+/set/#")
        print("[bridge-server] MQTT: subscribed to lennox_dds/+/+/set/# (control)", flush=True)
    try:
        async for message in _mqtt.messages:
            with suppress(Exception):
                await _handle_mqtt_command(str(message.topic), message.payload.decode())
    except Exception as err:  # noqa: BLE001
        print(f"[bridge-server] MQTT command loop ended: {err}", flush=True)


# --------------------------------------------------------------------------- #
# DDS bridge subprocess
# --------------------------------------------------------------------------- #
async def _run_bridge() -> None:
    argv = [
        BRIDGE_BIN, "-DCPSConfigFile", INI,
        "--domain", DOMAIN, "--topic", TOPIC, "--partition", PARTITION,
        "--security-dir", SEC_DIR, "--stream", "-DCPSDebugLevel", DCPS_DEBUG,
    ]
    global _bridge_proc
    proc = None
    try:
      while True:
        print(f"[bridge-server] launching DDS bridge: {' '.join(argv)}", flush=True)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE,   # control commands go here
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)         # forwarded with timestamps
        _bridge_proc = proc
        assert proc.stdout and proc.stderr
        # forward the C++ bridge's stderr ([cmd]/[bridge]/[debug-*]/OpenDDS) so
        # every line is timestamped like the rest of our log.
        stderr_task = asyncio.create_task(_forward_stderr(proc.stderr))
        async for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue  # stray non-JSON line
            if RAW_DUMP:
                print(f"[raw] {line.decode('utf-8', 'replace')}", flush=True)
            _touch_heartbeat()  # liveness: a sample arrived (see HEARTBEAT_FILE)
            _latest[f"{sample.get('sysID')}:{sample.get('zoneId')}"] = sample
            await _ws_broadcast(sample)
            await _mqtt_publish(sample)
        rc = await proc.wait()
        _bridge_proc = None
        stderr_task.cancel()
        with suppress(asyncio.CancelledError):
            await stderr_task
        print(f"[bridge-server] DDS bridge exited rc={rc}; restarting in 5s", flush=True)
        await asyncio.sleep(5)  # reconnect/backoff
    finally:
        # On shutdown (task cancelled), terminate the DDS subprocess cleanly.
        if proc is not None and proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.terminate()
            with suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)
        _bridge_proc = None


async def _forward_stderr(stream: asyncio.StreamReader) -> None:
    """Re-emit the C++ bridge's stderr through our timestamped print()."""
    async for raw in stream:
        text = raw.rstrip(b"\n").decode("utf-8", "replace")
        if text:
            print(text, flush=True)


async def main() -> None:
    if not PARTITION:
        raise SystemExit("LENNOX_PARTITION (login homeId) is required")
    _persist_health_max_age()  # expose the health threshold to healthcheck.sh
    import websockets  # type: ignore

    # Graceful shutdown: SIGTERM/SIGINT (Supervisor stop/restart) sets a stop event
    # so we cancel the bridge and unwind cleanly -- NOT loop.stop(), which aborts
    # run_until_complete mid-await ("Event loop stopped before Future completed").
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    if MQTT_HOST:
        await _mqtt_connect()
        if _mqtt is not None:
            asyncio.create_task(_mqtt_command_loop())  # external MQTT control

    async with websockets.serve(_ws_handler, WS_HOST, WS_PORT):
        print(f"[bridge-server] WebSocket on ws://{WS_HOST}:{WS_PORT}", flush=True)
        # Announce ourselves to HA so the companion integration auto-configures.
        # Advertise our container hostname (resolvable by core on the Supervisor
        # network), not the 0.0.0.0 bind address.
        _post_discovery(socket.gethostname(), WS_PORT)
        bridge = asyncio.create_task(_run_bridge())
        await stop.wait()
        print("[bridge-server] shutdown signal; stopping bridge", flush=True)
        bridge.cancel()
        with suppress(asyncio.CancelledError):
            await bridge


if __name__ == "__main__":
    await_identity_valid()  # block until our identity cert's notBefore has passed
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
