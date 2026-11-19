"""EmotionKit integration — CS2 game events for Home Assistant automations.

The integration connects to the EmotionKit MQTT broker with the credentials
obtained through the claim-code flow and exposes what happens in a match as
device triggers and entities.

Where things live:
  coordinator.py   the runtime of a config entry (connection, timers, output)
  connection.py    reconnect / back-off loop (plain asyncio)
  engine.py        message processing; uses protocol.py, state.py,
                   triggers.py and clock.py
  broker.py        broker URL parsing
  api.py           claim-code HTTP calls
The modules of the last five lines do not import Home Assistant and are
covered by the unit tests in tests/.
"""

from __future__ import annotations

import logging

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiError, async_fetch_broker_url
from .broker import BrokerAddress, InvalidBrokerURL, derive_broker_url, parse_broker_url
from .const import (
    CONF_CONTROL_PLANE_URL,
    CONF_DEVICE_ID,
    CONF_MQTT_BROKER,
    CONF_MQTT_BROKER_MANUAL,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_USERNAME,
    DEFAULT_CONTROL_PLANE_URL,
    DOMAIN,
)
from .coordinator import EmotionKitConfigEntry, EmotionKitRuntime, device_name_of

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]


async def _async_server_broker_url(hass: HomeAssistant, control_plane_url: str) -> str:
    """Ask the server for the broker URL (installations without a stored one)."""
    try:
        url = await async_fetch_broker_url(
            async_get_clientsession(hass), control_plane_url
        )
    except ApiError as err:
        raise ConfigEntryNotReady(
            "Could not ask the EmotionKit server for the MQTT broker address"
        ) from err
    # A server that announces no broker runs it next to itself.
    return url or derive_broker_url(control_plane_url)


async def _async_broker_address(
    hass: HomeAssistant, entry: EmotionKitConfigEntry
) -> BrokerAddress:
    """Work out where to connect to."""
    control_plane_url: str = entry.data.get(
        CONF_CONTROL_PLANE_URL, DEFAULT_CONTROL_PLANE_URL
    )
    stored: str = entry.data.get(CONF_MQTT_BROKER) or ""

    if stored:
        try:
            return parse_broker_url(stored)
        except InvalidBrokerURL as err:
            if entry.data.get(CONF_MQTT_BROKER_MANUAL):
                raise ConfigEntryError(
                    f"Invalid MQTT broker URL: {err}. "
                    "Use 'Reconfigure' to correct it."
                ) from err
            _LOGGER.warning(
                "The stored MQTT broker URL is not usable (%s); "
                "asking the EmotionKit server for the current one",
                err,
            )

    try:
        return parse_broker_url(await _async_server_broker_url(hass, control_plane_url))
    except InvalidBrokerURL as err:
        raise ConfigEntryError(
            f"The EmotionKit server announced an unusable MQTT broker URL: {err}. "
            "Use 'Reconfigure' to set one."
        ) from err


async def async_setup_entry(hass: HomeAssistant, entry: EmotionKitConfigEntry) -> bool:
    """Set up EmotionKit from a config entry."""
    if not entry.data.get(CONF_MQTT_USERNAME) or not entry.data.get(CONF_MQTT_PASSWORD):
        raise ConfigEntryAuthFailed("The MQTT credentials are missing")

    address = await _async_broker_address(hass, entry)

    # The identifier is the unique id of the config entry: it stays the same
    # when the device is claimed again, so automations keep their device.
    unique_id = entry.unique_id or entry.data.get(CONF_DEVICE_ID) or entry.entry_id
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, unique_id)},
        name=device_name_of(entry),
        manufacturer="EmotionKit",
        model="CS2 Game Events",
    )

    runtime = EmotionKitRuntime(
        hass, entry, address=address, device_id=device.id, unique_id=unique_id
    )
    entry.runtime_data = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _async_stop(_event: Event) -> None:
        # Announce "offline" before Home Assistant goes down.
        await runtime.async_stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )

    runtime.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EmotionKitConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok
