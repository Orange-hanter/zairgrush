import time
from collections import OrderedDict
from typing import Callable


class TTLCache:
    def __init__(self, maxsize: int, ttl: float, clock: Callable[[], float] = time.monotonic):
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        if ttl <= 0:
            raise ValueError("ttl must be > 0")
        self.maxsize = maxsize
        self.ttl = ttl
        self.clock = clock
        self._cache = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expirations = 0

    def _cleanup(self):
        now = self.clock()
        expired = [k for k, (v, exp) in self._cache.items() if exp <= now]
        for k in expired:
            del self._cache[k]
            self._expirations += 1

    def set(self, key, value):
        now = self.clock()
        expires = now + self.ttl
        if key in self._cache:
            old_value, old_expires = self._cache[key]
            if old_expires <= now:
                del self._cache[key]
                self._expirations += 1
            else:
                self._cache[key] = (value, expires)
                self._cache.move_to_end(key)
                return
        self._cache[key] = (value, expires)
        self._cache.move_to_end(key)
        self._cleanup()
        while len(self._cache) > self.maxsize:
            self._cache.popitem(last=False)
            self._evictions += 1

    def get(self, key):
        if key not in self._cache:
            self._misses += 1
            return None
        value, expires = self._cache[key]
        now = self.clock()
        if expires <= now:
            del self._cache[key]
            self._expirations += 1
            self._misses += 1
            return None
        self._cache.move_to_end(key)
        self._hits += 1
        return value

    def delete(self, key):
        if key not in self._cache:
            return False
        value, expires = self._cache[key]
        now = self.clock()
        if expires <= now:
            del self._cache[key]
            self._expirations += 1
            return False
        del self._cache[key]
        return True

    def __len__(self):
        self._cleanup()
        return len(self._cache)

    def stats(self):
        self._cleanup()
        return {
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "expirations": self._expirations,
        }
