"""Config, options, re-auth and reconfigure flows in a real Home Assistant.

The control-plane API is mocked with Home Assistant's own aiohttp client
mock; the broker connection is the fake of conftest.py. Nothing here touches
the network.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import device_registry as dr, entity_registry as er

from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.emotionkit.const import DOMAIN

from ha_support import (
    LEGACY_DEVICE_ID,
    LEGACY_PASSWORD,
    LEGACY_USERNAME,
    FakeBroker,
    legacy_entry,
    wait_for,
)

BASE = "https://emotionkit.de"
ANNOUNCE_URL = f"{BASE}/api/v1/devices/announce"
INFO_URL = f"{BASE}/api/v1/info"
CODE = "ABCD-EFGH"
PENDING_URL = f"{BASE}/api/v1/devices/pending/{CODE}"
TOKEN = "device-token-1"

NEW_DEVICE_ID = "dcv1newdevice0002"
NEW_CREDENTIALS = {
    "device_id": NEW_DEVICE_ID,
    "mqtt_username": f"device-{NEW_DEVICE_ID}",
    "mqtt_password": "new-mqtt-password",
    "mqtt_broker": "tcp://emotionkit.de:1883",
}


@pytest.fixture(autouse=True)
def claim_code() -> Generator[str]:
    """Make the random claim code predictable."""
    with patch(
        "custom_components.emotionkit.config_flow.generate_claim_code",
        return_value=CODE,
    ):
        yield CODE


def _mock_claim(aioclient_mock: AiohttpClientMocker) -> None:
    """The server accepts the code and reports it as claimed at once."""
    aioclient_mock.post(ANNOUNCE_URL, json={"device_token": TOKEN})
    aioclient_mock.get(PENDING_URL, json=NEW_CREDENTIALS)


async def _finish_progress(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    """Follow a flow through its progress step to the next real result."""
    for _ in range(10):
        if result["type"] not in (
            FlowResultType.SHOW_PROGRESS,
            FlowResultType.SHOW_PROGRESS_DONE,
        ):
            return result
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    raise AssertionError(f"the flow is stuck in its progress step: {result}")


def _assert_shows_code(result: dict[str, Any]) -> None:
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "claim"
    assert result["progress_action"] == "wait_for_claim"
    assert result["description_placeholders"] == {"claim_code": CODE, "url": BASE}


# --- d. user flow -------------------------------------------------------------


async def test_user_flow_creates_and_sets_up_the_entry(
    hass: HomeAssistant,
    broker: FakeBroker,
    aioclient_mock: AiohttpClientMocker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Name -> claim code shown as progress -> entry created and connected."""
    _mock_claim(aioclient_mock)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result["errors"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_name": "Gaming PC"}
    )
    # The request returns at once with the code; waiting happens in a task.
    _assert_shows_code(result)

    result = await _finish_progress(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Gaming PC"
    assert result["data"] == {
        "control_plane_url": BASE,
        "device_name": "Gaming PC",
        "device_id": NEW_DEVICE_ID,
        "mqtt_username": NEW_CREDENTIALS["mqtt_username"],
        "mqtt_password": NEW_CREDENTIALS["mqtt_password"],
        "mqtt_broker": NEW_CREDENTIALS["mqtt_broker"],
        "remote_config": {},
    }
    entry = result["result"]
    assert entry.unique_id == NEW_DEVICE_ID
    assert (entry.version, entry.minor_version) == (1, 1)

    # What the server was asked.
    announce, poll = aioclient_mock.mock_calls[0], aioclient_mock.mock_calls[-1]
    assert (announce[0].upper(), str(announce[1])) == ("POST", ANNOUNCE_URL)
    assert announce[2] == {"code": CODE, "kind": "ha-integration"}
    assert (poll[0].upper(), str(poll[1])) == ("GET", PENDING_URL)
    assert poll[3] == {"X-Device-Token": TOKEN}

    # The new entry is set up and talks to the broker with its credentials.
    client = await broker.wait_ready()
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert client.options["username"] == NEW_CREDENTIALS["mqtt_username"]
    assert client.options["password"] == NEW_CREDENTIALS["mqtt_password"]
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert [device.identifiers for device in devices] == [{(DOMAIN, NEW_DEVICE_ID)}]
    assert devices[0].name == "Gaming PC"
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []


async def test_user_flow_reports_server_problems_and_recovers(
    hass: HomeAssistant,
    broker: FakeBroker,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Announce errors are named in the form; a retry then goes through."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    problems: list[tuple[dict[str, Any], str]] = [
        ({"exc": TimeoutError()}, "timeout_connect"),
        ({"exc": aiohttp.ClientConnectionError("no route")}, "cannot_connect"),
        ({"status": 503}, "server_busy"),
        ({"status": 500}, "cannot_connect"),
        ({"json": {"unexpected": True}}, "invalid_response"),
    ]
    for response, error in problems:
        aioclient_mock.clear_requests()
        aioclient_mock.post(ANNOUNCE_URL, **response)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_name": "Gaming PC"}
        )
        assert result["type"] is FlowResultType.FORM, error
        assert result["step_id"] == "user", error
        assert result["errors"] == {"base": error}

    aioclient_mock.clear_requests()
    _mock_claim(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_name": "Gaming PC"}
    )
    _assert_shows_code(result)
    result = await _finish_progress(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await broker.wait_ready()
    await hass.async_block_till_done()


async def test_user_flow_aborts_for_a_device_that_is_already_set_up(
    hass: HomeAssistant,
    broker: FakeBroker,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The same server-side device cannot be added twice."""
    existing = legacy_entry(unique_id=NEW_DEVICE_ID)
    existing.add_to_hass(hass)
    _mock_claim(aioclient_mock)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_name": "Gaming PC"}
    )
    result = await _finish_progress(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert broker.clients == []


# --- d. options flow ----------------------------------------------------------


async def test_options_flow_saves_and_reloads(
    hass: HomeAssistant, broker: FakeBroker
) -> None:
    """Options are stored and take effect through an automatic reload."""
    entry = legacy_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    first = await broker.wait_ready()
    await hass.async_block_till_done()
    old_runtime = entry.runtime_data
    assert old_runtime.idle_timeout == 30
    assert old_runtime.processor.all_players is False

    # Opening the options used to raise (assignment to config_entry).
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"idle_timeout": 99999, "react_to_all_players": False}
        )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"idle_timeout": 45, "react_to_all_players": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"idle_timeout": 45, "react_to_all_players": True}

    # The entry reloads by itself (it used to need a restart).
    second = await broker.wait_ready(sessions=2)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not old_runtime
    assert entry.runtime_data.idle_timeout == 45
    assert entry.runtime_data.processor.all_players is True
    assert first.closed
    assert second.statuses()[0]["config"]["idle_timeout_s"] == 45


async def test_options_flow_keeps_a_name_set_in_the_web_ui(
    hass: HomeAssistant, broker: FakeBroker
) -> None:
    """Options that are not in the form survive saving it."""
    entry = legacy_entry(options={"device_name": "Desk", "idle_timeout": 60})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await broker.wait_ready()
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"idle_timeout": 0, "react_to_all_players": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "device_name": "Desk",
        "idle_timeout": 0,
        "react_to_all_players": False,
    }
    second = await broker.wait_ready(sessions=2)
    await hass.async_block_till_done()
    assert second.statuses()[0]["name"] == "Desk"
    assert second.statuses()[0]["config"]["idle_timeout_s"] == 0


# --- d. re-authentication -----------------------------------------------------


async def test_refused_credentials_start_reauth_which_keeps_the_unique_id(
    hass: HomeAssistant,
    broker: FakeBroker,
    aioclient_mock: AiohttpClientMocker,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """A deleted device leads to a re-auth flow; completing it keeps everything."""
    # After a few failures the integration asks whether the broker moved.
    aioclient_mock.get(INFO_URL, json={})
    _mock_claim(aioclient_mock)
    # The device was deleted on the server: the broker refuses its CONNECT
    # (with the real aiomqtt error and paho reason code "Not authorized").
    broker.deleted_usernames.add(LEGACY_USERNAME)

    entry = legacy_entry()
    entry.add_to_hass(hass)
    # Setting up still succeeds: the connection is made in the background.
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    device_id = entry.runtime_data.device_id
    entities_before = {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    }
    assert len(entities_before) == 11

    await wait_for(lambda: hass.config_entries.flow.async_progress_by_handler(DOMAIN))
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    flow = flows[0]
    assert flow["context"]["source"] == SOURCE_REAUTH
    assert flow["context"]["entry_id"] == entry.entry_id
    assert flow["step_id"] == "reauth_confirm"
    assert not entry.runtime_data.connected
    assert broker.sessions == []

    # The user confirms and claims the device again on the website.
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    _assert_shows_code(result)
    result = await _finish_progress(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"

    # The entry reloads with the new credentials and connects.
    client = await broker.wait_ready()
    await hass.async_block_till_done()
    assert client.options["username"] == NEW_CREDENTIALS["mqtt_username"]
    assert client.options["password"] == NEW_CREDENTIALS["mqtt_password"]
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.connected
    assert entry.data["mqtt_username"] == NEW_CREDENTIALS["mqtt_username"]
    assert entry.data["mqtt_password"] == NEW_CREDENTIALS["mqtt_password"]
    assert entry.data["device_id"] == NEW_DEVICE_ID
    assert entry.data["mqtt_password"] != LEGACY_PASSWORD

    # Same entry identity, same device, same entities: automations survive.
    assert entry.unique_id == LEGACY_DEVICE_ID
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert [device.id for device in devices] == [device_id]
    assert devices[0].identifiers == {(DOMAIN, LEGACY_DEVICE_ID)}
    entities_after = {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    }
    assert entities_after == entities_before
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []


async def test_manually_started_reauth_flow(
    hass: HomeAssistant,
    broker: FakeBroker,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The re-auth flow also works when Home Assistant starts it."""
    _mock_claim(aioclient_mock)
    entry = legacy_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await broker.wait_ready()
    await hass.async_block_till_done()

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    _assert_shows_code(result)
    result = await _finish_progress(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"

    client = await broker.wait_ready(sessions=2)
    await hass.async_block_till_done()
    assert client.options["username"] == NEW_CREDENTIALS["mqtt_username"]
    assert entry.unique_id == LEGACY_DEVICE_ID
    assert entry.state is ConfigEntryState.LOADED


# --- reconfigure --------------------------------------------------------------


async def test_reconfigure_broker_address(
    hass: HomeAssistant,
    broker: FakeBroker,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The broker URL can be changed; nonsense is refused; empty means automatic."""
    entry = legacy_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await broker.wait_ready()
    await hass.async_block_till_done()

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    for nonsense in ("http://example.org", "example.org:1883", "mqtt+ssl://example.org"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"mqtt_broker": nonsense}
        )
        assert result["type"] is FlowResultType.FORM, nonsense
        assert result["errors"] == {"base": "invalid_broker"}, nonsense
    assert entry.data["mqtt_broker"] == "tcp://emotionkit.de:1883"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"mqtt_broker": "wss://example.org/mqtt"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["mqtt_broker"] == "wss://example.org/mqtt"
    assert entry.data["mqtt_broker_manual"] is True

    second = await broker.wait_ready(sessions=2)
    await hass.async_block_till_done()
    assert (second.hostname, second.port) == ("example.org", 443)
    assert second.options["transport"] == "websockets"
    assert second.options["websocket_path"] == "/mqtt"
    # The credentials are untouched: no re-claim for a new address.
    assert second.options["username"] == LEGACY_USERNAME
    assert entry.state is ConfigEntryState.LOADED

    # An empty field hands the choice back to the server.
    aioclient_mock.get(INFO_URL, json={"mqtt_broker_url": "tcp://announced.example:1883"})
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["mqtt_broker"] == ""
    assert entry.data["mqtt_broker_manual"] is False

    third = await broker.wait_ready(sessions=3)
    await hass.async_block_till_done()
    assert (third.hostname, third.port) == ("announced.example", 1883)
    assert "transport" not in third.options
