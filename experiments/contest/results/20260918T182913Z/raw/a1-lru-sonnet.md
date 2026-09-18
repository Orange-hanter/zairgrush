```python
import time
from collections import OrderedDict
from typing import Callable, Optional, Any, Dict


class TTLCache:
    def __init__(self, maxsize: int, ttl: float, clock: Callable[[], float] = time.monotonic):
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

    def _is_expired(self, set_time: float, now: Optional[float] = None) -> bool:
        if now is None:
            now = self.clock()
        return now > set_time + self.ttl

    def set(self, key: str, value: Any) -> None:
        now = self.clock()

        if key in self._data:
            # update existing key: refresh value, time, recency
            self._data[key] = (value, now)
            self._data.move_to_end(key)
        else:
            self._data[key] = (value, now)
            self._data.move_to_end(key)

        # Enforce capacity
        while len(self._data) > self.maxsize:
            oldest_key = next(iter(self._data))
            oldest_value, oldest_time = self._data[oldest_key]
            check_now = self.clock()
            if self._is_expired(oldest_time, check_now):
                del self._data[oldest_key]
                self._expirations += 1
            else:
                del self._data[oldest_key]
                self._evictions += 1

    def get(self, key: str) -> Optional[Any]:
        if key not in self._data:
            self._misses += 1
            return None

        value, set_time = self._data[key]
        now = self.clock()

        if self._is_expired(set_time, now):
            del self._data[key]
            self._expirations += 1
            self._misses += 1
            return None

        # alive: update recency
        self._data.move_to_end(key)
        self._hits += 1
        return value

    def delete(self, key: str) -> bool:
        if key not in self._data:
            return False

        value, set_time = self._data[key]
        now = self.clock()

        if self._is_expired(set_time, now):
            del self._data[key]
            self._expirations += 1
            return False

        del self._data[key]
        return True

    def _purge_expired(self) -> None:
        now = self.clock()
        expired_keys = [
            k for k, (v, t) in self._data.items() if self._is_expired(t, now)
        ]
        for k in expired_keys:
            del self._data[k]
            self._expirations += 1

    def __len__(self) -> int:
        self._purge_expired()
        return len(self._data)

    def stats(self) -> Dict[str, int]:
        self._purge_expired()
        return {
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "expirations": self._expirations,
        }
```