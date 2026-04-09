"""Config flow for EmotionKit integration."""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import (
    CLAIM_CODE_ALPHABET,
    CLAIM_CODE_LENGTH,
    DEFAULT_CONTROL_PLANE_URL,
    DEFAULT_DEVICE_NAME,
    DEFAULT_IDLE_TIMEOUT,
    DEVICE_KIND,
    DOMAIN,
    POLL_INTERVAL,
    POLL_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required("device_name", default=DEFAULT_DEVICE_NAME): str,
    }
)


def _generate_claim_code() -> str:
    """Generate a claim code in XXXX-XXXX format."""
    chars = [secrets.choice(CLAIM_CODE_ALPHABET) for _ in range(CLAIM_CODE_LENGTH)]
    return "".join(chars[:4]) + "-" + "".join(chars[4:])


class EmotionKitOptionsFlow(OptionsFlow):
    """Handle options flow for EmotionKit."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "idle_timeout",
                        default=self.config_entry.options.get(
                            "idle_timeout", DEFAULT_IDLE_TIMEOUT
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0))
                }
            ),
        )


class EmotionKitConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EmotionKit."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EmotionKitOptionsFlow(config_entry)

    def __init__(self) -> None:
        """Initialize."""
        self._control_plane_url: str = DEFAULT_CONTROL_PLANE_URL
        self._device_name: str = DEFAULT_DEVICE_NAME
        self._claim_code: str = ""
        self._device_token: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step where the user enters a device name."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)

        self._device_name = user_input["device_name"]
        self._claim_code = _generate_claim_code()

        # Announce the device to get a device token.
        errors: dict[str, str] = {}
        try:
            self._device_token = await self._announce()
        except aiohttp.ClientError:
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
            )

        return await self.async_step_claim()

    async def async_step_claim(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Poll for claim completion. Shows the claim code to the user."""
        if user_input is not None:
            # User clicked submit — poll for credentials.
            try:
                credentials = await self._poll_for_credentials()
            except TimeoutError:
                return self.async_show_form(
                    step_id="claim",
                    description_placeholders={"claim_code": self._claim_code},
                    errors={"base": "claim_timeout"},
                )

            if credentials is None:
                return self.async_show_form(
                    step_id="claim",
                    description_placeholders={"claim_code": self._claim_code},
                    errors={"base": "claim_failed"},
                )

            await self.async_set_unique_id(credentials["device_id"])
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=self._device_name,
                data={
                    "control_plane_url": self._control_plane_url,
                    "device_name": self._device_name,
                    "device_id": credentials["device_id"],
                    "mqtt_username": credentials["mqtt_username"],
                    "mqtt_password": credentials["mqtt_password"],
                    "mqtt_broker": credentials.get("mqtt_broker", ""),
                },
            )

        # First visit — show the claim code and ask the user to confirm.
        return self.async_show_form(
            step_id="claim",
            data_schema=vol.Schema({}),
            description_placeholders={"claim_code": self._claim_code},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow reconfiguring the MQTT broker URL."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        current_broker = entry.data.get("mqtt_broker", "") if entry else ""

        if user_input is not None:
            new_broker = user_input.get("mqtt_broker", "").strip()
            if not new_broker:
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=vol.Schema(
                        {vol.Required("mqtt_broker", default=current_broker): str}
                    ),
                    errors={"base": "invalid_broker"},
                )
            return self.async_update_reload_and_abort(
                entry,
                data={**entry.data, "mqtt_broker": new_broker},
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required("mqtt_broker", default=current_broker): str}
            ),
        )

    # --- HTTP helpers ---

    async def _announce(self) -> str:
        """Call POST /api/v1/devices/announce and return the device token."""
        url = f"{self._control_plane_url}/api/v1/devices/announce"
        payload = {"code": self._claim_code, "kind": DEVICE_KIND}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data["device_token"]

    async def _poll_for_credentials(self) -> dict[str, str] | None:
        """Poll GET /api/v1/devices/pending/{code} until claimed or timeout."""
        url = f"{self._control_plane_url}/api/v1/devices/pending/{self._claim_code}"
        headers = {"X-Device-Token": self._device_token}
        deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT

        async with aiohttp.ClientSession() as session:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    async with session.get(
                        url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status == 200:
                            return await resp.json()
                        if resp.status == 404:
                            # Code expired — cannot recover in this flow.
                            return None
                        # 202 or other — keep waiting.
                except aiohttp.ClientError:
                    _LOGGER.debug("Poll request failed, retrying")

                await asyncio.sleep(POLL_INTERVAL)

        raise TimeoutError("Claim code was not entered within the timeout period")
