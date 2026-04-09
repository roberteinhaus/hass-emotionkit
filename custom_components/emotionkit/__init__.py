"""EmotionKit integration — CS2 game events for Home Assistant automations."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from urllib.parse import urlparse

import aiohttp
import aiomqtt

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import CALLBACK_TYPE, async_call_later

from .const import (
    DEFAULT_CONTROL_PLANE_URL,
    DEFAULT_IDLE_TIMEOUT,
    DEVICE_KIND,
    DOMAIN,
    EVENT_BOMB_DEFUSED,
    EVENT_BOMB_EXPLODED,
    EVENT_BOMB_PLANTED,
    EVENT_EMOTIONKIT,
    EVENT_IDLE,
    EVENT_FREEZETIME,
    EVENT_ROUND_LIVE,
    EVENT_ROUND_OVER_CT,
    EVENT_ROUND_OVER_T,
    TOPIC_CONFIG,
    TOPIC_EVENTS,
    TOPIC_STATUS,
)

_LOGGER = logging.getLogger(__name__)

# Role-priority constants – owner trumps admin, admin trumps plain user.
_ROLE_PRIORITY = {"owner": 3, "admin": 2, "user": 1}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EmotionKit from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    mqtt_username: str = entry.data["mqtt_username"]
    mqtt_password: str = entry.data["mqtt_password"]
    mqtt_broker: str = entry.data.get("mqtt_broker", "")
    device_name: str = entry.data.get("device_name", "EmotionKit")
    device_id: str = entry.data["device_id"]
    control_plane_url: str = entry.data.get(
        "control_plane_url", DEFAULT_CONTROL_PLANE_URL
    )
    idle_timeout: int = entry.options.get("idle_timeout", DEFAULT_IDLE_TIMEOUT)

    # Resolve broker URL if missing from stored config (legacy installs).
    if not mqtt_broker:
        mqtt_broker = await _resolve_broker_url(control_plane_url)
        _LOGGER.info("Resolved MQTT broker URL to %s", mqtt_broker)

    # Parse broker URL (tcp://host:port or tls://host:port).
    host, port, use_tls = _parse_broker_url(mqtt_broker)
    _LOGGER.info(
        "EmotionKit MQTT broker configured as %s (host=%s port=%s tls=%s)",
        mqtt_broker,
        host,
        port,
        use_tls,
    )
    _LOGGER.info("EmotionKit idle timeout set to %s seconds", idle_timeout)

    # Register device in HA device registry.
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device_id)},
        name=device_name,
        manufacturer="EmotionKit",
        model="CS2 Game Events",
    )

    status_topic = TOPIC_STATUS.format(mqtt_username)
    config_topic = TOPIC_CONFIG.format(mqtt_username)

    # State tracking for event deduplication.
    state = _GameState()
    enabled = True
    idle_handle: CALLBACK_TYPE | None = None
    last_event_type = ""
    last_event_at = ""

    def _cancel_idle_timer() -> None:
        nonlocal idle_handle
        if idle_handle is not None:
            idle_handle()
            idle_handle = None

    def _schedule_idle_timer() -> None:
        nonlocal idle_handle
        if idle_timeout <= 0:
            return
        _cancel_idle_timer()
        idle_handle = async_call_later(hass, idle_timeout, _fire_idle)

    def _fire_idle(_: object) -> None:
        nonlocal idle_handle
        idle_handle = None
        _LOGGER.info(
            "EmotionKit idle state reached after %s seconds", idle_timeout
        )
        hass.bus.async_fire(
            EVENT_EMOTIONKIT,
            {
                "device_id": device_entry.id,
                "type": EVENT_IDLE,
                "timeout": idle_timeout,
                "last_event_type": last_event_type,
                "last_event_at": last_event_at,
            },
        )

    lwt_payload = json.dumps(
        {"name": device_name, "kind": DEVICE_KIND, "online": False}
    )

    status_payload = json.dumps(
        {
            "name": device_name,
            "kind": DEVICE_KIND,
            "online": True,
            "config": {"actor_name": device_name, "enabled": state._enabled_override},
            "config_schema": [
                {"key": "actor_name", "label": "Name", "type": "string"},
            ],
        }
    )

    async def _mqtt_loop() -> None:
        """Main MQTT loop with auto-reconnect."""
        nonlocal enabled

        tls_params = aiomqtt.TLSParameters(ssl.create_default_context()) if use_tls else None

        while True:
            try:
                async with aiomqtt.Client(
                    hostname=host,
                    port=port,
                    username=mqtt_username,
                    password=mqtt_password,
                    tls_params=tls_params,
                    will=aiomqtt.Will(
                        topic=status_topic, payload=lwt_payload, qos=1, retain=True
                    ),
                    keepalive=30,
                ) as client:
                    _LOGGER.info("Connected to EmotionKit MQTT broker %s:%s", host, port)

                    # Publish online status (retained).
                    await client.publish(
                        status_topic, status_payload, qos=1, retain=True
                    )

                    # Subscribe to game events and config updates.
                    await client.subscribe(TOPIC_EVENTS, qos=1)
                    await client.subscribe(config_topic, qos=1)
                    _LOGGER.debug(
                        "Subscribed to %s and %s", TOPIC_EVENTS, config_topic
                    )

                    async for message in client.messages:
                        topic = str(message.topic)

                        if topic == config_topic:
                            _handle_config(message.payload, state)
                            if hasattr(state, "_enabled_override"):
                                enabled = state._enabled_override
                            continue

                        # Must be a team/+/events topic.
                        if not enabled:
                            continue

                        # Filter by allowed subjects using match fingerprints.
                        # Each subject's latest match_fingerprint is tracked.
                        # The active match is determined by role priority:
                        # owner > admin majority > user majority.

                        if state._allowed_subjects:
                            subject = _subject_from_payload(message.payload)
                            fp = _fingerprint_from_payload(message.payload)
                            role = state._allowed_subjects.get(subject, "")

                            # Track fingerprint for known subjects.
                            if role and fp:
                                state._subject_fps[subject] = (fp, role)

                            # Determine active fingerprint via priority.
                            active_fp = _active_fingerprint(state._subject_fps)

                            if active_fp and fp:
                                if fp != active_fp:
                                    continue
                            elif not role:
                                continue

                        events = _extract_events(message.payload, state)
                        if events:
                            last_event_type, last_event_data = events[-1]
                            last_event_at = last_event_data.get("occurred_at", "")
                            _schedule_idle_timer()

                        for event_type, event_data in events:
                            hass.bus.async_fire(
                                EVENT_EMOTIONKIT,
                                {
                                    "device_id": device_entry.id,
                                    "type": event_type,
                                    **event_data,
                                },
                            )
                            _LOGGER.debug("Fired %s: %s", EVENT_EMOTIONKIT, event_type)

            except aiomqtt.MqttError as err:
                _LOGGER.warning(
                    "MQTT connection lost (%s) [%s], reconnecting in 5s",
                    err,
                    type(err).__name__,
                )
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                _LOGGER.info("EmotionKit MQTT loop cancelled")
                return

    task = hass.async_create_background_task(_mqtt_loop(), f"emotionkit_mqtt_{entry.entry_id}")
    hass.data[DOMAIN][entry.entry_id] = {
        "task": task,
        "cancel_idle_timer": _cancel_idle_timer,
    }

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data[DOMAIN].pop(entry.entry_id, None)
    if data:
        if "cancel_idle_timer" in data and data["cancel_idle_timer"] is not None:
            data["cancel_idle_timer"]()
        if "task" in data:
            data["task"].cancel()
            try:
                await data["task"]
            except asyncio.CancelledError:
                pass
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_broker_url(url: str) -> tuple[str, int, bool]:
    """Parse a broker URL like 'tcp://host:1883' into (host, port, use_tls)."""
    use_tls = url.startswith("tls://") or url.startswith("ssl://")
    # Normalize scheme for urlparse.
    normalized = url.replace("tcp://", "http://", 1).replace("tls://", "https://", 1).replace("ssl://", "https://", 1)
    parsed = urlparse(normalized)
    host = parsed.hostname or "localhost"
    default_port = 8883 if use_tls else 1883
    port = parsed.port or default_port
    return host, port, use_tls


async def _resolve_broker_url(control_plane_url: str) -> str:
    """Fetch the MQTT broker URL from the control-plane, with fallback."""
    info_url = f"{control_plane_url.rstrip('/')}/api/v1/info"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                info_url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    broker = data.get("mqtt_broker_url", "")
                    if broker:
                        return broker
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not fetch broker URL from %s", info_url)

    # Fallback: derive from control-plane hostname.
    return _derive_broker_url(control_plane_url)


def _derive_broker_url(control_plane_url: str) -> str:
    """Best-effort derivation of broker URL from the control-plane URL."""
    parsed = urlparse(control_plane_url)
    host = parsed.hostname or "localhost"
    if parsed.scheme == "https":
        return f"tls://{host}:8883"
    return f"tcp://{host}:1883"


class _GameState:
    """Track last-known game state for change detection."""

    def __init__(self) -> None:
        self.bomb: str = ""
        self.phase: str = ""
        self.win_team: str = ""
        self._enabled_override: bool = True
        self._allowed_subjects: dict[str, str] = {}  # subject → role; empty = allow all
        self._subject_fps: dict[str, tuple[str, str]] = {}  # subject → (fingerprint, role)
        self._config_received: bool = True


def _handle_config(payload: bytes | bytearray, state: _GameState) -> None:
    """Process a config update message."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return
    if isinstance(data.get("enabled"), bool):
        state._enabled_override = data["enabled"]
    if "allowed_subjects" in data:
        raw = data["allowed_subjects"]
        if isinstance(raw, dict):
            state._allowed_subjects = {
                subj: role for subj, role in raw.items()
                if isinstance(subj, str) and isinstance(role, str) and subj and role
            }
        elif isinstance(raw, list):
            state._allowed_subjects = {
                s: "user" for s in raw if isinstance(s, str) and s
            }
        else:
            state._allowed_subjects = {}
    state._config_received = True


def _subject_from_payload(payload: bytes | bytearray) -> str:
    """Extract the subject (Steam ID) from a game event payload."""
    try:
        msg = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return ""
    player_id = msg.get("player_id", "")
    if not isinstance(player_id, str):
        return ""
    # player_id is "SteamID64:NNNNN" — extract the numeric part as subject.
    if ":" in player_id:
        return player_id.split(":", 1)[1]
    return player_id


def _fingerprint_from_payload(payload: bytes | bytearray) -> str:
    """Extract the match_fingerprint from a game event payload."""
    try:
        msg = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return ""
    fp = msg.get("match_fingerprint", "")
    return fp if isinstance(fp, str) else ""


def _active_fingerprint(subject_fps: dict[str, tuple[str, str]]) -> str:
    """Return the match fingerprint that should be followed based on role priority.

    Owner > admin (majority) > user (majority).
    """
    owner_fp = ""
    admin_counts: dict[str, int] = {}
    user_counts: dict[str, int] = {}

    for fp, role in subject_fps.values():
        if role == "owner":
            owner_fp = fp
        elif role == "admin":
            admin_counts[fp] = admin_counts.get(fp, 0) + 1
        else:
            user_counts[fp] = user_counts.get(fp, 0) + 1

    if owner_fp:
        return owner_fp

    if admin_counts:
        return max(admin_counts, key=admin_counts.get)  # type: ignore[arg-type]

    if user_counts:
        return max(user_counts, key=user_counts.get)  # type: ignore[arg-type]

    return ""


def _map_name_from_payload(payload: bytes | bytearray) -> str:
    """Extract the map name (e.g. 'de_dust2') from a game event payload."""
    try:
        msg = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return ""
    map_block = msg.get("payload", {}).get("map")
    if isinstance(map_block, dict):
        name = map_block.get("name", "")
        return name if isinstance(name, str) else ""
    return ""


def _extract_events(
    payload: bytes | bytearray, state: _GameState
) -> list[tuple[str, dict]]:
    """Parse a game event message and return (event_type, data) tuples for changed states."""
    try:
        msg = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return []

    round_data = (msg.get("payload") or {}).get("round") or {}
    bomb = round_data.get("bomb", "")
    phase = round_data.get("phase", "")
    win_team = round_data.get("win_team", "")

    base = {
        "team_id": msg.get("team_id", ""),
        "player_id": msg.get("player_id", ""),
        "event_id": msg.get("id", ""),
        "occurred_at": msg.get("occurred_at", ""),
    }

    events: list[tuple[str, dict]] = []

    # Bomb state changes (highest priority).
    if bomb and bomb != state.bomb:
        mapping = {
            "planted": EVENT_BOMB_PLANTED,
            "defused": EVENT_BOMB_DEFUSED,
            "exploded": EVENT_BOMB_EXPLODED,
        }
        if bomb in mapping:
            events.append((mapping[bomb], base))

    # Phase changes.
    if phase and phase != state.phase:
        if phase == "live":
            events.append((EVENT_ROUND_LIVE, base))
        elif phase == "freezetime":
            events.append((EVENT_FREEZETIME, base))
        elif phase == "over":
            if win_team == "T":
                events.append((EVENT_ROUND_OVER_T, base))
            elif win_team == "CT":
                events.append((EVENT_ROUND_OVER_CT, base))

    # Update tracked state.
    if bomb:
        state.bomb = bomb
    if phase:
        state.phase = phase
    if win_team:
        state.win_team = win_team

    # Reset bomb state on freezetime (new round).
    if phase == "freezetime":
        state.bomb = ""

    return events
