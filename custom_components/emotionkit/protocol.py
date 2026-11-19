"""Wire formats of the EmotionKit device protocol (plain Python).

Covers the event envelope (docs/protocol/device-events-v1.md) and the device
status / config exchange (docs/protocol/device-capability-handshake-v1.md).
Nothing in here imports Home Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any

from .const import DEVICE_KIND, MAX_DEVICE_NAME_LENGTH, MAX_IDLE_TIMEOUT

SCHEMA_VERSION = "1.0"
PROTOCOL_VERSION = "1.0"

ROLE_OWNER = "owner"
ROLE_USER = "user"

SIDES = ("CT", "T")

# Event types sent by the control-plane.
SRV_MATCH_START = "match_start"
SRV_MATCH_OVER = "match_over"
SRV_HALFTIME = "halftime"
SRV_SIDE_SWITCH = "side_switch"
SRV_ROUND_FREEZETIME = "round_freezetime"
SRV_ROUND_LIVE = "round_live"
SRV_ROUND_OVER = "round_over"
SRV_BOMB_PLANTED = "bomb_planted"
SRV_BOMB_DEFUSED = "bomb_defused"
SRV_BOMB_EXPLODED = "bomb_exploded"
SRV_TIMEOUT = "timeout"
SRV_PLAYER_KILL = "player_kill"
SRV_PLAYER_DEATH = "player_death"
SRV_PLAYER_FLASHED = "player_flashed"
SRV_PLAYER_BURNING = "player_burning"
SRV_PLAYER_LOW_HEALTH = "player_low_health"
SRV_PLAYER_MVP = "player_mvp"
SRV_ACE = "ace"
SRV_MATCH_LEFT = "match_left"

_TIMESTAMP_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})"
    r"(?:[.,](\d+))?\s*(Z|z|[+-]\d{2}(?::?\d{2})?)?$"
)


def parse_timestamp(value: object) -> float | None:
    """Parse an RFC 3339 timestamp into epoch seconds.

    Go writes up to nine fractional digits, which datetime.fromisoformat does
    not accept on every Python version, hence the explicit parser. Timestamps
    without an offset are taken as UTC. Returns None for anything else.
    """
    if not isinstance(value, str):
        return None
    match = _TIMESTAMP_RE.match(value.strip())
    if match is None:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    micro = int((fraction or "0")[:6].ljust(6, "0"))
    tzinfo = timezone.utc
    if zone and zone not in ("Z", "z"):
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
        tzinfo = timezone(sign * offset)
    try:
        moment = datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
            micro,
            tzinfo=tzinfo,
        )
        return moment.timestamp()
    except (ValueError, OverflowError):
        return None


def _load_json_object(raw: object) -> dict[str, Any] | None:
    """Decode raw MQTT payload bytes into a JSON object, or None."""
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def as_int(value: object) -> int | None:
    """Return value as int if it is a real number (bool is not a number)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        return int(value)
    return None


def as_dict(value: object) -> Mapping[str, Any]:
    """Return value if it is a JSON object, else an empty mapping."""
    return value if isinstance(value, Mapping) else {}


# ---------------------------------------------------------------------------
# Event envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GameEvent:
    """One entry of the envelope's `events` list."""

    type: str
    at: str = ""
    round: int | None = None
    # Player the event is about; empty for match-level events.
    subject: str = ""
    side: str = ""
    reason: str = ""
    value: int | None = None
    headshot: bool = False

    @property
    def player_scoped(self) -> bool:
        """Return True if the event concerns a single player."""
        return bool(self.subject)


@dataclass(frozen=True, slots=True)
class Envelope:
    """One message on devices/<mqttUsername>/events."""

    id: str = ""
    team_id: str = ""
    player_id: str = ""
    # SteamID of the player whose client sent the packet.
    subject: str = ""
    player_side: str = ""
    occurred_at: str = ""
    # occurred_at as epoch seconds (server clock), None if missing/invalid.
    occurred_ts: float | None = None
    match_id: str = ""
    events: tuple[GameEvent, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)

    # --- payload accessors (never raise on malformed data) ---

    @property
    def map_block(self) -> Mapping[str, Any]:
        return as_dict(self.payload.get("map"))

    @property
    def round_block(self) -> Mapping[str, Any]:
        return as_dict(self.payload.get("round"))

    @property
    def player_block(self) -> Mapping[str, Any]:
        return as_dict(self.payload.get("player"))

    @property
    def map_name(self) -> str:
        return _text(self.map_block.get("name"))

    @property
    def in_match(self) -> bool:
        """Return True if the packet belongs to a running map."""
        return bool(self.match_id) or bool(self.map_name)

    @property
    def match_key(self) -> str:
        """Identify the match (the id, or the map name without a tracker)."""
        if self.match_id:
            return self.match_id
        return f"map:{self.map_name}" if self.map_name else ""

    @property
    def own_view(self) -> bool:
        """Return True if `payload.player` describes the sender themselves.

        A dead player's client reports the teammate being spectated; the two
        cases are told apart by comparing player.steamid with the provider.
        """
        player_id = _text(self.player_block.get("steamid"))
        if not player_id:
            return False
        provider = _text(as_dict(self.payload.get("provider")).get("steamid"))
        return player_id == (provider or self.subject)


def _parse_event(item: object) -> GameEvent | None:
    if not isinstance(item, Mapping):
        return None
    event_type = _text(item.get("type"))
    if not event_type:
        return None
    return GameEvent(
        type=event_type,
        at=_text(item.get("at")),
        round=as_int(item.get("round")),
        subject=_text(item.get("subject")),
        side=_text(item.get("side")),
        reason=_text(item.get("reason")),
        value=as_int(item.get("value")),
        headshot=item.get("headshot") is True,
    )


def parse_envelope(raw: object) -> Envelope | None:
    """Parse an event message. Returns None if it is not a JSON object."""
    data = _load_json_object(raw)
    if data is None:
        return None

    player_id = _text(data.get("player_id"))
    subject = _text(data.get("subject"))
    if not subject and ":" in player_id:
        # player_id is "<team>:<SteamID>".
        subject = player_id.rsplit(":", 1)[1]

    raw_events = data.get("events")
    events: list[GameEvent] = []
    if isinstance(raw_events, list):
        for item in raw_events:
            event = _parse_event(item)
            if event is not None:
                events.append(event)

    occurred_at = _text(data.get("occurred_at"))
    return Envelope(
        id=_text(data.get("id")),
        team_id=_text(data.get("team_id")),
        player_id=player_id,
        subject=subject,
        player_side=_text(data.get("player_side")),
        occurred_at=occurred_at,
        occurred_ts=parse_timestamp(occurred_at),
        match_id=_text(data.get("match_id")) or _text(data.get("match_fingerprint")),
        events=tuple(events),
        payload=as_dict(data.get("payload")),
    )


# ---------------------------------------------------------------------------
# Device config (devices/<mqttUsername>/config)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """A config message. Fields that were absent or invalid are None."""

    enabled: bool | None = None
    actor_name: str | None = None
    idle_timeout_s: int | None = None
    # subject -> role; None if the message did not carry the key.
    allowed_subjects: dict[str, str] | None = None


def _parse_allowed_subjects(raw: object) -> dict[str, str]:
    if isinstance(raw, Mapping):
        return {
            subject.strip(): role.strip().lower()
            for subject, role in raw.items()
            if isinstance(subject, str)
            and isinstance(role, str)
            and subject.strip()
            and role.strip()
        }
    if isinstance(raw, list):
        # Legacy form: a plain list of subjects without roles.
        return {s.strip(): ROLE_USER for s in raw if isinstance(s, str) and s.strip()}
    return {}


def parse_device_config(raw: object) -> DeviceConfig | None:
    """Parse a config message. Returns None if it is not a JSON object."""
    data = _load_json_object(raw)
    if data is None:
        return None

    enabled = data.get("enabled")
    actor_name: str | None = None
    name = data.get("actor_name")
    if isinstance(name, str) and name.strip():
        actor_name = name.strip()[:MAX_DEVICE_NAME_LENGTH]

    idle_timeout_s = as_int(data.get("idle_timeout_s"))
    if idle_timeout_s is not None and not 0 <= idle_timeout_s <= MAX_IDLE_TIMEOUT:
        idle_timeout_s = None

    return DeviceConfig(
        enabled=enabled if isinstance(enabled, bool) else None,
        actor_name=actor_name,
        idle_timeout_s=idle_timeout_s,
        allowed_subjects=(
            _parse_allowed_subjects(data["allowed_subjects"])
            if "allowed_subjects" in data
            else None
        ),
    )


def owner_of(allowed_subjects: Mapping[str, str] | None) -> str | None:
    """Return the subject with the owner role, if the server told us one."""
    if not allowed_subjects:
        return None
    for subject, role in allowed_subjects.items():
        if role == ROLE_OWNER:
            return subject
    return None


# ---------------------------------------------------------------------------
# Device status (devices/<mqttUsername>/status)
# ---------------------------------------------------------------------------

CONFIG_SCHEMA: tuple[dict[str, Any], ...] = (
    {
        "key": "actor_name",
        "label": "Name",
        "type": "string",
        "required": True,
        "max_length": MAX_DEVICE_NAME_LENGTH,
        "group": "general",
    },
    {
        "key": "enabled",
        "label": "Enabled",
        "type": "bool",
        "required": True,
        "group": "general",
    },
    {
        "key": "idle_timeout_s",
        "label": "Idle timeout (s)",
        "type": "number",
        "required": True,
        "min": 0,
        "max": MAX_IDLE_TIMEOUT,
        "group": "general",
    },
)


def build_status_payload(
    *,
    device_id: str,
    device_name: str,
    enabled: bool,
    idle_timeout: int,
    allowed_subjects: Mapping[str, str],
) -> str:
    """Build the retained "online" status the control-plane discovers us by."""
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "device_id": device_id,
            "name": device_name,
            "kind": DEVICE_KIND,
            "online": True,
            "config": {
                "actor_name": device_name,
                "enabled": enabled,
                "idle_timeout_s": idle_timeout,
                "allowed_subjects": dict(allowed_subjects),
            },
            "config_schema": [dict(item) for item in CONFIG_SCHEMA],
            "capabilities": {
                "intent_types": ["event-mapping", "ha-trigger"],
                "supports_idle": True,
                "supports_state_restore": False,
                "supports_ack": False,
                "protocol_version": PROTOCOL_VERSION,
            },
        }
    )


def build_offline_payload(*, device_id: str, device_name: str) -> str:
    """Build the retained "offline" status (last will and clean shutdown)."""
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "device_id": device_id,
            "name": device_name,
            "kind": DEVICE_KIND,
            "online": False,
        }
    )
