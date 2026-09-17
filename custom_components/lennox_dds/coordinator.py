"""WebSocket coordinator: maintains a persistent connection to the DDS sidecar
bridge and exposes the latest zoneStatus per (sysID, zoneId).

The bridge (add-on) streams one JSON object per zoneStatus sample. This is
local_push: we hold the connection open and update entities as samples arrive.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import websockets
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


def zone_key(sample: dict) -> str:
    return f"{sample.get('sysID')}:{sample.get('zoneId')}"


class M30BridgeCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Holds {zone_key: latest zoneStatus dict} and pushes updates from the WS."""

    def __init__(self, hass: HomeAssistant, ws_url: str) -> None:
        super().__init__(hass, _LOGGER, name="lennox_dds")
        self._ws_url = ws_url
        self._task: asyncio.Task | None = None
        self._ws = None  # live connection, for sending control commands
        self.data = {}

    async def async_send_command(self, command: dict) -> None:
        """Send a control command frame to the bridge (setpoint/mode/fan).

        The bridge translates it to a DDS scheduleUpdate write. Raises
        HomeAssistantError if the bridge isn't currently connected."""
        ws = self._ws
        if ws is None:
            raise HomeAssistantError("lennox_dds: bridge not connected; cannot send command")
        payload = {"type": "command", **command}
        _LOGGER.debug("lennox_dds: sending command %s", payload)
        await ws.send(json.dumps(payload))

    async def async_start(self) -> None:
        if self._task is None:
            self._task = self.hass.async_create_background_task(
                self._run(), name="lennox_dds_ws"
            )

    async def async_stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        """Connect + stream forever, reconnecting with backoff."""
        backoff = 1
        while True:
            try:
                async with websockets.connect(self._ws_url, open_timeout=10) as ws:
                    _LOGGER.info("lennox_dds: connected to bridge %s", self._ws_url)
                    backoff = 1
                    self._ws = ws  # enable outbound control commands
                    async for raw in ws:
                        try:
                            sample = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        data = dict(self.data or {})
                        data[zone_key(sample)] = sample
                        self.async_set_updated_data(data)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("lennox_dds: bridge connection lost (%s); retrying in %ss",
                                err, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                self._ws = None
