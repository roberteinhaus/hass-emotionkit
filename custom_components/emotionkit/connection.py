"""Keep one broker connection alive (plain asyncio).

This module knows neither Home Assistant nor aiomqtt: the caller passes in
how to connect and what to do with a connection. That keeps the part that
must never give up (reconnect, back off, survive any exception, stop
cleanly) small and testable on its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
import enum
import logging
import random
import time
from typing import Any

# CONNACK return codes that mean "these credentials are not accepted":
# 4/5 in MQTT 3.1.1, 134/135 as MQTT 5 reason codes (paho-mqtt 2 reports the
# 3.1.1 codes converted to these).
_AUTH_REASON_CODES = frozenset({4, 5, 134, 135})


def reason_code_value(reason_code: object) -> int | None:
    """Return the numeric value of a paho reason code or plain int."""
    if isinstance(reason_code, bool):
        return None
    if isinstance(reason_code, int):
        return reason_code
    value = getattr(reason_code, "value", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def is_auth_reason_code(reason_code: object) -> bool:
    """Return True if a CONNACK reason code means the credentials are bad.

    Only meaningful for the reason code of a refused *connect*; other MQTT
    errors reuse the same small integers for different things.
    """
    return reason_code_value(reason_code) in _AUTH_REASON_CODES


class FailureKind(enum.Enum):
    """Why a connection attempt or connection ended."""

    TRANSIENT = "transient"
    AUTH = "auth"


class Backoff:
    """Exponential back-off with a cap and jitter."""

    def __init__(
        self,
        initial: float = 2.0,
        maximum: float = 300.0,
        factor: float = 2.0,
        jitter: float = 0.2,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.initial = initial
        self.maximum = maximum
        self.factor = factor
        self.jitter = jitter
        self._rng = rng
        self.attempts = 0

    def reset(self) -> None:
        """Start over with the shortest delay."""
        self.attempts = 0

    def peek(self) -> float:
        """Return the next delay without jitter and without counting it."""
        # The exponent is capped so that a very long outage cannot overflow.
        return min(self.initial * self.factor ** min(self.attempts, 32), self.maximum)

    def next(self) -> float:
        """Return the delay before the next attempt."""
        delay = self.peek()
        self.attempts += 1
        if self.jitter:
            delay *= 1 + self.jitter * (2 * self._rng() - 1)
        return max(delay, 0.0)


class ConnectionSupervisor:
    """Run a connection forever: connect, serve, back off, repeat.

    connect: returns an async context manager that yields a connected client.
    session: coroutine function serving one connection; it ends by raising
        (connection lost) and is cancelled to stop.
    farewell: awaited with the client when the supervisor is cancelled while
        connected, before the connection is closed (to say goodbye properly).
    classify: tells an authentication failure from everything else.
    on_connected / on_disconnected: plain callbacks.
    on_auth_failed: plain callback, called once per streak when the broker
        has refused the credentials `auth_failures_before_reauth` times.
    on_failure: coroutine function awaited after every failure, before the
        back-off sleep; receives the number of consecutive failures.

    No callback can break the loop: whatever they raise is logged and
    swallowed. Only cancellation ends `run`.
    """

    def __init__(
        self,
        *,
        connect: Callable[[], AbstractAsyncContextManager[Any]],
        session: Callable[[Any], Awaitable[None]],
        farewell: Callable[[Any], Awaitable[None]] | None = None,
        classify: Callable[[BaseException], FailureKind] | None = None,
        on_connected: Callable[[], None] | None = None,
        on_disconnected: Callable[[], None] | None = None,
        on_auth_failed: Callable[[], None] | None = None,
        on_failure: Callable[[int], Awaitable[None]] | None = None,
        backoff: Backoff | None = None,
        auth_backoff: Backoff | None = None,
        stable_after: float = 30.0,
        auth_failures_before_reauth: int = 2,
        farewell_timeout: float = 3.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
        name: str = "broker",
    ) -> None:
        self._connect = connect
        self._session = session
        self._farewell = farewell
        self._classify = classify
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._on_auth_failed = on_auth_failed
        self._on_failure = on_failure
        self._backoff = backoff or Backoff()
        # Wrong credentials do not fix themselves: retry rarely.
        self._auth_backoff = auth_backoff or Backoff(initial=30.0, maximum=900.0)
        self._stable_after = stable_after
        self._auth_threshold = max(auth_failures_before_reauth, 1)
        self._farewell_timeout = farewell_timeout
        self._sleep = sleep
        self._monotonic = monotonic
        self._log = logger or logging.getLogger(__name__)
        self._name = name

        self.connected = False
        self.connects = 0
        self.failures = 0
        self.consecutive_failures = 0
        self.consecutive_auth_failures = 0
        self.last_error: str | None = None
        self.last_failure_kind: FailureKind | None = None
        self.next_retry_in: float | None = None

    # ------------------------------------------------------------------

    def _call(self, callback: Callable[[], None] | None, what: str) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 - a callback must not end the loop
            self._log.exception("Unexpected error in %s handler", what)

    def _mark_connected(self) -> None:
        self.connected = True
        self.connects += 1
        self.next_retry_in = None
        if self.consecutive_failures:
            self._log.info(
                "Reconnected to %s after %d failed attempt(s)",
                self._name,
                self.consecutive_failures,
            )
        else:
            self._log.info("Connected to %s", self._name)
        # The credentials evidently work.
        self.consecutive_auth_failures = 0
        self._auth_backoff.reset()
        self._call(self._on_connected, "connect")

    def _mark_disconnected(self) -> None:
        if not self.connected:
            return
        self.connected = False
        self._call(self._on_disconnected, "disconnect")

    async def _say_goodbye(self, client: Any) -> None:
        if self._farewell is None:
            return
        try:
            await asyncio.wait_for(self._farewell(client), self._farewell_timeout)
        except Exception as err:  # noqa: BLE001 - best effort while stopping
            self._log.debug("Could not say goodbye to %s: %r", self._name, err)

    def _register_failure(
        self, error: BaseException | None, connected_at: float | None
    ) -> float:
        """Account for a failure and return the delay before the next try."""
        if (
            connected_at is not None
            and self._monotonic() - connected_at >= self._stable_after
        ):
            # The connection had been fine for a while: start over gently.
            self._backoff.reset()
            self.consecutive_failures = 0

        kind = FailureKind.TRANSIENT
        if error is not None and self._classify is not None:
            try:
                kind = self._classify(error)
            except Exception:  # noqa: BLE001
                self._log.exception("Unexpected error while classifying a failure")

        self.failures += 1
        self.consecutive_failures += 1
        self.last_failure_kind = kind
        self.last_error = (
            "connection closed" if error is None else f"{type(error).__name__}: {error}"
        )

        if kind is FailureKind.AUTH:
            self.consecutive_auth_failures += 1
            delay = self._auth_backoff.next()
            if self.consecutive_auth_failures == self._auth_threshold:
                self._log.warning(
                    "%s keeps rejecting the credentials of this device (%s). "
                    "It was probably removed; re-authentication is required",
                    self._name,
                    self.last_error,
                )
                self._call(self._on_auth_failed, "authentication failure")
            else:
                self._log.debug(
                    "%s rejected the credentials (%s); next attempt in %.0f s",
                    self._name,
                    self.last_error,
                    delay,
                )
            return delay

        self.consecutive_auth_failures = 0
        self._auth_backoff.reset()
        delay = self._backoff.next()
        # One warning per outage is enough; the retries are debug noise.
        log = self._log.warning if self.consecutive_failures == 1 else self._log.debug
        log(
            "Connection to %s %s (%s); next attempt in %.0f s",
            self._name,
            "lost" if connected_at is not None else "failed",
            self.last_error,
            delay,
        )
        return delay

    async def run(self) -> None:
        """Serve until cancelled."""
        while True:
            connected_at: float | None = None
            error: BaseException | None = None
            stopping = False
            try:
                async with self._connect() as client:
                    connected_at = self._monotonic()
                    self._mark_connected()
                    try:
                        await self._session(client)
                    except asyncio.CancelledError:
                        stopping = True
                        await self._say_goodbye(client)
                        raise
                    finally:
                        self._mark_disconnected()
            except asyncio.CancelledError:
                self._mark_disconnected()
                raise
            except Exception as err:  # noqa: BLE001 - never give up
                if stopping:
                    # Closing the connection failed while being cancelled:
                    # the cancellation still stands.
                    raise asyncio.CancelledError from err
                error = err

            delay = self._register_failure(error, connected_at)
            self.next_retry_in = delay
            if self._on_failure is not None:
                try:
                    await self._on_failure(self.consecutive_failures)
                except Exception:  # noqa: BLE001
                    self._log.exception("Unexpected error in failure handler")
            await self._sleep(delay)
