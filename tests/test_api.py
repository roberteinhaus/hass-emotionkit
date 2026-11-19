"""Claim-code HTTP calls, against a fake aiohttp session."""

from __future__ import annotations

import asyncio
from typing import Any
import unittest

import _support  # noqa: F401
from _support import FakeResponse, FakeSession

from custom_components.emotionkit import api
from custom_components.emotionkit.api import (
    ApiConnectionError,
    ApiResponseError,
    ApiTimeout,
    ClaimStatus,
    async_announce,
    async_fetch_broker_url,
    async_wait_for_claim,
    generate_claim_code,
)
from custom_components.emotionkit.const import CLAIM_CODE_ALPHABET

BASE = "https://control.example"
CODE = "ABCD-EFGH"
TOKEN = "tok-123"
CREDENTIALS = {
    "device_id": "dabc",
    "mqtt_username": "device-dabc",
    "mqtt_password": "s3cret",
    "mqtt_broker": "tcp://broker.example:1883",
}


class ClaimCodeTest(unittest.TestCase):
    def test_format(self) -> None:
        for _ in range(200):
            code = generate_claim_code()
            self.assertEqual(len(code), 9)
            self.assertEqual(code[4], "-")
            for char in code.replace("-", ""):
                self.assertIn(char, CLAIM_CODE_ALPHABET)

    def test_no_ambiguous_characters(self) -> None:
        for char in "0O1I":
            self.assertNotIn(char, CLAIM_CODE_ALPHABET)

    def test_codes_differ(self) -> None:
        self.assertGreater(len({generate_claim_code() for _ in range(50)}), 45)


class AnnounceTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_the_device_token(self) -> None:
        session = FakeSession(FakeResponse(200, {"device_token": TOKEN}))
        self.assertEqual(await async_announce(session, BASE + "/", CODE), TOKEN)
        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{BASE}/api/v1/devices/announce")
        self.assertEqual(call["json"], {"code": CODE, "kind": "ha-integration"})

    async def test_server_busy(self) -> None:
        for status in (429, 503):
            with self.subTest(status=status):
                with self.assertRaises(ApiResponseError) as caught:
                    await async_announce(FakeSession(FakeResponse(status)), BASE, CODE)
                self.assertEqual(caught.exception.key, "server_busy")
                self.assertEqual(caught.exception.status, status)

    async def test_other_status(self) -> None:
        for status in (400, 409, 500):
            with self.subTest(status=status):
                with self.assertRaises(ApiResponseError) as caught:
                    await async_announce(FakeSession(FakeResponse(status)), BASE, CODE)
                self.assertEqual(caught.exception.key, "cannot_connect")

    async def test_unexpected_body(self) -> None:
        for body in ({}, {"device_token": ""}, {"device_token": 5}, [], "text", None):
            with self.subTest(body=body):
                with self.assertRaises(ApiResponseError) as caught:
                    await async_announce(FakeSession(FakeResponse(200, body)), BASE, CODE)
                self.assertEqual(caught.exception.key, "invalid_response")

    async def test_timeout_has_its_own_error(self) -> None:
        # A timeout used to surface as "Unknown error".
        session = FakeSession(FakeResponse(200, {"device_token": TOKEN}, delay=5))
        with self.assertRaises(ApiTimeout) as caught:
            await api._request(  # noqa: SLF001
                session, "POST", "https://x/", timeout=0.01
            )
        self.assertEqual(caught.exception.key, "timeout_connect")

    async def test_timeout_raised_by_the_client(self) -> None:
        with self.assertRaises(ApiTimeout):
            await async_announce(FakeSession(TimeoutError()), BASE, CODE)

    async def test_connection_error(self) -> None:
        for error in (OSError("no route"), ConnectionResetError(), RuntimeError("x")):
            with self.subTest(error=error):
                with self.assertRaises(ApiConnectionError) as caught:
                    await async_announce(FakeSession(error), BASE, CODE)
                self.assertEqual(caught.exception.key, "cannot_connect")

    async def test_invalid_json(self) -> None:
        session = FakeSession(FakeResponse(200, ValueError("not json")))
        with self.assertRaises(ApiConnectionError):
            await async_announce(session, BASE, CODE)

    async def test_cancellation_passes_through(self) -> None:
        session = FakeSession(FakeResponse(200, {"device_token": TOKEN}, delay=30))
        task = asyncio.create_task(async_announce(session, BASE, CODE))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_errors_do_not_leak_secrets(self) -> None:
        url = f"{BASE}/api/v1/devices/pending/{CODE}"
        session = FakeSession(OSError(f"cannot connect to {url} with token {TOKEN}"))
        with self.assertRaises(ApiConnectionError) as caught:
            await api._request(session, "GET", url)  # noqa: SLF001
        text = f"{caught.exception} {caught.exception!r} {caught.exception.__cause__!r}"
        self.assertNotIn(CODE, text)
        self.assertNotIn(TOKEN, text)
        self.assertTrue(caught.exception.__suppress_context__)


class FetchBrokerUrlTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_the_announced_url(self) -> None:
        session = FakeSession(FakeResponse(200, {"mqtt_broker_url": " wss://example.org/mqtt "}))
        self.assertEqual(await async_fetch_broker_url(session, BASE), "wss://example.org/mqtt")
        self.assertEqual(session.calls[0]["url"], f"{BASE}/api/v1/info")
        self.assertEqual(session.calls[0]["method"], "GET")

    async def test_none_when_not_announced(self) -> None:
        for body in ({}, {"mqtt_broker_url": ""}, {"mqtt_broker_url": None}, []):
            with self.subTest(body=body):
                session = FakeSession(FakeResponse(200, body))
                self.assertIsNone(await async_fetch_broker_url(session, BASE))

    async def test_errors(self) -> None:
        with self.assertRaises(ApiResponseError):
            await async_fetch_broker_url(FakeSession(FakeResponse(500)), BASE)
        with self.assertRaises(ApiConnectionError):
            await async_fetch_broker_url(FakeSession(OSError()), BASE)


class WaitForClaimTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = 0.0
        self.sleeps: list[float] = []

    async def _sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.clock += delay

    async def _wait(self, session: FakeSession, *, deadline: float = 1800.0) -> Any:
        return await async_wait_for_claim(
            session,
            BASE,
            CODE,
            TOKEN,
            deadline=deadline,
            interval=5,
            sleep=self._sleep,
            monotonic=lambda: self.clock,
        )

    async def test_claimed_after_waiting(self) -> None:
        session = FakeSession(
            FakeResponse(202, {"status": "waiting"}),
            FakeResponse(202),
            FakeResponse(200, dict(CREDENTIALS)),
        )
        outcome = await self._wait(session)
        self.assertIs(outcome.status, ClaimStatus.CLAIMED)
        self.assertEqual(outcome.credentials, CREDENTIALS)
        self.assertEqual(self.sleeps, [5, 5])
        call = session.calls[0]
        self.assertEqual(call["url"], f"{BASE}/api/v1/devices/pending/{CODE}")
        self.assertEqual(call["headers"], {"X-Device-Token": TOKEN})

    async def test_expired_code(self) -> None:
        # 404: the server no longer knows the code -> a fresh one is needed.
        outcome = await self._wait(FakeSession(FakeResponse(202), FakeResponse(404)))
        self.assertIs(outcome.status, ClaimStatus.EXPIRED)
        self.assertIsNone(outcome.credentials)

    async def test_foreign_token(self) -> None:
        outcome = await self._wait(FakeSession(FakeResponse(403)))
        self.assertIs(outcome.status, ClaimStatus.EXPIRED)

    async def test_times_out(self) -> None:
        outcome = await self._wait(FakeSession(FakeResponse(202)), deadline=60.0)
        self.assertIs(outcome.status, ClaimStatus.TIMEOUT)
        self.assertLessEqual(self.clock, 60.0)
        self.assertGreaterEqual(len(self.sleeps), 10)

    async def test_network_trouble_is_retried(self) -> None:
        session = FakeSession(
            OSError("down"),
            TimeoutError(),
            FakeResponse(500),
            FakeResponse(429),
            FakeResponse(200, dict(CREDENTIALS)),
        )
        outcome = await self._wait(session)
        self.assertIs(outcome.status, ClaimStatus.CLAIMED)
        self.assertEqual(len(session.calls), 5)

    async def test_incomplete_credentials(self) -> None:
        for body in (
            {},
            {"device_id": "d", "mqtt_username": "u"},
            {**CREDENTIALS, "mqtt_password": ""},
            {**CREDENTIALS, "device_id": 5},
            [],
        ):
            with self.subTest(body=body):
                outcome = await self._wait(FakeSession(FakeResponse(200, body)))
                self.assertIs(outcome.status, ClaimStatus.INVALID)
                self.assertIsNone(outcome.credentials)

    async def test_missing_broker_becomes_empty(self) -> None:
        body = {key: value for key, value in CREDENTIALS.items() if key != "mqtt_broker"}
        outcome = await self._wait(FakeSession(FakeResponse(200, body)))
        self.assertIs(outcome.status, ClaimStatus.CLAIMED)
        assert outcome.credentials is not None
        self.assertEqual(outcome.credentials["mqtt_broker"], "")

    async def test_extra_fields_are_dropped(self) -> None:
        outcome = await self._wait(FakeSession(FakeResponse(200, {**CREDENTIALS, "x": 1})))
        assert outcome.credentials is not None
        self.assertEqual(set(outcome.credentials), set(CREDENTIALS))


if __name__ == "__main__":
    unittest.main()
