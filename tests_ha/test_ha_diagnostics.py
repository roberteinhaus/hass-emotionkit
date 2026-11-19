"""Diagnostics in a real Home Assistant: complete, serialisable, redacted."""

from __future__ import annotations

import json

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.emotionkit.diagnostics import async_get_config_entry_diagnostics

from ha_support import (
    CONFIG_TOPIC,
    EVENT_TOPIC,
    LEGACY_DEVICE_ID,
    LEGACY_PASSWORD,
    LEGACY_USERNAME,
    MATE,
    OWNER,
    OWNER_CONFIG,
    FakeBroker,
    envelope,
    event,
    legacy_entry,
)

SECRETS = (LEGACY_PASSWORD, LEGACY_USERNAME, LEGACY_DEVICE_ID, OWNER, MATE)


async def _setup_with_traffic(hass: HomeAssistant, broker: FakeBroker):
    entry = legacy_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = await broker.wait_ready()
    await client.deliver(CONFIG_TOPIC, OWNER_CONFIG, retain=True)
    await client.deliver(
        EVENT_TOPIC,
        envelope(event("player_kill", subject=OWNER, value=1, round=4), ct=2, t=1),
    )
    await hass.async_block_till_done()
    return entry


def _assert_redacted(data: dict) -> None:
    text = json.dumps(data)
    for secret in SECRETS:
        assert secret not in text

    entry_data = data["entry"]["data"]
    assert entry_data["mqtt_password"] == REDACTED
    assert entry_data["mqtt_username"] == REDACTED
    assert entry_data["device_id"] == REDACTED
    # What helps when looking for a problem stays readable.
    assert entry_data["mqtt_broker"] == "tcp://emotionkit.de:1883"
    assert data["entry"]["title"] == "Home Assistant"

    runtime = data["runtime"]
    assert runtime["broker"]["address"] == "tcp://emotionkit.de:1883"
    assert runtime["broker"]["tls"] is False
    assert runtime["connection"]["connected"] is True
    assert runtime["config"]["owner_known"] is True
    assert runtime["config"]["allowed_subjects_by_role"] == {"owner": 1, "user": 1}
    assert runtime["state"]["in_match"] is True
    assert runtime["state"]["map"] == "de_dust2"
    assert runtime["last_trigger"]["type"] == "player_kill"
    assert runtime["stats"]["triggers"] == 1


async def test_diagnostics_are_redacted(hass: HomeAssistant, broker: FakeBroker) -> None:
    """The diagnostics function returns no credentials and no SteamIDs."""
    entry = await _setup_with_traffic(hass, broker)
    _assert_redacted(await async_get_config_entry_diagnostics(hass, entry))


async def test_diagnostics_download(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    broker: FakeBroker,
) -> None:
    """Home Assistant finds the diagnostics platform and serves the download."""
    entry = await _setup_with_traffic(hass, broker)
    _assert_redacted(await get_diagnostics_for_config_entry(hass, hass_client, entry))


async def test_diagnostics_of_an_entry_that_is_not_loaded(hass: HomeAssistant) -> None:
    """Diagnostics can be asked for when the entry failed to set up."""
    entry = legacy_entry()
    entry.add_to_hass(hass)

    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["runtime"] is None
    assert data["entry"]["data"]["mqtt_password"] == REDACTED
    assert LEGACY_PASSWORD not in json.dumps(data)
