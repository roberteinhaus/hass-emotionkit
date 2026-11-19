"""Staleness detection independent of clock skew."""

from __future__ import annotations

import unittest

import _support  # noqa: F401

from custom_components.emotionkit.clock import ClockOffsetEstimator

# An arbitrary server epoch and an unrelated local monotonic origin.
SERVER = 1_795_000_000.0
MONO = 5_000.0


class ClockOffsetEstimatorTest(unittest.TestCase):
    def test_first_message_is_never_stale(self) -> None:
        clock = ClockOffsetEstimator()
        self.assertEqual(clock.observe(SERVER, MONO), 0.0)
        self.assertFalse(ClockOffsetEstimator().is_stale(SERVER, MONO))

    def test_missing_timestamp_is_never_stale(self) -> None:
        clock = ClockOffsetEstimator()
        self.assertIsNone(clock.observe(None, MONO))
        self.assertFalse(clock.is_stale(None, MONO + 1000))
        self.assertIsNone(clock.offset)

    def test_steady_stream_is_fresh(self) -> None:
        clock = ClockOffsetEstimator()
        for second in range(0, 600, 5):
            # Network delay jitters between 20 and 60 ms.
            delay = 0.02 + (second % 3) * 0.02
            self.assertFalse(clock.is_stale(SERVER + second, MONO + second + delay))
        assert clock.last_age is not None
        self.assertLess(clock.last_age, 0.1)

    def test_any_constant_skew_is_irrelevant(self) -> None:
        # The old filter dropped everything once the Home Assistant clock was
        # more than 20 s ahead. Whatever the skew: nothing may be dropped.
        for skew in (-86_400.0, -3_600.0, -25.0, 0.0, 25.0, 3_600.0, 86_400.0):
            with self.subTest(skew=skew):
                clock = ClockOffsetEstimator(max_age=20.0)
                for second in range(0, 120, 5):
                    stale = clock.is_stale(SERVER + skew + second, MONO + second + 0.03)
                    self.assertFalse(stale)

    def test_delayed_message_is_stale(self) -> None:
        clock = ClockOffsetEstimator(max_age=20.0)
        clock.observe(SERVER, MONO + 0.05)
        clock.observe(SERVER + 5, MONO + 5.05)
        # Stamped at +10 s but delivered at +45 s: 35 s late.
        age = clock.observe(SERVER + 10, MONO + 45.05)
        assert age is not None
        self.assertAlmostEqual(age, 35.0, places=3)
        self.assertTrue(clock.is_stale(SERVER + 11, MONO + 45.06))
        # The stream has caught up again.
        self.assertFalse(clock.is_stale(SERVER + 45, MONO + 45.1))

    def test_backlog_after_a_stall_is_dropped_but_live_messages_are_not(self) -> None:
        clock = ClockOffsetEstimator(max_age=20.0)
        for second in range(0, 30, 5):
            clock.observe(SERVER + second, MONO + second + 0.05)
        # The connection stalls for 40 s, then everything arrives at once.
        results = [
            clock.is_stale(SERVER + stamped, MONO + 70.0)
            for stamped in (31, 36, 41, 46, 51, 56, 61, 66, 69.9)
        ]
        self.assertEqual(results, [True, True, True, True, False, False, False, False, False])

    def test_slightly_late_message_is_not_stale(self) -> None:
        clock = ClockOffsetEstimator(max_age=20.0)
        clock.observe(SERVER, MONO)
        self.assertFalse(clock.is_stale(SERVER + 10, MONO + 29.9))
        self.assertTrue(clock.is_stale(SERVER + 10, MONO + 30.1))

    def test_faster_message_lowers_the_baseline(self) -> None:
        clock = ClockOffsetEstimator()
        # The first message happened to be slow (2 s).
        clock.observe(SERVER, MONO + 2.0)
        self.assertEqual(clock.observe(SERVER + 5, MONO + 5.1), 0.0)
        age = clock.observe(SERVER + 10, MONO + 12.0)
        assert age is not None
        self.assertAlmostEqual(age, 1.9, places=3)

    def test_server_clock_stepping_back_cannot_silence_for_long(self) -> None:
        clock = ClockOffsetEstimator(max_age=20.0, window=300.0)
        for second in range(0, 30, 5):
            self.assertFalse(clock.is_stale(SERVER + second, MONO + second))
        # The server clock is set back by one hour: every message now looks
        # an hour old. After `recovery` seconds the baseline is rebuilt.
        stale_for = None
        for second in range(30, 200, 5):
            if not clock.is_stale(SERVER - 3600 + second, MONO + second):
                stale_for = second - 30
                break
        self.assertIsNotNone(stale_for)
        assert stale_for is not None
        self.assertLessEqual(stale_for, 45)
        self.assertEqual(clock.rebaselined, 1)
        # And it stays fresh afterwards.
        for second in range(200, 300, 5):
            self.assertFalse(clock.is_stale(SERVER - 3600 + second, MONO + second))

    def test_server_clock_stepping_forward_is_harmless(self) -> None:
        clock = ClockOffsetEstimator()
        clock.observe(SERVER, MONO)
        self.assertFalse(clock.is_stale(SERVER + 3600 + 5, MONO + 5))
        self.assertFalse(clock.is_stale(SERVER + 3600 + 10, MONO + 10))

    def test_window_forgets_old_baseline(self) -> None:
        clock = ClockOffsetEstimator(max_age=20.0, window=60.0)
        clock.observe(SERVER, MONO)
        # Ten minutes of silence, then a message whose difference is 100 s
        # larger: with nothing recent to compare with, it is the new baseline.
        self.assertFalse(clock.is_stale(SERVER + 500, MONO + 600))
        self.assertEqual(clock.observe(SERVER + 505, MONO + 605), 0.0)

    def test_reset(self) -> None:
        clock = ClockOffsetEstimator()
        clock.observe(SERVER, MONO)
        self.assertIsNotNone(clock.offset)
        clock.reset()
        self.assertIsNone(clock.offset)
        self.assertIsNone(clock.last_age)
        self.assertEqual(clock.observe(SERVER + 100, MONO + 500), 0.0)

    def test_memory_stays_bounded(self) -> None:
        clock = ClockOffsetEstimator(window=60.0)
        for tick in range(20_000):
            clock.observe(SERVER + tick * 0.1, MONO + tick * 0.1 + (tick % 7) * 0.001)
        self.assertLess(len(clock._samples), 1000)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
