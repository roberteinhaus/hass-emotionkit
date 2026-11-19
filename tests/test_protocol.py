"""Envelope, config and status formats."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest

import _support
from _support import MATE, OWNER, envelope, event, raw

from custom_components.emotionkit.protocol import (
    Envelope,
    build_offline_payload,
    build_status_payload,
    owner_of,
    parse_device_config,
    parse_envelope,
    parse_timestamp,
)


class ParseTimestampTest(unittest.TestCase):
    def test_go_timestamps(self) -> None:
        expected = datetime(2026, 11, 18, 20, 15, 4, 123000, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(parse_timestamp("2026-11-18T20:15:04.123Z"), expected, places=4)
        # Go writes nanoseconds.
        self.assertAlmostEqual(
            parse_timestamp("2026-11-18T20:15:04.123456789Z"), expected + 0.000456, places=4
        )
        self.assertAlmostEqual(
            parse_timestamp("2026-11-18T20:15:04Z"), expected - 0.123, places=4
        )

    def test_offsets(self) -> None:
        utc = parse_timestamp("2026-11-18T20:15:04Z")
        self.assertEqual(parse_timestamp("2026-11-18T21:15:04+01:00"), utc)
        self.assertEqual(parse_timestamp("2026-11-18T21:15:04+0100"), utc)
        self.assertEqual(parse_timestamp("2026-11-18T15:15:04-05:00"), utc)
        # No offset means UTC.
        self.assertEqual(parse_timestamp("2026-11-18 20:15:04"), utc)

    def test_garbage_is_none(self) -> None:
        for value in (None, "", "yesterday", 1700000000, "2026-13-40T99:99:99Z", [], {}):
            with self.subTest(value=value):
                self.assertIsNone(parse_timestamp(value))


class ParseEnvelopeTest(unittest.TestCase):
    def test_documented_envelope(self) -> None:
        # The example of docs/protocol/device-events-v1.md.
        message = {
            "id": "d1k2",
            "team_id": "team-1",
            "player_id": "team-1:76561198000000001",
            "subject": "76561198000000001",
            "player_side": "CT",
            "occurred_at": "2026-11-18T20:15:04.123Z",
            "match_id": "d1k0",
            "match_fingerprint": "d1k0",
            "events": [
                {"type": "bomb_planted", "at": "2026-11-18T20:15:04.123Z", "round": 7}
            ],
            "payload": {"map": {"name": "de_mirage"}, "round": {"phase": "live"}, "player": {}},
        }
        env = parse_envelope(raw(message))
        assert env is not None
        self.assertEqual(env.id, "d1k2")
        self.assertEqual(env.subject, "76561198000000001")
        self.assertEqual(env.player_side, "CT")
        self.assertEqual(env.match_id, "d1k0")
        self.assertIsNotNone(env.occurred_ts)
        self.assertEqual(len(env.events), 1)
        self.assertEqual(env.events[0].type, "bomb_planted")
        self.assertEqual(env.events[0].round, 7)
        self.assertFalse(env.events[0].player_scoped)
        self.assertEqual(env.map_name, "de_mirage")
        self.assertTrue(env.in_match)
        self.assertEqual(env.match_key, "d1k0")

    def test_accepts_bytes_bytearray_str_and_dict(self) -> None:
        message = envelope(event("round_live", round=1))
        encoded = json.dumps(message)
        for form in (encoded.encode(), bytearray(encoded.encode()), encoded, message):
            with self.subTest(type=type(form).__name__):
                env = parse_envelope(form)
                assert env is not None
                self.assertEqual(env.events[0].type, "round_live")

    def test_not_an_object(self) -> None:
        for bad in (b"", b"not json", b"[1, 2]", b'"text"', b"42", b"\xff\xfe", None, 3.5):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_envelope(bad))

    def test_missing_fields_do_not_raise(self) -> None:
        env = parse_envelope(b"{}")
        assert env is not None
        self.assertEqual(env, Envelope())
        self.assertEqual(env.events, ())
        self.assertFalse(env.in_match)
        self.assertEqual(env.match_key, "")
        self.assertIsNone(env.occurred_ts)
        self.assertFalse(env.own_view)

    def test_wrong_types_do_not_raise(self) -> None:
        env = parse_envelope(
            raw(
                {
                    "id": 7,
                    "subject": ["x"],
                    "player_side": None,
                    "occurred_at": 12,
                    "match_id": {"a": 1},
                    "events": "bomb_planted",
                    "payload": "nope",
                }
            )
        )
        assert env is not None
        self.assertEqual(env.id, "")
        self.assertEqual(env.subject, "")
        self.assertEqual(env.events, ())
        self.assertEqual(env.payload, {})
        self.assertEqual(env.map_name, "")

        env = parse_envelope(raw({"payload": {"map": "x", "round": 3, "player": [1]}}))
        assert env is not None
        self.assertEqual(env.map_block, {})
        self.assertEqual(env.round_block, {})
        self.assertEqual(env.player_block, {})

    def test_event_fields(self) -> None:
        env = parse_envelope(
            raw(
                envelope(
                    event("player_kill", subject=MATE, side="T", value=3, headshot=True, round=4),
                    event("round_over", round=4, side="CT", reason="defuse"),
                    event("player_flashed", subject=OWNER, value=200),
                )
            )
        )
        assert env is not None
        kill, over, flashed = env.events
        self.assertEqual((kill.subject, kill.side, kill.value, kill.headshot, kill.round), (MATE, "T", 3, True, 4))
        self.assertTrue(kill.player_scoped)
        self.assertEqual((over.side, over.reason, over.subject), ("CT", "defuse", ""))
        self.assertIsNone(over.value)
        self.assertFalse(over.headshot)
        self.assertEqual(flashed.value, 200)

    def test_malformed_events_are_skipped(self) -> None:
        message = envelope()
        message["events"] = [
            "bomb_planted",
            None,
            {"round": 3},
            {"type": ""},
            {"type": 5},
            {"type": "round_live", "round": "seven", "value": True, "headshot": "yes"},
        ]
        env = parse_envelope(raw(message))
        assert env is not None
        self.assertEqual(len(env.events), 1)
        only = env.events[0]
        self.assertEqual(only.type, "round_live")
        self.assertIsNone(only.round)
        self.assertIsNone(only.value)  # a bool is not a number
        self.assertFalse(only.headshot)

    def test_subject_falls_back_to_player_id(self) -> None:
        env = parse_envelope(raw({"player_id": "team-1:7656", "payload": {}}))
        assert env is not None
        self.assertEqual(env.subject, "7656")

    def test_match_id_falls_back_to_fingerprint(self) -> None:
        env = parse_envelope(raw({"match_fingerprint": "fp-1"}))
        assert env is not None
        self.assertEqual(env.match_id, "fp-1")
        self.assertTrue(env.in_match)

    def test_menu_packet_is_not_in_match(self) -> None:
        env = parse_envelope(raw(_support.menu_envelope(event("match_left", subject=OWNER))))
        assert env is not None
        self.assertFalse(env.in_match)
        self.assertEqual(env.match_key, "")
        self.assertEqual(env.events[0].type, "match_left")
        self.assertEqual(env.events[0].subject, OWNER)

    def test_map_without_match_id_still_counts_as_match(self) -> None:
        env = parse_envelope(raw(envelope(match_id="")))
        assert env is not None
        self.assertTrue(env.in_match)
        self.assertEqual(env.match_key, "map:de_dust2")

    def test_own_view(self) -> None:
        own = parse_envelope(raw(envelope(subject=OWNER)))
        assert own is not None
        self.assertTrue(own.own_view)

        # Dead and spectating a teammate: player.steamid is the teammate's.
        spectating = parse_envelope(raw(envelope(subject=OWNER, steamid=MATE, provider=OWNER)))
        assert spectating is not None
        self.assertFalse(spectating.own_view)

        # Without a provider block the envelope's subject is the reference.
        message = envelope(subject=OWNER)
        del message["payload"]["provider"]
        env = parse_envelope(raw(message))
        assert env is not None
        self.assertTrue(env.own_view)
        message["payload"]["player"]["steamid"] = MATE
        env = parse_envelope(raw(message))
        assert env is not None
        self.assertFalse(env.own_view)

        # No steamid at all: cannot be verified, so not trusted.
        del message["payload"]["player"]["steamid"]
        env = parse_envelope(raw(message))
        assert env is not None
        self.assertFalse(env.own_view)


class ParseDeviceConfigTest(unittest.TestCase):
    def test_full_config(self) -> None:
        config = parse_device_config(
            raw(
                {
                    "actor_name": "  Desk  ",
                    "enabled": False,
                    "idle_timeout_s": 45,
                    "allowed_subjects": {OWNER: "owner", MATE: "user"},
                }
            )
        )
        assert config is not None
        self.assertEqual(config.actor_name, "Desk")
        self.assertIs(config.enabled, False)
        self.assertEqual(config.idle_timeout_s, 45)
        self.assertEqual(config.allowed_subjects, {OWNER: "owner", MATE: "user"})

    def test_absent_keys_are_none(self) -> None:
        config = parse_device_config(b"{}")
        assert config is not None
        self.assertIsNone(config.enabled)
        self.assertIsNone(config.actor_name)
        self.assertIsNone(config.idle_timeout_s)
        self.assertIsNone(config.allowed_subjects)

    def test_invalid_values_are_ignored(self) -> None:
        config = parse_device_config(
            raw(
                {
                    "actor_name": "   ",
                    "enabled": "yes",
                    "idle_timeout_s": 99999,
                    "allowed_subjects": "everyone",
                }
            )
        )
        assert config is not None
        self.assertIsNone(config.enabled)
        self.assertIsNone(config.actor_name)
        self.assertIsNone(config.idle_timeout_s)
        self.assertEqual(config.allowed_subjects, {})

        for bad in (-1, True, "30", None, 3601):
            with self.subTest(idle_timeout_s=bad):
                config = parse_device_config(raw({"idle_timeout_s": bad}))
                assert config is not None
                self.assertIsNone(config.idle_timeout_s)

    def test_number_forms(self) -> None:
        # JSON numbers from Go may arrive as floats.
        config = parse_device_config(raw({"idle_timeout_s": 60.0}))
        assert config is not None
        self.assertEqual(config.idle_timeout_s, 60)
        config = parse_device_config(raw({"idle_timeout_s": 0}))
        assert config is not None
        self.assertEqual(config.idle_timeout_s, 0)

    def test_long_name_is_cut(self) -> None:
        config = parse_device_config(raw({"actor_name": "x" * 200}))
        assert config is not None
        self.assertEqual(len(config.actor_name or ""), 64)

    def test_allowed_subjects_forms(self) -> None:
        config = parse_device_config(
            raw({"allowed_subjects": {OWNER: " Owner ", "": "user", MATE: "", "x": 5}})
        )
        assert config is not None
        self.assertEqual(config.allowed_subjects, {OWNER: "owner"})

        # Legacy: a plain list.
        config = parse_device_config(raw({"allowed_subjects": [OWNER, "", 5, MATE]}))
        assert config is not None
        self.assertEqual(config.allowed_subjects, {OWNER: "user", MATE: "user"})

    def test_not_an_object(self) -> None:
        for bad in (b"", b"[]", b"nope", None):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_device_config(bad))

    def test_owner_of(self) -> None:
        self.assertEqual(owner_of({MATE: "admin", OWNER: "owner"}), OWNER)
        self.assertIsNone(owner_of({MATE: "admin"}))
        self.assertIsNone(owner_of({}))
        self.assertIsNone(owner_of(None))


class StatusPayloadTest(unittest.TestCase):
    def _status(self, **overrides: object) -> dict:
        args: dict = {
            "device_id": "dabc",
            "device_name": "EmotionKit Test",
            "enabled": True,
            "idle_timeout": 45,
            "allowed_subjects": {OWNER: "owner"},
        }
        args.update(overrides)
        return json.loads(build_status_payload(**args))

    def test_contains_handshake_fields(self) -> None:
        status = self._status()
        # Required by docs/protocol/device-capability-handshake-v1.md.
        for key in (
            "schema_version",
            "device_id",
            "name",
            "kind",
            "online",
            "config",
            "config_schema",
            "capabilities",
        ):
            self.assertIn(key, status)
        self.assertEqual(status["schema_version"], "1.0")
        self.assertEqual(status["name"], "EmotionKit Test")
        self.assertEqual(status["kind"], "ha-integration")
        self.assertIs(status["online"], True)
        self.assertEqual(
            status["config"],
            {
                "actor_name": "EmotionKit Test",
                "enabled": True,
                "idle_timeout_s": 45,
                "allowed_subjects": {OWNER: "owner"},
            },
        )
        self.assertEqual(status["capabilities"]["protocol_version"], "1.0")
        self.assertIs(status["capabilities"]["supports_idle"], True)
        self.assertIn("ha-trigger", status["capabilities"]["intent_types"])

    def test_every_advertised_setting_is_reported_and_typed(self) -> None:
        status = self._status()
        schema = {field["key"]: field for field in status["config_schema"]}
        self.assertEqual(set(schema), {"actor_name", "enabled", "idle_timeout_s"})
        for key in schema:
            self.assertIn(key, status["config"])
        self.assertEqual(schema["idle_timeout_s"]["min"], 0)
        self.assertEqual(schema["idle_timeout_s"]["max"], 3600)
        self.assertEqual(schema["actor_name"]["type"], "string")
        self.assertEqual(schema["enabled"]["type"], "bool")

    def test_reflects_disabled_state(self) -> None:
        status = self._status(enabled=False, idle_timeout=0)
        self.assertIs(status["config"]["enabled"], False)
        self.assertEqual(status["config"]["idle_timeout_s"], 0)

    def test_offline_payload(self) -> None:
        offline = json.loads(build_offline_payload(device_id="dabc", device_name="Desk"))
        self.assertIs(offline["online"], False)
        self.assertEqual(offline["name"], "Desk")
        self.assertEqual(offline["kind"], "ha-integration")

    def test_no_credentials_in_status(self) -> None:
        text = build_status_payload(
            device_id="dabc",
            device_name="Desk",
            enabled=True,
            idle_timeout=30,
            allowed_subjects={},
        )
        self.assertNotIn("password", text)
        self.assertNotIn("mqtt_", text)


if __name__ == "__main__":
    unittest.main()
