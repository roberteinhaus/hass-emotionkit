"""Config flow for the EmotionKit integration.

Registration works with a claim code: the integration announces a random
code to the EmotionKit server, shows it to the user and waits (as a progress
step, without blocking the request) until the user has entered it in the
web UI. The server then hands out the MQTT credentials.

Claim codes, device tokens and credentials are never logged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    ApiError,
    ClaimOutcome,
    ClaimStatus,
    async_announce,
    async_wait_for_claim,
    generate_claim_code,
)
from .broker import InvalidBrokerURL, parse_broker_url
from .const import (
    CLAIM_FLOW_TIMEOUT,
    CONF_ALL_PLAYERS,
    CONF_CONTROL_PLANE_URL,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_IDLE_TIMEOUT,
    CONF_MQTT_BROKER,
    CONF_MQTT_BROKER_MANUAL,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_USERNAME,
    CONF_REMOTE_CONFIG,
    DEFAULT_CONTROL_PLANE_URL,
    DEFAULT_DEVICE_NAME,
    DEFAULT_IDLE_TIMEOUT,
    DOMAIN,
    MAX_DEVICE_NAME_LENGTH,
    MAX_IDLE_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_DEVICE_NAME, default=DEFAULT_DEVICE_NAME): str}
)

ERROR_CLAIM_TIMEOUT = "claim_timeout"
ERROR_CLAIM_FAILED = "claim_failed"
ERROR_UNKNOWN = "unknown"


class EmotionKitOptionsFlow(OptionsFlowWithReload):
    """Options of an EmotionKit device. The entry is reloaded on change."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        options = self.config_entry.options
        if user_input is not None:
            # Keep what is not in the form (e.g. a name set in the web UI).
            return self.async_create_entry(data={**options, **user_input})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_IDLE_TIMEOUT,
                        default=options.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT),
                    ): vol.All(
                        vol.Coerce(int), vol.Range(min=0, max=MAX_IDLE_TIMEOUT)
                    ),
                    vol.Required(
                        CONF_ALL_PLAYERS,
                        default=bool(options.get(CONF_ALL_PLAYERS, False)),
                    ): bool,
                }
            ),
        )


class EmotionKitConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EmotionKit."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EmotionKitOptionsFlow:
        """Return the options flow."""
        return EmotionKitOptionsFlow()

    def __init__(self) -> None:
        """Initialize."""
        self._control_plane_url: str = DEFAULT_CONTROL_PLANE_URL
        self._device_name: str = DEFAULT_DEVICE_NAME
        self._claim_code: str = ""
        self._device_token: str = ""
        self._claim_task: asyncio.Task[ClaimOutcome] | None = None
        self._claim_deadline: float = 0.0
        self._claim_error: str = ERROR_CLAIM_FAILED
        self._credentials: dict[str, str] | None = None

    # --- claim machinery ---------------------------------------------------

    async def _async_announce(self) -> str | None:
        """Announce a fresh claim code. Returns an error key, or None."""
        code = generate_claim_code()
        try:
            token = await async_announce(
                async_get_clientsession(self.hass), self._control_plane_url, code
            )
        except ApiError as err:
            _LOGGER.debug("Announcing a claim code failed: %s", type(err).__name__)
            return err.key
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error while announcing a claim code")
            return ERROR_UNKNOWN
        self._claim_code = code
        self._device_token = token
        return None

    async def _async_begin_claim(self) -> str | None:
        """Start a new registration attempt. Returns an error key, or None."""
        self._async_cancel_claim_task()
        error = await self._async_announce()
        if error is None:
            self._claim_deadline = time.monotonic() + CLAIM_FLOW_TIMEOUT
        return error

    @callback
    def _async_cancel_claim_task(self) -> None:
        if self._claim_task is not None and not self._claim_task.done():
            self._claim_task.cancel()
        self._claim_task = None

    @callback
    def _async_show_claim_progress(self) -> ConfigFlowResult:
        """Show the claim code while a background task waits for the claim."""
        if self._claim_task is None:
            self._claim_task = self.hass.async_create_task(
                async_wait_for_claim(
                    async_get_clientsession(self.hass),
                    self._control_plane_url,
                    self._claim_code,
                    self._device_token,
                    deadline=self._claim_deadline,
                ),
                "emotionkit wait for claim",
            )
        return self.async_show_progress(
            step_id="claim",
            progress_action="wait_for_claim",
            description_placeholders={
                "claim_code": self._claim_code,
                "url": self._control_plane_url,
            },
            progress_task=self._claim_task,
        )

    async def async_step_claim(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for the user to enter the claim code in the web UI."""
        if self._claim_task is None or not self._claim_task.done():
            return self._async_show_claim_progress()

        task, self._claim_task = self._claim_task, None
        try:
            outcome = task.result()
        except asyncio.CancelledError:
            outcome = ClaimOutcome(ClaimStatus.TIMEOUT)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error while waiting for the claim")
            self._claim_error = ERROR_UNKNOWN
            return self.async_show_progress_done(next_step_id="claim_failed")

        if outcome.status is ClaimStatus.CLAIMED and outcome.credentials:
            self._credentials = outcome.credentials
            return self.async_show_progress_done(next_step_id="finish")

        if (
            outcome.status is ClaimStatus.EXPIRED
            and time.monotonic() < self._claim_deadline
        ):
            # The server no longer knows the code. Showing it any longer
            # would be pointless: announce a fresh one and keep waiting.
            error = await self._async_announce()
            if error is None:
                return self._async_show_claim_progress()
            self._claim_error = error
        elif outcome.status is ClaimStatus.INVALID:
            self._claim_error = ERROR_CLAIM_FAILED
        else:
            self._claim_error = ERROR_CLAIM_TIMEOUT
        return self.async_show_progress_done(next_step_id="claim_failed")

    async def async_step_claim_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain why the registration did not complete; offer a retry."""
        error = self._claim_error
        if user_input is not None:
            error = await self._async_begin_claim()
            if error is None:
                return await self.async_step_claim()
        return self.async_show_form(
            step_id="claim_failed",
            data_schema=vol.Schema({}),
            errors={"base": error or ERROR_CLAIM_FAILED},
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the credentials handed out by the server."""
        credentials = self._credentials
        if credentials is None:
            return self.async_abort(reason=ERROR_CLAIM_FAILED)

        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            updates: dict[str, Any] = {
                CONF_DEVICE_ID: credentials["device_id"],
                CONF_MQTT_USERNAME: credentials["mqtt_username"],
                CONF_MQTT_PASSWORD: credentials["mqtt_password"],
                # A new device on the server: forget what the old one said.
                CONF_REMOTE_CONFIG: {},
            }
            if credentials["mqtt_broker"]:
                updates[CONF_MQTT_BROKER] = credentials["mqtt_broker"]
                updates[CONF_MQTT_BROKER_MANUAL] = False
            # The unique id of the entry is kept on purpose: the Home
            # Assistant device, its entities and the automations using them
            # stay the same although the server-side device is a new one.
            return self.async_update_reload_and_abort(entry, data_updates=updates)

        await self.async_set_unique_id(credentials["device_id"])
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self._device_name,
            data={
                CONF_CONTROL_PLANE_URL: self._control_plane_url,
                CONF_DEVICE_NAME: self._device_name,
                CONF_DEVICE_ID: credentials["device_id"],
                CONF_MQTT_USERNAME: credentials["mqtt_username"],
                CONF_MQTT_PASSWORD: credentials["mqtt_password"],
                CONF_MQTT_BROKER: credentials["mqtt_broker"],
                # An empty mapping (as opposed to no key at all) marks an
                # entry whose device was claimed with this version: the name
                # chosen in the web UI is taken over on the first connect.
                CONF_REMOTE_CONFIG: {},
            },
        )

    @callback
    def async_remove(self) -> None:
        """Stop polling when the flow is closed."""
        self._async_cancel_claim_task()

    # --- entry points ------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a device name, then show the claim code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_DEVICE_NAME, "")).strip()
            self._device_name = name[:MAX_DEVICE_NAME_LENGTH] or DEFAULT_DEVICE_NAME
            error = await self._async_begin_claim()
            if error is None:
                return await self.async_step_claim()
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, {CONF_DEVICE_NAME: self._device_name}
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The broker rejected the credentials: the device has to be claimed again."""
        self._control_plane_url = entry_data.get(
            CONF_CONTROL_PLANE_URL, DEFAULT_CONTROL_PLANE_URL
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain the situation, then run the claim again."""
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_begin_claim()
            if error is None:
                return await self.async_step_claim()
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=vol.Schema({}), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the MQTT broker URL.

        Leaving the field empty returns to the address the EmotionKit server
        announces.
        """
        entry = self._get_reconfigure_entry()
        current: str = entry.data.get(CONF_MQTT_BROKER) or ""
        errors: dict[str, str] = {}

        if user_input is not None:
            current = str(user_input.get(CONF_MQTT_BROKER) or "").strip()
            if not current:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_MQTT_BROKER: "", CONF_MQTT_BROKER_MANUAL: False},
                )
            try:
                parse_broker_url(current)
            except InvalidBrokerURL as err:
                _LOGGER.debug("Rejected broker URL: %s", err)
                errors["base"] = "invalid_broker"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_MQTT_BROKER: current,
                        CONF_MQTT_BROKER_MANUAL: True,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Optional(CONF_MQTT_BROKER): str}),
                {CONF_MQTT_BROKER: current},
            ),
            errors=errors,
        )
