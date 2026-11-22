"""Map server events to Home Assistant trigger types (plain Python).

Nothing in here imports Home Assistant.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .const import (
    EVENT_ACE,
    EVENT_BOMB_DEFUSED,
    EVENT_BOMB_EXPLODED,
    EVENT_BOMB_PLANTED,
    EVENT_DOUBLE_KILL,
    EVENT_FREEZETIME,
    EVENT_HALFTIME,
    EVENT_LEAD_BEHIND,
    EVENT_LEAD_EQUALIZED,
    EVENT_LEAD_TAKEN,
    EVENT_LEAD_TIED,
    EVENT_MATCH_LEFT,
    EVENT_MATCH_LOST,
    EVENT_MATCH_OVER,
    EVENT_MATCH_START,
    EVENT_MATCH_WON,
    EVENT_PLAYER_DEATH,
    EVENT_PLAYER_FLASHED,
    EVENT_PLAYER_HEADSHOT,
    EVENT_PLAYER_KILL,
    EVENT_PLAYER_LOW_HEALTH,
    EVENT_PLAYER_MVP,
    EVENT_QUAD_KILL,
    EVENT_ROUND_LIVE,
    EVENT_ROUND_LOST,
    EVENT_ROUND_OVER_CT,
    EVENT_ROUND_OVER_T,
    EVENT_ROUND_WON,
    EVENT_TRIPLE_KILL,
    EVENT_WIN_STREAK,
)
from .protocol import (
    SIDES,
    SRV_ACE,
    SRV_BOMB_DEFUSED,
    SRV_BOMB_EXPLODED,
    SRV_BOMB_PLANTED,
    SRV_HALFTIME,
    SRV_LEAD_TAKEN,
    SRV_MATCH_LEFT,
    SRV_MATCH_OVER,
    SRV_MATCH_START,
    SRV_PLAYER_DEATH,
    SRV_PLAYER_FLASHED,
    SRV_PLAYER_KILL,
    SRV_PLAYER_LOW_HEALTH,
    SRV_PLAYER_MVP,
    SRV_ROUND_FREEZETIME,
    SRV_ROUND_LIVE,
    SRV_ROUND_OVER,
    SRV_SCORE_TIED,
    SRV_WIN_STREAK,
    Envelope,
    GameEvent,
)

# Server event -> trigger type, where the mapping is one to one.
_MATCH_LEVEL = {
    SRV_BOMB_PLANTED: EVENT_BOMB_PLANTED,
    SRV_BOMB_DEFUSED: EVENT_BOMB_DEFUSED,
    SRV_BOMB_EXPLODED: EVENT_BOMB_EXPLODED,
    SRV_ROUND_LIVE: EVENT_ROUND_LIVE,
    SRV_ROUND_FREEZETIME: EVENT_FREEZETIME,
    SRV_MATCH_START: EVENT_MATCH_START,
    SRV_HALFTIME: EVENT_HALFTIME,
}
_PLAYER_LEVEL = {
    SRV_PLAYER_KILL: EVENT_PLAYER_KILL,
    SRV_ACE: EVENT_ACE,
    SRV_PLAYER_DEATH: EVENT_PLAYER_DEATH,
    SRV_PLAYER_FLASHED: EVENT_PLAYER_FLASHED,
    SRV_PLAYER_LOW_HEALTH: EVENT_PLAYER_LOW_HEALTH,
    SRV_PLAYER_MVP: EVENT_PLAYER_MVP,
    SRV_MATCH_LEFT: EVENT_MATCH_LEFT,
}
_ROUND_OVER = {"T": EVENT_ROUND_OVER_T, "CT": EVENT_ROUND_OVER_CT}
# Server event about a side -> trigger when it is (the device's own side,
# the other side). None: no trigger for that case.
_FROM_OWN_SIDE = {
    SRV_LEAD_TAKEN: (EVENT_LEAD_TAKEN, EVENT_LEAD_BEHIND),
    SRV_SCORE_TIED: (EVENT_LEAD_EQUALIZED, EVENT_LEAD_TIED),
    SRV_WIN_STREAK: (EVENT_WIN_STREAK, None),
}
# Kills of a player in the round -> trigger. Five are an ace, which the
# server reports as an event of its own.
_MULTI_KILL = {2: EVENT_DOUBLE_KILL, 3: EVENT_TRIPLE_KILL, 4: EVENT_QUAD_KILL}


@dataclass(frozen=True, slots=True)
class Trigger:
    """One trigger to fire: its type and the data of the bus event."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


def is_for_owner(subject: str, owner: str | None, *, all_players: bool) -> bool:
    """Decide whether a player event may trigger on this device.

    Player triggers fire for the device owner only. If the server has not
    told us who the owner is, or the user chose to react to all players,
    every player counts.
    """
    return all_players or owner is None or subject == owner


def _base_data(env: Envelope) -> dict[str, Any]:
    data: dict[str, Any] = {
        # These four keys exist since the first release.
        "team_id": env.team_id,
        "player_id": env.player_id,
        "event_id": env.id,
        "occurred_at": env.occurred_at,
    }
    if env.match_id:
        data["match_id"] = env.match_id
    return data


def _event_data(base: dict[str, Any], event: GameEvent, *, resync: bool) -> dict[str, Any]:
    data = dict(base)
    if event.round is not None:
        data["round"] = event.round
    if event.side:
        data["side"] = event.side
    if event.reason:
        data["reason"] = event.reason
    if event.value is not None:
        data["value"] = event.value
    if event.subject:
        data["subject"] = event.subject
    if resync:
        # A transition missed during a connection gap, reported late.
        data["resync"] = True
    return data


def map_events(
    env: Envelope,
    events: Iterable[GameEvent],
    *,
    owner: str | None,
    all_players: bool,
    reference_side: str = "",
    resync: bool = False,
) -> list[Trigger]:
    """Turn server events into triggers.

    env provides the envelope fields for the event data, events are the ones
    to map (usually a filtered subset of env.events). reference_side is the
    side the device is on. Everything that is judged from it (match_won /
    match_lost, round_won / round_lost, the lead, a winning streak) needs
    one; without it (unknown, or the device takes no side) only the neutral
    triggers fire.
    """
    base = _base_data(env)
    triggers: list[Trigger] = []

    for event in events:
        kind = event.type

        if kind in _MATCH_LEVEL:
            triggers.append(
                Trigger(_MATCH_LEVEL[kind], _event_data(base, event, resync=resync))
            )

        elif kind == SRV_ROUND_OVER:
            winner = event.side.upper()
            trigger_type = _ROUND_OVER.get(winner)
            if trigger_type is not None:
                data = _event_data(base, event, resync=resync)
                triggers.append(Trigger(trigger_type, data))
                if reference_side in SIDES:
                    outcome = (
                        EVENT_ROUND_WON if winner == reference_side else EVENT_ROUND_LOST
                    )
                    triggers.append(
                        Trigger(outcome, {**data, "player_side": reference_side})
                    )

        elif kind in _FROM_OWN_SIDE:
            side = event.side.upper()
            if side in SIDES and reference_side in SIDES:
                own, other = _FROM_OWN_SIDE[kind]
                trigger_type = own if side == reference_side else other
                if trigger_type is not None:
                    data = _event_data(base, event, resync=resync)
                    triggers.append(
                        Trigger(trigger_type, {**data, "player_side": reference_side})
                    )

        elif kind == SRV_MATCH_OVER:
            data = _event_data(base, event, resync=resync)
            triggers.append(Trigger(EVENT_MATCH_OVER, data))
            winner = event.side.upper()
            if winner in SIDES and reference_side in SIDES:
                outcome = EVENT_MATCH_WON if winner == reference_side else EVENT_MATCH_LOST
                triggers.append(Trigger(outcome, {**data, "player_side": reference_side}))

        elif kind in _PLAYER_LEVEL:
            subject = event.subject or env.subject
            if not is_for_owner(subject, owner, all_players=all_players):
                continue
            data = _event_data(base, event, resync=resync)
            data["subject"] = subject
            if kind == SRV_PLAYER_KILL:
                data["headshot"] = event.headshot
            triggers.append(Trigger(_PLAYER_LEVEL[kind], data))
            if kind == SRV_PLAYER_KILL and event.headshot:
                triggers.append(Trigger(EVENT_PLAYER_HEADSHOT, dict(data)))
            if kind == SRV_PLAYER_KILL and event.value in _MULTI_KILL:
                triggers.append(Trigger(_MULTI_KILL[event.value], dict(data)))

        # Everything else (timeout, side_switch, player_burning, event types
        # of a newer server) has no trigger and is ignored.

    return triggers
