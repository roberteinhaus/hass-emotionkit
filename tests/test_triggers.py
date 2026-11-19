"""Mapping of server events to trigger types, and the owner filter."""

from __future__ import annotations

from typing import Any
import unittest

import _support  # noqa: F401
from _support import MATE, OWNER, envelope, event

from custom_components.emotionkit import const
from custom_components.emotionkit.protocol import Envelope, parse_envelope
from custom_components.emotionkit.triggers import Trigger, is_for_owner, map_events


def mapped(
    *events: dict[str, Any],
    owner: str | None = OWNER,
    all_players: bool = False,
    reference_side: str = "",
    resync: bool = False,
    **envelope_args: Any,
) -> list[Trigger]:
    env = parse_envelope(envelope(*events, **envelope_args))
    assert env is not None
    return map_events(
        env,
        env.events,
        owner=owner,
        all_players=all_players,
        reference_side=reference_side,
        resync=resync,
    )


def types(triggers: list[Trigger]) -> list[str]:
    return [trigger.type for trigger in triggers]


class LegacyTriggerNamesTest(unittest.TestCase):
    """Existing automations refer to these names; they must never change."""

    def test_names(self) -> None:
        self.assertEqual(
            list(const.LEGACY_TRIGGER_TYPES),
            [
                "bomb_planted",
                "bomb_defused",
                "bomb_exploded",
                "round_live",
                "round_over_t",
                "round_over_ct",
                "freezetime",
            ],
        )
        self.assertEqual(const.EVENT_IDLE, "idle")
        self.assertEqual(const.EVENT_EMOTIONKIT, "emotionkit_event")

    def test_all_types(self) -> None:
        self.assertEqual(
            set(const.ALL_EVENT_TYPES),
            {
                "bomb_planted",
                "bomb_defused",
                "bomb_exploded",
                "round_live",
                "round_over_t",
                "round_over_ct",
                "freezetime",
                "idle",
                "match_start",
                "match_over",
                "match_won",
                "match_lost",
                "halftime",
                "player_kill",
                "player_headshot",
                "ace",
                "player_death",
                "player_flashed",
                "player_low_health",
                "player_mvp",
                "match_left",
            },
        )
        self.assertEqual(len(const.ALL_EVENT_TYPES), len(set(const.ALL_EVENT_TYPES)))

    def test_server_events_map_to_legacy_names(self) -> None:
        cases = {
            "bomb_planted": "bomb_planted",
            "bomb_defused": "bomb_defused",
            "bomb_exploded": "bomb_exploded",
            "round_live": "round_live",
            "round_freezetime": "freezetime",
        }
        for server_type, trigger_type in cases.items():
            with self.subTest(server_type=server_type):
                self.assertEqual(types(mapped(event(server_type, round=3))), [trigger_type])

    def test_round_over_by_winner(self) -> None:
        self.assertEqual(types(mapped(event("round_over", side="T", round=3))), ["round_over_t"])
        self.assertEqual(types(mapped(event("round_over", side="CT", round=3))), ["round_over_ct"])
        self.assertEqual(types(mapped(event("round_over", side="ct", round=3))), ["round_over_ct"])
        # No winner known: neither of the two.
        self.assertEqual(mapped(event("round_over", round=3)), [])


class MatchTriggersTest(unittest.TestCase):
    def test_match_start_and_halftime(self) -> None:
        self.assertEqual(types(mapped(event("match_start"))), ["match_start"])
        self.assertEqual(types(mapped(event("halftime", round=12))), ["halftime"])

    def test_match_won(self) -> None:
        triggers = mapped(event("match_over", side="CT"), reference_side="CT")
        self.assertEqual(types(triggers), ["match_over", "match_won"])
        self.assertEqual(triggers[1].data["side"], "CT")
        self.assertEqual(triggers[1].data["player_side"], "CT")

    def test_match_lost(self) -> None:
        triggers = mapped(event("match_over", side="T"), reference_side="CT")
        self.assertEqual(types(triggers), ["match_over", "match_lost"])
        self.assertEqual(triggers[1].data["player_side"], "CT")

    def test_draw_is_neither_won_nor_lost(self) -> None:
        self.assertEqual(types(mapped(event("match_over"), reference_side="CT")), ["match_over"])

    def test_unknown_own_side(self) -> None:
        self.assertEqual(types(mapped(event("match_over", side="T"))), ["match_over"])

    def test_events_without_trigger(self) -> None:
        self.assertEqual(
            mapped(
                event("timeout", side="CT"),
                event("side_switch", round=13),
                event("player_burning", subject=OWNER, value=120),
                event("something_from_the_future", round=1),
            ),
            [],
        )


class PlayerTriggersTest(unittest.TestCase):
    def test_one_to_one(self) -> None:
        for kind in (
            "ace",
            "player_death",
            "player_flashed",
            "player_low_health",
            "player_mvp",
            "match_left",
        ):
            with self.subTest(kind=kind):
                self.assertEqual(types(mapped(event(kind, subject=OWNER))), [kind])

    def test_kill(self) -> None:
        triggers = mapped(event("player_kill", subject=OWNER, side="CT", value=2, round=5))
        self.assertEqual(types(triggers), ["player_kill"])
        self.assertIs(triggers[0].data["headshot"], False)

    def test_headshot_kill_fires_both(self) -> None:
        triggers = mapped(event("player_kill", subject=OWNER, value=1, headshot=True, round=5))
        self.assertEqual(types(triggers), ["player_kill", "player_headshot"])
        for trigger in triggers:
            self.assertIs(trigger.data["headshot"], True)
            self.assertEqual(trigger.data["value"], 1)
            self.assertEqual(trigger.data["subject"], OWNER)

    def test_only_the_owner_by_default(self) -> None:
        events = (
            event("player_kill", subject=MATE, value=1, headshot=True),
            event("player_death", subject=MATE),
            event("player_flashed", subject=MATE, value=255),
            event("match_left", subject=MATE),
            event("player_flashed", subject=OWNER, value=90),
        )
        triggers = mapped(*events, subject=MATE)
        self.assertEqual(types(triggers), ["player_flashed"])
        self.assertEqual(triggers[0].data["subject"], OWNER)

    def test_all_players_option(self) -> None:
        triggers = mapped(
            event("player_kill", subject=MATE, value=1, headshot=True),
            event("player_death", subject=OWNER),
            all_players=True,
        )
        self.assertEqual(types(triggers), ["player_kill", "player_headshot", "player_death"])
        self.assertEqual(triggers[0].data["subject"], MATE)
        self.assertEqual(triggers[2].data["subject"], OWNER)

    def test_unknown_owner_means_everybody(self) -> None:
        triggers = mapped(event("player_death", subject=MATE), owner=None)
        self.assertEqual(types(triggers), ["player_death"])

    def test_subject_defaults_to_the_sender(self) -> None:
        # Events are supposed to carry the subject; if one does not, the
        # player who sent the packet is meant.
        triggers = mapped(event("player_death"), subject=OWNER)
        self.assertEqual(types(triggers), ["player_death"])
        self.assertEqual(triggers[0].data["subject"], OWNER)
        self.assertEqual(mapped(event("player_death"), subject=MATE), [])

    def test_match_level_triggers_fire_for_any_sender(self) -> None:
        triggers = mapped(event("bomb_planted", round=2), subject=MATE)
        self.assertEqual(types(triggers), ["bomb_planted"])

    def test_is_for_owner(self) -> None:
        self.assertTrue(is_for_owner(OWNER, OWNER, all_players=False))
        self.assertFalse(is_for_owner(MATE, OWNER, all_players=False))
        self.assertTrue(is_for_owner(MATE, OWNER, all_players=True))
        self.assertTrue(is_for_owner(MATE, None, all_players=False))


class TriggerDataTest(unittest.TestCase):
    def test_legacy_keys_are_kept(self) -> None:
        triggers = mapped(event("bomb_planted", round=7), envelope_id="evt-1")
        data = triggers[0].data
        self.assertEqual(data["team_id"], "team-1")
        self.assertEqual(data["player_id"], f"team-1:{OWNER}")
        self.assertEqual(data["event_id"], "evt-1")
        self.assertEqual(data["occurred_at"], "2026-11-18T20:15:04.123Z")

    def test_fields_where_present(self) -> None:
        over = mapped(event("round_over", round=7, side="CT", reason="defuse"))[0].data
        self.assertEqual(over["round"], 7)
        self.assertEqual(over["side"], "CT")
        self.assertEqual(over["reason"], "defuse")
        self.assertEqual(over["match_id"], "match-1")
        self.assertNotIn("subject", over)
        self.assertNotIn("value", over)
        self.assertNotIn("resync", over)

        flashed = mapped(event("player_flashed", subject=OWNER, value=180, round=7, side="CT"))[0].data
        self.assertEqual(flashed["subject"], OWNER)
        self.assertEqual(flashed["value"], 180)
        self.assertEqual(flashed["round"], 7)
        self.assertNotIn("reason", flashed)
        self.assertNotIn("headshot", flashed)

        start = mapped(event("match_start"))[0].data
        for absent in ("round", "side", "reason", "value", "subject"):
            self.assertNotIn(absent, start)

    def test_reserved_keys_are_not_used(self) -> None:
        # "device_id" and "type" are added by the integration when firing.
        for trigger in mapped(
            event("player_kill", subject=OWNER, value=1, headshot=True, round=1, side="T"),
            event("match_over", side="T"),
            reference_side="T",
        ):
            self.assertNotIn("device_id", trigger.data)
            self.assertNotIn("type", trigger.data)

    def test_data_dicts_are_independent(self) -> None:
        kill, headshot = mapped(event("player_kill", subject=OWNER, value=1, headshot=True))
        kill.data["extra"] = 1
        self.assertNotIn("extra", headshot.data)

    def test_resync_flag(self) -> None:
        triggers = mapped(event("bomb_planted", round=7), resync=True)
        self.assertIs(triggers[0].data["resync"], True)

    def test_no_match_id_key_outside_a_match(self) -> None:
        env = parse_envelope(_support.menu_envelope(event("match_left", subject=OWNER)))
        assert env is not None
        triggers = map_events(env, env.events, owner=OWNER, all_players=False)
        self.assertEqual(types(triggers), ["match_left"])
        self.assertNotIn("match_id", triggers[0].data)

    def test_events_argument_selects_what_is_mapped(self) -> None:
        env = parse_envelope(envelope(event("bomb_planted", round=1), event("round_live", round=1)))
        assert env is not None
        self.assertEqual(
            types(map_events(env, env.events[1:], owner=OWNER, all_players=False)), ["round_live"]
        )
        self.assertEqual(map_events(Envelope(), (), owner=None, all_players=True), [])


if __name__ == "__main__":
    unittest.main()
