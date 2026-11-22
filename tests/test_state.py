"""Game state tracking for the entities."""

from __future__ import annotations

from typing import Any
import unittest

import _support  # noqa: F401
from _support import MATE, OWNER, STRANGER, envelope, event, menu_envelope

from custom_components.emotionkit.protocol import parse_envelope
from custom_components.emotionkit.state import (
    IDLE_SNAPSHOT,
    ApplyResult,
    GameStateTracker,
)


class TrackerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = GameStateTracker()
        self.now = 1000.0

    def feed(
        self, message: dict[str, Any], *, owner: str | None = OWNER, dt: float = 1.0
    ) -> ApplyResult:
        self.now += dt
        env = parse_envelope(message)
        assert env is not None
        return self.tracker.apply(env, owner=owner, now=self.now)

    @staticmethod
    def types(events: Any) -> list[str]:
        return [item.type for item in events]


class IdleStateTest(TrackerTestCase):
    def test_starts_idle(self) -> None:
        snapshot = self.tracker.snapshot
        self.assertIs(snapshot, IDLE_SNAPSHOT)
        self.assertFalse(snapshot.in_match)
        self.assertFalse(snapshot.bomb_planted)
        for field in (
            "map_name",
            "round_phase",
            "score_ct",
            "score_t",
            "round_number",
            "health",
            "armor",
            "money",
        ):
            self.assertIsNone(getattr(snapshot, field), field)

    def test_reset_goes_back_to_idle(self) -> None:
        self.feed(envelope(event("bomb_planted", round=3), bomb="planted", ct=1, t=1))
        self.assertTrue(self.tracker.snapshot.bomb_planted)
        self.assertTrue(self.tracker.reset())
        self.assertEqual(self.tracker.snapshot, IDLE_SNAPSHOT)
        self.assertEqual(self.tracker.active_subjects, ())
        self.assertFalse(self.tracker.reset())  # nothing left to change

    def test_menu_packet_while_idle_changes_nothing(self) -> None:
        result = self.feed(menu_envelope())
        self.assertFalse(result.in_match)
        self.assertFalse(result.followed)
        self.assertFalse(result.changed)
        self.assertEqual(result.resync_events, ())


class JoiningAMatchTest(TrackerTestCase):
    def test_first_packet_is_adopted_silently(self) -> None:
        # Home Assistant restarts in the middle of round 8 with the bomb down.
        result = self.feed(envelope(phase="live", bomb="planted", ct=4, t=3))
        self.assertTrue(result.in_match and result.followed and result.changed)
        self.assertEqual(result.events, ())
        self.assertEqual(result.resync_events, ())  # nothing may trigger
        snapshot = self.tracker.snapshot
        self.assertTrue(snapshot.in_match)
        self.assertEqual(snapshot.match_id, "match-1")
        self.assertEqual(snapshot.map_name, "de_dust2")
        self.assertEqual(snapshot.round_phase, "live")
        self.assertTrue(snapshot.bomb_planted)
        self.assertEqual((snapshot.score_ct, snapshot.score_t), (4, 3))
        self.assertEqual(snapshot.round_number, 8)

    def test_round_number_while_round_is_over(self) -> None:
        self.feed(envelope(phase="over", win_team="CT", ct=5, t=3))
        self.assertEqual(self.tracker.snapshot.round_number, 8)
        self.assertEqual(self.tracker.snapshot.round_phase, "over")

    def test_unchanged_packets_report_no_change(self) -> None:
        self.feed(envelope(ct=1, t=0))
        result = self.feed(envelope(ct=1, t=0))
        self.assertTrue(result.followed)
        self.assertFalse(result.changed)

    def test_packet_without_round_block(self) -> None:
        result = self.feed(envelope(phase=None))
        self.assertTrue(result.followed)
        self.assertIsNone(self.tracker.snapshot.round_phase)
        self.assertEqual(self.tracker.snapshot.map_name, "de_dust2")

    def test_unknown_phase_is_ignored(self) -> None:
        self.feed(envelope(phase="warmup-ish"))
        self.assertIsNone(self.tracker.snapshot.round_phase)


class EventsDriveTheStateTest(TrackerTestCase):
    def test_a_round(self) -> None:
        result = self.feed(envelope(event("round_freezetime", round=4), phase="freezetime", ct=2, t=1))
        self.assertEqual(self.types(result.events), ["round_freezetime"])
        self.assertEqual(self.tracker.snapshot.round_phase, "freezetime")
        self.assertEqual(self.tracker.snapshot.round_number, 4)

        self.feed(envelope(event("round_live", round=4), phase="live", ct=2, t=1))
        self.assertEqual(self.tracker.snapshot.round_phase, "live")

        result = self.feed(envelope(event("bomb_planted", round=4), bomb="planted", ct=2, t=1))
        self.assertEqual(self.types(result.events), ["bomb_planted"])
        self.assertTrue(self.tracker.snapshot.bomb_planted)

        result = self.feed(
            envelope(
                event("round_over", round=4, side="T", reason="bomb"),
                event("bomb_exploded", round=4),
                phase="over",
                bomb="exploded",
                win_team="T",
                ct=2,
                t=2,
            )
        )
        self.assertEqual(self.types(result.events), ["round_over", "bomb_exploded"])
        snapshot = self.tracker.snapshot
        self.assertEqual(snapshot.round_phase, "over")
        self.assertFalse(snapshot.bomb_planted)  # exploded is not "planted"
        self.assertEqual((snapshot.score_ct, snapshot.score_t), (2, 2))
        self.assertEqual(snapshot.round_number, 4)

        self.feed(envelope(event("round_freezetime", round=5), phase="freezetime", ct=2, t=2))
        self.assertEqual(self.tracker.snapshot.round_number, 5)
        self.assertFalse(self.tracker.snapshot.bomb_planted)

    def test_bomb_defused(self) -> None:
        self.feed(envelope(event("bomb_planted", round=2), bomb="planted"))
        self.assertTrue(self.tracker.snapshot.bomb_planted)
        self.feed(envelope(event("bomb_defused", round=2), bomb="defused"))
        self.assertFalse(self.tracker.snapshot.bomb_planted)

    def test_new_round_clears_the_bomb_even_without_freezetime(self) -> None:
        self.feed(envelope(event("round_live", round=2), phase="live"))
        self.feed(envelope(event("bomb_planted", round=2), bomb="planted"))
        # Freezetime of round 3 was never seen.
        self.feed(envelope(event("round_live", round=3), phase="live", ct=1))
        self.assertFalse(self.tracker.snapshot.bomb_planted)
        self.assertEqual(self.tracker.snapshot.round_number, 3)

    def test_unknown_event_types_are_passed_through(self) -> None:
        result = self.feed(envelope(event("something_new", round=1), event("timeout", side="CT")))
        self.assertEqual(self.types(result.events), ["something_new", "timeout"])
        self.assertEqual(result.duplicates, 0)


class DuplicateEventsTest(TrackerTestCase):
    """An event that does not move the state forward must not trigger."""

    def test_same_phase_twice(self) -> None:
        first = self.feed(envelope(event("round_live", round=3), phase="live"))
        again = self.feed(envelope(event("round_live", round=3), phase="live", subject=MATE))
        self.assertEqual(self.types(first.events), ["round_live"])
        self.assertEqual(again.events, ())
        self.assertEqual(again.duplicates, 1)

    def test_bomb_planted_twice_in_one_round(self) -> None:
        # Two clients disagree for a moment: A sees the bomb, B not yet.
        self.feed(envelope(event("round_live", round=3), phase="live"))
        first = self.feed(envelope(event("bomb_planted", round=3), bomb="planted"))
        self.feed(envelope(subject=MATE, phase="live"))  # B: no bomb yet
        self.assertTrue(self.tracker.snapshot.bomb_planted, "the bomb must not flap")
        again = self.feed(envelope(event("bomb_planted", round=3), bomb="planted", subject=MATE))
        self.assertEqual(self.types(first.events), ["bomb_planted"])
        self.assertEqual(again.events, ())
        self.assertEqual(again.duplicates, 1)
        self.assertTrue(self.tracker.snapshot.bomb_planted)

    def test_bomb_end_states_do_not_repeat(self) -> None:
        self.feed(envelope(event("bomb_planted", round=3), bomb="planted"))
        first = self.feed(envelope(event("bomb_exploded", round=3), bomb="exploded"))
        again = self.feed(envelope(event("bomb_exploded", round=3), bomb="exploded"))
        late_plant = self.feed(envelope(event("bomb_planted", round=3), bomb="planted"))
        self.assertEqual(self.types(first.events), ["bomb_exploded"])
        self.assertEqual(again.events, ())
        self.assertEqual(late_plant.events, ())

    def test_bomb_fires_again_in_the_next_round(self) -> None:
        self.feed(envelope(event("round_live", round=3), phase="live"))
        self.feed(envelope(event("bomb_planted", round=3), bomb="planted"))
        self.feed(envelope(event("round_over", round=3, side="T"), phase="over", bomb="exploded"))
        self.feed(envelope(event("round_freezetime", round=4), phase="freezetime", t=1))
        self.feed(envelope(event("round_live", round=4), phase="live", t=1))
        result = self.feed(envelope(event("bomb_planted", round=4), bomb="planted", t=1))
        self.assertEqual(self.types(result.events), ["bomb_planted"])

    def test_same_round_number_after_warmup_is_not_a_duplicate(self) -> None:
        # Warm-up and the first real round both count as round 1.
        warmup = self.feed(envelope(event("round_live", round=1), phase="live"))
        freeze = self.feed(
            envelope(event("match_start"), event("round_freezetime", round=1), phase="freezetime")
        )
        live = self.feed(envelope(event("round_live", round=1), phase="live"))
        self.assertEqual(self.types(warmup.events), ["round_live"])
        self.assertEqual(self.types(freeze.events), ["match_start", "round_freezetime"])
        self.assertEqual(self.types(live.events), ["round_live"])

    def test_match_start_and_halftime_only_once(self) -> None:
        first = self.feed(envelope(event("match_start")))
        again = self.feed(envelope(event("match_start"), subject=MATE))
        self.assertEqual(self.types(first.events), ["match_start"])
        self.assertEqual(again.events, ())

        first = self.feed(envelope(event("halftime", round=12)))
        again = self.feed(envelope(event("halftime", round=12)))
        self.assertEqual(self.types(first.events), ["halftime"])
        self.assertEqual(again.events, ())

    def test_match_over_only_once(self) -> None:
        first = self.feed(envelope(event("match_over", side="CT")))
        again = self.feed(envelope(event("match_over", side="CT")), dt=120.0)
        self.assertEqual(self.types(first.events), ["match_over"])
        self.assertEqual(again.events, ())

    def test_player_events_are_never_duplicates(self) -> None:
        kill = event("player_kill", subject=OWNER, value=1, round=2)
        self.assertEqual(len(self.feed(envelope(kill)).events), 1)
        self.assertEqual(len(self.feed(envelope(kill)).events), 1)

    def test_events_after_joining_mid_round(self) -> None:
        # Joined while the round is live; the server then repeats nothing,
        # but if it did, the state would already match.
        self.feed(envelope(phase="live", ct=3, t=3))
        repeated = self.feed(envelope(event("round_live", round=7), phase="live", ct=3, t=3))
        self.assertEqual(repeated.events, ())
        over = self.feed(
            envelope(event("round_over", round=7, side="CT"), phase="over", win_team="CT", ct=3, t=3)
        )
        self.assertEqual(self.types(over.events), ["round_over"])

    def test_round_number_guess_is_corrected_without_side_effects(self) -> None:
        # Joined while "over" before the score moved: the guess (6) is one
        # short. The server's number must not look like a new round.
        self.feed(envelope(phase="over", bomb="exploded", win_team="T", ct=3, t=3))
        self.assertEqual(self.tracker.snapshot.round_number, 6)
        repeated = self.feed(
            envelope(event("round_over", round=7, side="T"), phase="over", bomb="exploded", ct=3, t=4)
        )
        self.assertEqual(repeated.events, ())
        self.assertEqual(self.tracker.snapshot.round_number, 7)


class LaggingPacketsTest(TrackerTestCase):
    """A packet with only player events may show an older state."""

    def test_lagging_payload_does_not_move_the_state_back(self) -> None:
        self.feed(envelope(event("round_live", round=5), phase="live", ct=2, t=2))
        self.feed(
            envelope(
                event("round_over", round=5, side="CT"), phase="over", win_team="CT", ct=3, t=2
            )
        )
        # The mate's client is a moment behind: still "live", old score.
        lagging = self.feed(
            envelope(
                event("player_death", subject=MATE, side="CT", round=5),
                subject=MATE,
                phase="live",
                ct=2,
                t=2,
            )
        )
        self.assertEqual(self.types(lagging.events), ["player_death"])
        self.assertEqual(lagging.resync_events, ())
        snapshot = self.tracker.snapshot
        self.assertEqual(snapshot.round_phase, "over")
        self.assertEqual((snapshot.score_ct, snapshot.score_t), (3, 2))

        # The mate catches up: no second "round over".
        caught_up = self.feed(envelope(subject=MATE, phase="over", win_team="CT", ct=3, t=2))
        self.assertEqual(caught_up.resync_events, ())
        self.assertFalse(caught_up.changed)

    def test_first_packet_being_a_lagging_one_sets_no_baseline(self) -> None:
        self.feed(
            envelope(event("player_death", subject=MATE, round=5), subject=MATE, phase="live", ct=2, t=2)
        )
        self.assertIsNone(self.tracker.snapshot.round_phase)
        # The next trustworthy packet is adopted silently.
        result = self.feed(envelope(phase="over", win_team="T", ct=2, t=3))
        self.assertEqual(result.resync_events, ())
        self.assertEqual(self.tracker.snapshot.round_phase, "over")


class ResyncTest(TrackerTestCase):
    """Transitions missed during a short connection gap are made up for."""

    def test_missed_bomb_plant(self) -> None:
        self.feed(envelope(event("round_live", round=3), phase="live", ct=1, t=1))
        # 6 s without connection; the plant happened in between.
        result = self.feed(envelope(phase="live", bomb="planted", ct=1, t=1), dt=6.0)
        self.assertEqual(result.events, ())
        self.assertEqual(self.types(result.resync_events), ["bomb_planted"])
        self.assertEqual(result.resync_events[0].round, 3)
        self.assertTrue(self.tracker.snapshot.bomb_planted)
        # Reported once only.
        again = self.feed(envelope(phase="live", bomb="planted", ct=1, t=1))
        self.assertEqual(again.resync_events, ())

    def test_missed_round_end(self) -> None:
        self.feed(envelope(event("round_live", round=3), phase="live", ct=1, t=1))
        result = self.feed(
            envelope(phase="over", bomb="exploded", win_team="T", ct=1, t=2), dt=8.0
        )
        self.assertEqual(self.types(result.resync_events), ["round_over", "bomb_exploded"])
        self.assertEqual(result.resync_events[0].side, "T")
        self.assertEqual(self.tracker.snapshot.round_phase, "over")

    def test_missed_new_round(self) -> None:
        self.feed(envelope(event("round_over", round=3, side="T"), phase="over", ct=1, t=2))
        result = self.feed(envelope(phase="freezetime", ct=1, t=2), dt=5.0)
        self.assertEqual(self.types(result.resync_events), ["round_freezetime"])
        self.assertEqual(result.resync_events[0].round, 4)
        self.assertEqual(self.tracker.snapshot.round_number, 4)

    def test_long_gap_is_adopted_silently(self) -> None:
        self.feed(envelope(event("round_live", round=3), phase="live", ct=1, t=1))
        result = self.feed(envelope(phase="live", bomb="planted", ct=1, t=1), dt=25.0)
        self.assertEqual(result.resync_events, ())
        self.assertTrue(self.tracker.snapshot.bomb_planted)
        self.assertTrue(result.changed)

    def test_events_in_the_envelope_take_precedence(self) -> None:
        self.feed(envelope(event("round_live", round=3), phase="live", ct=1, t=1))
        result = self.feed(
            envelope(event("bomb_planted", round=3), phase="live", bomb="planted", ct=1, t=1),
            dt=6.0,
        )
        self.assertEqual(self.types(result.events), ["bomb_planted"])
        self.assertEqual(result.resync_events, ())

    def test_bomb_never_runs_backwards_within_a_round(self) -> None:
        self.feed(envelope(event("round_live", round=3), phase="live"))
        self.feed(envelope(event("bomb_planted", round=3), bomb="planted"))
        result = self.feed(envelope(phase="live"))  # no bomb in this client's view
        self.assertEqual(result.resync_events, ())
        self.assertTrue(self.tracker.snapshot.bomb_planted)

    def test_bomb_is_cleared_when_the_payload_shows_a_new_round(self) -> None:
        self.feed(envelope(event("round_live", round=3), phase="live", ct=1, t=1))
        self.feed(envelope(event("bomb_planted", round=3), bomb="planted", ct=1, t=1))
        # Everything up to the next round's freezetime was missed.
        self.feed(envelope(phase="freezetime", ct=1, t=2), dt=30.0)
        self.assertFalse(self.tracker.snapshot.bomb_planted)
        self.assertEqual(self.tracker.snapshot.round_number, 4)


class OwnerValuesTest(TrackerTestCase):
    def test_owner_values_from_own_packets(self) -> None:
        self.feed(envelope(subject=OWNER, health=87, armor=50, money=3200))
        snapshot = self.tracker.snapshot
        self.assertEqual((snapshot.health, snapshot.armor, snapshot.money), (87, 50, 3200))

    def test_other_players_do_not_touch_owner_values(self) -> None:
        self.feed(envelope(subject=OWNER, health=87, armor=50, money=3200))
        self.feed(envelope(subject=MATE, health=12, armor=0, money=100))
        snapshot = self.tracker.snapshot
        self.assertEqual((snapshot.health, snapshot.armor, snapshot.money), (87, 50, 3200))

    def test_spectated_teammate_is_not_the_owner(self) -> None:
        self.feed(envelope(subject=OWNER, health=87, armor=50, money=3200))
        # The owner died and now watches the mate: player.steamid differs.
        self.feed(
            envelope(subject=OWNER, steamid=MATE, provider=OWNER, health=33, armor=1, money=5)
        )
        snapshot = self.tracker.snapshot
        self.assertEqual((snapshot.health, snapshot.armor, snapshot.money), (87, 50, 3200))

    def test_owner_death_sets_health_to_zero(self) -> None:
        self.feed(envelope(subject=OWNER, health=40))
        self.feed(
            envelope(
                event("player_death", subject=OWNER, round=1),
                subject=OWNER,
                steamid=MATE,
                provider=OWNER,
                health=100,
            )
        )
        self.assertEqual(self.tracker.snapshot.health, 0)
        # A mate's death does not.
        self.feed(envelope(subject=OWNER, health=100))
        self.feed(envelope(event("player_death", subject=MATE, round=2), subject=MATE))
        self.assertEqual(self.tracker.snapshot.health, 100)

    def test_unknown_owner_takes_any_own_view(self) -> None:
        self.feed(envelope(subject=MATE, health=55, armor=10, money=900), owner=None)
        snapshot = self.tracker.snapshot
        self.assertEqual((snapshot.health, snapshot.armor, snapshot.money), (55, 10, 900))

    def test_values_are_unknown_until_the_owner_reports(self) -> None:
        self.feed(envelope(subject=MATE, health=55))
        snapshot = self.tracker.snapshot
        self.assertTrue(snapshot.in_match)
        self.assertIsNone(snapshot.health)
        self.assertIsNone(snapshot.armor)
        self.assertIsNone(snapshot.money)

    def test_malformed_player_state(self) -> None:
        message = envelope(subject=OWNER)
        message["payload"]["player"]["state"] = {"health": "full", "armor": None, "money": True}
        self.feed(message)
        self.assertIsNone(self.tracker.snapshot.health)
        self.assertIsNone(self.tracker.snapshot.money)


class PresenceTest(TrackerTestCase):
    def test_match_left_ends_the_match(self) -> None:
        self.feed(envelope(event("round_live", round=1), phase="live"))
        self.assertTrue(self.tracker.snapshot.in_match)
        result = self.feed(menu_envelope(event("match_left", subject=OWNER)))
        self.assertFalse(result.in_match)
        self.assertTrue(result.changed)
        self.assertEqual(self.types(result.events), ["match_left"])
        self.assertEqual(result.resync_events, ())
        self.assertEqual(self.tracker.snapshot, IDLE_SNAPSHOT)

    def test_match_goes_on_while_a_mate_is_still_playing(self) -> None:
        self.feed(envelope(subject=OWNER))
        self.feed(envelope(subject=MATE))
        self.feed(menu_envelope(event("match_left", subject=OWNER)))
        self.assertTrue(self.tracker.snapshot.in_match)
        self.feed(menu_envelope(event("match_left", subject=MATE), subject=MATE))
        self.assertFalse(self.tracker.snapshot.in_match)

    def test_menu_packet_without_event_counts_as_leaving(self) -> None:
        # The match_left event itself was missed (connection gap).
        self.feed(envelope(subject=OWNER))
        result = self.feed(menu_envelope())
        self.assertEqual(self.types(result.resync_events), ["match_left"])
        self.assertEqual(result.resync_events[0].subject, OWNER)
        self.assertFalse(self.tracker.snapshot.in_match)
        # Further menu packets are not another "left".
        result = self.feed(menu_envelope())
        self.assertEqual(result.resync_events, ())
        self.assertFalse(result.changed)

    def test_menu_packets_of_someone_not_playing_are_ignored(self) -> None:
        self.feed(envelope(subject=OWNER))
        result = self.feed(menu_envelope(subject=MATE))
        self.assertEqual(result.resync_events, ())
        self.assertTrue(self.tracker.snapshot.in_match)

    def test_new_match_resets_the_round_state(self) -> None:
        self.feed(envelope(event("bomb_planted", round=9), bomb="planted", ct=5, t=3))
        self.feed(envelope(match_id="match-2", map_name="de_nuke", phase="freezetime"))
        snapshot = self.tracker.snapshot
        self.assertEqual(snapshot.match_id, "match-2")
        self.assertEqual(snapshot.map_name, "de_nuke")
        self.assertFalse(snapshot.bomb_planted)
        self.assertEqual((snapshot.score_ct, snapshot.score_t), (0, 0))
        self.assertEqual(snapshot.round_number, 1)


class FollowingOneMatchTest(TrackerTestCase):
    def test_other_match_is_ignored_while_the_owner_plays(self) -> None:
        self.feed(envelope(subject=OWNER, match_id="A"))
        result = self.feed(
            envelope(event("bomb_planted", round=1), subject=STRANGER, match_id="B", bomb="planted")
        )
        self.assertTrue(result.in_match)
        self.assertFalse(result.followed)
        self.assertEqual(result.events, ())
        self.assertEqual(self.tracker.snapshot.match_id, "A")
        self.assertFalse(self.tracker.snapshot.bomb_planted)

    def test_owner_always_wins(self) -> None:
        self.feed(envelope(subject=MATE, match_id="B"))
        self.assertEqual(self.tracker.snapshot.match_id, "B")
        result = self.feed(envelope(subject=OWNER, match_id="A", map_name="de_inferno"))
        self.assertTrue(result.followed)
        self.assertEqual(self.tracker.snapshot.match_id, "A")
        self.assertEqual(self.tracker.snapshot.map_name, "de_inferno")

    def test_player_moving_on_takes_the_device_along(self) -> None:
        # No owner known; the only player starts the next match.
        self.feed(envelope(subject=MATE, match_id="A"), owner=None)
        result = self.feed(envelope(subject=MATE, match_id="B"), owner=None)
        self.assertTrue(result.followed)
        self.assertEqual(self.tracker.snapshot.match_id, "B")

    def test_second_match_waits_while_the_first_is_alive(self) -> None:
        self.feed(envelope(subject=MATE, match_id="A"), owner=None)
        result = self.feed(envelope(subject=STRANGER, match_id="B"), owner=None, dt=2.0)
        self.assertFalse(result.followed)
        self.assertEqual(self.tracker.snapshot.match_id, "A")
        # Match A went silent: follow the one that is alive.
        result = self.feed(envelope(subject=STRANGER, match_id="B"), owner=None, dt=20.0)
        self.assertTrue(result.followed)
        self.assertEqual(self.tracker.snapshot.match_id, "B")


class ReferenceSideTest(TrackerTestCase):
    def test_owner_side(self) -> None:
        self.feed(envelope(subject=OWNER, side="T"))
        self.feed(envelope(subject=MATE, side="CT"))
        self.assertEqual(self.tracker.reference_side(OWNER, any_player=False), "T")

    def test_follows_the_side_switch(self) -> None:
        self.feed(envelope(subject=OWNER, side="T"))
        self.feed(envelope(subject=OWNER, side="CT"))
        self.assertEqual(self.tracker.reference_side(OWNER, any_player=False), "CT")

    def test_unknown_side_values_are_ignored(self) -> None:
        self.feed(envelope(subject=OWNER, side="T"))
        self.feed(envelope(subject=OWNER, side="UNKNOWN"))
        self.assertEqual(self.tracker.reference_side(OWNER, any_player=False), "T")

    def test_owner_not_playing(self) -> None:
        self.feed(envelope(subject=MATE, side="CT"))
        self.assertEqual(self.tracker.reference_side(OWNER, any_player=False), "")
        self.assertEqual(self.tracker.reference_side(OWNER, any_player=True), "CT")

    def test_unknown_owner(self) -> None:
        self.feed(envelope(subject=MATE, side="CT"), owner=None)
        self.assertEqual(self.tracker.reference_side(None, any_player=False), "CT")

    def test_nothing_seen(self) -> None:
        self.assertEqual(self.tracker.reference_side(OWNER, any_player=True), "")

    def test_no_side_while_the_devices_players_are_opponents(self) -> None:
        self.feed(envelope(subject=OWNER, side="T"))
        self.assertFalse(self.tracker.mixed_sides)
        self.feed({**envelope(subject=MATE, side="CT"), "mixed_sides": True})
        self.assertTrue(self.tracker.mixed_sides)
        self.assertEqual(self.tracker.reference_side(OWNER, any_player=False), "")
        self.assertEqual(self.tracker.reference_side(OWNER, any_player=True), "")
        self.assertEqual(self.tracker.reference_side(None, any_player=False), "")
        # The server takes it back with any later packet of the match.
        self.feed(envelope(subject=OWNER, side="T"))
        self.assertFalse(self.tracker.mixed_sides)
        self.assertEqual(self.tracker.reference_side(OWNER, any_player=False), "T")

    def test_mixed_sides_ends_with_the_match(self) -> None:
        self.feed({**envelope(subject=OWNER, side="T"), "mixed_sides": True})
        self.assertTrue(self.tracker.mixed_sides)
        self.feed(menu_envelope(event("match_left", subject=OWNER)))
        self.assertFalse(self.tracker.mixed_sides)

    def test_mixed_sides_of_a_match_not_followed_does_not_count(self) -> None:
        self.feed(envelope(subject=OWNER, side="T"))
        other = envelope(subject=MATE, side="CT", match_id="match-2")
        result = self.feed({**other, "mixed_sides": True})
        self.assertFalse(result.followed)
        self.assertFalse(self.tracker.mixed_sides)


if __name__ == "__main__":
    unittest.main()
