"""Phase 1: Lennox M30 prod4 (v4) online-status client.

Verified flow for the current Lennox cloud the M30 lives on:

    authenticate (device cert)      -> certificateToken
    login        (user creds)       -> userToken + system list (+ STALE myPresence)
    registerLCCOwner (plantdevices) -> plant token (+ globalConfig)
    GET plantdevices/systems/       -> status.alive  == the REAL online signal

The old lennoxs30api reads the message-bus presence (myPresence / ic3server), which
is permanently "offline" for migrated M30s. The truth is plantdevices status.alive.

Design: wrap lennoxs30api's s30api_async (repointed to prod4) for authenticate/login
— it builds the exact (non-JSON) login body and parses the response — and implement
only the prod4-new bits (registerLCCOwner + plantdevices) here. This folds cleanly
into the integration, which is already s30api_async-based.

Usage:  LENNOX_EMAIL=... LENNOX_PASSWORD=... python m30_prod4.py
   or:  python m30_prod4.py --creds creds.json   ({"email","password","app_id"})
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any

import lennoxs30api.s30api_async as s30mod
from lennoxs30api.s30api_async import s30api_async

GATEWAY = "https://gatewaymobile.prod4.myicomfort.com"
PLANT = "https://plantdevices.myicomfort.com"
USER_AGENT = "lx_ic3_mobile/4.70.0133 (Android)"

# prod4 host repoint (message endpoints kept for later phases; paths are the app's).
PROD4_URLS = {
    "url_authenticate": f"{GATEWAY}/v1/mobile/authenticate",
    "url_login": f"{GATEWAY}/v2/User/Login",
    "url_negotiate": "https://signalrapimobile.prod4.myicomfort.com/signalr/negotiate",
    "url_retrieve": "https://retrieveapimobile.prod4.myicomfort.com/v1/messages/Retrieve",
    "url_requestdata": "https://requestdataapimobile.prod4.myicomfort.com/v1/Messages/RequestData",
    "url_publish": "https://publishapimobile.prod4.myicomfort.com/v1/Messages/Publish",
    "url_logout": f"{GATEWAY}/v1/User/Logout",
}


@dataclass
class M30System:
    sys_id: str
    home_id: str
    home_name: str
    system_type: str
    plant_numeric_id: int | None = None
    online: bool | None = None           # plantdevices status.alive (the truth)
    raw_status: dict[str, Any] = field(default_factory=dict)


class M30Prod4Client:
    def __init__(self, email: str, password: str, app_id: str) -> None:
        self._email = email
        self._app_id = app_id
        self._api = s30api_async(username=email, password=password, app_id=app_id,
                                 ip_address=None, pii_message_logs=False,
                                 message_debug_logging=False, timeout=60)
        for attr, url in PROD4_URLS.items():
            setattr(self._api, attr, url)
        self._plant_token: str | None = None
        self._user_id: int | None = None
        self.systems: list[M30System] = []
        self.global_config: dict[str, Any] = {}

    async def __aenter__(self) -> "M30Prod4Client":
        self._api._create_session()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._api._close_session()

    async def authenticate_and_login(self) -> None:
        await self._api.authenticate()
        await self._api.login()
        self.systems = []
        for home in self._api._homeList:
            for s in home.systems if hasattr(home, "systems") else []:
                pass
        # s30api_async populates system_list; build our view from it.
        for s in self._api.system_list:
            home = s.home
            self.systems.append(M30System(
                sys_id=s.sysId, home_id=getattr(home, "id", ""),
                home_name=getattr(home, "name", "") or "", system_type=""))

    async def register_plant(self) -> None:
        """registerLCCOwner -> plant token for the plantdevices online query."""
        r = await self._api._session.post(
            f"{PLANT}/auth/registerLCCOwner",
            json={"id": self._email, "token": self._api.loginBearerToken},
            headers={"User-Agent": USER_AGENT}, ssl=self._api.ssl)
        if r.status != 200:
            raise RuntimeError(f"registerLCCOwner failed: {r.status} {(await r.text())[:200]!r}")
        j = json.loads(await r.text())
        self._plant_token = j["token"]
        self._user_id = j["user"]["id"]
        self.global_config = j.get("globalConfig", {})

    def _plant_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self._plant_token, "User": str(self._user_id),
                "User-Agent": USER_AGENT, "Content-Type": "application/json"}

    async def refresh_online_status(self) -> None:
        """GET plantdevices/systems/ -> online = status.alive per system."""
        r = await self._api._session.get(f"{PLANT}/systems/", headers=self._plant_headers(),
                                         ssl=self._api.ssl)
        if r.status != 200:
            raise RuntimeError(f"plantdevices/systems failed: {r.status}")
        arr = json.loads(await r.text())
        by_ext = {rec.get("extId"): rec for rec in arr}
        for sysrec in self.systems:
            rec = by_ext.get(sysrec.sys_id)
            if not rec:
                continue
            st = rec.get("status") or {}
            sysrec.plant_numeric_id = rec.get("id")
            sysrec.online = bool(st.get("alive"))
            sysrec.raw_status = {"alive": st.get("alive"), "active": st.get("active"),
                                 "isConnected": rec.get("isConnected"),
                                 "substatuses_alive": [x.get("alive") for x in st.get("substatuses", [])]}

    async def connect(self) -> list[M30System]:
        await self.authenticate_and_login()
        await self.register_plant()
        await self.refresh_online_status()
        return self.systems


def _load_creds(path: str | None) -> dict[str, str]:
    if os.environ.get("LENNOX_EMAIL") and os.environ.get("LENNOX_PASSWORD"):
        return {"email": os.environ["LENNOX_EMAIL"], "password": os.environ["LENNOX_PASSWORD"],
                "app_id": os.environ.get("LENNOX_APP_ID", "mapp079372367644467046827005")}
    if path:
        d = json.load(open(path))
        return {"email": d["email"], "password": d["password"],
                "app_id": d.get("app_id", "mapp079372367644467046827005")}
    raise SystemExit("provide LENNOX_EMAIL/LENNOX_PASSWORD env or --creds file")


async def _main(args) -> int:
    creds = _load_creds(args.creds)
    async with M30Prod4Client(creds["email"], creds["password"], creds["app_id"]) as c:
        systems = await c.connect()
        print(f"lccLivelinessTimeoutSeconds = {c.global_config.get('lccLivelinessTimeoutSeconds')}")
        for s in systems:
            print(f"  {s.home_name} sysId={s.sys_id}")
            print(f"    ONLINE={s.online}  plantId={s.plant_numeric_id}  status={s.raw_status}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--creds", help="JSON file with email/password/app_id")
    raise SystemExit(asyncio.run(_main(p.parse_args())))
