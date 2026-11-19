"""Smoke test of the Home Assistant side of the integration.

Runs the real coordinator, config flow, entity platforms, device triggers
and diagnostics against the stand-ins of _ha_stubs.py. See that module for
what this does and does not prove. Skipped when the real Home Assistant is
installed (the stand-ins must not shadow it).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import importlib
import importlib.machinery
import importlib.util
import json
import logging
import re
import sys
import types
from typing import Any
import unittest
from unittest import mock

import _ha_stubs as stubs
import _support  # noqa: F401  (registers the package shell)
from _support import (
    MATE,
    OWNER,
    PACKAGE_DIR,
    FakeResponse,
    FakeSession,
    envelope,
    event,
    menu_envelope,
    raw,
)

PACKAGE = "custom_components.emotionkit"
GLUE_MODULES = (
    "coordinator",
    "entity",
    "sensor",
    "binary_sensor",
    "event",
    "config_flow",
    "device_trigger",
    "diagnostics",
)

STATUS_TOPIC = "devices/device-dev-1/status"
EVENT_TOPIC = "devices/device-dev-1/events"
CONFIG_TOPIC = "devices/device-dev-1/config"
PASSWORD = "s3cret-pw"

ENTRY_DATA = {
    "control_plane_url": "https://control.example",
    "device_name": "Home Assistant",
    "device_id": "dev-1",
    "mqtt_username": "device-dev-1",
    "mqtt_password": PASSWORD,
    "mqtt_broker": "tcp://broker.example:1883",
}
OWNER_CONFIG = {"enabled": True, "allowed_subjects": {OWNER: "owner", MATE: "user"}}
CREDENTIALS = {
    "device_id": "dnew",
    "mqtt_username": "device-dnew",
    "mqtt_password": "new-pw",
    "mqtt_broker": "tcp://broker.example:1883",
}

_PACKAGE_LOGGER = logging.getLogger(PACKAGE)
_NULL_HANDLER = logging.NullHandler()

_uninstall: Callable[[], None] | None = None
integration: Any = None
coordinator: Any = None
config_flow: Any = None
sensor: Any = None
binary_sensor: Any = None
event_platform: Any = None
device_trigger: Any = None
diagnostics: Any = None


def setUpModule() -> None:  # noqa: N802
    global _uninstall, integration, coordinator, config_flow, sensor  # noqa: PLW0603
    global binary_sensor, event_platform, device_trigger, diagnostics  # noqa: PLW0603
    if sys.version_info < (3, 12):
        # The Home Assistant side uses Python 3.12 syntax (HA itself needs 3.13).
        raise unittest.SkipTest("needs Python 3.12 or newer")
    if stubs.real_home_assistant_available():
        raise unittest.SkipTest("the real Home Assistant is installed")
    _uninstall = stubs.install()
    # The integration warns about the failures these tests provoke; keep
    # that out of the test output (assertLogs still sees it).
    _PACKAGE_LOGGER.addHandler(_NULL_HANDLER)
    _PACKAGE_LOGGER.propagate = False

    coordinator = importlib.import_module(f"{PACKAGE}.coordinator")
    sensor = importlib.import_module(f"{PACKAGE}.sensor")
    binary_sensor = importlib.import_module(f"{PACKAGE}.binary_sensor")
    event_platform = importlib.import_module(f"{PACKAGE}.event")
    config_flow = importlib.import_module(f"{PACKAGE}.config_flow")
    device_trigger = importlib.import_module(f"{PACKAGE}.device_trigger")
    diagnostics = importlib.import_module(f"{PACKAGE}.diagnostics")

    # __init__.py itself, as a plain module under another name so that the
    # package shell of _support stays in place. (Loading it through
    # importlib would make it a package of its own and import a second copy
    # of every module.)
    name = f"{PACKAGE}._integration"
    loader = importlib.machinery.SourceFileLoader(name, str(PACKAGE_DIR / "__init__.py"))
    spec = importlib.machinery.ModuleSpec(name, loader, origin=loader.path, is_package=False)
    integration = importlib.util.module_from_spec(spec)
    sys.modules[name] = integration
    loader.exec_module(integration)
    assert integration.__package__ == PACKAGE
    assert integration.EmotionKitRuntime is coordinator.EmotionKitRuntime


def tearDownModule() -> None:  # noqa: N802
    _PACKAGE_LOGGER.removeHandler(_NULL_HANDLER)
    _PACKAGE_LOGGER.propagate = True
    for name in (*GLUE_MODULES, "_integration"):
        sys.modules.pop(f"{PACKAGE}.{name}", None)
    if _uninstall is not None:
        _uninstall()


class FakeReasonCode:
    def __init__(self, value: int) -> None:
        self.value = value

    def __str__(self) -> str:
        return "Not authorized"


async def eventually(predicate: Callable[[], Any], timeout: float = 3.0) -> None:
    """Let the event loop run until predicate() holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.001)


async def _no_sleep(delay: float) -> None:
    await asyncio.sleep(0)


class GlueTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        stubs.Client.reset()
        self.hass = stubs.FakeHass()
        self.hass.data["session"] = FakeSession(FakeResponse(500))
        self.entries: list[stubs.FakeEntry] = []

    async def asyncTearDown(self) -> None:
        for entry in self.entries:
            runtime = getattr(entry, "runtime_data", None)
            if runtime is not None:
                await runtime.async_stop()

    def make_entry(self, *, data: dict[str, Any] | None = None, **kwargs: Any) -> stubs.FakeEntry:
        entry = stubs.FakeEntry(data={**ENTRY_DATA, **(data or {})}, **kwargs)
        self.entries.append(entry)
        return entry

    async def start(self, entry: stubs.FakeEntry | None = None, **kwargs: Any) -> Any:
        """Set an entry up and wait until its client is subscribed."""
        entry = entry or self.make_entry(**kwargs)
        before = len(stubs.Client.created)
        self.assertTrue(await integration.async_setup_entry(self.hass, entry))
        runtime = entry.runtime_data
        runtime._supervisor._sleep = _no_sleep  # noqa: SLF001
        await eventually(
            lambda: len(stubs.Client.created) > before
            and len(stubs.Client.created[-1].subscriptions) == 2
        )
        return entry, runtime, stubs.Client.created[-1]

    async def add_entities(self, entry: stubs.FakeEntry) -> dict[str, Any]:
        entities: list[Any] = []
        for platform in (sensor, binary_sensor, event_platform):
            await platform.async_setup_entry(self.hass, entry, entities.extend)
        for entity in entities:
            entity.hass = self.hass
            await entity.async_added_to_hass()
        return {entity._attr_translation_key: entity for entity in entities}  # noqa: SLF001

    def fired(self, trigger_type: str | None = None) -> list[dict[str, Any]]:
        return [
            data
            for name, data in self.hass.bus.fired
            if name == "emotionkit_event" and trigger_type in (None, data["type"])
        ]

    @staticmethod
    def statuses(client: Any) -> list[dict[str, Any]]:
        return [
            json.loads(item["payload"]) for item in client.published if item["topic"] == STATUS_TOPIC
        ]


class SetupTest(GlueTestCase):
    async def test_connects_announces_and_subscribes(self) -> None:
        entry, runtime, client = await self.start()

        device = self.hass.data["device_registry"].devices["registry-1"]
        self.assertEqual(device.identifiers, {("emotionkit", "dev-1")})
        self.assertEqual(device.name, "Home Assistant")
        self.assertEqual(runtime.device_id, "registry-1")
        self.assertEqual(set(self.hass.config_entries.forwarded), {"binary_sensor", "event", "sensor"})

        self.assertEqual((client.hostname, client.port), ("broker.example", 1883))
        self.assertEqual((client.username, client.password), ("device-dev-1", PASSWORD))
        self.assertEqual(client.keepalive, 30)
        self.assertEqual(client.transport, "tcp")
        self.assertIsNone(client.tls_context)
        self.assertIsNone(client.tls_params)
        self.assertIsNone(client.websocket_path)
        self.assertEqual(self.hass.executor_jobs, [])

        will = client.will
        self.assertEqual((will.topic, will.qos, will.retain), (STATUS_TOPIC, 1, True))
        self.assertIs(json.loads(will.payload)["online"], False)

        first = client.published[0]
        self.assertEqual((first["topic"], first["qos"], first["retain"]), (STATUS_TOPIC, 1, True))
        status = json.loads(first["payload"])
        self.assertIs(status["online"], True)
        self.assertEqual(status["name"], "Home Assistant")
        self.assertEqual(status["config"]["idle_timeout_s"], 30)
        self.assertEqual(client.subscriptions, [(EVENT_TOPIC, 1), (CONFIG_TOPIC, 1)])
        self.assertTrue(runtime.connected)

    async def test_tls_and_websocket_connections(self) -> None:
        cases = {
            "tls://b.example": ("b.example", 8883, "tcp", True, None),
            "mqtts://b.example:8884": ("b.example", 8884, "tcp", True, None),
            "ssl://b.example": ("b.example", 8883, "tcp", True, None),
            "wss://example.org/mqtt": ("example.org", 443, "websockets", True, "/mqtt"),
            "wss://example.org": ("example.org", 443, "websockets", True, "/mqtt"),
            "ws://b.example:9001/ws": ("b.example", 9001, "websockets", False, "/ws"),
            "mqtt://b.example": ("b.example", 1883, "tcp", False, None),
        }
        for url, (host, port, transport, tls, path) in cases.items():
            with self.subTest(url=url):
                self.hass.executor_jobs.clear()
                entry, runtime, client = await self.start(data={"mqtt_broker": url})
                self.assertEqual((client.hostname, client.port), (host, port))
                self.assertEqual(client.transport, transport)
                self.assertEqual(client.websocket_path, path)
                # TLS goes through tls_context (an ssl.SSLContext), never
                # through tls_params, and the context comes from Home
                # Assistant's helper, fetched off the event loop.
                self.assertIsNone(client.tls_params)
                if tls:
                    self.assertIs(client.tls_context, stubs.SSL_CONTEXT)
                    self.assertEqual(len(self.hass.executor_jobs), 1)
                else:
                    self.assertIsNone(client.tls_context)
                    self.assertEqual(self.hass.executor_jobs, [])
                await runtime.async_stop()

    async def test_entry_without_broker_asks_the_server(self) -> None:
        self.hass.data["session"] = FakeSession(
            FakeResponse(200, {"mqtt_broker_url": "wss://announced.example/mqtt"})
        )
        entry, runtime, client = await self.start(data={"mqtt_broker": ""})
        self.assertEqual(client.hostname, "announced.example")
        self.assertEqual(client.transport, "websockets")
        self.assertEqual(self.hass.data["session"].calls[0]["url"], "https://control.example/api/v1/info")

    async def test_server_without_broker_announcement(self) -> None:
        self.hass.data["session"] = FakeSession(FakeResponse(200, {}))
        entry, runtime, client = await self.start(data={"mqtt_broker": ""})
        self.assertEqual((client.hostname, client.port), ("control.example", 8883))

    async def test_setup_errors(self) -> None:
        # Server unreachable while the address is unknown: try again later.
        self.hass.data["session"] = FakeSession(OSError("down"))
        with self.assertRaises(stubs.ConfigEntryNotReady):
            await integration.async_setup_entry(
                self.hass, stubs.FakeEntry(data={**ENTRY_DATA, "mqtt_broker": ""})
            )
        # A URL the user typed and that cannot work: a clear error.
        with self.assertRaises(stubs.ConfigEntryError) as caught:
            await integration.async_setup_entry(
                self.hass,
                stubs.FakeEntry(
                    data={**ENTRY_DATA, "mqtt_broker": "http://x", "mqtt_broker_manual": True}
                ),
            )
        self.assertIn("Reconfigure", str(caught.exception))
        with self.assertRaises(stubs.ConfigEntryAuthFailed):
            await integration.async_setup_entry(
                self.hass, stubs.FakeEntry(data={**ENTRY_DATA, "mqtt_password": ""})
            )
        self.assertEqual(stubs.Client.created, [])

    async def test_unusable_stored_url_is_replaced_by_the_servers(self) -> None:
        self.hass.data["session"] = FakeSession(
            FakeResponse(200, {"mqtt_broker_url": "tcp://good.example:1883"})
        )
        entry, runtime, client = await self.start(data={"mqtt_broker": "bogus"})
        self.assertEqual(client.hostname, "good.example")

    async def test_options_reach_the_runtime(self) -> None:
        entry, runtime, client = await self.start(
            options={"idle_timeout": 77, "react_to_all_players": True, "device_name": "Desk"}
        )
        self.assertEqual(runtime.idle_timeout, 77)
        self.assertTrue(runtime.processor.all_players)
        self.assertEqual(runtime.device_name, "Desk")
        self.assertEqual(self.statuses(client)[0]["config"]["idle_timeout_s"], 77)
        self.assertEqual(self.statuses(client)[0]["name"], "Desk")


class MessageFlowTest(GlueTestCase):
    async def test_events_reach_bus_and_entities(self) -> None:
        entry, runtime, client = await self.start()
        entities = await self.add_entities(entry)
        self.assertEqual(
            set(entities),
            {
                "round_phase", "map", "score_ct", "score_t", "round", "health", "armor",
                "money", "bomb_planted", "in_match", "game_event",
            },
        )
        for entity in entities.values():
            self.assertTrue(entity.available)
            self.assertFalse(entity._attr_should_poll)  # noqa: SLF001
            self.assertTrue(entity._attr_has_entity_name)  # noqa: SLF001
            self.assertTrue(entity._attr_unique_id.startswith("dev-1_"))  # noqa: SLF001
        self.assertIs(entities["in_match"].is_on, False)
        self.assertIsNone(entities["round_phase"].native_value)

        client.deliver(CONFIG_TOPIC, raw(OWNER_CONFIG), retain=True)
        await eventually(lambda: len(self.statuses(client)) == 2)
        self.assertEqual(self.statuses(client)[1]["config"]["allowed_subjects"], OWNER_CONFIG["allowed_subjects"])

        client.deliver(
            EVENT_TOPIC,
            raw(envelope(event("round_live", round=3), phase="live", ct=1, t=1, health=87, armor=50, money=3200)),
        )
        await eventually(lambda: self.fired("round_live"))
        fired = self.fired("round_live")[0]
        self.assertEqual(fired["device_id"], "registry-1")
        self.assertEqual(fired["round"], 3)
        self.assertEqual(fired["match_id"], "match-1")

        self.assertIs(entities["in_match"].is_on, True)
        self.assertEqual(entities["round_phase"].native_value, "live")
        self.assertEqual(entities["map"].native_value, "de_dust2")
        self.assertEqual(entities["score_ct"].native_value, 1)
        self.assertEqual(entities["score_t"].native_value, 1)
        self.assertEqual(entities["round"].native_value, 3)
        self.assertEqual(entities["health"].native_value, 87)
        self.assertEqual(entities["armor"].native_value, 50)
        self.assertEqual(entities["money"].native_value, 3200)
        self.assertIs(entities["bomb_planted"].is_on, False)
        self.assertGreaterEqual(entities["map"].state_writes, 1)
        self.assertEqual(entities["game_event"].triggered[0][0], "round_live")
        self.assertEqual(entities["game_event"].triggered[0][1]["round"], 3)

        client.deliver(EVENT_TOPIC, raw(envelope(event("bomb_planted", round=3), bomb="planted", ct=1, t=1)))
        await eventually(lambda: self.fired("bomb_planted"))
        self.assertIs(entities["bomb_planted"].is_on, True)

        # Only the owner's player events trigger.
        client.deliver(
            EVENT_TOPIC,
            raw(envelope(event("player_kill", subject=MATE, value=1), subject=MATE, ct=1, t=1)),
        )
        client.deliver(
            EVENT_TOPIC,
            raw(envelope(event("player_kill", subject=OWNER, value=1, headshot=True), ct=1, t=1)),
        )
        await eventually(lambda: self.fired("player_headshot"))
        self.assertEqual(len(self.fired("player_kill")), 1)
        self.assertEqual(self.fired("player_kill")[0]["subject"], OWNER)

        client.deliver(EVENT_TOPIC, raw(menu_envelope(event("match_left", subject=OWNER))))
        await eventually(lambda: self.fired("match_left"))
        # The mate is still playing, so the match goes on for this device.
        self.assertIs(entities["in_match"].is_on, True)
        client.deliver(
            EVENT_TOPIC, raw(menu_envelope(event("match_left", subject=MATE), subject=MATE))
        )
        await eventually(lambda: not runtime.snapshot.in_match)
        self.assertIs(entities["in_match"].is_on, False)
        self.assertIs(entities["bomb_planted"].is_on, False)
        for key in ("round_phase", "map", "score_ct", "round", "health", "money"):
            self.assertIsNone(entities[key].native_value, key)
        self.assertEqual(
            [name for name, _ in entities["game_event"].triggered],
            ["round_live", "bomb_planted", "player_kill", "player_headshot", "match_left"],
        )

    async def test_idle_trigger_fires_and_state_expires(self) -> None:
        clock = types.SimpleNamespace(now=1000.0)
        fake_time = types.SimpleNamespace(monotonic=lambda: clock.now)
        with mock.patch.object(coordinator, "time", fake_time):
            entry, runtime, client = await self.start()
            entities = await self.add_entities(entry)

            client.deliver(EVENT_TOPIC, raw(envelope(event("round_live", round=1))))
            await eventually(lambda: self.hass.timers)
            self.assertEqual(len(self.hass.timers), 1)
            self.assertAlmostEqual(self.hass.timers[0]["delay"], 30.05, places=2)

            # Every in-match message restarts the countdown, without
            # touching the timer.
            clock.now = 1020.0
            client.deliver(EVENT_TOPIC, raw(envelope()))
            await eventually(lambda: runtime.processor.stats["messages"] == 2)
            self.assertEqual(len(self.hass.timers), 1)

            # A menu packet does not.
            clock.now = 1025.0
            client.deliver(EVENT_TOPIC, raw(menu_envelope(subject=MATE)))
            await eventually(lambda: runtime.processor.stats["messages"] == 3)

            # The timer fires at its original deadline: too early now.
            clock.now = 1030.1
            self.hass.timers[-1]["action"](None)
            self.assertEqual(self.fired("idle"), [])
            self.assertEqual(len(self.hass.timers), 2)
            self.assertAlmostEqual(self.hass.timers[-1]["delay"], 19.95, places=2)

            clock.now = 1050.1
            self.hass.timers[-1]["action"](None)
            idle = self.fired("idle")
            self.assertEqual(len(idle), 1)
            self.assertEqual(idle[0]["device_id"], "registry-1")
            self.assertEqual(idle[0]["timeout"], 30)
            self.assertEqual(idle[0]["last_event_type"], "round_live")
            self.assertEqual(entities["game_event"].triggered[-1][0], "idle")
            # The entities keep their state a little longer (GSI heartbeat).
            self.assertIs(entities["in_match"].is_on, True)
            self.assertEqual(len(self.hass.timers), 3)
            self.assertAlmostEqual(self.hass.timers[-1]["delay"], 59.95, places=2)

            clock.now = 1110.1
            self.hass.timers[-1]["action"](None)
            self.assertIs(entities["in_match"].is_on, False)
            self.assertEqual(len(self.fired("idle")), 1)
            self.assertEqual(len(self.hass.timers), 3)  # nothing left to wait for

            # New activity arms everything again.
            clock.now = 2000.0
            client.deliver(EVENT_TOPIC, raw(envelope()))
            await eventually(lambda: len(self.hass.timers) == 4)
            clock.now = 2031.0
            self.hass.timers[-1]["action"](None)
            self.assertEqual(len(self.fired("idle")), 2)

    async def test_idle_timeout_zero_disables_the_trigger(self) -> None:
        clock = types.SimpleNamespace(now=1000.0)
        fake_time = types.SimpleNamespace(monotonic=lambda: clock.now)
        with mock.patch.object(coordinator, "time", fake_time):
            entry, runtime, client = await self.start(options={"idle_timeout": 0})
            client.deliver(EVENT_TOPIC, raw(envelope()))
            await eventually(lambda: self.hass.timers)
            self.assertAlmostEqual(self.hass.timers[0]["delay"], 90.05, places=2)
            clock.now = 1100.0
            self.hass.timers[0]["action"](None)
            self.assertEqual(self.fired("idle"), [])
            self.assertFalse(runtime.snapshot.in_match)

    async def test_bad_messages_do_not_end_the_session(self) -> None:
        entry, runtime, client = await self.start()
        original = runtime.processor.handle_event
        calls = {"count": 0}

        def flaky(payload: Any, *, monotonic: float) -> Any:
            calls["count"] += 1
            if calls["count"] == 3:
                raise RuntimeError("bug in a handler")
            return original(payload, monotonic=monotonic)

        runtime.processor.handle_event = flaky
        client.deliver(EVENT_TOPIC, b"garbage")
        client.deliver(EVENT_TOPIC, None)
        client.deliver(EVENT_TOPIC, raw(envelope(event("round_live", round=1))))  # raises
        client.deliver(CONFIG_TOPIC, b"[1, 2]")
        client.deliver("devices/device-dev-1/unknown", b"{}")
        client.deliver(EVENT_TOPIC, raw(envelope(event("bomb_planted", round=1), bomb="planted")))
        with self.assertLogs("custom_components.emotionkit.coordinator", level="ERROR"):
            await eventually(lambda: self.fired("bomb_planted"))
        self.assertEqual(len(stubs.Client.created), 1)
        self.assertTrue(runtime.connected)
        self.assertEqual(runtime._handler_errors, 1)  # noqa: SLF001

    async def test_disabled_device_ignores_events(self) -> None:
        entry, runtime, client = await self.start()
        client.deliver(CONFIG_TOPIC, raw({"enabled": False}), retain=True)
        await eventually(lambda: len(self.statuses(client)) == 2)
        self.assertIs(self.statuses(client)[1]["config"]["enabled"], False)
        client.deliver(EVENT_TOPIC, raw(envelope(event("round_live", round=1))))
        await eventually(lambda: runtime.processor.stats["ignored_disabled"] == 1)
        self.assertEqual(self.fired(), [])
        self.assertEqual(self.hass.timers, [])

        client.deliver(CONFIG_TOPIC, raw({"enabled": True}))
        await eventually(lambda: len(self.statuses(client)) == 3)
        client.deliver(EVENT_TOPIC, raw(envelope(event("round_over", round=1, side="T"), phase="over")))
        await eventually(lambda: self.fired("round_over_t"))


class RemoteSettingsTest(GlueTestCase):
    async def test_change_in_the_web_ui_is_applied_and_stored(self) -> None:
        entry, runtime, client = await self.start(
            data={"remote_config": {"actor_name": "Home Assistant", "idle_timeout_s": 30}}
        )
        client.deliver(
            CONFIG_TOPIC, raw({**OWNER_CONFIG, "actor_name": "Desk", "idle_timeout_s": 120})
        )
        await eventually(lambda: len(self.statuses(client)) == 2)

        status = self.statuses(client)[1]
        self.assertEqual(status["name"], "Desk")
        self.assertEqual(status["config"]["actor_name"], "Desk")
        self.assertEqual(status["config"]["idle_timeout_s"], 120)

        self.assertEqual(entry.options, {"device_name": "Desk", "idle_timeout": 120})
        self.assertEqual(entry.title, "Desk")
        self.assertEqual(entry.data["remote_config"], {"actor_name": "Desk", "idle_timeout_s": 120})
        self.assertEqual(self.hass.data["device_registry"].devices["registry-1"].name, "Desk")
        self.assertEqual(runtime.idle_timeout, 120)
        self.assertEqual(runtime.watchdog.idle_timeout, 120)
        # Applied live; storing it must not reload the entry.
        self.assertEqual(self.hass.config_entries.reloads, [])
        # Credentials survive the update of the entry data.
        self.assertEqual(entry.data["mqtt_password"], PASSWORD)

    async def test_updated_installation_keeps_its_settings(self) -> None:
        # No stored baseline: the retained config describes the past.
        entry, runtime, client = await self.start(options={"idle_timeout": 60})
        client.deliver(
            CONFIG_TOPIC,
            raw({**OWNER_CONFIG, "actor_name": "Name from the web", "idle_timeout_s": 30}),
            retain=True,
        )
        await eventually(lambda: len(self.statuses(client)) == 2)
        status = self.statuses(client)[1]
        self.assertEqual(status["name"], "Home Assistant")
        self.assertEqual(status["config"]["idle_timeout_s"], 60)
        self.assertEqual(entry.options, {"idle_timeout": 60})
        self.assertEqual(entry.title, "Home Assistant")
        self.assertEqual(
            entry.data["remote_config"],
            {"actor_name": "Name from the web", "idle_timeout_s": 30},
        )

    async def test_unchanged_config_is_not_stored_again(self) -> None:
        entry, runtime, client = await self.start(
            data={"remote_config": {"actor_name": "Home Assistant", "idle_timeout_s": 30}}
        )
        client.deliver(
            CONFIG_TOPIC,
            raw({**OWNER_CONFIG, "actor_name": "Home Assistant", "idle_timeout_s": 30}),
            retain=True,
        )
        await eventually(lambda: len(self.statuses(client)) == 2)
        self.assertEqual(self.hass.config_entries.updates, [])


class ConnectionLifecycleTest(GlueTestCase):
    async def test_unload_publishes_offline_and_stops(self) -> None:
        entry, runtime, client = await self.start()
        task = entry.tasks[0]
        self.assertTrue(await integration.async_unload_entry(self.hass, entry))

        last = client.published[-1]
        self.assertEqual((last["topic"], last["qos"], last["retain"]), (STATUS_TOPIC, 1, True))
        self.assertIs(json.loads(last["payload"])["online"], False)
        self.assertTrue(client.disconnected_cleanly)
        self.assertTrue(task.done())
        self.assertFalse(runtime.connected)
        self.assertEqual(set(self.hass.config_entries.unloaded), {"binary_sensor", "event", "sensor"})
        await runtime.async_stop()  # a second stop is harmless

    async def test_home_assistant_stop_publishes_offline(self) -> None:
        entry, runtime, client = await self.start()
        listener = self.hass.bus.listeners["homeassistant_stop"][0]
        await listener({})
        self.assertIs(json.loads(client.published[-1]["payload"])["online"], False)
        self.assertEqual(len(entry.unload_callbacks), 1)

    async def test_unload_is_robust_when_the_task_has_crashed(self) -> None:
        # Unload used to re-raise whatever had killed the task, which made
        # reload and reconfigure fail until Home Assistant was restarted.
        entry, runtime, client = await self.start()
        await runtime.async_stop()

        async def crash() -> None:
            raise RuntimeError("the loop died")

        task = asyncio.get_running_loop().create_task(crash())
        await asyncio.wait({task})
        runtime._task = task  # noqa: SLF001
        self.assertTrue(await integration.async_unload_entry(self.hass, entry))

    async def test_reconnects_after_connection_loss(self) -> None:
        entry, runtime, first = await self.start()
        entities = await self.add_entities(entry)
        writes = entities["map"].state_writes if hasattr(entities["map"], "state_writes") else 0
        first.drop()
        await eventually(
            lambda: len(stubs.Client.created) == 2
            and len(stubs.Client.created[1].subscriptions) == 2
        )
        second = stubs.Client.created[1]
        self.assertIs(json.loads(second.published[0]["payload"])["online"], True)
        self.assertTrue(runtime.connected)
        # Availability changed twice (down, up): the entities were told.
        self.assertGreaterEqual(entities["map"].state_writes, writes + 2)

        second.deliver(EVENT_TOPIC, raw(envelope(event("round_live", round=1))))
        await eventually(lambda: self.fired("round_live"))

    async def test_authentication_failure_starts_reauth(self) -> None:
        stubs.Client.connect_errors = [
            stubs.MqttConnectError(FakeReasonCode(135)),
            stubs.MqttConnectError(FakeReasonCode(135)),
        ]
        entry = self.make_entry()
        entry.active_flows = [
            {"flow_id": "flow-9", "context": {"source": "reauth"}},
            {"flow_id": "flow-3", "context": {"source": "reconfigure"}},
        ]
        self.assertTrue(await integration.async_setup_entry(self.hass, entry))
        runtime = entry.runtime_data
        runtime._supervisor._sleep = _no_sleep  # noqa: SLF001
        with self.assertLogs("custom_components.emotionkit.coordinator", level="WARNING") as logs:
            await eventually(lambda: entry.reauth_requests == 1)
            # The third attempt is accepted (it was a hiccup after all):
            # the pending re-auth flow is dismissed, other flows are not.
            await eventually(lambda: runtime.connected)
        self.assertIn("re-authentication is required", logs.output[0])
        self.assertNotIn(PASSWORD, " ".join(logs.output))
        self.assertEqual(self.hass.config_entries.flow.aborted, ["flow-9"])
        self.assertEqual(entry.reauth_requests, 1)

    async def test_other_refusals_do_not_start_reauth(self) -> None:
        stubs.Client.connect_errors = [
            stubs.MqttConnectError(FakeReasonCode(136)),  # server unavailable
            stubs.MqttError("timed out"),
            stubs.MqttCodeError(5, "not a connect error"),
            OSError("unexpected"),
        ]
        entry = self.make_entry()
        await integration.async_setup_entry(self.hass, entry)
        entry.runtime_data._supervisor._sleep = _no_sleep  # noqa: SLF001
        await eventually(lambda: entry.runtime_data.connected)
        self.assertEqual(entry.reauth_requests, 0)
        self.assertEqual(len(stubs.Client.created), 5)

    async def test_broker_address_is_refreshed_after_repeated_failures(self) -> None:
        stubs.Client.connect_errors = [stubs.MqttError("refused") for _ in range(3)]
        self.hass.data["session"] = FakeSession(
            FakeResponse(200, {"mqtt_broker_url": "wss://new.example/mqtt"})
        )
        entry = self.make_entry()
        await integration.async_setup_entry(self.hass, entry)
        runtime = entry.runtime_data
        runtime._supervisor._sleep = _no_sleep  # noqa: SLF001
        await eventually(lambda: runtime.connected)

        hosts = [client.hostname for client in stubs.Client.created]
        self.assertEqual(hosts, ["broker.example"] * 3 + ["new.example"])
        self.assertEqual(stubs.Client.created[-1].transport, "websockets")
        self.assertIs(stubs.Client.created[-1].tls_context, stubs.SSL_CONTEXT)
        self.assertEqual(entry.data["mqtt_broker"], "wss://new.example/mqtt")
        self.assertEqual(len(self.hass.data["session"].calls), 1)
        # The same credentials: no re-claim.
        self.assertEqual(stubs.Client.created[-1].password, PASSWORD)

    async def test_manual_broker_address_is_never_replaced(self) -> None:
        stubs.Client.connect_errors = [stubs.MqttError("refused") for _ in range(5)]
        self.hass.data["session"] = FakeSession(
            FakeResponse(200, {"mqtt_broker_url": "wss://new.example/mqtt"})
        )
        entry = self.make_entry(data={"mqtt_broker_manual": True})
        await integration.async_setup_entry(self.hass, entry)
        runtime = entry.runtime_data
        runtime._supervisor._sleep = _no_sleep  # noqa: SLF001
        await eventually(lambda: runtime.connected)
        self.assertEqual({client.hostname for client in stubs.Client.created}, {"broker.example"})
        self.assertEqual(self.hass.data["session"].calls, [])


class DiagnosticsAndTriggersTest(GlueTestCase):
    async def test_diagnostics_hold_no_secrets(self) -> None:
        entry, runtime, client = await self.start()
        client.deliver(CONFIG_TOPIC, raw(OWNER_CONFIG), retain=True)
        client.deliver(EVENT_TOPIC, raw(envelope(event("player_kill", subject=OWNER, value=1))))
        await eventually(lambda: self.fired("player_kill"))

        data = await diagnostics.async_get_config_entry_diagnostics(self.hass, entry)
        text = json.dumps(data)
        for secret in (PASSWORD, "device-dev-1", OWNER, MATE):
            self.assertNotIn(secret, text)
        self.assertEqual(data["entry"]["data"]["mqtt_password"], "**REDACTED**")
        self.assertEqual(data["entry"]["data"]["mqtt_username"], "**REDACTED**")
        self.assertIs(data["runtime"]["connection"]["connected"], True)
        self.assertIs(data["runtime"]["config"]["owner_known"], True)
        self.assertEqual(data["runtime"]["config"]["allowed_subjects_by_role"], {"owner": 1, "user": 1})
        self.assertEqual(data["runtime"]["stats"]["triggers"], 1)
        self.assertEqual(data["runtime"]["broker"]["address"], "tcp://broker.example:1883")

    async def test_diagnostics_of_an_entry_that_is_not_loaded(self) -> None:
        entry = stubs.FakeEntry(data=dict(ENTRY_DATA))
        data = await diagnostics.async_get_config_entry_diagnostics(self.hass, entry)
        self.assertIsNone(data["runtime"])
        self.assertNotIn(PASSWORD, json.dumps(data))

    async def test_device_triggers(self) -> None:
        entry, runtime, client = await self.start()
        triggers = await device_trigger.async_get_triggers(self.hass, "registry-1")
        types_offered = [trigger["type"] for trigger in triggers]
        self.assertEqual(len(types_offered), 21)
        for legacy in (
            "bomb_planted", "bomb_defused", "bomb_exploded", "round_live", "round_over_t",
            "round_over_ct", "freezetime", "idle",
        ):
            self.assertIn(legacy, types_offered)
        for trigger in triggers:
            self.assertEqual(
                (trigger["platform"], trigger["domain"], trigger["device_id"]),
                ("device", "emotionkit", "registry-1"),
            )
            device_trigger.TRIGGER_SCHEMA(trigger)
        self.assertEqual(await device_trigger.async_get_triggers(self.hass, "nope"), [])

        with self.assertRaises(stubs.Invalid):
            device_trigger.TRIGGER_SCHEMA({**triggers[0], "type": "no_such_trigger"})

        detach = await device_trigger.async_attach_trigger(
            self.hass, {**triggers[0], "type": "match_won"}, None, {}
        )
        self.assertTrue(callable(detach))
        attached = stubs._EventTrigger.attached[-1]  # noqa: SLF001
        self.assertEqual(attached["platform_type"], "device")
        self.assertEqual(attached["config"]["event_type"], "emotionkit_event")
        self.assertEqual(
            attached["config"]["event_data"], {"device_id": "registry-1", "type": "match_won"}
        )
        self.assertEqual(await device_trigger.async_get_trigger_capabilities(self.hass, {}), {})


class ConfigFlowTest(GlueTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # Poll without waiting.
        from custom_components.emotionkit import api

        def fast_wait(*args: Any, **kwargs: Any) -> Any:
            return api.async_wait_for_claim(*args, interval=0, **kwargs)

        patcher = mock.patch.object(config_flow, "async_wait_for_claim", fast_wait)
        patcher.start()
        self.addCleanup(patcher.stop)

    def new_flow(self, *session_script: Any) -> Any:
        self.hass.data["session"] = FakeSession(*session_script)
        flow = config_flow.EmotionKitConfigFlow()
        flow.hass = self.hass
        return flow

    def assert_shows_code(self, result: dict[str, Any]) -> str:
        self.assertEqual(result["type"], "progress")
        self.assertEqual(result["step_id"], "claim")
        self.assertEqual(result["progress_action"], "wait_for_claim")
        placeholders = result["description_placeholders"]
        self.assertEqual(placeholders["url"], "https://emotionkit.de")
        self.assertRegex(placeholders["claim_code"], r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")
        return placeholders["claim_code"]

    async def test_user_flow(self) -> None:
        flow = self.new_flow(
            FakeResponse(200, {"device_token": "tok-1"}),
            FakeResponse(202),
            FakeResponse(202),
            FakeResponse(200, dict(CREDENTIALS)),
        )
        form = await flow.async_step_user()
        self.assertEqual((form["type"], form["step_id"], form["errors"]), ("form", "user", {}))

        result = await flow.async_step_user({"device_name": "  My HA  "})
        code = self.assert_shows_code(result)
        calls = self.hass.data["session"].calls
        self.assertEqual(calls[0]["url"], "https://emotionkit.de/api/v1/devices/announce")
        self.assertEqual(calls[0]["json"], {"code": code, "kind": "ha-integration"})

        # The request returns at once; the waiting happens in the task.
        task = result["progress_task"]
        self.assertFalse(task.done())
        again = await flow.async_step_claim()
        self.assertIs(again["progress_task"], task)
        await task
        self.assertEqual(calls[-1]["url"], f"https://emotionkit.de/api/v1/devices/pending/{code}")
        self.assertEqual(calls[-1]["headers"], {"X-Device-Token": "tok-1"})

        done = await flow.async_step_claim()
        self.assertEqual(done, {"type": "progress_done", "step_id": "finish"})
        created = await flow.async_step_finish()
        self.assertEqual(created["type"], "create_entry")
        self.assertEqual(created["title"], "My HA")
        self.assertEqual(
            created["data"],
            {
                "control_plane_url": "https://emotionkit.de",
                "device_name": "My HA",
                "device_id": "dnew",
                "mqtt_username": "device-dnew",
                "mqtt_password": "new-pw",
                "mqtt_broker": "tcp://broker.example:1883",
                "remote_config": {},
            },
        )
        self.assertEqual(flow.unique_id, "dnew")

    async def test_announce_errors_are_named(self) -> None:
        cases = [
            (TimeoutError(), "timeout_connect"),  # used to be "Unknown error"
            (OSError("down"), "cannot_connect"),
            (FakeResponse(503), "server_busy"),
            (FakeResponse(500), "cannot_connect"),
            (FakeResponse(200, {"nothing": 1}), "invalid_response"),
        ]
        for outcome, error in cases:
            with self.subTest(error=error):
                flow = self.new_flow(outcome)
                result = await flow.async_step_user({"device_name": "HA"})
                self.assertEqual((result["type"], result["step_id"]), ("form", "user"))
                self.assertEqual(result["errors"], {"base": error})

    async def test_expired_code_is_replaced_by_a_fresh_one(self) -> None:
        flow = self.new_flow(
            FakeResponse(200, {"device_token": "tok-1"}),
            FakeResponse(404),  # the server no longer knows the code
            FakeResponse(200, {"device_token": "tok-2"}),
            FakeResponse(200, dict(CREDENTIALS)),
        )
        first = await flow.async_step_user({"device_name": "HA"})
        old_code = self.assert_shows_code(first)
        await first["progress_task"]

        second = await flow.async_step_claim()
        new_code = self.assert_shows_code(second)
        self.assertNotEqual(new_code, old_code)
        self.assertIsNot(second["progress_task"], first["progress_task"])
        await second["progress_task"]
        calls = self.hass.data["session"].calls
        self.assertEqual(calls[2]["json"]["code"], new_code)
        self.assertTrue(calls[3]["url"].endswith(new_code))
        self.assertEqual(calls[3]["headers"], {"X-Device-Token": "tok-2"})

        done = await flow.async_step_claim()
        self.assertEqual(done["step_id"], "finish")

    async def test_timeout_and_retry(self) -> None:
        flow = self.new_flow(
            FakeResponse(200, {"device_token": "tok-1"}),
            FakeResponse(202),
            FakeResponse(200, {"device_token": "tok-2"}),
            FakeResponse(200, dict(CREDENTIALS)),
        )
        with mock.patch.object(config_flow, "CLAIM_FLOW_TIMEOUT", -1):
            result = await flow.async_step_user({"device_name": "HA"})
            await result["progress_task"]
        done = await flow.async_step_claim()
        self.assertEqual(done, {"type": "progress_done", "step_id": "claim_failed"})

        form = await flow.async_step_claim_failed()
        self.assertEqual((form["type"], form["step_id"]), ("form", "claim_failed"))
        self.assertEqual(form["errors"], {"base": "claim_timeout"})

        retry = await flow.async_step_claim_failed({})
        self.assert_shows_code(retry)
        await retry["progress_task"]
        self.assertEqual((await flow.async_step_claim())["step_id"], "finish")

    async def test_invalid_credentials_from_the_server(self) -> None:
        flow = self.new_flow(
            FakeResponse(200, {"device_token": "tok-1"}),
            FakeResponse(200, {"device_id": "d"}),
        )
        result = await flow.async_step_user({"device_name": "HA"})
        await result["progress_task"]
        self.assertEqual((await flow.async_step_claim())["step_id"], "claim_failed")
        form = await flow.async_step_claim_failed()
        self.assertEqual(form["errors"], {"base": "claim_failed"})

    async def test_closing_the_flow_stops_polling(self) -> None:
        flow = self.new_flow(FakeResponse(200, {"device_token": "tok-1"}), FakeResponse(202, delay=30))
        result = await flow.async_step_user({"device_name": "HA"})
        task = result["progress_task"]
        flow.async_remove()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_reauth_claims_again_and_keeps_the_device(self) -> None:
        entry = stubs.FakeEntry(
            data={**ENTRY_DATA, "mqtt_broker_manual": True, "remote_config": {"actor_name": "X"}},
            unique_id="dev-1",
        )
        flow = self.new_flow(
            FakeResponse(200, {"device_token": "tok-1"}),
            FakeResponse(200, dict(CREDENTIALS)),
        )
        flow.source = "reauth"
        flow.entry = entry

        form = await flow.async_step_reauth(entry.data)
        self.assertEqual((form["type"], form["step_id"]), ("form", "reauth_confirm"))
        result = await flow.async_step_reauth_confirm({})
        placeholders = result["description_placeholders"]
        self.assertEqual(placeholders["url"], "https://control.example")
        await result["progress_task"]
        self.assertEqual((await flow.async_step_claim())["step_id"], "finish")

        finished = await flow.async_step_finish()
        self.assertEqual(finished, {"type": "abort", "reason": "reauth_successful"})
        self.assertEqual(entry.data["mqtt_username"], "device-dnew")
        self.assertEqual(entry.data["mqtt_password"], "new-pw")
        self.assertEqual(entry.data["device_id"], "dnew")
        self.assertEqual(entry.data["remote_config"], {})
        self.assertIs(entry.data["mqtt_broker_manual"], False)
        # The identity of the entry (and with it the Home Assistant device,
        # its entities and the automations) stays the same.
        self.assertEqual(entry.unique_id, "dev-1")
        self.assertEqual(entry.data["device_name"], "Home Assistant")

    async def test_reauth_announce_error(self) -> None:
        flow = self.new_flow(OSError("down"))
        flow.source = "reauth"
        flow.entry = stubs.FakeEntry(data=dict(ENTRY_DATA))
        result = await flow.async_step_reauth_confirm({})
        self.assertEqual((result["step_id"], result["errors"]), ("reauth_confirm", {"base": "cannot_connect"}))

    async def test_setup_after_reauth_reuses_the_device(self) -> None:
        # The entry data now holds another device_id; the registry device
        # must still be found through the entry's unique id.
        first, runtime, _ = await self.start()
        await runtime.async_stop()
        second = self.make_entry(data={"device_id": "dnew", "mqtt_username": "device-dnew"})
        self.assertTrue(await integration.async_setup_entry(self.hass, second))
        self.assertEqual(second.runtime_data.device_id, "registry-1")
        self.assertEqual(len(self.hass.data["device_registry"].devices), 1)

    async def test_reconfigure(self) -> None:
        entry = stubs.FakeEntry(data=dict(ENTRY_DATA))
        flow = self.new_flow(FakeResponse(500))
        flow.source = "reconfigure"
        flow.entry = entry

        form = await flow.async_step_reconfigure()
        self.assertEqual((form["type"], form["step_id"], form["errors"]), ("form", "reconfigure", {}))

        for bad in ("http://example.org", "example.org:1883", "tcp://"):
            with self.subTest(bad=bad):
                result = await flow.async_step_reconfigure({"mqtt_broker": bad})
                self.assertEqual(result["errors"], {"base": "invalid_broker"})
        self.assertEqual(entry.data["mqtt_broker"], ENTRY_DATA["mqtt_broker"])

        result = await flow.async_step_reconfigure({"mqtt_broker": " wss://example.org/mqtt "})
        self.assertEqual(result, {"type": "abort", "reason": "reconfigure_successful"})
        self.assertEqual(entry.data["mqtt_broker"], "wss://example.org/mqtt")
        self.assertIs(entry.data["mqtt_broker_manual"], True)

        # Empty: back to the address announced by the server.
        result = await flow.async_step_reconfigure({})
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(entry.data["mqtt_broker"], "")
        self.assertIs(entry.data["mqtt_broker_manual"], False)

    async def test_options_flow(self) -> None:
        entry = stubs.FakeEntry(
            data=dict(ENTRY_DATA), options={"device_name": "Desk", "idle_timeout": 45}
        )
        flow = config_flow.EmotionKitConfigFlow.async_get_options_flow(entry)
        self.assertIsInstance(flow, stubs.OptionsFlowWithReload)
        flow.hass = self.hass
        flow.config_entry = entry

        form = await flow.async_step_init()
        self.assertEqual((form["type"], form["step_id"]), ("form", "init"))
        schema = form["data_schema"]
        self.assertEqual(schema({}), {"idle_timeout": 45, "react_to_all_players": False})
        self.assertEqual(
            schema({"idle_timeout": "20", "react_to_all_players": True}),
            {"idle_timeout": 20, "react_to_all_players": True},
        )
        for bad in (-1, 3601, "soon"):
            with self.assertRaises(stubs.Invalid):
                schema({"idle_timeout": bad})

        result = await flow.async_step_init({"idle_timeout": 10, "react_to_all_players": True})
        self.assertEqual(result["type"], "create_entry")
        # What is not in the form (a name set in the web UI) is kept.
        self.assertEqual(
            result["data"],
            {"device_name": "Desk", "idle_timeout": 10, "react_to_all_players": True},
        )


class SourceChecksTest(unittest.TestCase):
    """Things about the glue code that are cheap to pin down statically."""

    def source(self, name: str) -> str:
        return (PACKAGE_DIR / name).read_text(encoding="utf-8")

    def test_runtime_data_instead_of_hass_data(self) -> None:
        for name in ("__init__.py", "coordinator.py", "sensor.py", "event.py", "binary_sensor.py"):
            self.assertNotIn("hass.data[DOMAIN]", self.source(name), name)
        self.assertIn("entry.runtime_data = runtime", self.source("__init__.py"))

    def test_no_update_listener(self) -> None:
        # An update listener together with OptionsFlowWithReload or
        # async_update_reload_and_abort is an error in current Home Assistant.
        for path in PACKAGE_DIR.glob("*.py"):
            self.assertNotIn("add_update_listener", path.read_text(encoding="utf-8"), path.name)

    def test_no_private_client_session(self) -> None:
        for path in PACKAGE_DIR.glob("*.py"):
            self.assertIsNone(
                re.search(r"aiohttp\.ClientSession\(", path.read_text(encoding="utf-8")), path.name
            )

    def test_timer_callbacks_run_in_the_event_loop(self) -> None:
        # async_call_later runs a plain function in the executor unless it
        # is marked as a callback - where hass.bus.async_fire then raises.
        source = self.source("coordinator.py")
        match = re.search(r"((?:    @\w+\n)*)    def _async_timer_fired\(", source)
        assert match is not None
        self.assertIn("@callback", match.group(1))


if __name__ == "__main__":
    unittest.main()
