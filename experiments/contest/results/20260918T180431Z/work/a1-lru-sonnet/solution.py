import time
from collections import OrderedDict
from typing import Any, Callable, Optional


class TTLCache:
    def __init__(
        self,
        maxsize: int,
        ttl: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        if ttl <= 0:
            raise ValueError("ttl must be > 0")

        self.maxsize = maxsize
        self.ttl = ttl
        self.clock = clock

        # key -> (value, set_time)
        self._data: "OrderedDict[str, tuple]" = OrderedDict()

        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expirations = 0

    def _is_expired(self, set_time: float) -> bool:
        return self.clock() > set_time + self.ttl

    def _evict_if_needed(self) -> None:
        while len(self._data) > self.maxsize:
            oldest_key, (oldest_val, oldest_time) = next(iter(self._data.items()))
            if self._is_expired(oldest_time):
                del self._data[oldest_key]
                self._expirations += 1
            else:
                del self._data[oldest_key]
                self._evictions += 1

    def set(self, key: str, value: Any) -> None:
        now = self.clock()
        if key in self._data:
            del self._data[key]
        self._data[key] = (value, now)
        self._data.move_to_end(key)
        self._evict_if_needed()

    def get(self, key: str) -> Optional[Any]:
        if key not in self._data:
            self._misses += 1
            return None

        value, set_time = self._data[key]
        if self._is_expired(set_time):
            del self._data[key]
            self._expirations += 1
            self._misses += 1
            return None

        self._data.move_to_end(key)
        self._hits += 1
        return value

    def delete(self, key: str) -> bool:
        if key not in self._data:
            return False

        value, set_time = self._data[key]
        if self._is_expired(set_time):
            del self._data[key]
            self._expirations += 1
            return False

        del self._data[key]
        return True

    def _purge_expired(self) -> None:
        expired_keys = [
            k for k, (v, t) in self._data.items() if self._is_expired(t)
        ]
        for k in expired_keys:
            del self._data[k]
            self._expirations += 1

    def __len__(self) -> int:
        self._purge_expired()
        return len(self._data)

    def stats(self) -> dict:
        self._purge_expired()
        return {
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "expirations": self._expirations,
        }
