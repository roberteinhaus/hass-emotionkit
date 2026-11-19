"""Broker URL parsing (plain Python, no Home Assistant imports)."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_WEBSOCKET_PATH = "/mqtt"

TRANSPORT_TCP = "tcp"
TRANSPORT_WEBSOCKETS = "websockets"

# scheme -> (transport, tls, default port)
_SCHEMES: dict[str, tuple[str, bool, int]] = {
    "tcp": (TRANSPORT_TCP, False, 1883),
    "mqtt": (TRANSPORT_TCP, False, 1883),
    "tls": (TRANSPORT_TCP, True, 8883),
    "ssl": (TRANSPORT_TCP, True, 8883),
    "mqtts": (TRANSPORT_TCP, True, 8883),
    "ws": (TRANSPORT_WEBSOCKETS, False, 80),
    "wss": (TRANSPORT_WEBSOCKETS, True, 443),
}

SUPPORTED_SCHEMES = tuple(_SCHEMES)

_EXAMPLES = "tcp://host:1883, tls://host:8883 or wss://host/mqtt"


class InvalidBrokerURL(ValueError):
    """The broker URL cannot be used.

    The message never contains the URL itself, so it is safe to log even if
    someone put credentials into the URL.
    """


@dataclass(frozen=True, slots=True)
class BrokerAddress:
    """Where and how to reach the MQTT broker."""

    scheme: str
    host: str
    port: int
    tls: bool
    transport: str = TRANSPORT_TCP
    # Only set for the websocket transport.
    websocket_path: str | None = None

    @property
    def is_websocket(self) -> bool:
        """Return True for MQTT over WebSockets."""
        return self.transport == TRANSPORT_WEBSOCKETS

    def describe(self) -> str:
        """Return a log-friendly form of the address (it holds no secrets)."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}{self.websocket_path or ''}"


def parse_broker_url(url: object) -> BrokerAddress:
    """Parse a broker URL.

    Supported schemes:
      tcp://, mqtt://          plain MQTT, default port 1883
      tls://, ssl://, mqtts:// MQTT over TLS, default port 8883
      ws://                    MQTT over WebSockets, default port 80
      wss://                   MQTT over secure WebSockets, default port 443

    WebSocket URLs carry the path (default "/mqtt"). Anything else raises
    InvalidBrokerURL: a typo must never end in a silent fallback to an
    unencrypted connection or to localhost.
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidBrokerURL(f"The broker URL is empty (expected e.g. {_EXAMPLES})")
    text = url.strip()
    if "://" not in text:
        raise InvalidBrokerURL(
            f"The broker URL has no scheme (expected e.g. {_EXAMPLES})"
        )

    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise InvalidBrokerURL(
            "The broker URL is malformed (check host and port)"
        ) from None

    scheme = parts.scheme.lower()
    if scheme not in _SCHEMES:
        raise InvalidBrokerURL(
            f"Unsupported broker URL scheme '{scheme}' "
            f"(supported: {', '.join(s + '://' for s in SUPPORTED_SCHEMES)})"
        )
    transport, tls, default_port = _SCHEMES[scheme]

    if not host:
        raise InvalidBrokerURL("The broker URL has no host name")
    if parts.username is not None or parts.password is not None:
        raise InvalidBrokerURL(
            "The broker URL must not contain credentials; "
            "they are provided by the claim"
        )
    if port is None:
        port = default_port
    elif not 1 <= port <= 65535:
        raise InvalidBrokerURL("The broker URL has an invalid port")
    if parts.fragment:
        raise InvalidBrokerURL("The broker URL must not contain a fragment")

    websocket_path: str | None = None
    if transport == TRANSPORT_WEBSOCKETS:
        websocket_path = parts.path or DEFAULT_WEBSOCKET_PATH
        if parts.query:
            websocket_path = f"{websocket_path}?{parts.query}"
    elif parts.path not in ("", "/") or parts.query:
        raise InvalidBrokerURL(
            f"A {scheme}:// broker URL must not contain a path "
            "(use ws:// or wss:// for MQTT over WebSockets)"
        )

    return BrokerAddress(
        scheme=scheme,
        host=host,
        port=port,
        tls=tls,
        transport=transport,
        websocket_path=websocket_path,
    )


def derive_broker_url(control_plane_url: str) -> str:
    """Best-effort guess of the broker URL from the control-plane URL.

    Only used when the server does not announce a broker URL at all.
    """
    try:
        parts = urlsplit(control_plane_url)
        host = parts.hostname or "localhost"
    except ValueError:
        return "tcp://localhost:1883"
    if ":" in host:
        host = f"[{host}]"
    if parts.scheme == "https":
        return f"tls://{host}:8883"
    return f"tcp://{host}:1883"
