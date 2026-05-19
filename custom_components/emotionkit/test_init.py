from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone


def _install_test_stubs() -> None:
    """Provide minimal aiomqtt/homeassistant stubs for unit tests."""
    if "aiomqtt" not in sys.modules:
        aiomqtt = types.ModuleType("aiomqtt")

        class _TLSParameters:  # pragma: no cover - stub for import only
            def __init__(self, *_args, **_kwargs) -> None:
                pass

        class _Will:  # pragma: no cover - stub for import only
            def __init__(self, *_args, **_kwargs) -> None:
                pass

        class _Client:  # pragma: no cover - stub for import only
            pass

        aiomqtt.TLSParameters = _TLSParameters
        aiomqtt.Will = _Will
        aiomqtt.Client = _Client
        sys.modules["aiomqtt"] = aiomqtt

    if "homeassistant" not in sys.modules:
        homeassistant = types.ModuleType("homeassistant")
        config_entries = types.ModuleType("homeassistant.config_entries")
        core = types.ModuleType("homeassistant.core")
        helpers = types.ModuleType("homeassistant.helpers")
        device_registry = types.ModuleType("homeassistant.helpers.device_registry")
        event = types.ModuleType("homeassistant.helpers.event")

        class _ConfigEntry:  # pragma: no cover - stub for import only
            pass

        class _HomeAssistant:  # pragma: no cover - stub for import only
            pass

        config_entries.ConfigEntry = _ConfigEntry
        core.HomeAssistant = _HomeAssistant
        device_registry.async_get = lambda *_args, **_kwargs: None
        event.CALLBACK_TYPE = object
        event.async_call_later = lambda *_args, **_kwargs: (lambda: None)

        sys.modules["homeassistant"] = homeassistant
        sys.modules["homeassistant.config_entries"] = config_entries
        sys.modules["homeassistant.core"] = core
        sys.modules["homeassistant.helpers"] = helpers
        sys.modules["homeassistant.helpers.device_registry"] = device_registry
        sys.modules["homeassistant.helpers.event"] = event


_install_test_stubs()

from .__init__ import (
    _GameState,
    _build_status_payload,
    _extract_events,
    _handle_config,
    _parse_broker_url,
    _derive_broker_url,
)


def test_parse_broker_url_tcp() -> None:
    host, port, use_tls = _parse_broker_url("tcp://mqtt.example.com:1883")
    assert host == "mqtt.example.com"
    assert port == 1883
    assert use_tls is False


def test_parse_broker_url_tls() -> None:
    host, port, use_tls = _parse_broker_url("tls://mqtt.example.com:8883")
    assert host == "mqtt.example.com"
    assert port == 8883
    assert use_tls is True


def test_parse_broker_url_empty_fallback() -> None:
    host, port, use_tls = _parse_broker_url("")
    assert host == "localhost"
    assert port == 1883
    assert use_tls is False


def test_derive_broker_url_https() -> None:
    result = _derive_broker_url("https://emotionkit.de")
    assert result == "tls://emotionkit.de:8883"


def test_derive_broker_url_http() -> None:
    result = _derive_broker_url("http://localhost:8085")
    assert result == "tcp://localhost:1883"


def test_handle_config_sets_config_received_and_allowed_subjects() -> None:
    state = _GameState()
    assert not state._config_received
    payload = json.dumps(
        {
            "enabled": False,
            "allowed_subjects": {"steam:owner123": "owner", "steam:user456": "user"},
        }
    ).encode()

    _handle_config(payload, state)

    assert state._config_received
    assert state._enabled_override is False
    assert state._allowed_subjects == {
        "steam:owner123": "owner",
        "steam:user456": "user",
    }


def test_handle_config_accepts_legacy_allowed_subjects_list() -> None:
    state = _GameState()
    payload = json.dumps(
        {
            "allowed_subjects": ["steam:player1", "steam:player2"],
        }
    ).encode()

    _handle_config(payload, state)

    assert state._config_received
    assert state._allowed_subjects == {
        "steam:player1": "user",
        "steam:player2": "user",
    }


def test_game_state_config_received_starts_false() -> None:
    state = _GameState()
    assert not state._config_received


def test_idle_event_is_configurable() -> None:
    from .const import DEFAULT_IDLE_TIMEOUT, EVENT_IDLE, ALL_EVENT_TYPES

    assert DEFAULT_IDLE_TIMEOUT == 30
    assert EVENT_IDLE in ALL_EVENT_TYPES


def test_build_status_payload_contains_handshake_fields() -> None:
    state = _GameState()
    state._enabled_override = True
    state._allowed_subjects = {"steam:owner123": "owner"}

    payload = json.loads(_build_status_payload("EmotionKit Test", state, 45))

    assert payload["schema_version"] == "1.0"
    assert payload["name"] == "EmotionKit Test"
    assert payload["online"] is True

    assert payload["config"]["enabled"] is True
    assert payload["config"]["idle_timeout_s"] == 45
    assert payload["config"]["allowed_subjects"] == {"steam:owner123": "owner"}

    schema_keys = {field["key"] for field in payload["config_schema"]}
    assert {"actor_name", "enabled", "idle_timeout_s"}.issubset(schema_keys)

    caps = payload["capabilities"]
    assert caps["protocol_version"] == "1.0"
    assert caps["supports_idle"] is True
    assert "ha-trigger" in caps["intent_types"]


def test_build_status_payload_reflects_disabled_state() -> None:
    state = _GameState()
    state._enabled_override = False

    payload = json.loads(_build_status_payload("EmotionKit Test", state, 0))

    assert payload["config"]["enabled"] is False
    assert payload["config"]["idle_timeout_s"] == 0


def test_extract_events_ignores_stale_event() -> None:
    state = _GameState()
    occurred_at = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    payload = json.dumps(
        {
            "occurred_at": occurred_at,
            "payload": {"round": {"phase": "live"}},
        }
    ).encode()

    events = _extract_events(payload, state)
    assert events == []


def test_extract_events_processes_fresh_event() -> None:
    state = _GameState()
    occurred_at = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(
        {
            "occurred_at": occurred_at,
            "payload": {"round": {"phase": "live"}},
        }
    ).encode()

    events = _extract_events(payload, state)
    assert len(events) == 1
