"""Config flow for Lennox iComfort (DDS).

Two ways in:
  * manual (async_step_user): the user types the DDS sidecar bridge WebSocket URL.
  * Supervisor add-on discovery (async_step_hassio): the companion add-on posts a
    discovery message with its host/port; HA offers a one-click "Configure" with
    no typing. Credentials/security live in the add-on.
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import CONF_WS_URL, DEFAULT_WS_URL, DOMAIN

TITLE = "Lennox iComfort (DDS)"


class M30ConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered_url: str | None = None

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_WS_URL])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=TITLE, data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_WS_URL, default=DEFAULT_WS_URL): str}),
            errors=errors,
        )

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """Handle discovery from the companion Supervisor add-on.

        The add-on is a trusted, single-purpose sidecar, so we set the entry up
        automatically (no confirm click needed)."""
        config = discovery_info.config or {}
        host, port = config.get("host"), config.get("port", 8099)
        if not host:
            return self.async_abort(reason="invalid_discovery_info")
        url = f"ws://{host}:{port}"
        await self.async_set_unique_id(url)
        self._abort_if_unique_id_configured(updates={CONF_WS_URL: url})
        return self.async_create_entry(title=TITLE, data={CONF_WS_URL: url})
