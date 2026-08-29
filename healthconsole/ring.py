"""In-memory ring buffer covering the last 60 minutes.

Displaying finely and keeping long are two distinct needs, and conflating them
makes the database explode. The 2-second live view lives here — roughly 360 KiB
for 25 metrics — and only a value aggregated every 30 seconds reaches the disk.
"""

from __future__ import annotations

from collections import deque


class Ring:
    def __init__(self, window_seconds: int = 3600, live_seconds: int = 2) -> None:
        if live_seconds < 1:
            raise ValueError("live_seconds must be at least 1")
        self.window_seconds = window_seconds
        self.live_seconds = live_seconds
        self.capacity = max(1, window_seconds // live_seconds)
        self._data: dict[str, deque[tuple[float, float]]] = {}

    def push(self, key: str, value: float, ts: float) -> None:
        buffer = self._data.get(key)
        if buffer is None:
            buffer = self._data[key] = deque(maxlen=self.capacity)
        buffer.append((ts, value))

    def series(self, key: str) -> list[tuple[float, float]]:
        return list(self._data.get(key, ()))

    def keys(self) -> list[str]:
        return list(self._data)

    def aggregate(self, key: str, since: float
                  ) -> tuple[float, float, float] | None:
        """(average, minimum, maximum) since `since`, or None if no point.

        None rather than zero: a zero would read as a real measurement.
        """
        buffer = self._data.get(key)
        if not buffer:
            return None
        values = [value for ts, value in buffer if ts >= since]
        if not values:
            return None
        return (sum(values) / len(values), min(values), max(values))
