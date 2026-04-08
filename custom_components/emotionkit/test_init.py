from __future__ import annotations

import json

from .__init__ import _GameState, _handle_config, _parse_broker_url, _derive_broker_url


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
