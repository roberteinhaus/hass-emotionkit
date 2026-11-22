"""Constants for the EmotionKit integration.

This module must stay free of Home Assistant imports: it is shared by the
plain-Python logic modules, which are unit-tested without Home Assistant.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "emotionkit"

DEFAULT_CONTROL_PLANE_URL: Final = "https://emotionkit.de"
DEFAULT_DEVICE_NAME: Final = "Home Assistant"
DEFAULT_IDLE_TIMEOUT: Final = 30
MAX_IDLE_TIMEOUT: Final = 3600
MAX_DEVICE_NAME_LENGTH: Final = 64
DEVICE_KIND: Final = "ha-integration"

# Seconds without in-match packets after which the entities fall back to their
# idle state (off / unknown). GSI clients send a heartbeat every 10 s (30 s
# with older cfg files), so this has to be well above that.
STATE_TIMEOUT: Final = 90

# Claim code alphabet (matches the Go bootstrap: no 0, O, I, 1).
CLAIM_CODE_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CLAIM_CODE_LENGTH: Final = 8  # shown as XXXX-XXXX

POLL_INTERVAL: Final = 5  # seconds between two claim polls
# A claim code lives 15 minutes on the server. Expired codes are replaced by a
# fresh one until this overall limit is reached.
CLAIM_FLOW_TIMEOUT: Final = 30 * 60

# Config entry data.
CONF_CONTROL_PLANE_URL: Final = "control_plane_url"
CONF_DEVICE_NAME: Final = "device_name"
CONF_DEVICE_ID: Final = "device_id"
CONF_MQTT_USERNAME: Final = "mqtt_username"
CONF_MQTT_PASSWORD: Final = "mqtt_password"
CONF_MQTT_BROKER: Final = "mqtt_broker"
# True when the user typed the broker URL in the reconfigure dialog. Such a
# URL is never replaced by the one the server announces.
CONF_MQTT_BROKER_MANUAL: Final = "mqtt_broker_manual"
# Last values of the server-side device settings this integration has seen,
# used to tell a new change made in the web UI from a stale retained message.
CONF_REMOTE_CONFIG: Final = "remote_config"

# Config entry options.
CONF_IDLE_TIMEOUT: Final = "idle_timeout"
CONF_ALL_PLAYERS: Final = "react_to_all_players"

# MQTT topics (formatted with the MQTT username).
TOPIC_EVENTS: Final = "devices/{}/events"
TOPIC_STATUS: Final = "devices/{}/status"
TOPIC_CONFIG: Final = "devices/{}/config"

# Event fired on the Home Assistant bus for every trigger.
EVENT_EMOTIONKIT: Final = "emotionkit_event"

# --- Trigger types -------------------------------------------------------
# The first seven names (and "idle") exist since the first release; user
# automations refer to them, so they must never be renamed.
EVENT_BOMB_PLANTED: Final = "bomb_planted"
EVENT_BOMB_DEFUSED: Final = "bomb_defused"
EVENT_BOMB_EXPLODED: Final = "bomb_exploded"
EVENT_ROUND_LIVE: Final = "round_live"
EVENT_ROUND_OVER_T: Final = "round_over_t"
EVENT_ROUND_OVER_CT: Final = "round_over_ct"
EVENT_FREEZETIME: Final = "freezetime"
EVENT_IDLE: Final = "idle"

EVENT_MATCH_START: Final = "match_start"
EVENT_MATCH_OVER: Final = "match_over"
EVENT_MATCH_WON: Final = "match_won"
EVENT_MATCH_LOST: Final = "match_lost"
EVENT_HALFTIME: Final = "halftime"
# From the point of view of the device's side. None of them fires while the
# device takes no side (its players are opponents).
EVENT_ROUND_WON: Final = "round_won"
EVENT_ROUND_LOST: Final = "round_lost"
EVENT_LEAD_TAKEN: Final = "lead_taken"
EVENT_LEAD_EQUALIZED: Final = "lead_equalized"
EVENT_LEAD_TIED: Final = "lead_tied"
EVENT_LEAD_BEHIND: Final = "lead_behind"
EVENT_WIN_STREAK: Final = "win_streak"
# Ten seconds before the bomb goes off; timed by the integration itself.
EVENT_BOMB_10S: Final = "bomb_10s"

EVENT_PLAYER_KILL: Final = "player_kill"
EVENT_PLAYER_HEADSHOT: Final = "player_headshot"
EVENT_ACE: Final = "ace"
EVENT_DOUBLE_KILL: Final = "double_kill"
EVENT_TRIPLE_KILL: Final = "triple_kill"
EVENT_QUAD_KILL: Final = "quad_kill"
EVENT_PLAYER_DEATH: Final = "player_death"
EVENT_PLAYER_FLASHED: Final = "player_flashed"
EVENT_PLAYER_LOW_HEALTH: Final = "player_low_health"
EVENT_PLAYER_MVP: Final = "player_mvp"
EVENT_MATCH_LEFT: Final = "match_left"

LEGACY_TRIGGER_TYPES: Final = (
    EVENT_BOMB_PLANTED,
    EVENT_BOMB_DEFUSED,
    EVENT_BOMB_EXPLODED,
    EVENT_ROUND_LIVE,
    EVENT_ROUND_OVER_T,
    EVENT_ROUND_OVER_CT,
    EVENT_FREEZETIME,
)

MATCH_TRIGGER_TYPES: Final = (
    EVENT_MATCH_START,
    EVENT_MATCH_OVER,
    EVENT_MATCH_WON,
    EVENT_MATCH_LOST,
    EVENT_HALFTIME,
    EVENT_ROUND_WON,
    EVENT_ROUND_LOST,
    EVENT_LEAD_TAKEN,
    EVENT_LEAD_EQUALIZED,
    EVENT_LEAD_TIED,
    EVENT_LEAD_BEHIND,
    EVENT_WIN_STREAK,
    EVENT_BOMB_10S,
)

# Triggers about a single player. By default they fire for the device owner
# only (see the "react to all players" option).
PLAYER_TRIGGER_TYPES: Final = (
    EVENT_PLAYER_KILL,
    EVENT_PLAYER_HEADSHOT,
    EVENT_DOUBLE_KILL,
    EVENT_TRIPLE_KILL,
    EVENT_QUAD_KILL,
    EVENT_ACE,
    EVENT_PLAYER_DEATH,
    EVENT_PLAYER_FLASHED,
    EVENT_PLAYER_LOW_HEALTH,
    EVENT_PLAYER_MVP,
    EVENT_MATCH_LEFT,
)

ALL_EVENT_TYPES: Final = [
    *LEGACY_TRIGGER_TYPES,
    *MATCH_TRIGGER_TYPES,
    *PLAYER_TRIGGER_TYPES,
    EVENT_IDLE,
]

# --- Entities ------------------------------------------------------------
ROUND_PHASES: Final = ("freezetime", "live", "over")

BINARY_SENSOR_KEYS: Final = ("bomb_planted", "in_match")
SENSOR_KEYS: Final = (
    "round_phase",
    "map",
    "score_ct",
    "score_t",
    "round",
    "health",
    "armor",
    "money",
)
EVENT_ENTITY_KEY: Final = "game_event"
