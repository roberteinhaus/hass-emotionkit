"""Game state tracking for the entities (plain Python, no Home Assistant).

The tracker follows one match at a time and keeps what the entities show:
whether a match is running, round phase, bomb, map, score and the owner's
health, armor and money. It also decides which of the server's events are
news: an event that does not move the state forward (the same round going
live twice, a bomb planted twice in one round) is reported as a duplicate so
that it triggers nothing.

Round phase and bomb come from the server's `events`, which are already
de-duplicated across the players of a match. The raw GSI `payload` is only
used where the events cannot help:

* to learn the state when joining a running match (after a restart of Home
  Assistant or a reconnect) and
* to notice that a transition was missed while the connection was down.

A payload is only trusted for that when the envelope cannot be lagging behind
the match: the server never publishes a lagging packet unless it carries
events, and a lagging packet never carries match-level events. So an envelope
without events, or with at least one match-level event, shows the current
state of the match. Even then the bomb never runs backwards within a round,
because two clients can disagree about it for a moment.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import ROUND_PHASES
from .protocol import (
    SIDES,
    SRV_BOMB_DEFUSED,
    SRV_BOMB_EXPLODED,
    SRV_BOMB_PLANTED,
    SRV_HALFTIME,
    SRV_MATCH_LEFT,
    SRV_MATCH_OVER,
    SRV_MATCH_START,
    SRV_PLAYER_DEATH,
    SRV_ROUND_FREEZETIME,
    SRV_ROUND_LIVE,
    SRV_ROUND_OVER,
    Envelope,
    GameEvent,
    as_dict,
    as_int,
)

_BOMB_RANK = {"": 0, "planted": 1, "defused": 2, "exploded": 2}
_BOMB_EVENTS = {
    SRV_BOMB_PLANTED: "planted",
    SRV_BOMB_DEFUSED: "defused",
    SRV_BOMB_EXPLODED: "exploded",
}
_BOMB_EVENT_BY_STATE = {state: event for event, state in _BOMB_EVENTS.items()}
_PHASE_EVENTS = {
    SRV_ROUND_FREEZETIME: "freezetime",
    SRV_ROUND_LIVE: "live",
    SRV_ROUND_OVER: "over",
}
_PHASE_EVENT_BY_STATE = {state: event for event, state in _PHASE_EVENTS.items()}


@dataclass(frozen=True, slots=True)
class GameSnapshot:
    """What the entities show. The default instance is the idle state."""

    in_match: bool = False
    match_id: str | None = None
    map_name: str | None = None
    round_phase: str | None = None
    bomb_planted: bool = False
    score_ct: int | None = None
    score_t: int | None = None
    round_number: int | None = None
    health: int | None = None
    armor: int | None = None
    money: int | None = None


IDLE_SNAPSHOT = GameSnapshot()


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Outcome of feeding one envelope into the tracker."""

    # The packet came from a running match (as opposed to the menu).
    in_match: bool = False
    # The packet belongs to the match this device follows.
    followed: bool = False
    # The snapshot changed.
    changed: bool = False
    # The envelope's events that are news. Duplicates and the events of a
    # match this device does not follow are left out.
    events: tuple[GameEvent, ...] = ()
    # Transitions that were missed (connection gap) and are made up for.
    resync_events: tuple[GameEvent, ...] = ()
    # Number of events dropped as duplicates.
    duplicates: int = 0


def _lower(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


class GameStateTracker:
    """Follow one match and keep the state the entities show."""

    # A missed transition is only made up for (as a late trigger) when the
    # connection gap was short; after that the state is adopted silently.
    RESYNC_MAX_GAP = 10.0
    # With packets from two different matches (possible while the owner is
    # not playing), stay with the current one as long as it is alive.
    MATCH_STICKY = 15.0
    # match_start and halftime repeated within this time are duplicates.
    REPEAT_WINDOW = 30.0

    def __init__(self) -> None:
        # subject -> key of the match that player is in
        self._active: dict[str, str] = {}
        self._clear_match()

    def _clear_match(self) -> None:
        self._match_key = ""
        self._match_id: str | None = None
        self._last_seen: float | None = None
        self._baseline = False
        self._phase: str | None = None
        self._bomb = ""
        self._round: int | None = None
        # False while the round number is only a guess from the score.
        self._round_exact = False
        self._map: str | None = None
        self._ct: int | None = None
        self._t: int | None = None
        self._health: int | None = None
        self._armor: int | None = None
        self._money: int | None = None
        self._sides: dict[str, str] = {}
        self._last_side = ""
        self._over = False
        self._once_at: dict[str, float] = {}

    # ------------------------------------------------------------------

    @property
    def snapshot(self) -> GameSnapshot:
        """Return the current state for the entities."""
        if not self._match_key:
            return IDLE_SNAPSHOT
        return GameSnapshot(
            in_match=True,
            match_id=self._match_id,
            map_name=self._map,
            round_phase=self._phase,
            bomb_planted=self._bomb == "planted",
            score_ct=self._ct,
            score_t=self._t,
            round_number=self._round,
            health=self._health,
            armor=self._armor,
            money=self._money,
        )

    @property
    def active_subjects(self) -> tuple[str, ...]:
        """Return the players currently known to be in a match."""
        return tuple(self._active)

    def reset(self) -> bool:
        """Go back to the idle state. Returns True if the snapshot changed."""
        changed = bool(self._match_key)
        self._active.clear()
        self._clear_match()
        return changed

    def reference_side(self, owner: str | None, *, any_player: bool) -> str:
        """Return the side a match result is judged from ("" if unknown).

        That is the owner's side. Without a known owner, or when the device
        reacts to all players, the side of the player seen last is used.
        """
        if owner and owner in self._sides:
            return self._sides[owner]
        if owner is None or any_player:
            return self._last_side
        return ""

    # ------------------------------------------------------------------

    def apply(self, env: Envelope, *, owner: str | None, now: float) -> ApplyResult:
        """Feed one envelope. `now` is a monotonic timestamp in seconds."""
        before = self.snapshot
        subject = env.subject

        explicit_left = {
            ev.subject or subject for ev in env.events if ev.type == SRV_MATCH_LEFT
        }
        for left in explicit_left:
            self._active.pop(left, None)

        if not env.in_match:
            # A packet from the menu: its sender is not in a match (any more).
            missed: tuple[GameEvent, ...] = ()
            if subject not in explicit_left and subject in self._active:
                # The match_left event itself was missed.
                del self._active[subject]
                missed = (
                    GameEvent(type=SRV_MATCH_LEFT, at=env.occurred_at, subject=subject),
                )
            if self._match_key and self._match_key not in self._active.values():
                self._clear_match()
            return ApplyResult(
                changed=self.snapshot != before,
                events=env.events,
                resync_events=missed,
            )

        key = env.match_key
        self._active[subject] = key
        if not self._should_follow(key, subject, owner, now):
            return ApplyResult(in_match=True)

        if key != self._match_key:
            self._clear_match()
            self._match_key = key
            self._match_id = env.match_id or None
        gap = None if self._last_seen is None else now - self._last_seen
        self._last_seen = now

        side = env.player_side.upper()
        if side in SIDES:
            self._sides[subject] = side
            self._last_side = side

        if (owner is None or subject == owner) and env.own_view:
            self._update_player(env)

        match_level = False
        fresh: list[GameEvent] = []
        for event in env.events:
            if not event.subject:
                match_level = True
            if self._apply_event(event, sender=subject, owner=owner, now=now):
                fresh.append(event)

        has_events = bool(env.events)
        trusted = not has_events or match_level
        resync: tuple[GameEvent, ...] = ()
        if trusted:
            resync = self._sync_round_state(env, has_events=has_events, gap=gap)
        self._update_map(env, trusted=trusted)

        return ApplyResult(
            in_match=True,
            followed=True,
            changed=self.snapshot != before,
            events=tuple(fresh),
            resync_events=resync,
            duplicates=len(env.events) - len(fresh),
        )

    # ------------------------------------------------------------------

    def _should_follow(
        self, key: str, subject: str, owner: str | None, now: float
    ) -> bool:
        current = self._match_key
        if not current or key == current:
            return True
        if owner and subject == owner:
            return True
        others = [s for s, m in self._active.items() if m == current and s != subject]
        if not others:
            return True
        if owner and owner in others:
            return False
        return self._last_seen is None or now - self._last_seen > self.MATCH_STICKY

    def _enter_round(self, number: int | None) -> bool:
        """Note the round an event belongs to. Returns True for a new round."""
        if number is None or number <= 0:
            return False
        if number == self._round:
            self._round_exact = True
            return False
        # A number that merely corrects a guess does not start a round.
        began = self._round_exact or self._round is None
        self._round = number
        self._round_exact = True
        if began:
            self._bomb = ""
        return began

    def _apply_event(
        self, event: GameEvent, *, sender: str, owner: str | None, now: float
    ) -> bool:
        """Apply one server event. Returns False if it is a duplicate."""
        kind = event.type

        if kind in _PHASE_EVENTS:
            phase = _PHASE_EVENTS[kind]
            began = self._enter_round(event.round)
            if not began and phase == self._phase:
                return False
            self._phase = phase
            if phase == "freezetime":
                self._bomb = ""
            return True

        if kind in _BOMB_EVENTS:
            self._enter_round(event.round)
            bomb = _BOMB_EVENTS[kind]
            if _BOMB_RANK[bomb] <= _BOMB_RANK[self._bomb]:
                return False
            self._bomb = bomb
            return True

        if kind == SRV_MATCH_OVER:
            if self._over:
                return False
            self._over = True
            return True

        if kind in (SRV_MATCH_START, SRV_HALFTIME):
            last = self._once_at.get(kind)
            if last is not None and now - last < self.REPEAT_WINDOW:
                return False
            self._once_at[kind] = now
            return True

        if kind == SRV_PLAYER_DEATH:
            victim = event.subject or sender
            if owner is None or victim == owner:
                self._health = 0
        return True

    def _update_player(self, env: Envelope) -> None:
        state = as_dict(env.player_block.get("state"))
        health = as_int(state.get("health"))
        armor = as_int(state.get("armor"))
        money = as_int(state.get("money"))
        if health is not None:
            self._health = health
        if armor is not None:
            self._armor = armor
        if money is not None:
            self._money = money

    @staticmethod
    def _scores(env: Envelope) -> tuple[int | None, int | None]:
        map_block = env.map_block
        return (
            as_int(as_dict(map_block.get("team_ct")).get("score")),
            as_int(as_dict(map_block.get("team_t")).get("score")),
        )

    def _update_map(self, env: Envelope, *, trusted: bool) -> None:
        if not env.map_block:
            return
        ct, t = self._scores(env)
        if (
            not trusted
            and ct is not None
            and t is not None
            and self._ct is not None
            and self._t is not None
            and ct + t < self._ct + self._t
        ):
            # Possibly a lagging packet: never let the score run backwards.
            return
        if env.map_name:
            self._map = env.map_name
        if ct is not None:
            self._ct = ct
        if t is not None:
            self._t = t

    def _round_from_payload(self, env: Envelope, phase: str) -> int | None:
        """Estimate the round number from the score (rounds played so far)."""
        ct, t = self._scores(env)
        if ct is None or t is None:
            return None
        completed = ct + t
        return completed + 1 if phase != "over" else max(completed, 1)

    def _sync_round_state(
        self, env: Envelope, *, has_events: bool, gap: float | None
    ) -> tuple[GameEvent, ...]:
        """Align round phase and bomb with a payload that is not lagging."""
        round_block = env.round_block
        phase = _lower(round_block.get("phase"))
        if phase not in ROUND_PHASES:
            return ()
        bomb = _lower(round_block.get("bomb"))
        if bomb not in _BOMB_RANK:
            bomb = ""

        guess = self._round_from_payload(env, phase)
        new_round = (
            guess is not None and self._round is not None and guess > self._round
        )
        if self._baseline and not (new_round or phase == "freezetime"):
            # Within a round the bomb only moves forward.
            if _BOMB_RANK[bomb] < _BOMB_RANK[self._bomb]:
                bomb = self._bomb

        missed: list[GameEvent] = []
        if (
            self._baseline
            and not has_events
            and gap is not None
            and gap <= self.RESYNC_MAX_GAP
        ):
            number = guess if new_round else self._round
            if phase != self._phase:
                winner = round_block.get("win_team")
                missed.append(
                    GameEvent(
                        type=_PHASE_EVENT_BY_STATE[phase],
                        at=env.occurred_at,
                        round=number,
                        side=(
                            winner.strip().upper()
                            if phase == "over" and isinstance(winner, str)
                            else ""
                        ),
                    )
                )
            if bomb and bomb != self._bomb:
                missed.append(
                    GameEvent(
                        type=_BOMB_EVENT_BY_STATE[bomb],
                        at=env.occurred_at,
                        round=number,
                    )
                )

        self._baseline = True
        self._phase = phase
        self._bomb = bomb
        if guess is not None and (self._round is None or guess > self._round):
            self._round = guess
            self._round_exact = False
        return tuple(missed)
