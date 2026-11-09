import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    """Sliding-window limit per client, held in memory: enough for one instance. Several
    instances would share the count in Redis or apply the limit at a gateway."""

    def __init__(
        self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)

    def retry_after(self, client: str) -> int | None:
        """Records a request; returns seconds to wait if it is over the limit, else None."""
        now = self._clock()
        hits = self._hits[client]
        while hits and hits[0] <= now - self._window_s:
            hits.popleft()
        if len(hits) >= self._limit:
            return max(1, int(hits[0] + self._window_s - now + 0.999))
        hits.append(now)
        return None
