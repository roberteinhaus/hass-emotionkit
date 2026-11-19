"""The reconnect loop: back-off, error handling, clean stop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from typing import Any
import unittest

import _support  # noqa: F401

from custom_components.emotionkit.connection import (
    Backoff,
    ConnectionSupervisor,
    FailureKind,
    is_auth_reason_code,
    reason_code_value,
)

_QUIET = logging.getLogger("emotionkit.tests.connection")
_QUIET.addHandler(logging.NullHandler())
_QUIET.propagate = False


class FakeReasonCode:
    """Stands in for paho's ReasonCode."""

    def __init__(self, value: int) -> None:
        self.value = value


class Refused(Exception):
    """Stands in for aiomqtt's MqttConnectError."""

    def __init__(self, rc: Any) -> None:
        super().__init__(f"refused ({getattr(rc, 'value', rc)})")
        self.rc = rc


class StopLoop(BaseException):
    """Ends supervisor.run() from inside a test (not caught by `except Exception`)."""


def classify(err: BaseException) -> FailureKind:
    if isinstance(err, Refused) and is_auth_reason_code(err.rc):
        return FailureKind.AUTH
    return FailureKind.TRANSIENT


class ReasonCodeTest(unittest.TestCase):
    def test_auth_codes(self) -> None:
        # 4/5: MQTT 3.1.1 return codes; 134/135: what paho-mqtt 2 reports.
        for code in (4, 5, 134, 135):
            with self.subTest(code=code):
                self.assertTrue(is_auth_reason_code(code))
                self.assertTrue(is_auth_reason_code(FakeReasonCode(code)))

    def test_other_codes(self) -> None:
        for code in (0, 1, 2, 3, 7, 16, 128, 136, 137):
            with self.subTest(code=code):
                self.assertFalse(is_auth_reason_code(code))
                self.assertFalse(is_auth_reason_code(FakeReasonCode(code)))

    def test_not_a_code(self) -> None:
        for value in (None, "5", True, 4.0, object(), FakeReasonCode("5")):  # type: ignore[arg-type]
            with self.subTest(value=value):
                self.assertFalse(is_auth_reason_code(value))
        self.assertIsNone(reason_code_value(True))
        self.assertEqual(reason_code_value(FakeReasonCode(135)), 135)


class BackoffTest(unittest.TestCase):
    def test_grows_exponentially_up_to_the_cap(self) -> None:
        backoff = Backoff(initial=2, maximum=300, jitter=0)
        self.assertEqual(
            [backoff.next() for _ in range(10)], [2, 4, 8, 16, 32, 64, 128, 256, 300, 300]
        )

    def test_reset(self) -> None:
        backoff = Backoff(initial=2, maximum=300, jitter=0)
        for _ in range(5):
            backoff.next()
        backoff.reset()
        self.assertEqual(backoff.next(), 2)

    def test_jitter_stays_within_bounds(self) -> None:
        low = Backoff(initial=10, jitter=0.2, rng=lambda: 0.0)
        high = Backoff(initial=10, jitter=0.2, rng=lambda: 1.0)
        self.assertAlmostEqual(low.next(), 8.0)
        self.assertAlmostEqual(high.next(), 12.0)

    def test_never_overflows(self) -> None:
        backoff = Backoff(initial=2, maximum=300, jitter=0)
        for _ in range(5000):
            delay = backoff.next()
        self.assertEqual(delay, 300)

    def test_peek_does_not_count(self) -> None:
        backoff = Backoff(initial=2, jitter=0)
        self.assertEqual(backoff.peek(), 2)
        self.assertEqual(backoff.peek(), 2)
        self.assertEqual(backoff.attempts, 0)


class Harness:
    """A supervisor wired to scripted connections and a fake clock."""

    def __init__(
        self,
        *,
        connect_script: list[Any],
        session_script: list[Any] | None = None,
        stop_after_sleeps: int | None = None,
        **supervisor_args: Any,
    ) -> None:
        # connect_script: per attempt an exception to raise, or None to succeed.
        # session_script: per successful connection an exception to raise,
        #   a number of seconds the connection lasts, or None to stay up.
        self.connect_script = list(connect_script)
        self.session_script = list(session_script or [])
        self.stop_after_sleeps = stop_after_sleeps
        self.clock = 0.0
        self.sleeps: list[float] = []
        self.events: list[str] = []
        self.failures_seen: list[int] = []
        self.farewells = 0
        self.closed = 0
        self.session_started = asyncio.Event()

        supervisor_args.setdefault("backoff", Backoff(initial=2, maximum=300, jitter=0))
        supervisor_args.setdefault("auth_backoff", Backoff(initial=30, maximum=900, jitter=0))
        self.supervisor = ConnectionSupervisor(
            connect=self._connect,
            session=self._session,
            farewell=self._farewell,
            classify=classify,
            on_connected=lambda: self.events.append("connected"),
            on_disconnected=lambda: self.events.append("disconnected"),
            on_auth_failed=lambda: self.events.append("auth_failed"),
            on_failure=self._on_failure,
            sleep=self._sleep,
            monotonic=lambda: self.clock,
            logger=_QUIET,
            **supervisor_args,
        )

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[str]:
        if not self.connect_script:
            raise StopLoop
        outcome = self.connect_script.pop(0)
        if outcome is not None:
            raise outcome
        try:
            yield "client"
        finally:
            self.closed += 1

    async def _session(self, client: str) -> None:
        assert client == "client"
        self.session_started.set()
        outcome = self.session_script.pop(0) if self.session_script else None
        if outcome is None:
            await asyncio.Event().wait()  # stay connected until cancelled
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            self.clock += float(outcome)  # the connection lasted this long

    async def _farewell(self, client: str) -> None:
        self.farewells += 1

    async def _on_failure(self, consecutive: int) -> None:
        self.failures_seen.append(consecutive)

    async def _sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.clock += delay
        if self.stop_after_sleeps is not None and len(self.sleeps) >= self.stop_after_sleeps:
            raise StopLoop
        await asyncio.sleep(0)

    async def run_to_end(self) -> None:
        try:
            await self.supervisor.run()
        except StopLoop:
            pass


class SupervisorTest(unittest.IsolatedAsyncioTestCase):
    async def test_retries_with_growing_delays(self) -> None:
        harness = Harness(connect_script=[OSError("down")] * 6, stop_after_sleeps=6)
        await harness.run_to_end()
        self.assertEqual(harness.sleeps, [2, 4, 8, 16, 32, 64])
        self.assertEqual(harness.failures_seen, [1, 2, 3, 4, 5, 6])
        self.assertEqual(harness.events, [])
        self.assertFalse(harness.supervisor.connected)
        self.assertEqual(harness.supervisor.failures, 6)
        self.assertIn("OSError", harness.supervisor.last_error or "")

    async def test_any_exception_is_survived(self) -> None:
        # Only MqttError used to be caught; anything else ended the loop for
        # good while the entry still looked loaded.
        harness = Harness(
            connect_script=[None, None, None, None],
            session_script=[
                TypeError("bug in a handler"),
                KeyError("x"),
                RuntimeError("boom"),
                ZeroDivisionError(),
            ],
            stop_after_sleeps=4,
        )
        await harness.run_to_end()
        self.assertEqual(harness.supervisor.connects, 4)
        self.assertEqual(harness.events, ["connected", "disconnected"] * 4)
        self.assertEqual(harness.closed, 4)
        self.assertEqual(harness.sleeps, [2, 4, 8, 16])

    async def test_session_returning_is_treated_as_a_lost_connection(self) -> None:
        harness = Harness(connect_script=[None, None], session_script=[1, 1], stop_after_sleeps=2)
        await harness.run_to_end()
        self.assertEqual(harness.supervisor.connects, 2)
        self.assertEqual(harness.supervisor.last_error, "connection closed")

    async def test_backoff_resets_after_a_stable_connection(self) -> None:
        harness = Harness(
            connect_script=[OSError(), OSError(), OSError(), None, OSError(), OSError()],
            session_script=[120],  # up for two minutes, then lost
            stop_after_sleeps=6,
        )
        await harness.run_to_end()
        self.assertEqual(harness.sleeps, [2, 4, 8, 2, 4, 8])
        self.assertEqual(harness.failures_seen, [1, 2, 3, 1, 2, 3])

    async def test_flapping_connection_keeps_backing_off(self) -> None:
        # Accepted and dropped again at once: no reset, or the loop would
        # hammer the broker every two seconds forever.
        harness = Harness(
            connect_script=[None] * 5,
            session_script=[0.5, 0.5, 0.5, 0.5, 0.5],
            stop_after_sleeps=5,
        )
        await harness.run_to_end()
        self.assertEqual(harness.sleeps, [2, 4, 8, 16, 32])

    async def test_auth_failure_requests_reauth_once_and_slows_down(self) -> None:
        refused = [Refused(FakeReasonCode(135)) for _ in range(8)]
        harness = Harness(connect_script=refused, stop_after_sleeps=8)
        await harness.run_to_end()
        self.assertEqual(harness.events, ["auth_failed"])
        self.assertEqual(harness.sleeps, [30, 60, 120, 240, 480, 900, 900, 900])
        self.assertEqual(harness.supervisor.consecutive_auth_failures, 8)
        self.assertIs(harness.supervisor.last_failure_kind, FailureKind.AUTH)

    async def test_single_auth_failure_does_not_request_reauth(self) -> None:
        harness = Harness(
            connect_script=[Refused(5), None],
            session_script=[None],
        )
        task = asyncio.create_task(harness.supervisor.run())
        await asyncio.wait_for(harness.session_started.wait(), 2)
        self.assertEqual(harness.events, ["connected"])
        self.assertEqual(harness.supervisor.consecutive_auth_failures, 0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_auth_streak_is_broken_by_other_failures(self) -> None:
        harness = Harness(
            connect_script=[Refused(5), OSError(), Refused(5), OSError()],
            stop_after_sleeps=4,
        )
        await harness.run_to_end()
        self.assertNotIn("auth_failed", harness.events)
        self.assertEqual(harness.sleeps, [30, 2, 30, 4])

    async def test_refused_for_another_reason_is_not_auth(self) -> None:
        harness = Harness(connect_script=[Refused(3), Refused(3), Refused(3)], stop_after_sleeps=3)
        await harness.run_to_end()
        self.assertEqual(harness.events, [])
        self.assertEqual(harness.sleeps, [2, 4, 8])

    async def test_reauth_is_requested_again_after_a_recovery(self) -> None:
        harness = Harness(
            connect_script=[Refused(5), Refused(5), None, Refused(5), Refused(5)],
            session_script=[60],
            stop_after_sleeps=5,
        )
        await harness.run_to_end()
        self.assertEqual(
            harness.events, ["auth_failed", "connected", "disconnected", "auth_failed"]
        )

    async def test_cancel_while_connected_says_goodbye_and_stops(self) -> None:
        harness = Harness(connect_script=[None], session_script=[None])
        task = asyncio.create_task(harness.supervisor.run())
        await asyncio.wait_for(harness.session_started.wait(), 2)
        self.assertTrue(harness.supervisor.connected)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(task.cancelled())
        self.assertEqual(harness.farewells, 1)
        self.assertEqual(harness.closed, 1)
        self.assertEqual(harness.events, ["connected", "disconnected"])
        self.assertFalse(harness.supervisor.connected)
        self.assertEqual(harness.sleeps, [])

    async def test_cancel_while_waiting_stops(self) -> None:
        sleeping = asyncio.Event()

        async def sleep(delay: float) -> None:
            sleeping.set()
            await asyncio.Event().wait()

        harness = Harness(connect_script=[OSError()])
        harness.supervisor._sleep = sleep  # noqa: SLF001
        task = asyncio.create_task(harness.supervisor.run())
        await asyncio.wait_for(sleeping.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(harness.farewells, 0)

    async def test_cancel_stands_even_if_goodbye_and_close_fail(self) -> None:
        @asynccontextmanager
        async def connect() -> AsyncIterator[str]:
            try:
                yield "client"
            finally:
                # e.g. the disconnect times out
                raise OSError("close failed")

        async def farewell(client: str) -> None:
            raise RuntimeError("publish failed")

        started = asyncio.Event()

        async def session(client: str) -> None:
            started.set()
            await asyncio.Event().wait()

        supervisor = ConnectionSupervisor(
            connect=connect, session=session, farewell=farewell, logger=_QUIET
        )
        task = asyncio.create_task(supervisor.run())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertTrue(task.cancelled())

    async def test_slow_goodbye_is_cut_short(self) -> None:
        async def farewell(client: str) -> None:
            await asyncio.sleep(60)

        harness = Harness(connect_script=[None], session_script=[None])
        harness.supervisor._farewell = farewell  # noqa: SLF001
        harness.supervisor._farewell_timeout = 0.05  # noqa: SLF001
        task = asyncio.create_task(harness.supervisor.run())
        await asyncio.wait_for(harness.session_started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(harness.closed, 1)

    async def test_callbacks_cannot_break_the_loop(self) -> None:
        def boom() -> None:
            raise RuntimeError("callback bug")

        async def failing_hook(consecutive: int) -> None:
            raise RuntimeError("hook bug")

        def bad_classify(err: BaseException) -> FailureKind:
            raise RuntimeError("classify bug")

        harness = Harness(
            connect_script=[None, OSError(), None],
            session_script=[1, 1],
            stop_after_sleeps=3,
        )
        supervisor = harness.supervisor
        supervisor._on_connected = boom  # noqa: SLF001
        supervisor._on_disconnected = boom  # noqa: SLF001
        supervisor._on_failure = failing_hook  # noqa: SLF001
        supervisor._classify = bad_classify  # noqa: SLF001
        await harness.run_to_end()
        self.assertEqual(supervisor.connects, 2)
        self.assertEqual(len(harness.sleeps), 3)

    async def test_works_without_optional_callbacks(self) -> None:
        attempts = 0

        @asynccontextmanager
        async def connect() -> AsyncIterator[None]:
            nonlocal attempts
            attempts += 1
            if attempts > 2:
                raise StopLoop
            yield None

        async def session(client: None) -> None:
            raise ValueError("lost")

        async def sleep(delay: float) -> None:
            return None

        supervisor = ConnectionSupervisor(
            connect=connect, session=session, sleep=sleep, logger=_QUIET
        )
        with self.assertRaises(StopLoop):
            await supervisor.run()
        self.assertEqual(supervisor.connects, 2)

    async def test_next_retry_is_reported(self) -> None:
        harness = Harness(connect_script=[OSError()], stop_after_sleeps=1)
        await harness.run_to_end()
        self.assertEqual(harness.supervisor.next_retry_in, 2)
        self.assertEqual(harness.supervisor.consecutive_failures, 1)


class LastErrorTest(unittest.TestCase):
    def test_last_error_is_type_and_message(self) -> None:
        # Kept for the diagnostics download. The supervisor never sees the
        # credentials; they live inside the connect callable.
        harness = Harness(connect_script=[])
        delay = harness.supervisor._register_failure(  # noqa: SLF001
            OSError("connection refused"), None
        )
        self.assertEqual(delay, 2)
        self.assertEqual(harness.supervisor.last_error, "OSError: connection refused")


if __name__ == "__main__":
    unittest.main()
