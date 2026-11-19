"""Staleness detection that does not depend on synchronised clocks.

The server stamps every envelope with its own wall clock. Comparing that with
the local wall clock (as the first version of this integration did) silences
everything as soon as the two clocks disagree by more than the allowed age.

Instead, the offset between the server clock and the local *monotonic* clock
is estimated from the messages themselves: the smallest difference seen
recently belongs to the message that travelled fastest and serves as the
baseline. A message is old by however much its own difference exceeds that
baseline. A constant skew, a wrong time zone or a stepping wall clock on the
Home Assistant host therefore have no effect at all.

Plain Python, no Home Assistant imports.
"""

from __future__ import annotations

from collections import deque

DEFAULT_MAX_AGE = 20.0
DEFAULT_WINDOW = 300.0


class ClockOffsetEstimator:
    """Estimate how old a server-stamped message is.

    max_age: age in seconds above which a message counts as stale.
    window: how long a baseline sample is remembered (monotonic seconds).
    recovery: if every message has been stale for this long, the baseline is
        assumed to be wrong (the server clock was stepped) and is rebuilt.
        A genuine backlog drains within moments, so this never lets one
        through; it only guarantees that the filter cannot stay shut.
    """

    def __init__(
        self,
        max_age: float = DEFAULT_MAX_AGE,
        window: float = DEFAULT_WINDOW,
        recovery: float | None = None,
    ) -> None:
        self.max_age = max_age
        self.window = window
        self.recovery = recovery if recovery is not None else 2 * max_age
        # (monotonic, delta) with strictly increasing delta: the leftmost
        # entry is the minimum of the window.
        self._samples: deque[tuple[float, float]] = deque()
        self._stale_since: float | None = None
        self.last_age: float | None = None
        self.rebaselined = 0

    @property
    def offset(self) -> float | None:
        """Return the current baseline (monotonic minus server time)."""
        return self._samples[0][1] if self._samples else None

    def reset(self) -> None:
        """Forget everything (e.g. after a reconnect to another server)."""
        self._samples.clear()
        self._stale_since = None
        self.last_age = None

    def observe(self, server_ts: float | None, monotonic: float) -> float | None:
        """Record a message and return its estimated age in seconds.

        Returns None when the message carries no usable timestamp; such a
        message is never considered stale.
        """
        if server_ts is None:
            return None

        delta = monotonic - server_ts
        while self._samples and monotonic - self._samples[0][0] > self.window:
            self._samples.popleft()

        baseline = self._samples[0][1] if self._samples else delta
        age = max(delta - baseline, 0.0)

        if age > self.max_age:
            if self._stale_since is None:
                self._stale_since = monotonic
            elif monotonic - self._stale_since >= self.recovery:
                self._samples.clear()
                self._stale_since = None
                self.rebaselined += 1
                age = 0.0
        else:
            self._stale_since = None

        while self._samples and self._samples[-1][1] >= delta:
            self._samples.pop()
        self._samples.append((monotonic, delta))

        self.last_age = age
        return age

    def is_stale(self, server_ts: float | None, monotonic: float) -> bool:
        """Record a message and tell whether it is older than max_age."""
        age = self.observe(server_ts, monotonic)
        return age is not None and age > self.max_age
