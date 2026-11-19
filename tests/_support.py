"""Test support.

The logic of the integration lives in plain-Python modules that do not
import Home Assistant. Importing them the normal way would still execute
custom_components/emotionkit/__init__.py, which does. So the package is
registered here as an empty shell that only knows where its modules are;
`from custom_components.emotionkit import engine` then loads engine.py (and
what it imports relatively) without touching __init__.py.

The tests are unittest.TestCase classes on purpose: they run under pytest
(the CI) as well as under `python -m unittest` on a machine without pytest.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import types
from typing import Any

INTEGRATION_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = INTEGRATION_ROOT / "custom_components" / "emotionkit"

# Modules that must stay free of Home Assistant, aiomqtt and aiohttp.
PURE_MODULES = (
    "api",
    "broker",
    "clock",
    "connection",
    "const",
    "engine",
    "protocol",
    "state",
    "triggers",
)


def _register_shell(name: str, path: Path) -> None:
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    module.__path__ = [str(path)]  # makes it a package
    sys.modules[name] = module


_register_shell("custom_components", PACKAGE_DIR.parent)
_register_shell("custom_components.emotionkit", PACKAGE_DIR)


OWNER = "76561198000000001"
MATE = "76561198000000002"
STRANGER = "76561198000000003"


def payload(
    *,
    steamid: str = OWNER,
    provider: str | None = None,
    map_name: str | None = "de_dust2",
    phase: str | None = "live",
    bomb: str | None = None,
    win_team: str | None = None,
    ct: int = 0,
    t: int = 0,
    health: int | None = 100,
    armor: int | None = 100,
    money: int | None = 800,
) -> dict[str, Any]:
    """Build a GSI payload as a playing client sends it."""
    data: dict[str, Any] = {"provider": {"steamid": provider or steamid}}
    if map_name is not None:
        data["map"] = {
            "name": map_name,
            "phase": "live",
            "team_ct": {"score": ct},
            "team_t": {"score": t},
        }
    if phase is not None:
        round_block: dict[str, Any] = {"phase": phase}
        if bomb:
            round_block["bomb"] = bomb
        if win_team:
            round_block["win_team"] = win_team
        data["round"] = round_block
    state = {
        key: value
        for key, value in (("health", health), ("armor", armor), ("money", money))
        if value is not None
    }
    data["player"] = {"steamid": steamid, "team": "CT", "state": state}
    return data


_counter = 0


def envelope(
    *events: dict[str, Any],
    subject: str = OWNER,
    match_id: str = "match-1",
    side: str = "CT",
    occurred_at: str = "2026-11-18T20:15:04.123Z",
    envelope_id: str | None = None,
    **payload_args: Any,
) -> dict[str, Any]:
    """Build an event envelope (as a dict; see raw() for the wire form)."""
    global _counter  # noqa: PLW0603
    _counter += 1
    payload_args.setdefault("steamid", subject)
    data: dict[str, Any] = {
        "id": envelope_id if envelope_id is not None else f"env-{_counter}",
        "team_id": "team-1",
        "player_id": f"team-1:{subject}",
        "subject": subject,
        "player_side": side,
        "occurred_at": occurred_at,
        "payload": payload(**payload_args),
    }
    if match_id:
        data["match_id"] = match_id
        data["match_fingerprint"] = match_id
    if events:
        data["events"] = list(events)
    return data


def menu_envelope(
    *events: dict[str, Any], subject: str = OWNER, **kwargs: Any
) -> dict[str, Any]:
    """Build the envelope of a packet sent from the menu (no match)."""
    return envelope(
        *events,
        subject=subject,
        match_id="",
        side="UNKNOWN",
        map_name=None,
        phase=None,
        health=None,
        armor=None,
        money=None,
        **kwargs,
    )


def raw(data: Any) -> bytes:
    """Encode like the broker delivers it."""
    return json.dumps(data).encode()


def event(event_type: str, **fields: Any) -> dict[str, Any]:
    """Build one entry of the envelope's events list."""
    return {"type": event_type, "at": "2026-11-18T20:15:04.123Z", **fields}


# --- a fake aiohttp session -------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: Any = None, *, delay: float = 0.0) -> None:
        self.status = status
        self._body = body
        self._delay = delay

    async def __aenter__(self) -> FakeResponse:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def json(self, content_type: str | None = "application/json") -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """Answers requests from a script; records what was asked."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        outcome = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
