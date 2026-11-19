"""Helpers for the tests that run against the real Home Assistant core.

The only thing replaced is the broker connection: `aiomqtt.Client`, the one
place where the integration touches the network for MQTT. Everything from
there on is the real code - the connection supervisor, the message loop,
the processing, the entities, the triggers. Messages are injected into the
fake client exactly where the real client would hand them out, as real
`aiomqtt.Message` objects.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
from typing import Any

import aiomqtt

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emotionkit.const import DOMAIN

OWNER = "76561198000000001"
MATE = "76561198000000002"

# --- what the integration at git HEAD (manifest version 1.1.0) stored ------
#
# config_flow.py at HEAD: VERSION = 1, title = the device name, unique id =
# the server's device id, and exactly these six data keys. The options flow
# of that version could not be opened (it crashed), so options are empty.
# __init__.py at HEAD registered the device with the identifier
# (DOMAIN, data["device_id"]), manufacturer "EmotionKit" and model
# "CS2 Game Events".
LEGACY_DEVICE_ID = "dcv0legacydevice0001"
LEGACY_USERNAME = f"device-{LEGACY_DEVICE_ID}"
LEGACY_PASSWORD = "legacy-mqtt-password"
LEGACY_DATA: dict[str, Any] = {
    "control_plane_url": "https://emotionkit.de",
    "device_name": "Home Assistant",
    "device_id": LEGACY_DEVICE_ID,
    "mqtt_username": LEGACY_USERNAME,
    "mqtt_password": LEGACY_PASSWORD,
    "mqtt_broker": "tcp://emotionkit.de:1883",
}
LEGACY_TITLE = "Home Assistant"
LEGACY_IDENTIFIERS = {(DOMAIN, LEGACY_DEVICE_ID)}

STATUS_TOPIC = f"devices/{LEGACY_USERNAME}/status"
EVENT_TOPIC = f"devices/{LEGACY_USERNAME}/events"
CONFIG_TOPIC = f"devices/{LEGACY_USERNAME}/config"

OWNER_CONFIG: dict[str, Any] = {
    "enabled": True,
    "allowed_subjects": {OWNER: "owner", MATE: "user"},
}

ENTITY_KEYS = {
    "binary_sensor": {"bomb_planted", "in_match"},
    "sensor": {
        "round_phase",
        "map",
        "score_ct",
        "score_t",
        "round",
        "health",
        "armor",
        "money",
    },
    "event": {"game_event"},
}


def legacy_entry(**overrides: Any) -> MockConfigEntry:
    """Return a config entry as the integration at git HEAD created it."""
    arguments: dict[str, Any] = {
        "domain": DOMAIN,
        "title": LEGACY_TITLE,
        "data": dict(LEGACY_DATA),
        "options": {},
        "unique_id": LEGACY_DEVICE_ID,
        "version": 1,
        "minor_version": 1,
    }
    arguments.update(overrides)
    return MockConfigEntry(**arguments)


async def wait_for(predicate: Callable[[], Any], timeout: float = 5.0) -> None:
    """Let the event loop run until predicate() holds."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def not_authorized() -> BaseException:
    """Return what the real aiomqtt raises when the broker refuses a CONNECT.

    Built from the real classes on purpose: the integration has to tell this
    error (credentials no longer valid -> re-authentication) from every
    other connection problem.
    """
    from aiomqtt.exceptions import MqttConnectError  # noqa: PLC0415
    from paho.mqtt.packettypes import PacketTypes  # noqa: PLC0415
    from paho.mqtt.reasoncodes import ReasonCode  # noqa: PLC0415

    return MqttConnectError(ReasonCode(PacketTypes.CONNACK, "Not authorized"))


class _Messages:
    """Stands in for aiomqtt's MessagesIterator."""

    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def __aiter__(self) -> _Messages:
        return self

    async def __anext__(self) -> aiomqtt.Message:
        client = self._client
        if client.in_flight:
            # The loop asks for the next message: the previous one has been
            # handled completely.
            client.in_flight = False
            client.queue.task_done()
        item = await client.queue.get()
        if isinstance(item, BaseException):
            client.queue.task_done()
            raise item
        client.in_flight = True
        return item


class FakeClient:
    """Stands in for one aiomqtt.Client, i.e. one connection attempt."""

    def __init__(self, broker: FakeBroker, hostname: str, port: int = 1883, **kwargs: Any) -> None:
        self.broker = broker
        self.hostname = hostname
        self.port = port
        self.options = kwargs
        self.published: list[dict[str, Any]] = []
        self.subscriptions: list[str] = []
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.in_flight = False
        self.connected = False
        self.closed = False

    # --- what the integration calls ---

    async def __aenter__(self) -> FakeClient:
        error = self.broker.next_connect_error(self.options.get("username"))
        if error is not None:
            raise error
        self.connected = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.connected = False
        self.closed = True

    @property
    def messages(self) -> _Messages:
        return _Messages(self)

    async def publish(
        self,
        topic: str,
        payload: Any = None,
        qos: int = 0,
        retain: bool = False,
        **kwargs: Any,
    ) -> None:
        if not self.connected:
            raise aiomqtt.MqttError("not connected")
        self.published.append(
            {"topic": topic, "payload": payload, "qos": qos, "retain": retain}
        )

    async def subscribe(self, topic: str, qos: int = 0, **kwargs: Any) -> None:
        if not self.connected:
            raise aiomqtt.MqttError("not connected")
        self.subscriptions.append(topic)

    # --- what the tests use ---

    @property
    def ready(self) -> bool:
        """Return True once the integration has subscribed to both topics."""
        return self.connected and len(self.subscriptions) >= 2

    def statuses(self) -> list[dict[str, Any]]:
        """Return the status payloads published so far, decoded."""
        return [
            json.loads(item["payload"])
            for item in self.published
            if item["topic"].endswith("/status")
        ]

    async def deliver(self, topic: str, payload: Any, *, retain: bool = False) -> None:
        """Hand a message to the integration and wait until it is handled."""
        if not isinstance(payload, (bytes, bytearray)):
            payload = json.dumps(payload).encode()
        self.queue.put_nowait(
            aiomqtt.Message(
                topic=topic, payload=bytes(payload), qos=1, retain=retain, mid=1, properties=None
            )
        )
        await asyncio.wait_for(self.queue.join(), 5)

    def drop(self) -> None:
        """Break the connection, as the real client does on a network error."""
        self.queue.put_nowait(aiomqtt.MqttError("Disconnected during message iteration"))


class FakeBroker:
    """Hands out FakeClients in place of aiomqtt.Client."""

    def __init__(self) -> None:
        self.clients: list[FakeClient] = []
        # Raised by the next connection attempts, one each.
        self.connect_errors: list[BaseException] = []
        # A factory for an error raised by every attempt while it is set.
        self.refuse: Callable[[], BaseException] | None = None
        # Devices that were deleted on the server: their CONNECT is refused
        # with "not authorized", like the real broker does.
        self.deleted_usernames: set[str] = set()

    def client(self, hostname: str, port: int = 1883, **kwargs: Any) -> FakeClient:
        """The replacement for `aiomqtt.Client(...)`."""
        client = FakeClient(self, hostname, port, **kwargs)
        self.clients.append(client)
        return client

    def next_connect_error(self, username: str | None) -> BaseException | None:
        if self.connect_errors:
            return self.connect_errors.pop(0)
        if username in self.deleted_usernames:
            return not_authorized()
        if self.refuse is not None:
            return self.refuse()
        return None

    @property
    def sessions(self) -> list[FakeClient]:
        """Return the clients whose connection was accepted, oldest first."""
        return [client for client in self.clients if client.subscriptions]

    async def wait_ready(self, sessions: int = 1) -> FakeClient:
        """Wait until the given number of connections have been set up."""
        await wait_for(lambda: len(self.sessions) >= sessions and self.sessions[-1].ready)
        return self.sessions[-1]


# --- message builders --------------------------------------------------------

_BASE_SECONDS = 15 * 60  # 20:15:00
_sequence = 0


def timestamp(offset: float = 0.0) -> str:
    """Return a server timestamp `offset` seconds after 2026-11-18 20:15 UTC."""
    total = _BASE_SECONDS + offset
    minutes, seconds = divmod(total, 60)
    return f"2026-11-18T20:{int(minutes):02d}:{seconds:06.3f}Z"


def event(event_type: str, **fields: Any) -> dict[str, Any]:
    """Build one entry of the envelope's `events` list."""
    return {"type": event_type, "at": timestamp(), **fields}


def envelope(
    *events: dict[str, Any],
    subject: str = OWNER,
    match_id: str = "match-1",
    side: str = "CT",
    at: float = 0.0,
    envelope_id: str | None = None,
    map_name: str | None = "de_dust2",
    phase: str | None = "live",
    bomb: str | None = None,
    win_team: str | None = None,
    ct: int = 0,
    t: int = 0,
    health: int = 100,
    armor: int = 100,
    money: int = 800,
) -> dict[str, Any]:
    """Build an event envelope as documented in device-events-v1.md."""
    global _sequence  # noqa: PLW0603
    _sequence += 1
    payload: dict[str, Any] = {
        "provider": {"steamid": subject},
        "player": {
            "steamid": subject,
            "team": side,
            "state": {"health": health, "armor": armor, "money": money},
        },
    }
    if map_name is not None:
        payload["map"] = {
            "name": map_name,
            "phase": "live",
            "team_ct": {"score": ct},
            "team_t": {"score": t},
        }
    if phase is not None:
        payload["round"] = {"phase": phase}
        if bomb:
            payload["round"]["bomb"] = bomb
        if win_team:
            payload["round"]["win_team"] = win_team

    data: dict[str, Any] = {
        "id": envelope_id or f"env-{_sequence}",
        "team_id": "team-1",
        "player_id": f"team-1:{subject}",
        "subject": subject,
        "player_side": side,
        "occurred_at": timestamp(at),
        "payload": payload,
    }
    if match_id:
        data["match_id"] = match_id
        data["match_fingerprint"] = match_id
    if events:
        data["events"] = list(events)
    return data


def menu_envelope(*events: dict[str, Any], subject: str = OWNER, at: float = 0.0) -> dict[str, Any]:
    """Build the envelope of a packet sent from the menu (not in a match)."""
    global _sequence  # noqa: PLW0603
    _sequence += 1
    data: dict[str, Any] = {
        "id": f"env-{_sequence}",
        "team_id": "team-1",
        "player_id": f"team-1:{subject}",
        "subject": subject,
        "player_side": "UNKNOWN",
        "occurred_at": timestamp(at),
        "payload": {"provider": {"steamid": subject}, "player": {"activity": "menu"}},
    }
    if events:
        data["events"] = list(events)
    return data
