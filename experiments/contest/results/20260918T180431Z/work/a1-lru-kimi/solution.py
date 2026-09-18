import time
from collections import OrderedDict
from typing import Callable, Dict


class _Node:
    __slots__ = ("value", "set_time")

    def __init__(self, value, set_time: float):
        self.value = value
        self.set_time = set_time


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
        self._cache: OrderedDict[str, _Node] = OrderedDict()

        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expirations = 0
        self._live_count = 0

    def _is_expired(self, node: _Node) -> bool:
        return self._clock() > node.set_time + self._ttl

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [
            key
            for key, node in self._cache.items()
            if now > node.set_time + self._ttl
        ]
        for key in expired:
            self._cache.pop(key, None)
            self._live_count -= 1
            self._expirations += 1

    def _evict_one(self, now: float) -> None:
        while self._cache:
            key, node = self._cache.popitem(last=False)
            if now > node.set_time + self._ttl:
                self._live_count -= 1
                self._expirations += 1
                continue
            self._live_count -= 1
            self._evictions += 1
            return

    def set(self, key: str, value) -> None:
        now = self._clock()

        if key in self._cache:
            node = self._cache[key]
            node.value = value
            node.set_time = now
            self._cache.move_to_end(key)
        else:
            self._cache[key] = _Node(value, now)
            self._live_count += 1

        while self._live_count > self._maxsize:
            self._evict_one(now)

    def get(self, key: str):
        node = self._cache.get(key)
        if node is None:
            self._misses += 1
            return None

        if self._is_expired(node):
            self._cache.pop(key)
            self._live_count -= 1
            self._expirations += 1
            self._misses += 1
            return None

        self._cache.move_to_end(key)
        self._hits += 1
        return node.value

    def delete(self, key: str) -> bool:
        node = self._cache.get(key)
        if node is None:
            return False

        self._cache.pop(key)

        if self._is_expired(node):
            self._live_count -= 1
            self._expirations += 1
            return False

        self._live_count -= 1
        return True

    def __len__(self) -> int:
        self._purge_expired()
        return self._live_count

    def stats(self) -> Dict[str, int]:
        self._purge_expired()
        return {
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "expirations": self._expirations,
        }
