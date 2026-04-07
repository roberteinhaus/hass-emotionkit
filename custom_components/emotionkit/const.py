"""Constants for the EmotionKit integration."""

DOMAIN = "emotionkit"

DEFAULT_CONTROL_PLANE_URL = "https://emotionkit.de"
DEFAULT_DEVICE_NAME = "Home Assistant"
DEVICE_KIND = "ha-integration"

# Claim code alphabet (matches Go bootstrap: no 0, O, I, 1).
CLAIM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CLAIM_CODE_LENGTH = 8  # 4+4 with dash

POLL_INTERVAL = 5  # seconds
POLL_TIMEOUT = 300  # seconds (5 minutes max wait)

# MQTT topics (formatted with mqtt_username).
TOPIC_EVENTS = "team/+/events"
TOPIC_STATUS = "devices/{}/status"
TOPIC_CONFIG = "devices/{}/config"

# Event fired on the HA bus.
EVENT_EMOTIONKIT = "emotionkit_event"

# Game event types derived from CS2 GSI payload.
EVENT_BOMB_PLANTED = "bomb_planted"
EVENT_BOMB_DEFUSED = "bomb_defused"
EVENT_BOMB_EXPLODED = "bomb_exploded"
EVENT_ROUND_LIVE = "round_live"
EVENT_ROUND_OVER_T = "round_over_t"
EVENT_ROUND_OVER_CT = "round_over_ct"
EVENT_FREEZETIME = "freezetime"

ALL_EVENT_TYPES = [
    EVENT_BOMB_PLANTED,
    EVENT_BOMB_DEFUSED,
    EVENT_BOMB_EXPLODED,
    EVENT_ROUND_LIVE,
    EVENT_ROUND_OVER_T,
    EVENT_ROUND_OVER_CT,
    EVENT_FREEZETIME,
]
