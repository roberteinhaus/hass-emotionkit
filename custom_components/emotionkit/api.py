"""HTTP calls to the EmotionKit control-plane (claim-code registration).

The functions take an aiohttp.ClientSession but do not import aiohttp, so
they can be exercised with a fake session and without Home Assistant.

Claim codes, device tokens and credentials are secrets: nothing in here
logs them or puts them into an exception message.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import enum
import logging
import secrets
import time
from typing import Any

from .const import CLAIM_CODE_ALPHABET, CLAIM_CODE_LENGTH, DEVICE_KIND, POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15.0

ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_TIMEOUT = "timeout_connect"
ERROR_SERVER_BUSY = "server_busy"
ERROR_INVALID_RESPONSE = "invalid_response"


class ApiError(Exception):
    """A call to the control-plane failed. `key` is a translation key."""

    key = ERROR_CANNOT_CONNECT


class ApiTimeout(ApiError):
    """The control-plane did not answer in time."""

    key = ERROR_TIMEOUT


class ApiConnectionError(ApiError):
    """The control-plane could not be reached."""


class ApiResponseError(ApiError):
    """The control-plane answered with something unexpected."""

    def __init__(self, status: int, key: str = ERROR_CANNOT_CONNECT) -> None:
        super().__init__(f"unexpected HTTP status {status}")
        self.status = status
        self.key = key


def generate_claim_code() -> str:
    """Generate a claim code in XXXX-XXXX format."""
    chars = [secrets.choice(CLAIM_CODE_ALPHABET) for _ in range(CLAIM_CODE_LENGTH)]
    half = CLAIM_CODE_LENGTH // 2
    return "".join(chars[:half]) + "-" + "".join(chars[half:])


def _api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/api/v1/{path}"


async def _request(
    session: Any,
    method: str,
    url: str,
    *,
    json: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> tuple[int, Any]:
    """Perform a request and return (status, decoded JSON body or None)."""
    try:
        async with asyncio.timeout(timeout):
            async with session.request(
                method, url, json=json, headers=headers
            ) as response:
                status = int(response.status)
                body: Any = None
                if status == 200:
                    body = await response.json(content_type=None)
                return status, body
    except TimeoutError as err:
        raise ApiTimeout("request timed out") from err
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001 - aiohttp.ClientError, bad JSON, ...
        # Only the type: the message of a client error may contain the URL,
        # and the poll URL contains the claim code.
        raise ApiConnectionError(type(err).__name__) from None


async def async_announce(session: Any, base_url: str, code: str) -> str:
    """Register a claim code and return the device token for polling."""
    status, body = await _request(
        session,
        "POST",
        _api_url(base_url, "devices/announce"),
        json={"code": code, "kind": DEVICE_KIND},
    )
    if status in (429, 503):
        raise ApiResponseError(status, ERROR_SERVER_BUSY)
    if status != 200:
        raise ApiResponseError(status)
    token = body.get("device_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise ApiResponseError(status, ERROR_INVALID_RESPONSE)
    return token


async def async_fetch_broker_url(session: Any, base_url: str) -> str | None:
    """Ask the control-plane which broker URL devices should use.

    Returns None if the server does not announce one. Raises ApiError if the
    server cannot be asked.
    """
    status, body = await _request(
        session, "GET", _api_url(base_url, "info"), timeout=10.0
    )
    if status != 200:
        raise ApiResponseError(status)
    url = body.get("mqtt_broker_url") if isinstance(body, dict) else None
    return url.strip() if isinstance(url, str) and url.strip() else None


class ClaimStatus(enum.Enum):
    """How waiting for a claim ended."""

    CLAIMED = "claimed"
    # The server no longer knows the code (it expired or the server was
    # restarted): a fresh code has to be announced.
    EXPIRED = "expired"
    TIMEOUT = "timeout"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    """Result of waiting for a claim. Credentials only if CLAIMED."""

    status: ClaimStatus
    credentials: dict[str, str] | None = None


def _clean_credentials(body: object) -> dict[str, str] | None:
    """Validate the credentials the server hands out after a claim."""
    if not isinstance(body, dict):
        return None
    credentials: dict[str, str] = {}
    for key in ("device_id", "mqtt_username", "mqtt_password"):
        value = body.get(key)
        if not isinstance(value, str) or not value:
            return None
        credentials[key] = value
    broker = body.get("mqtt_broker")
    credentials["mqtt_broker"] = broker.strip() if isinstance(broker, str) else ""
    return credentials


async def async_wait_for_claim(
    session: Any,
    base_url: str,
    code: str,
    device_token: str,
    *,
    deadline: float,
    interval: float = POLL_INTERVAL,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ClaimOutcome:
    """Poll until the code was claimed, expired, or `deadline` has passed.

    `deadline` is a monotonic timestamp. Network errors and "still waiting"
    answers are retried; this only returns on a definite outcome.
    """
    url = _api_url(base_url, f"devices/pending/{code}")
    headers = {"X-Device-Token": device_token}

    while True:
        try:
            status, body = await _request(
                session, "GET", url, headers=headers, timeout=10.0
            )
        except ApiError as err:
            _LOGGER.debug("Claim poll failed (%s), retrying", type(err).__name__)
        else:
            if status == 200:
                credentials = _clean_credentials(body)
                if credentials is None:
                    return ClaimOutcome(ClaimStatus.INVALID)
                return ClaimOutcome(ClaimStatus.CLAIMED, credentials)
            if status in (403, 404):
                # 404: code unknown or expired. 403: the code exists but with
                # another token, i.e. it is not ours (any more).
                return ClaimOutcome(ClaimStatus.EXPIRED)
            # 202 (waiting), 429, 5xx: keep waiting.

        if monotonic() + interval > deadline:
            return ClaimOutcome(ClaimStatus.TIMEOUT)
        await sleep(interval)
