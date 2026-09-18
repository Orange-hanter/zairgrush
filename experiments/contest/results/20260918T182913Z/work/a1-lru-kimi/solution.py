import time
from collections import OrderedDict
from typing import Any, Callable


class TTLCache:
    def __init__(
        self,
        maxsize: int,
        ttl: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        if ttl <= 0:
            raise ValueError("ttl must be > 0")

        self._maxsize = maxsize
        self._ttl = ttl
        self._clock = clock
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()

        self._live = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expirations = 0

    def _is_expired(self, set_time: float, now: float) -> bool:
        return now > set_time + self._ttl

    def _purge_expired(self) -> int:
        now = self._clock()
        expired_keys = []
        for key, (_, set_time) in self._cache.items():
            if self._is_expired(set_time, now):
                expired_keys.append(key)

        for key in expired_keys:
            del self._cache[key]
            self._live -= 1
            self._expirations += 1

        return self._live

    def get(self, key: str):
        now = self._clock()

        if key not in self._cache:
            self._misses += 1
            return None

        value, set_time = self._cache[key]
        if self._is_expired(set_time, now):
            del self._cache[key]
            self._live -= 1
            self._expirations += 1
            self._misses += 1
            return None

        self._cache.move_to_end(key)
        self._hits += 1
        return value

    def set(self, key: str, value) -> None:
        now = self._clock()

        if key in self._cache:
            old_value, old_time = self._cache[key]
            if self._is_expired(old_time, now):
                del self._cache[key]
                self._live -= 1
                self._expirations += 1
            else:
                self._cache[key] = (value, now)
                self._cache.move_to_end(key)
                return

        self._cache[key] = (value, now)
        self._live += 1

        while self._live > self._maxsize:
            _, (_, set_time) = self._cache.popitem(last=False)
            if self._is_expired(set_time, now):
                self._live -= 1
                self._expirations += 1
            else:
                self._live -= 1
                self._evictions += 1

    def delete(self, key: str) -> bool:
        now = self._clock()

        if key not in self._cache:
            return False

        _, set_time = self._cache[key]
        del self._cache[key]
        self._live -= 1

        if self._is_expired(set_time, now):
            self._expirations += 1
            return False
        return True

    def __len__(self) -> int:
        self._purge_expired()
        return self._live

    def stats(self) -> dict:
        self._purge_expired()
        return {
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "expirations": self._expirations,
        }
