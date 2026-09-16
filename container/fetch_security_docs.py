"""Phase 2: fetch the Lennox M30 DDS-Security document bundle.

Static analysis of the decompiled S40 app (see research/security/FINDINGS.md)
shows the app does NOT fetch these in Java: it hands a Properties map + a plant
JWT supplier to native libLCS.so, which downloads + verifies + writes six files.
The endpoint PATHS are string constants inside libLCS.so; this scaffold replays
them from Python so we can obtain the same bundle without the app.

Flow (mirrors research/m30_prod4.py exactly for the first three steps):

    authenticate (device cert)      -> certificateToken
    login        (user creds)       -> userToken
    registerLCCOwner (plantdevices) -> plant JWT (+ user.id, permissions)
    GET  relay.json                          -> relay addrs + IdentityCADigest / etc.
    GET  /v1/dds-im/static/identity_ca.pem    -> identity_ca.pem      [LIVE-CONFIRMED]
    GET  /v1/dds-im/static/permissions_ca.pem -> permissions_ca.pem   [LIVE-CONFIRMED]
    GET  /v1/dds-im/static/governance.xml.p7s -> governance.xml.p7s   [LIVE-CONFIRMED]
    POST /v1/dds-im/identity/newcert          -> JSON{certificate,privateKey}  [LIVE-CONFIRMED]
    GET  /v1/dds-im/permissions/<homeId>      -> permissions.xml.p7s  [located; NONCE-GATED, see below]

The plant JWT must carry DDS.SECURITY_DOCUMENTS.GET and DDS.LCC_OWNER.

STATUS (2026-09-16, live-tested with a real plant token): 5 of 6 artifacts are
fully downloadable from Python with just the plant Bearer token + `User` header,
and the three static docs were verified byte-identical (SHA-1) to the bundle the
device provisioned. The 6th (participant permissions) endpoint is LOCATED
(`GET /v1/dds-im/permissions/<homeId>` returns 401, not 404) but is gated by the
session NONCE that libLCS `use_nonce` injects into the URL (static/newcert do NOT
need it; permissions does). Recovering the nonce needs a frida hook on
`Lennox::NONCE` / `use_nonce` in libLCS (or the Java `b0.o()` that sets it). Not
blocking: the device-pulled permissions.xml.p7s is valid ~3 months. Do NOT
hardcode secrets. NOTE: each newcert POST mints a fresh participant cert server
side; call it only when you actually need a new identity.

Usage:  LENNOX_EMAIL=... LENNOX_PASSWORD=... python fetch_security_docs.py
   or:  python fetch_security_docs.py --creds creds.json
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

# --- reuse the verified Phase-1 client (authenticate/login/registerLCCOwner) ---
# research/m30_prod4.py sits one directory up.
_RESEARCH_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RESEARCH_DIR))
from m30_prod4 import M30Prod4Client, _load_creds, USER_AGENT  # noqa: E402

# Hosts (LXBuildConstants PRD_BUILD; e.F / e.G in r8/e.java).
PLANT_ID_URL = "https://plantdevices.myicomfort.com"          # DDS_IDENTITY_MANAGER_URL
RELAY_URL = "https://relay.plantdevices.myicomfort.com"        # plant load balancer

# Output dir: env override lets the HA add-on point it at its persistent cache.
BUNDLE_DIR = Path(os.environ.get("LENNOX_BUNDLE_DIR",
                                 str(Path(__file__).resolve().parent / "bundle")))

# Endpoint paths — LIVE-CONFIRMED (base "/v1/dds-im" + libLCS path fragments).
DDS_IM = "/v1/dds-im"
STATIC_DOCS = {
    # local filename : path under PLANT_ID_URL, and which relay.json digest verifies it
    "identity_ca.pem":     (f"{DDS_IM}/static/identity_ca.pem",    "IdentityCADigest"),
    "permissions_ca.pem":  (f"{DDS_IM}/static/permissions_ca.pem", "PermissionsCADigest"),
    "governance.xml.p7s":  (f"{DDS_IM}/static/governance.xml.p7s", "GovernanceDigest"),
}
NEWCERT_PATH = f"{DDS_IM}/identity/newcert"   # POST {} -> JSON{certificate,privateKey,certificateRequest}
# LIVE-CAPTURED (frida SSL_write hook): the path segment is the ROLE, not the
# homeId: GET /v1/dds-im/permissions/DDS.LCC_OWNER?nonce=<nonce>. The nonce
# identifies the participant (the identity minted by newcert?nonce=<same>).
PERMISSIONS_PATH = DDS_IM + "/permissions/DDS.LCC_OWNER"


def gen_nonce() -> str:
    """Mint a permissions-download nonce the way the app does
    (com.krasamo.lx_ic3_mobile.b0.o()): the last hyphen segment of a random
    UUID = 12 hex chars. Client-generated; the server only requires it present."""
    import uuid
    return str(uuid.uuid4()).split("-")[-1]


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha1_hex(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _verify_digest(name: str, data: bytes, expected: str | None) -> None:
    """relay.json *Digest = hex SHA-1 of the file bytes (LIVE-CONFIRMED: all
    three static docs matched sha1)."""
    if not expected:
        print(f"    [warn] no relay.json digest for {name}; skipping verify")
        return
    got1 = _sha1_hex(data)
    if expected.lower() == got1:
        print(f"    [ok] {name} SHA-1 matches relay.json digest")
    else:
        print(f"    [warn] {name} digest mismatch:\n"
              f"           relay.json = {expected}\n"
              f"           sha1       = {got1}\n"
              f"           sha256     = {_sha256_hex(data)}")


class SecurityDocFetcher:
    def __init__(self, client: M30Prod4Client) -> None:
        self._c = client
        self._relay: dict = {}

    @property
    def _session(self):
        return self._c._api._session

    @property
    def _ssl(self):
        return self._c._api.ssl

    def _auth_headers(self) -> dict[str, str]:
        # Authorization: Bearer <plant JWT>  (libLCS: "Authorization" + "Bearer ").
        # The sibling plantdevices REST calls also send `User: <userId>`; libLCS
        # may or may not require it for the identity-manager API -> TODO confirm.
        return {
            "Authorization": "Bearer " + (self._c._plant_token or ""),
            "User": str(self._c._user_id),
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        }

    async def fetch_relay_json(self) -> dict:
        url = f"{RELAY_URL}/relay.json"
        # NOTE: the relay load-balancer host is picky about the exact request the
        # app sends; a generic GET currently returns 400 (both with and without
        # auth). Non-fatal: relay.json only supplies addresses + digests we
        # already know, so on failure we fall back to a cached bundle/relay.json.
        try:
            r = await self._session.get(url, headers={"User-Agent": USER_AGENT}, ssl=self._ssl)
            body = await r.text()
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}: {body[:120]!r}")
            self._relay = json.loads(body)
            (BUNDLE_DIR / "relay.json").write_text(body)
        except Exception as e:  # noqa: BLE001
            cached = BUNDLE_DIR / "relay.json"
            if cached.exists():
                self._relay = json.loads(cached.read_text())
                print(f"  [warn] live relay.json failed ({e}); using cached {cached}")
            else:
                print(f"  [warn] live relay.json failed ({e}) and no cache; "
                      "digest verification will be skipped. TODO: capture the "
                      "app's exact relay.json request (likely a specific header/query).")
                return self._relay
        print("  relay.json:")
        for k in ("SpdpRtpsRelayAddress", "SedpRtpsRelayAddress", "DataRtpsRelayAddress",
                  "ServerVersion", "IdentityCADigest", "PermissionsCADigest", "GovernanceDigest"):
            print(f"    {k} = {self._relay.get(k)}")
        return self._relay

    async def fetch_static_docs(self) -> None:
        for fname, (path, digest_key) in STATIC_DOCS.items():
            url = f"{PLANT_ID_URL}{path}"
            r = await self._session.get(url, headers=self._auth_headers(), ssl=self._ssl)
            data = await r.read()
            if r.status != 200:
                raise RuntimeError(f"{fname} failed: {r.status} {data[:200]!r}")
            (BUNDLE_DIR / fname).write_bytes(data)
            print(f"  saved {fname} ({len(data)} B) from {path}")
            _verify_digest(fname, data, self._relay.get(digest_key))

    async def fetch_participant_identity(self, nonce: str, device_id: str | None = None) -> None:
        """POST /v1/dds-im/identity/newcert?nonce=<value> -> JSON{certificate,
        privateKey, certificateRequest}. The server generates the CSR + EC P-256
        keypair + signed cert. Empty body yields subject `CN=<userId>`.

        CRITICAL (from libLCS: download_identity calls use_nonce): the newcert
        call MUST carry the same nonce that the subsequent permissions download
        uses. The server binds the minted identity to that nonce; permissions
        then returns the grant for that identity. Skipping the nonce here is why a
        later permissions GET 401s 'Account lacks sufficient permissions'.
        """
        url = f"{PLANT_ID_URL}{NEWCERT_PATH}?nonce={nonce}"
        body: dict = {}
        if device_id:
            # TODO(confirm key name): the deviceId suffix in CN=<uid>-<deviceId>
            # comes from a newcert body field; exact key not yet confirmed.
            body["deviceId"] = device_id
        r = await self._session.post(url, json=body, headers=self._auth_headers(), ssl=self._ssl)
        text = await r.text()
        if r.status not in (200, 201):
            print(f"  newcert -> HTTP {r.status}: {text[:200]!r}")
            return
        j = json.loads(text)
        cert = j.get("certificate")
        key = j.get("privateKey")
        if cert:
            (BUNDLE_DIR / "identity.pem").write_text(cert if cert.endswith("\n") else cert + "\n")
            print(f"  saved identity.pem ({len(cert)} B)")
        if key:
            (BUNDLE_DIR / "identity.key").write_text(key if key.endswith("\n") else key + "\n")
            print(f"  saved identity.key ({len(key)} B)")
        if not (cert and key):
            print(f"    [warn] unexpected newcert JSON keys: {list(j.keys())}")

    async def fetch_participant_permissions(self, home_id: str, nonce: str | None = None) -> None:
        """Participant permissions (S/MIME signed XML) -> permissions.xml.p7s.

        GET /v1/dds-im/permissions/<homeId>?nonce=<value>. Both parts recovered
        from libLCS + the app (2026-09-16):
          * param name "nonce" -- Lennox::use_nonce() does
            uri.addQueryParameter("nonce", props["NONCE"]).
          * value -- com.krasamo.lx_ic3_mobile.b0.o() generates it as the LAST
            hyphen segment of a random UUID (12 hex chars), cached in SharedPrefs.
            It is a plain client-generated token, so we mint our own.
        """
        if not nonce:
            nonce = gen_nonce()
        # home_id is NOT in the URL (it's carried inside the signed doc); the path
        # segment is the role. LIVE-CAPTURED from the app.
        path = PERMISSIONS_PATH
        url = f"{PLANT_ID_URL}{path}?nonce={nonce}"
        r = await self._session.get(url, headers=self._auth_headers(), ssl=self._ssl)
        data = await r.read()
        print(f"  permissions {path}?nonce={nonce} -> HTTP {r.status}, {len(data)} B, "
              f"content-type={r.headers.get('Content-Type')!r}")
        if r.status == 200 and data:
            (BUNDLE_DIR / "permissions.xml.p7s").write_bytes(data)
            print("    saved permissions.xml.p7s")
        else:
            print(f"    [{r.status}] body={data[:400]!r}")
            print(f"    resp headers: { {k: v for k, v in r.headers.items() if k.lower() in ('www-authenticate','x-amzn-errortype','x-amzn-remapped-authorization','www-authenticate','date')} }")

    async def run(self) -> None:
        BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
        home_id = self._c.systems[0].home_id if self._c.systems else os.environ.get("LENNOX_HOME_ID", "")
        # persist the derived homeId (DDS partition) so the add-on can auto-wire it
        if home_id:
            (BUNDLE_DIR / "home_id.txt").write_text(str(home_id))
        nonce = os.environ.get("LENNOX_DDS_NONCE") or gen_nonce()
        # ONE nonce threads through newcert + permissions (identity<->nonce bind).
        print(f"[*] session nonce = {nonce}")
        print("[1] relay.json"); await self.fetch_relay_json()
        print("[2] static CA + governance docs"); await self.fetch_static_docs()
        print("[3] participant identity (newcert)")
        await self.fetch_participant_identity(nonce, os.environ.get("LENNOX_DEVICE_ID"))
        print(f"[4] participant permissions (homeId={home_id})")
        await self.fetch_participant_permissions(home_id, nonce)
        print(f"\nBundle written to {BUNDLE_DIR}")
        print("OpenDDS property wiring (Phase 4):")
        print("  dds.sec.auth.identity_ca          = file:identity_ca.pem")
        print("  dds.sec.auth.identity_certificate = file:identity.pem")
        print("  dds.sec.auth.private_key          = file:identity.key")
        print("  dds.sec.access.permissions_ca     = file:permissions_ca.pem")
        print("  dds.sec.access.governance         = file:governance.xml.p7s")
        print("  dds.sec.access.permissions        = file:permissions.xml.p7s")


async def _main(args) -> int:
    creds = _load_creds(args.creds)
    async with M30Prod4Client(creds["email"], creds["password"], creds["app_id"]) as client:
        # authenticate -> login -> registerLCCOwner (from m30_prod4.py)
        await client.authenticate_and_login()
        await client.register_plant()
        if not client._plant_token:
            raise SystemExit("no plant token; registerLCCOwner failed")
        print(f"plant token acquired; user.id={client._user_id}")
        await SecurityDocFetcher(client).run()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--creds", help="JSON file with email/password/app_id")
    raise SystemExit(asyncio.run(_main(p.parse_args())))
