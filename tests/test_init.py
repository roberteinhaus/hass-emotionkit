"""The integration's original test file, moved out of the shipped package.

It used to live next to the code (and was installed on every user's system)
and needed hand-made stubs for aiomqtt and Home Assistant to import
__init__.py. The functions it tested have since moved into plain-Python
modules, so the stubs are gone; _support registers the package without
executing __init__.py.

The test names are the original ones. Each test checks the same behaviour as
before, except where the behaviour was a defect (noted in the test).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

import _support  # noqa: F401
from _support import envelope, event, raw

from custom_components.emotionkit.broker import (
    InvalidBrokerURL,
    derive_broker_url,
    parse_broker_url,
)
from custom_components.emotionkit.const import (
    ALL_EVENT_TYPES,
    DEFAULT_IDLE_TIMEOUT,
    EVENT_IDLE,
)
from custom_components.emotionkit.engine import EventProcessor
from custom_components.emotionkit.protocol import build_status_payload


def _status(processor: EventProcessor, name: str, idle_timeout: int) -> dict:
    return json.loads(
        build_status_payload(
            device_id="device-1",
            device_name=name,
            enabled=processor.enabled,
            idle_timeout=idle_timeout,
            allowed_subjects=processor.allowed_subjects,
        )
    )


class OriginalTests(unittest.TestCase):
    def test_parse_broker_url_tcp(self) -> None:
        address = parse_broker_url("tcp://mqtt.example.com:1883")
        self.assertEqual(address.host, "mqtt.example.com")
        self.assertEqual(address.port, 1883)
        self.assertIs(address.tls, False)

    def test_parse_broker_url_tls(self) -> None:
        address = parse_broker_url("tls://mqtt.example.com:8883")
        self.assertEqual(address.host, "mqtt.example.com")
        self.assertEqual(address.port, 8883)
        self.assertIs(address.tls, True)

    def test_parse_broker_url_empty_fallback(self) -> None:
        # Was: an empty URL silently meant localhost:1883 without TLS. That
        # fallback was a defect; an empty URL is now an error and the caller
        # asks the server for the address instead.
        with self.assertRaises(InvalidBrokerURL):
            parse_broker_url("")

    def test_derive_broker_url_https(self) -> None:
        self.assertEqual(derive_broker_url("https://emotionkit.de"), "tls://emotionkit.de:8883")

    def test_derive_broker_url_http(self) -> None:
        self.assertEqual(derive_broker_url("http://localhost:8085"), "tcp://localhost:1883")

    def test_handle_config_sets_config_received_and_allowed_subjects(self) -> None:
        processor = EventProcessor()
        self.assertFalse(processor.config_received)
        processor.handle_config(
            raw(
                {
                    "enabled": False,
                    "allowed_subjects": {"steam:owner123": "owner", "steam:user456": "user"},
                }
            ),
            retained=True,
        )
        self.assertTrue(processor.config_received)
        self.assertIs(processor.enabled, False)
        self.assertEqual(
            processor.allowed_subjects,
            {"steam:owner123": "owner", "steam:user456": "user"},
        )

    def test_handle_config_accepts_legacy_allowed_subjects_list(self) -> None:
        processor = EventProcessor()
        processor.handle_config(
            raw({"allowed_subjects": ["steam:player1", "steam:player2"]}), retained=True
        )
        self.assertTrue(processor.config_received)
        self.assertEqual(
            processor.allowed_subjects,
            {"steam:player1": "user", "steam:player2": "user"},
        )

    def test_game_state_config_received_starts_false(self) -> None:
        self.assertFalse(EventProcessor().config_received)

    def test_idle_event_is_configurable(self) -> None:
        self.assertEqual(DEFAULT_IDLE_TIMEOUT, 30)
        self.assertIn(EVENT_IDLE, ALL_EVENT_TYPES)

    def test_build_status_payload_contains_handshake_fields(self) -> None:
        processor = EventProcessor()
        processor.handle_config(
            raw({"enabled": True, "allowed_subjects": {"steam:owner123": "owner"}}),
            retained=True,
        )
        payload = _status(processor, "EmotionKit Test", 45)

        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(payload["name"], "EmotionKit Test")
        self.assertIs(payload["online"], True)

        self.assertIs(payload["config"]["enabled"], True)
        self.assertEqual(payload["config"]["idle_timeout_s"], 45)
        self.assertEqual(payload["config"]["allowed_subjects"], {"steam:owner123": "owner"})

        schema_keys = {field["key"] for field in payload["config_schema"]}
        self.assertTrue({"actor_name", "enabled", "idle_timeout_s"}.issubset(schema_keys))

        caps = payload["capabilities"]
        self.assertEqual(caps["protocol_version"], "1.0")
        self.assertIs(caps["supports_idle"], True)
        self.assertIn("ha-trigger", caps["intent_types"])

    def test_build_status_payload_reflects_disabled_state(self) -> None:
        processor = EventProcessor()
        processor.handle_config(raw({"enabled": False}), retained=True)
        payload = _status(processor, "EmotionKit Test", 0)
        self.assertIs(payload["config"]["enabled"], False)
        self.assertEqual(payload["config"]["idle_timeout_s"], 0)

    def test_extract_events_ignores_stale_event(self) -> None:
        # Was: an event whose occurred_at lies 40 s before the local wall
        # clock is dropped - which dropped *everything* on a host whose
        # clock runs 20 s ahead. An event is now stale when it arrives late
        # relative to the other events, whatever the clocks say.
        processor = EventProcessor()
        start = datetime(2026, 11, 18, 20, 0, 0, tzinfo=timezone.utc)
        processor.handle_event(
            raw(envelope(phase="freezetime", occurred_at=start.isoformat())), monotonic=100.0
        )
        late = envelope(
            event("round_live", round=1),
            phase="live",
            occurred_at=(start + timedelta(seconds=5)).isoformat(),
        )
        result = processor.handle_event(raw(late), monotonic=145.0)  # 40 s late
        self.assertTrue(result.stale)
        self.assertEqual(result.triggers, ())

    def test_extract_events_processes_fresh_event(self) -> None:
        processor = EventProcessor()
        occurred_at = datetime.now(timezone.utc).isoformat()
        result = processor.handle_event(
            raw(envelope(event("round_live", round=1), phase="live", occurred_at=occurred_at)),
            monotonic=100.0,
        )
        self.assertEqual(len(result.triggers), 1)
        self.assertEqual(result.triggers[0].type, "round_live")

    def test_extract_events_survives_a_skewed_clock(self) -> None:
        # New: the scenario the old stale filter failed on. occurred_at is
        # 40 s behind "now" for every event, i.e. the local clock is ahead.
        processor = EventProcessor()
        skewed = datetime.now(timezone.utc) - timedelta(seconds=40)
        for index in range(3):
            stamp = (skewed + timedelta(seconds=5 * index)).isoformat()
            result = processor.handle_event(
                raw(
                    envelope(
                        event("player_kill", subject=_support.OWNER, value=1, round=index + 1),
                        occurred_at=stamp,
                    )
                ),
                monotonic=100.0 + 5 * index,
            )
            self.assertFalse(result.stale)
            self.assertEqual(len(result.triggers), 1)


if __name__ == "__main__":
    unittest.main()
