"""Message processing of the integration (plain Python, no Home Assistant).

EventProcessor turns raw MQTT messages into triggers and entity state. The
Home Assistant side (coordinator.py) only moves bytes in and results out, so
everything that decides *what* happens can be unit-tested without it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .clock import DEFAULT_MAX_AGE, ClockOffsetEstimator
from .const import MAX_DEVICE_NAME_LENGTH, MAX_IDLE_TIMEOUT
from .protocol import (
    DeviceConfig,
    as_int,
    owner_of,
    parse_device_config,
    parse_envelope,
)
from .state import GameSnapshot, GameStateTracker
from .triggers import Trigger, map_events

# Server-side settings this integration applies locally.
REMOTE_ACTOR_NAME = "actor_name"
REMOTE_IDLE_TIMEOUT = "idle_timeout_s"
_REMOTE_KEYS = (REMOTE_ACTOR_NAME, REMOTE_IDLE_TIMEOUT)

IGNORED_INVALID = "invalid"
IGNORED_DISABLED = "disabled"
IGNORED_DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class EventResult:
    """What to do after an event message."""

    triggers: tuple[Trigger, ...] = ()
    # The snapshot for the entities changed.
    state_changed: bool = False
    # The message came from a running match: the idle timer starts over.
    in_match: bool = False
    # The message was too old to trigger anything.
    stale: bool = False
    # Why the message was dropped altogether, if it was.
    ignored: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigResult:
    """What to do after a config message."""

    valid: bool = False
    state_changed: bool = False
    enabled_changed: bool = False
    # Settings changed in the web UI that have to be applied locally,
    # keyed by REMOTE_ACTOR_NAME / REMOTE_IDLE_TIMEOUT.
    updates: dict[str, Any] = field(default_factory=dict)
    # The remembered server-side values changed and should be persisted.
    baseline_changed: bool = False


def clean_remote_baseline(raw: object) -> dict[str, Any] | None:
    """Validate a stored baseline. None means "never saw a config message"."""
    if not isinstance(raw, Mapping):
        return None
    baseline: dict[str, Any] = {}
    name = raw.get(REMOTE_ACTOR_NAME)
    if isinstance(name, str) and name.strip():
        baseline[REMOTE_ACTOR_NAME] = name.strip()[:MAX_DEVICE_NAME_LENGTH]
    timeout = as_int(raw.get(REMOTE_IDLE_TIMEOUT))
    if timeout is not None and 0 <= timeout <= MAX_IDLE_TIMEOUT:
        baseline[REMOTE_IDLE_TIMEOUT] = timeout
    return baseline


def reconcile_remote_config(
    config: DeviceConfig,
    baseline: Mapping[str, Any] | None,
    *,
    retained: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Find the server-side settings that are new and must be applied.

    The config topic is retained and always carries the full config, so after
    every reconnect the integration is handed the last server-side values
    again. Applying them blindly would undo a change the user has made in
    Home Assistant since. Therefore the last seen server-side value of each
    setting is remembered (the baseline) and a value is only applied when it
    differs from that, i.e. when it was changed on the server.

    A value seen for the first time is applied, except when it reaches an
    installation that predates this bookkeeping (baseline None) as a retained
    message: that one describes the past, and the local settings win.

    Returns (updates, new_baseline).
    """
    new_baseline = dict(baseline or {})
    updates: dict[str, Any] = {}
    for key in _REMOTE_KEYS:
        value = getattr(config, key)
        if value is None:
            continue
        if key in new_baseline:
            if new_baseline[key] != value:
                updates[key] = value
        elif baseline is not None or not retained:
            updates[key] = value
        new_baseline[key] = value
    return updates, new_baseline


class EventProcessor:
    """Turn MQTT messages into triggers and entity state."""

    def __init__(
        self,
        *,
        all_players: bool = False,
        remote_baseline: Mapping[str, Any] | None = None,
        max_event_age: float = DEFAULT_MAX_AGE,
    ) -> None:
        self.all_players = all_players
        self.enabled = True
        # subject -> role, as pushed by the server.
        self.allowed_subjects: dict[str, str] = {}
        self.config_received = False
        self.remote_baseline: dict[str, Any] | None = (
            dict(remote_baseline) if remote_baseline is not None else None
        )
        self.tracker = GameStateTracker()
        self.clock = ClockOffsetEstimator(max_age=max_event_age)
        self.last_trigger_type = ""
        self.last_trigger_at = ""
        self.stats: dict[str, int] = {
            "messages": 0,
            "invalid": 0,
            "ignored_disabled": 0,
            "duplicate_messages": 0,
            "duplicate_events": 0,
            "stale": 0,
            "triggers": 0,
            "resync_triggers": 0,
            "config_messages": 0,
        }
        # Envelope ids seen recently: QoS 1 may deliver a message twice.
        self._recent_ids: deque[str] = deque(maxlen=256)
        self._recent_id_set: set[str] = set()

    @property
    def owner(self) -> str | None:
        """Return the device owner's subject, if the server told us."""
        return owner_of(self.allowed_subjects)

    @property
    def snapshot(self) -> GameSnapshot:
        """Return the state for the entities."""
        return self.tracker.snapshot

    def reset_state(self) -> bool:
        """Put the entities back to idle. Returns True if anything changed."""
        return self.tracker.reset()

    # ------------------------------------------------------------------

    def _remember(self, envelope_id: str) -> bool:
        """Return True if the id was seen before, else remember it."""
        if not envelope_id:
            return False
        if envelope_id in self._recent_id_set:
            return True
        if len(self._recent_ids) == self._recent_ids.maxlen:
            self._recent_id_set.discard(self._recent_ids[0])
        self._recent_ids.append(envelope_id)
        self._recent_id_set.add(envelope_id)
        return False

    def handle_event(self, raw: object, *, monotonic: float) -> EventResult:
        """Process a message from the events topic."""
        envelope = parse_envelope(raw)
        if envelope is None:
            self.stats["invalid"] += 1
            return EventResult(ignored=IGNORED_INVALID)
        self.stats["messages"] += 1

        if not self.enabled:
            self.stats["ignored_disabled"] += 1
            return EventResult(ignored=IGNORED_DISABLED)

        if self._remember(envelope.id):
            self.stats["duplicate_messages"] += 1
            return EventResult(ignored=IGNORED_DUPLICATE)

        # The state is always updated; a message that is too old merely does
        # not trigger anything (a bomb plant reported half a minute late
        # should not turn the lights red any more).
        stale = self.clock.is_stale(envelope.occurred_ts, monotonic)
        owner = self.owner
        applied = self.tracker.apply(envelope, owner=owner, now=monotonic)
        self.stats["duplicate_events"] += applied.duplicates

        triggers: list[Trigger] = []
        if stale:
            self.stats["stale"] += 1
        else:
            reference_side = self.tracker.reference_side(
                owner, any_player=self.all_players
            )
            triggers = map_events(
                envelope,
                applied.events,
                owner=owner,
                all_players=self.all_players,
                reference_side=reference_side,
            )
            if applied.resync_events:
                late = map_events(
                    envelope,
                    applied.resync_events,
                    owner=owner,
                    all_players=self.all_players,
                    reference_side=reference_side,
                    resync=True,
                )
                self.stats["resync_triggers"] += len(late)
                triggers.extend(late)

        if triggers:
            self.stats["triggers"] += len(triggers)
            self.last_trigger_type = triggers[-1].type
            self.last_trigger_at = envelope.occurred_at

        return EventResult(
            triggers=tuple(triggers),
            state_changed=applied.changed,
            in_match=applied.in_match,
            stale=stale,
        )

    def handle_config(self, raw: object, *, retained: bool) -> ConfigResult:
        """Process a message from the config topic."""
        config = parse_device_config(raw)
        if config is None:
            self.stats["invalid"] += 1
            return ConfigResult()
        self.stats["config_messages"] += 1
        self.config_received = True

        state_changed = False
        enabled_changed = False
        if config.enabled is not None and config.enabled != self.enabled:
            self.enabled = config.enabled
            enabled_changed = True
            # Whatever happens while disabled is not seen, so start from
            # scratch in both directions.
            state_changed = self.tracker.reset()

        if config.allowed_subjects is not None:
            self.allowed_subjects = config.allowed_subjects

        updates, baseline = reconcile_remote_config(
            config, self.remote_baseline, retained=retained
        )
        baseline_changed = baseline != self.remote_baseline
        self.remote_baseline = baseline

        return ConfigResult(
            valid=True,
            state_changed=state_changed,
            enabled_changed=enabled_changed,
            updates=updates,
            baseline_changed=baseline_changed,
        )


class ActivityWatchdog:
    """Decide when the idle trigger fires and when the game state expires.

    Both count from the last in-match message. The watchdog does no timing
    itself: the caller asks for the next deadline, sleeps until then and
    polls. That way a busy match does not reschedule a timer per message.

    idle_timeout: seconds until the `idle` trigger fires; 0 disables it.
    state_timeout: seconds until the entities fall back to idle. Never
        shorter than the idle timeout.
    """

    def __init__(self, idle_timeout: float, state_timeout: float) -> None:
        self._last: float | None = None
        self._idle_pending = False
        self._expiry_pending = False
        self.idle_timeout = 0.0
        self.state_timeout = float(state_timeout)
        self._min_state_timeout = float(state_timeout)
        self.set_idle_timeout(idle_timeout)

    def set_idle_timeout(self, idle_timeout: float) -> None:
        """Change the idle timeout; a running countdown keeps its start."""
        self.idle_timeout = max(float(idle_timeout), 0.0)
        self.state_timeout = max(self._min_state_timeout, self.idle_timeout)
        if self.idle_timeout <= 0:
            self._idle_pending = False

    def touch(self, now: float) -> None:
        """Note an in-match message."""
        self._last = now
        self._idle_pending = self.idle_timeout > 0
        self._expiry_pending = True

    def stop(self) -> None:
        """Cancel both countdowns."""
        self._idle_pending = False
        self._expiry_pending = False

    def next_deadline(self) -> float | None:
        """Return the monotonic time of the next thing to happen, if any."""
        if self._last is None:
            return None
        deadlines = []
        if self._idle_pending:
            deadlines.append(self._last + self.idle_timeout)
        if self._expiry_pending:
            deadlines.append(self._last + self.state_timeout)
        return min(deadlines) if deadlines else None

    def poll(self, now: float) -> tuple[bool, bool]:
        """Return (fire the idle trigger, expire the state) for this moment."""
        if self._last is None:
            return False, False
        idle = False
        expired = False
        if self._idle_pending and now - self._last >= self.idle_timeout:
            self._idle_pending = False
            idle = True
        if self._expiry_pending and now - self._last >= self.state_timeout:
            self._expiry_pending = False
            expired = True
        return idle, expired
