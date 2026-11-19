"""Message processing end to end (without Home Assistant)."""

from __future__ import annotations

from typing import Any
import unittest

import _support  # noqa: F401
from _support import MATE, OWNER, envelope, event, menu_envelope, raw

from custom_components.emotionkit.engine import (
    IGNORED_DISABLED,
    IGNORED_DUPLICATE,
    IGNORED_INVALID,
    REMOTE_ACTOR_NAME,
    REMOTE_IDLE_TIMEOUT,
    ActivityWatchdog,
    EventProcessor,
    EventResult,
    clean_remote_baseline,
    reconcile_remote_config,
)
from custom_components.emotionkit.protocol import DeviceConfig
from custom_components.emotionkit.state import IDLE_SNAPSHOT

OWNER_CONFIG = {"enabled": True, "allowed_subjects": {OWNER: "owner", MATE: "user"}}


class ProcessorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.processor = EventProcessor()
        self.processor.handle_config(raw(OWNER_CONFIG), retained=True)
        self.now = 500.0

    def feed(self, message: dict[str, Any], *, dt: float = 1.0) -> EventResult:
        self.now += dt
        return self.processor.handle_event(raw(message), monotonic=self.now)

    @staticmethod
    def types(result: EventResult) -> list[str]:
        return [trigger.type for trigger in result.triggers]


class EventHandlingTest(ProcessorTestCase):
    def test_events_become_triggers(self) -> None:
        result = self.feed(envelope(event("bomb_planted", round=7), bomb="planted", ct=3, t=3))
        self.assertEqual(self.types(result), ["bomb_planted"])
        self.assertTrue(result.in_match)
        self.assertTrue(result.state_changed)
        self.assertFalse(result.stale)
        self.assertIsNone(result.ignored)
        self.assertEqual(result.triggers[0].data["round"], 7)
        self.assertTrue(self.processor.snapshot.bomb_planted)
        self.assertEqual(self.processor.last_trigger_type, "bomb_planted")
        self.assertEqual(self.processor.last_trigger_at, "2026-11-18T20:15:04.123Z")

    def test_no_trigger_without_events(self) -> None:
        # The first version derived triggers from payload.round; a restart
        # in the middle of a round must not fire "round_live" any more.
        result = self.feed(envelope(phase="live", bomb="planted"))
        self.assertEqual(result.triggers, ())
        self.assertTrue(result.in_match)
        self.assertTrue(self.processor.snapshot.bomb_planted)

    def test_several_players_one_trigger(self) -> None:
        # The server reports a transition once; every other player's packet
        # shows the same state without events.
        first = self.feed(envelope(event("round_live", round=2), subject=OWNER, phase="live"))
        second = self.feed(envelope(subject=MATE, phase="live"))
        third = self.feed(envelope(subject=OWNER, phase="live"))
        self.assertEqual(self.types(first), ["round_live"])
        self.assertEqual(second.triggers, ())
        self.assertEqual(third.triggers, ())

    def test_repeated_server_event_fires_once(self) -> None:
        self.feed(envelope(event("round_live", round=2), phase="live"))
        first = self.feed(envelope(event("bomb_planted", round=2), bomb="planted"))
        self.feed(envelope(subject=MATE, phase="live"))
        again = self.feed(envelope(event("bomb_planted", round=2), bomb="planted", subject=MATE))
        self.assertEqual(self.types(first), ["bomb_planted"])
        self.assertEqual(again.triggers, ())
        self.assertEqual(self.processor.stats["duplicate_events"], 1)

    def test_redelivered_message_is_dropped(self) -> None:
        message = envelope(event("player_kill", subject=OWNER, value=1), envelope_id="same")
        first = self.feed(message)
        again = self.feed(message)
        self.assertEqual(self.types(first), ["player_kill"])
        self.assertEqual(again.triggers, ())
        self.assertEqual(again.ignored, IGNORED_DUPLICATE)
        self.assertEqual(self.processor.stats["duplicate_messages"], 1)

    def test_messages_without_id_are_not_deduplicated(self) -> None:
        message = envelope(event("player_kill", subject=OWNER, value=1), envelope_id="")
        self.assertEqual(len(self.feed(message).triggers), 1)
        self.assertEqual(len(self.feed(message).triggers), 1)

    def test_id_memory_is_bounded(self) -> None:
        for index in range(1000):
            self.feed(envelope(envelope_id=f"id-{index}"))
        self.assertLessEqual(len(self.processor._recent_id_set), 256)  # noqa: SLF001
        self.assertEqual(
            len(self.processor._recent_ids), len(self.processor._recent_id_set)  # noqa: SLF001
        )
        # An old id is accepted again, a recent one is not.
        self.assertIsNone(self.feed(envelope(envelope_id="id-0")).ignored)
        self.assertEqual(self.feed(envelope(envelope_id="id-999")).ignored, IGNORED_DUPLICATE)

    def test_invalid_messages(self) -> None:
        for bad in (b"", b"not json", b"[]", None):
            with self.subTest(bad=bad):
                result = self.processor.handle_event(bad, monotonic=1.0)
                self.assertEqual(result.ignored, IGNORED_INVALID)
                self.assertEqual(result.triggers, ())
                self.assertFalse(result.in_match)
        self.assertEqual(self.processor.stats["invalid"], 4)

    def test_empty_object_is_harmless(self) -> None:
        result = self.processor.handle_event(b"{}", monotonic=1.0)
        self.assertIsNone(result.ignored)
        self.assertEqual(result.triggers, ())
        self.assertFalse(result.in_match)
        self.assertFalse(result.state_changed)


class OwnerFilterTest(ProcessorTestCase):
    def test_player_triggers_for_the_owner_only(self) -> None:
        result = self.feed(
            envelope(
                event("player_kill", subject=MATE, value=1),
                event("player_flashed", subject=OWNER, value=200),
                subject=MATE,
            )
        )
        self.assertEqual(self.types(result), ["player_flashed"])

    def test_react_to_all_players(self) -> None:
        self.processor.all_players = True
        result = self.feed(
            envelope(event("player_kill", subject=MATE, value=1, headshot=True), subject=MATE)
        )
        self.assertEqual(self.types(result), ["player_kill", "player_headshot"])

    def test_owner_unknown_until_the_server_says_so(self) -> None:
        processor = EventProcessor()
        self.assertIsNone(processor.owner)
        result = processor.handle_event(
            raw(envelope(event("player_death", subject=MATE), subject=MATE)), monotonic=1.0
        )
        self.assertEqual([t.type for t in result.triggers], ["player_death"])

        processor.handle_config(raw(OWNER_CONFIG), retained=True)
        self.assertEqual(processor.owner, OWNER)
        result = processor.handle_event(
            raw(envelope(event("player_death", subject=MATE), subject=MATE)), monotonic=2.0
        )
        self.assertEqual(result.triggers, ())

    def test_owner_follows_config_updates(self) -> None:
        self.processor.handle_config(
            raw({"allowed_subjects": {MATE: "owner"}}), retained=False
        )
        self.assertEqual(self.processor.owner, MATE)
        # A config message without the key keeps what is known.
        self.processor.handle_config(raw({"enabled": True}), retained=False)
        self.assertEqual(self.processor.owner, MATE)

    def test_match_won_and_lost_from_the_owners_side(self) -> None:
        self.feed(envelope(subject=OWNER, side="T"))
        self.feed(envelope(subject=MATE, side="T"))
        won = self.feed(envelope(event("match_over", side="T"), subject=MATE, side="T"))
        self.assertEqual(self.types(won), ["match_over", "match_won"])

    def test_match_lost(self) -> None:
        result = self.feed(envelope(event("match_over", side="T"), subject=OWNER, side="CT"))
        self.assertEqual(self.types(result), ["match_over", "match_lost"])

    def test_match_result_needs_the_owner_in_the_match(self) -> None:
        result = self.feed(envelope(event("match_over", side="T"), subject=MATE, side="T"))
        self.assertEqual(self.types(result), ["match_over"])
        # ... unless the device reacts to all players.
        processor = EventProcessor(all_players=True)
        processor.handle_config(raw(OWNER_CONFIG), retained=True)
        result = processor.handle_event(
            raw(envelope(event("match_over", side="T"), subject=MATE, side="T")), monotonic=1.0
        )
        self.assertEqual([t.type for t in result.triggers], ["match_over", "match_won"])


class LeavingTest(ProcessorTestCase):
    def test_match_left(self) -> None:
        self.feed(envelope(event("round_live", round=1), phase="live"))
        result = self.feed(menu_envelope(event("match_left", subject=OWNER)))
        self.assertEqual(self.types(result), ["match_left"])
        self.assertFalse(result.in_match)  # menu packets do not restart the idle timer
        self.assertTrue(result.state_changed)
        self.assertEqual(self.processor.snapshot, IDLE_SNAPSHOT)
        self.assertNotIn("resync", result.triggers[0].data)

    def test_menu_heartbeats_do_nothing(self) -> None:
        self.feed(envelope())
        self.feed(menu_envelope(event("match_left", subject=OWNER)))
        for _ in range(5):
            result = self.feed(menu_envelope())
            self.assertEqual(result.triggers, ())
            self.assertFalse(result.in_match)
            self.assertFalse(result.state_changed)

    def test_mate_leaving_does_not_trigger_for_the_owner(self) -> None:
        self.feed(envelope(subject=OWNER))
        self.feed(envelope(subject=MATE))
        result = self.feed(menu_envelope(event("match_left", subject=MATE), subject=MATE))
        self.assertEqual(result.triggers, ())
        self.assertTrue(self.processor.snapshot.in_match)

    def test_missed_match_left_is_made_up_for(self) -> None:
        self.feed(envelope(subject=OWNER))
        result = self.feed(menu_envelope())
        self.assertEqual(self.types(result), ["match_left"])
        self.assertIs(result.triggers[0].data["resync"], True)
        self.assertEqual(result.triggers[0].data["subject"], OWNER)


class ResyncAndStalenessTest(ProcessorTestCase):
    def test_missed_transition_fires_late_and_flagged(self) -> None:
        self.feed(envelope(event("round_live", round=3), phase="live", ct=1, t=1))
        result = self.feed(envelope(phase="live", bomb="planted", ct=1, t=1), dt=6.0)
        self.assertEqual(self.types(result), ["bomb_planted"])
        self.assertIs(result.triggers[0].data["resync"], True)
        self.assertEqual(self.processor.stats["resync_triggers"], 1)

    def test_stale_message_updates_state_but_triggers_nothing(self) -> None:
        self.feed(envelope(event("round_live", round=3), phase="live", occurred_at="2026-11-18T20:00:00Z"))
        # Stamped 5 s later but delivered 60 s later.
        result = self.feed(
            envelope(
                event("bomb_planted", round=3),
                bomb="planted",
                occurred_at="2026-11-18T20:00:05Z",
            ),
            dt=60.0,
        )
        self.assertTrue(result.stale)
        self.assertEqual(result.triggers, ())
        self.assertTrue(result.in_match)
        self.assertTrue(self.processor.snapshot.bomb_planted)
        self.assertEqual(self.processor.stats["stale"], 1)

    def test_skewed_clock_does_not_silence_triggers(self) -> None:
        # occurred_at is hours away from anything local: irrelevant.
        for index, stamp in enumerate(
            ("1999-01-01T00:00:00Z", "1999-01-01T00:00:05Z", "1999-01-01T00:00:10Z")
        ):
            result = self.feed(
                envelope(event("player_kill", subject=OWNER, value=index + 1), occurred_at=stamp),
                dt=5.0,
            )
            self.assertFalse(result.stale)
            self.assertEqual(self.types(result), ["player_kill"])

    def test_missing_timestamp_is_not_stale(self) -> None:
        self.feed(envelope(phase="freezetime"))
        message = envelope(event("round_live", round=1))
        del message["occurred_at"]
        result = self.feed(message, dt=500.0)
        self.assertFalse(result.stale)
        self.assertEqual(self.types(result), ["round_live"])


class ConfigHandlingTest(unittest.TestCase):
    def test_sets_config_received_and_allowed_subjects(self) -> None:
        processor = EventProcessor()
        self.assertFalse(processor.config_received)
        result = processor.handle_config(
            raw({"enabled": False, "allowed_subjects": {OWNER: "owner", MATE: "user"}}),
            retained=True,
        )
        self.assertTrue(result.valid)
        self.assertTrue(processor.config_received)
        self.assertFalse(processor.enabled)
        self.assertTrue(result.enabled_changed)
        self.assertEqual(processor.allowed_subjects, {OWNER: "owner", MATE: "user"})

    def test_accepts_legacy_allowed_subjects_list(self) -> None:
        processor = EventProcessor()
        processor.handle_config(raw({"allowed_subjects": [OWNER, MATE]}), retained=True)
        self.assertEqual(processor.allowed_subjects, {OWNER: "user", MATE: "user"})
        self.assertIsNone(processor.owner)

    def test_invalid_config_changes_nothing(self) -> None:
        processor = EventProcessor()
        result = processor.handle_config(b"garbage", retained=False)
        self.assertFalse(result.valid)
        self.assertFalse(processor.config_received)
        self.assertTrue(processor.enabled)

    def test_disabled_device_ignores_events_and_goes_idle(self) -> None:
        processor = EventProcessor()
        processor.handle_event(raw(envelope(event("round_live", round=1))), monotonic=1.0)
        self.assertTrue(processor.snapshot.in_match)

        result = processor.handle_config(raw({"enabled": False}), retained=False)
        self.assertTrue(result.enabled_changed)
        self.assertTrue(result.state_changed)
        self.assertEqual(processor.snapshot, IDLE_SNAPSHOT)

        ignored = processor.handle_event(
            raw(envelope(event("bomb_planted", round=1), bomb="planted")), monotonic=2.0
        )
        self.assertEqual(ignored.ignored, IGNORED_DISABLED)
        self.assertEqual(ignored.triggers, ())
        self.assertFalse(ignored.in_match)
        self.assertEqual(processor.snapshot, IDLE_SNAPSHOT)

        result = processor.handle_config(raw({"enabled": True}), retained=False)
        self.assertTrue(result.enabled_changed)
        # Re-enabled in the middle of a round: adopted silently.
        resumed = processor.handle_event(
            raw(envelope(phase="live", bomb="planted")), monotonic=3.0
        )
        self.assertEqual(resumed.triggers, ())
        self.assertTrue(processor.snapshot.bomb_planted)

    def test_repeating_the_same_enabled_value_is_no_change(self) -> None:
        processor = EventProcessor()
        result = processor.handle_config(raw({"enabled": True}), retained=True)
        self.assertFalse(result.enabled_changed)
        self.assertFalse(result.state_changed)


class RemoteSettingsTest(unittest.TestCase):
    """actor_name and idle_timeout_s are advertised, so they must be applied."""

    def test_change_in_the_web_ui_is_applied(self) -> None:
        processor = EventProcessor(remote_baseline={"actor_name": "Desk", "idle_timeout_s": 30})
        result = processor.handle_config(
            raw({"actor_name": "Desk", "idle_timeout_s": 120}), retained=False
        )
        self.assertEqual(result.updates, {REMOTE_IDLE_TIMEOUT: 120})
        self.assertTrue(result.baseline_changed)
        self.assertEqual(
            processor.remote_baseline, {"actor_name": "Desk", "idle_timeout_s": 120}
        )

        result = processor.handle_config(
            raw({"actor_name": "Living room", "idle_timeout_s": 120}), retained=False
        )
        self.assertEqual(result.updates, {REMOTE_ACTOR_NAME: "Living room"})

    def test_retained_message_does_not_undo_a_local_change(self) -> None:
        # The user set 60 s in Home Assistant; the retained config still
        # says 30 s, which is what the server said last time as well.
        processor = EventProcessor(remote_baseline={"actor_name": "Desk", "idle_timeout_s": 30})
        result = processor.handle_config(
            raw({"actor_name": "Desk", "idle_timeout_s": 30}), retained=True
        )
        self.assertEqual(result.updates, {})
        self.assertFalse(result.baseline_changed)

    def test_change_made_while_offline_is_applied_on_reconnect(self) -> None:
        processor = EventProcessor(remote_baseline={"actor_name": "Desk", "idle_timeout_s": 30})
        result = processor.handle_config(
            raw({"actor_name": "Desk", "idle_timeout_s": 45}), retained=True
        )
        self.assertEqual(result.updates, {REMOTE_IDLE_TIMEOUT: 45})

    def test_existing_installation_keeps_its_settings_on_update(self) -> None:
        # No baseline at all: the entry predates this version. The retained
        # config is merely remembered.
        processor = EventProcessor(remote_baseline=None)
        result = processor.handle_config(
            raw({"actor_name": "Name from the web", "idle_timeout_s": 30}), retained=True
        )
        self.assertEqual(result.updates, {})
        self.assertTrue(result.baseline_changed)
        self.assertEqual(
            processor.remote_baseline,
            {"actor_name": "Name from the web", "idle_timeout_s": 30},
        )
        # From now on changes are recognised.
        result = processor.handle_config(
            raw({"actor_name": "Name from the web", "idle_timeout_s": 10}), retained=False
        )
        self.assertEqual(result.updates, {REMOTE_IDLE_TIMEOUT: 10})

    def test_existing_installation_live_change_is_applied(self) -> None:
        processor = EventProcessor(remote_baseline=None)
        result = processor.handle_config(raw({"idle_timeout_s": 10}), retained=False)
        self.assertEqual(result.updates, {REMOTE_IDLE_TIMEOUT: 10})

    def test_freshly_claimed_device_takes_the_name_from_the_web_ui(self) -> None:
        # The flow stores an empty baseline for a new entry.
        processor = EventProcessor(remote_baseline={})
        result = processor.handle_config(
            raw({"actor_name": "Living room", "allowed_subjects": {OWNER: "owner"}}),
            retained=True,
        )
        self.assertEqual(result.updates, {REMOTE_ACTOR_NAME: "Living room"})
        self.assertEqual(processor.remote_baseline, {"actor_name": "Living room"})

    def test_invalid_remote_values_are_ignored(self) -> None:
        processor = EventProcessor(remote_baseline={"idle_timeout_s": 30})
        result = processor.handle_config(
            raw({"actor_name": "", "idle_timeout_s": -5}), retained=False
        )
        self.assertEqual(result.updates, {})
        self.assertFalse(result.baseline_changed)

    def test_reconcile_is_pure(self) -> None:
        baseline = {"idle_timeout_s": 30}
        updates, new_baseline = reconcile_remote_config(
            DeviceConfig(idle_timeout_s=40), baseline, retained=False
        )
        self.assertEqual(updates, {"idle_timeout_s": 40})
        self.assertEqual(new_baseline, {"idle_timeout_s": 40})
        self.assertEqual(baseline, {"idle_timeout_s": 30})

    def test_clean_remote_baseline(self) -> None:
        self.assertIsNone(clean_remote_baseline(None))
        self.assertIsNone(clean_remote_baseline("x"))
        self.assertEqual(clean_remote_baseline({}), {})
        self.assertEqual(
            clean_remote_baseline(
                {"actor_name": " Desk ", "idle_timeout_s": 30.0, "junk": 1}
            ),
            {"actor_name": "Desk", "idle_timeout_s": 30},
        )
        self.assertEqual(
            clean_remote_baseline({"actor_name": 5, "idle_timeout_s": "30"}), {}
        )
        self.assertEqual(clean_remote_baseline({"idle_timeout_s": 99999}), {})


class ActivityWatchdogTest(unittest.TestCase):
    def test_nothing_happens_without_activity(self) -> None:
        watchdog = ActivityWatchdog(30, 90)
        self.assertIsNone(watchdog.next_deadline())
        self.assertEqual(watchdog.poll(10_000.0), (False, False))

    def test_idle_then_expiry(self) -> None:
        watchdog = ActivityWatchdog(30, 90)
        watchdog.touch(100.0)
        self.assertEqual(watchdog.next_deadline(), 130.0)
        self.assertEqual(watchdog.poll(129.9), (False, False))
        self.assertEqual(watchdog.poll(130.0), (True, False))
        # The idle trigger fires once.
        self.assertEqual(watchdog.poll(131.0), (False, False))
        self.assertEqual(watchdog.next_deadline(), 190.0)
        self.assertEqual(watchdog.poll(190.0), (False, True))
        self.assertIsNone(watchdog.next_deadline())
        self.assertEqual(watchdog.poll(500.0), (False, False))

    def test_every_in_match_message_restarts_the_countdown(self) -> None:
        watchdog = ActivityWatchdog(30, 90)
        watchdog.touch(100.0)
        for now in range(105, 400, 5):  # a lifesign every 5 s
            self.assertEqual(watchdog.poll(float(now)), (False, False))
            watchdog.touch(float(now))
        self.assertEqual(watchdog.next_deadline(), 395.0 + 30)
        self.assertEqual(watchdog.poll(425.0), (True, False))

    def test_activity_after_idle_arms_it_again(self) -> None:
        watchdog = ActivityWatchdog(30, 90)
        watchdog.touch(100.0)
        self.assertEqual(watchdog.poll(200.0), (True, True))
        watchdog.touch(300.0)
        self.assertEqual(watchdog.next_deadline(), 330.0)
        self.assertEqual(watchdog.poll(330.0), (True, False))

    def test_idle_disabled_still_expires_the_state(self) -> None:
        watchdog = ActivityWatchdog(0, 90)
        watchdog.touch(100.0)
        self.assertEqual(watchdog.next_deadline(), 190.0)
        self.assertEqual(watchdog.poll(189.0), (False, False))
        self.assertEqual(watchdog.poll(190.0), (False, True))

    def test_long_idle_timeout_delays_the_expiry(self) -> None:
        watchdog = ActivityWatchdog(600, 90)
        watchdog.touch(100.0)
        self.assertEqual(watchdog.state_timeout, 600.0)
        self.assertEqual(watchdog.poll(400.0), (False, False))
        self.assertEqual(watchdog.poll(700.0), (True, True))

    def test_changing_the_timeout_keeps_the_running_countdown(self) -> None:
        watchdog = ActivityWatchdog(30, 90)
        watchdog.touch(100.0)
        watchdog.set_idle_timeout(60)
        self.assertEqual(watchdog.next_deadline(), 160.0)
        watchdog.set_idle_timeout(0)
        self.assertEqual(watchdog.next_deadline(), 190.0)  # only the expiry is left
        self.assertEqual(watchdog.poll(170.0), (False, False))

    def test_stop(self) -> None:
        watchdog = ActivityWatchdog(30, 90)
        watchdog.touch(100.0)
        watchdog.stop()
        self.assertIsNone(watchdog.next_deadline())
        self.assertEqual(watchdog.poll(1000.0), (False, False))


if __name__ == "__main__":
    unittest.main()
