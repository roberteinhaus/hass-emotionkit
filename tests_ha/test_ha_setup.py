"""Loading, setting up, unloading and reloading in a real Home Assistant.

Every test starts from a config entry exactly as the integration at git HEAD
(manifest version 1.1.0) stored it, so each of them is also an upgrade test.
"""

from __future__ import annotations

import json
from pathlib import Path
import ssl

import aiomqtt

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from homeassistant.loader import async_get_integration

from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.emotionkit.const import ALL_EVENT_TYPES, DOMAIN

from ha_support import (
    CONFIG_TOPIC,
    ENTITY_KEYS,
    EVENT_TOPIC,
    LEGACY_DEVICE_ID,
    LEGACY_IDENTIFIERS,
    LEGACY_PASSWORD,
    LEGACY_USERNAME,
    MATE,
    OWNER,
    OWNER_CONFIG,
    STATUS_TOPIC,
    FakeBroker,
    envelope,
    event,
    legacy_entry,
    menu_envelope,
    wait_for,
)

MANIFEST = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "emotionkit"
        / "manifest.json"
    ).read_text(encoding="utf-8")
)

EXPECTED_UNIQUE_IDS = {
    f"{LEGACY_DEVICE_ID}_{key}" for keys in ENTITY_KEYS.values() for key in keys
}


async def _setup(hass: HomeAssistant, broker: FakeBroker, **entry_args):
    """Set up a legacy entry and wait for its broker session."""
    entry = legacy_entry(**entry_args)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = await broker.wait_ready()
    await hass.async_block_till_done()
    return entry, client


def _entity_id(entity_registry: er.EntityRegistry, platform: str, key: str) -> str:
    entity_id = entity_registry.async_get_entity_id(
        platform, DOMAIN, f"{LEGACY_DEVICE_ID}_{key}"
    )
    assert entity_id is not None, f"{platform} entity '{key}' is not registered"
    return entity_id


def _state(hass: HomeAssistant, entity_registry: er.EntityRegistry, platform: str, key: str) -> str:
    state = hass.states.get(_entity_id(entity_registry, platform, key))
    assert state is not None, f"{platform} entity '{key}' has no state"
    return state.state


# --- a. loading -------------------------------------------------------------


async def test_manifest_is_accepted(hass: HomeAssistant) -> None:
    """Home Assistant's loader finds the integration and accepts its manifest."""
    integration = await async_get_integration(hass, DOMAIN)

    assert integration.domain == DOMAIN
    assert integration.name == "EmotionKit"
    assert not integration.is_built_in
    assert integration.config_flow is True
    # A custom integration without a valid version is refused by the loader.
    assert integration.version is not None
    assert str(integration.version) == MANIFEST["version"]
    assert integration.requirements == MANIFEST["requirements"]
    assert integration.documentation == MANIFEST["documentation"]

    expected = [
        "binary_sensor",
        "config_flow",
        "device_trigger",
        "diagnostics",
        "event",
        "sensor",
    ]
    assert sorted(integration.platforms_exists(expected)) == expected


async def test_translations_are_loaded(hass: HomeAssistant) -> None:
    """Home Assistant's translation loader finds every string the code uses."""

    async def load(language: str, category: str) -> dict[str, str]:
        return await async_get_translations(hass, language, category, [DOMAIN])

    prefix = f"component.{DOMAIN}"

    config = await load("en", "config")
    for key in (
        "step.user.title",
        "step.user.data.device_name",
        "step.claim.title",
        "step.claim_failed.title",
        "step.reauth_confirm.title",
        "step.reconfigure.data.mqtt_broker",
        "progress.wait_for_claim",
        "error.cannot_connect",
        "error.timeout_connect",
        "error.server_busy",
        "error.invalid_response",
        "error.claim_timeout",
        "error.claim_failed",
        "error.invalid_broker",
        "error.unknown",
        "abort.already_configured",
        "abort.claim_failed",
        "abort.reauth_successful",
        "abort.reconfigure_successful",
    ):
        assert f"{prefix}.config.{key}" in config, key
    assert "{claim_code}" in config[f"{prefix}.config.progress.wait_for_claim"]

    options = await load("en", "options")
    for key in ("idle_timeout", "react_to_all_players"):
        assert f"{prefix}.options.step.init.data.{key}" in options, key

    for language, bomb_exploded in (("en", "Bomb exploded"), ("de", "Bombe explodiert")):
        triggers = await load(language, "device_automation")
        for trigger_type in ALL_EVENT_TYPES:
            key = f"{prefix}.device_automation.trigger_type.{trigger_type}"
            assert key in triggers, f"{language}: {trigger_type}"
        assert (
            triggers[f"{prefix}.device_automation.trigger_type.bomb_exploded"]
            == bomb_exploded
        )

    entity = await load("en", "entity")
    for platform, keys in ENTITY_KEYS.items():
        for key in keys:
            assert f"{prefix}.entity.{platform}.{key}.name" in entity, key
    for phase in ("freezetime", "live", "over"):
        assert f"{prefix}.entity.sensor.round_phase.state.{phase}" in entity
    for trigger_type in ALL_EVENT_TYPES:
        assert (
            f"{prefix}.entity.event.game_event.state_attributes.event_type.state.{trigger_type}"
            in entity
        ), trigger_type


async def test_setup_connects_and_registers_device_and_entities(
    hass: HomeAssistant,
    broker: FakeBroker,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The entry loads; device, entities and the broker session are there."""
    entry, client = await _setup(hass, broker)
    assert entry.state is ConfigEntryState.LOADED

    # The broker session, with the stored credentials and address.
    assert (client.hostname, client.port) == ("emotionkit.de", 1883)
    assert client.options["username"] == LEGACY_USERNAME
    assert client.options["password"] == LEGACY_PASSWORD
    assert "tls_context" not in client.options
    assert "tls_params" not in client.options
    assert "transport" not in client.options
    will = client.options["will"]
    assert isinstance(will, aiomqtt.Will)
    assert (will.topic, will.qos, will.retain) == (STATUS_TOPIC, 1, True)
    assert json.loads(will.payload)["online"] is False

    first = client.published[0]
    assert (first["topic"], first["qos"], first["retain"]) == (STATUS_TOPIC, 1, True)
    status = json.loads(first["payload"])
    assert status["online"] is True
    assert status["kind"] == "ha-integration"
    assert status["name"] == "Home Assistant"
    assert status["config"]["idle_timeout_s"] == 30
    assert client.subscriptions == [EVENT_TOPIC, CONFIG_TOPIC]

    # One device, identified as before.
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert len(devices) == 1
    device = devices[0]
    assert device.identifiers == LEGACY_IDENTIFIERS
    assert device.name == "Home Assistant"
    assert device.manufacturer == "EmotionKit"
    assert device.model == "CS2 Game Events"
    assert entry.runtime_data.device_id == device.id

    # All platforms were forwarded: every expected entity is registered.
    entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert {item.unique_id for item in entries} == EXPECTED_UNIQUE_IDS
    by_domain: dict[str, set[str]] = {}
    for item in entries:
        by_domain.setdefault(item.domain, set()).add(item.translation_key)
        assert item.device_id == device.id
        assert item.platform == DOMAIN
        assert item.has_entity_name is True
    assert by_domain == ENTITY_KEYS

    # Connected and not in a match: binary sensors off, sensors unknown.
    for key in ENTITY_KEYS["binary_sensor"]:
        assert _state(hass, entity_registry, "binary_sensor", key) == "off", key
    for key in ENTITY_KEYS["sensor"]:
        assert _state(hass, entity_registry, "sensor", key) == "unknown", key
    assert _state(hass, entity_registry, "event", "game_event") == "unknown"

    # Names come from the translations.
    bomb = hass.states.get(_entity_id(entity_registry, "binary_sensor", "bomb_planted"))
    assert bomb.name == "Home Assistant Bomb planted"
    phase = hass.states.get(_entity_id(entity_registry, "sensor", "round_phase"))
    assert phase.name == "Home Assistant Round phase"
    assert phase.attributes["device_class"] == "enum"
    assert phase.attributes["options"] == ["freezetime", "live", "over"]
    game_event = hass.states.get(_entity_id(entity_registry, "event", "game_event"))
    assert sorted(game_event.attributes["event_types"]) == sorted(ALL_EVENT_TYPES)


async def test_unload_and_reload(
    hass: HomeAssistant,
    broker: FakeBroker,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Unload says goodbye; setting up and reloading keep device and entities."""
    entry, first = await _setup(hass, broker)
    device_id = entry.runtime_data.device_id
    before = {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    }
    assert set(before) == EXPECTED_UNIQUE_IDS

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    # A clean disconnect drops the last will, so "offline" is published.
    last = first.published[-1]
    assert (last["topic"], last["qos"], last["retain"]) == (STATUS_TOPIC, 1, True)
    assert json.loads(last["payload"])["online"] is False
    assert first.closed

    assert await hass.config_entries.async_setup(entry.entry_id)
    second = await broker.wait_ready(sessions=2)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert second is not first

    assert await hass.config_entries.async_reload(entry.entry_id)
    third = await broker.wait_ready(sessions=3)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert json.loads(second.published[-1]["payload"])["online"] is False
    assert json.loads(third.published[0]["payload"])["online"] is True

    after = {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    }
    assert after == before
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert [device.id for device in devices] == [device_id]
    assert _state(hass, entity_registry, "binary_sensor", "in_match") == "off"


# --- b. compatibility with what exists today --------------------------------


async def test_existing_entry_and_device_are_taken_over(
    hass: HomeAssistant,
    broker: FakeBroker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """An entry and device created by the old version need no migration."""
    entry = legacy_entry()
    entry.add_to_hass(hass)
    # The device as the old __init__.py registered it.
    old_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers=LEGACY_IDENTIFIERS,
        name="Home Assistant",
        manufacturer="EmotionKit",
        model="CS2 Game Events",
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await broker.wait_ready()
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert (entry.version, entry.minor_version) == (1, 1)
    assert entry.unique_id == LEGACY_DEVICE_ID
    # The same registry entry: automations refer to this id.
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert [device.id for device in devices] == [old_device.id]
    assert devices[0].identifiers == LEGACY_IDENTIFIERS
    assert entry.runtime_data.device_id == old_device.id
    # Nothing was rewritten that the old version would not understand.
    for key in (
        "control_plane_url",
        "device_name",
        "device_id",
        "mqtt_username",
        "mqtt_password",
        "mqtt_broker",
    ):
        assert entry.data[key] == legacy_entry().data[key]
    assert entry.options == {}


async def test_old_idle_timeout_option_is_honoured(
    hass: HomeAssistant, broker: FakeBroker
) -> None:
    """The only option the old version knew keeps its meaning."""
    entry, client = await _setup(hass, broker, options={"idle_timeout": 75})
    assert entry.runtime_data.idle_timeout == 75
    assert client.statuses()[0]["config"]["idle_timeout_s"] == 75


async def test_retained_config_does_not_change_an_existing_installation(
    hass: HomeAssistant,
    broker: FakeBroker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """The name stored on the server must not rename a device after the update."""
    entry, client = await _setup(hass, broker, options={"idle_timeout": 60})
    runtime = entry.runtime_data

    await client.deliver(
        CONFIG_TOPIC,
        {**OWNER_CONFIG, "actor_name": "Name from the web", "idle_timeout_s": 30},
        retain=True,
    )
    await hass.async_block_till_done()

    assert entry.title == "Home Assistant"
    assert entry.options == {"idle_timeout": 60}
    assert runtime.idle_timeout == 60
    device = device_registry.async_get(runtime.device_id)
    assert device.name == "Home Assistant"
    # The server-side values are remembered, to recognise a later change.
    assert entry.data["remote_config"] == {
        "actor_name": "Name from the web",
        "idle_timeout_s": 30,
    }
    assert entry.data["mqtt_password"] == LEGACY_PASSWORD
    status = client.statuses()[-1]
    assert status["name"] == "Home Assistant"
    assert status["config"]["idle_timeout_s"] == 60
    assert status["config"]["allowed_subjects"] == OWNER_CONFIG["allowed_subjects"]
    # Stored without reloading the entry.
    assert entry.runtime_data is runtime
    assert len(broker.sessions) == 1


async def test_settings_changed_in_the_web_ui_are_applied(
    hass: HomeAssistant,
    broker: FakeBroker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A live config message changes name and idle timeout without a reload."""
    entry, client = await _setup(hass, broker)
    runtime = entry.runtime_data
    await client.deliver(
        CONFIG_TOPIC,
        {**OWNER_CONFIG, "actor_name": "Home Assistant", "idle_timeout_s": 30},
        retain=True,
    )

    await client.deliver(
        CONFIG_TOPIC, {**OWNER_CONFIG, "actor_name": "Desk", "idle_timeout_s": 120}
    )
    await hass.async_block_till_done()

    assert entry.options == {"device_name": "Desk", "idle_timeout": 120}
    assert entry.title == "Desk"
    assert device_registry.async_get(runtime.device_id).name == "Desk"
    assert runtime.idle_timeout == 120
    status = client.statuses()[-1]
    assert status["name"] == "Desk"
    assert status["config"]["idle_timeout_s"] == 120
    assert entry.runtime_data is runtime
    assert entry.state is ConfigEntryState.LOADED
    assert len(broker.sessions) == 1


# --- entities ---------------------------------------------------------------


async def test_entities_follow_the_match(
    hass: HomeAssistant,
    broker: FakeBroker,
    entity_registry: er.EntityRegistry,
) -> None:
    """Entities show the match and fall back to idle when it is left."""
    entry, client = await _setup(hass, broker)
    await client.deliver(CONFIG_TOPIC, OWNER_CONFIG, retain=True)

    await client.deliver(
        EVENT_TOPIC,
        envelope(
            event("round_live", round=3),
            phase="live",
            ct=1,
            t=1,
            health=87,
            armor=50,
            money=3200,
        ),
    )
    await hass.async_block_till_done()

    def state(platform: str, key: str) -> str:
        return _state(hass, entity_registry, platform, key)

    assert state("binary_sensor", "in_match") == "on"
    assert state("binary_sensor", "bomb_planted") == "off"
    assert state("sensor", "round_phase") == "live"
    assert state("sensor", "map") == "de_dust2"
    assert state("sensor", "score_ct") == "1"
    assert state("sensor", "score_t") == "1"
    assert state("sensor", "round") == "3"
    assert state("sensor", "health") == "87"
    assert state("sensor", "armor") == "50"
    assert state("sensor", "money") == "3200"

    game_event = hass.states.get(_entity_id(entity_registry, "event", "game_event"))
    assert game_event.state not in ("unknown", "unavailable")
    assert game_event.attributes["event_type"] == "round_live"
    assert game_event.attributes["round"] == 3

    await client.deliver(
        EVENT_TOPIC,
        envelope(
            event("bomb_planted", round=3),
            bomb="planted",
            ct=1,
            t=1,
            health=87,
            armor=50,
            money=3200,
            at=1,
        ),
    )
    await hass.async_block_till_done()
    assert state("binary_sensor", "bomb_planted") == "on"
    assert state("sensor", "health") == "87"

    # A mate's values are not the owner's.
    await client.deliver(
        EVENT_TOPIC, envelope(subject=MATE, bomb="planted", ct=1, t=1, health=5, at=2)
    )
    await hass.async_block_till_done()
    assert state("sensor", "health") == "87"

    for subject, offset in ((OWNER, 3), (MATE, 4)):
        await client.deliver(
            EVENT_TOPIC,
            menu_envelope(event("match_left", subject=subject), subject=subject, at=offset),
        )
    await hass.async_block_till_done()
    assert state("binary_sensor", "in_match") == "off"
    assert state("binary_sensor", "bomb_planted") == "off"
    for key in ENTITY_KEYS["sensor"]:
        assert state("sensor", key) == "unknown", key


async def test_entities_are_unavailable_while_disconnected(
    hass: HomeAssistant,
    broker: FakeBroker,
    entity_registry: er.EntityRegistry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A lost connection shows; the integration reconnects by itself."""
    # After a few failures the integration asks the server whether the
    # broker has moved; answer "no" without touching the network.
    aioclient_mock.get("https://emotionkit.de/api/v1/info", json={})
    entry, first = await _setup(hass, broker)
    runtime = entry.runtime_data
    assert _state(hass, entity_registry, "binary_sensor", "in_match") == "off"

    broker.refuse = lambda: aiomqtt.MqttError("broker is down")
    first.drop()
    await wait_for(lambda: not runtime.connected)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    for platform, keys in ENTITY_KEYS.items():
        for key in keys:
            assert _state(hass, entity_registry, platform, key) == "unavailable", key

    broker.refuse = None
    second = await broker.wait_ready(sessions=2)
    await hass.async_block_till_done()
    assert runtime.connected
    assert json.loads(second.published[0]["payload"])["online"] is True
    assert _state(hass, entity_registry, "binary_sensor", "in_match") == "off"
    assert _state(hass, entity_registry, "sensor", "map") == "unknown"


async def test_bad_messages_do_not_break_the_session(
    hass: HomeAssistant, broker: FakeBroker, entity_registry: er.EntityRegistry
) -> None:
    """Garbage on the topics is ignored; the next real message still counts."""
    entry, client = await _setup(hass, broker)
    for payload in (b"", b"not json", b"[1, 2]", b"\xff\xfe", b'{"events": "x", "payload": 7}'):
        await client.deliver(EVENT_TOPIC, payload)
        await client.deliver(CONFIG_TOPIC, payload)
    await client.deliver(EVENT_TOPIC, envelope(event("round_live", round=1)))
    await hass.async_block_till_done()

    assert len(broker.sessions) == 1
    assert entry.runtime_data.connected
    assert _state(hass, entity_registry, "sensor", "round_phase") == "live"


async def test_tls_and_websocket_broker_addresses(
    hass: HomeAssistant, broker: FakeBroker
) -> None:
    """TLS uses a real ssl.SSLContext from Home Assistant; wss:// works too."""
    cases = {
        "tls://broker.example:8883": ("broker.example", 8883, None, None),
        "mqtts://broker.example": ("broker.example", 8883, None, None),
        "wss://example.org/mqtt": ("example.org", 443, "websockets", "/mqtt"),
        "wss://example.org": ("example.org", 443, "websockets", "/mqtt"),
    }
    for index, (url, (host, port, transport, path)) in enumerate(cases.items(), start=1):
        entry = legacy_entry(
            data={**legacy_entry().data, "mqtt_broker": url},
            unique_id=f"{LEGACY_DEVICE_ID}-{index}",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id), url
        client = await broker.wait_ready(sessions=index)
        await hass.async_block_till_done()

        assert (client.hostname, client.port) == (host, port), url
        # An SSLContext goes into tls_context. (Passing it as TLSParameters
        # was the defect that kept every TLS URL from connecting.)
        assert isinstance(client.options["tls_context"], ssl.SSLContext), url
        assert client.options["tls_context"].check_hostname is True, url
        assert "tls_params" not in client.options, url
        assert client.options.get("transport") == transport, url
        assert client.options.get("websocket_path") == path, url

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
