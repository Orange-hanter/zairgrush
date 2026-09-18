```python
import time
from collections import OrderedDict
from typing import Callable, Any, Optional, Dict


class TTLCache:
    def __init__(self, maxsize: int, ttl: float, clock: Callable[[], float] = time.monotonic):
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        if ttl <= 0:
            raise ValueError("ttl must be > 0")

        self.maxsize = maxsize
        self.ttl = ttl
        self.clock = clock

        # key -> (value, expire_at)
        self._data: "OrderedDict[str, tuple]" = OrderedDict()

        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expirations = 0

    def _purge_expired(self) -> None:
        now = self.clock()
        expired_keys = [k for k, (v, exp) in self._data.items() if exp <= now]
        for k in expired_keys:
            del self._data[k]
            self._expirations += 1

    def set(self, key: str, value: Any) -> None:
        self._purge_expired()

        now = self.clock()
        expire_at = now + self.ttl

        if key in self._data:
            del self._data[key]

        self._data[key] = (value, expire_at)
        self._data.move_to_end(key)

        while len(self._data) > self.maxsize:
            oldest_key, _ = next(iter(self._data.items()))
            del self._data[oldest_key]
            self._evictions += 1

    def get(self, key: str) -> Optional[Any]:
        self._purge_expired()

        if key in self._data:
            value, exp = self._data[key]
            self._data.move_to_end(key)
            self._hits += 1
            return value
        else:
            self._misses += 1
            return None

    def delete(self, key: str) -> bool:
        self._purge_expired()

        if key in self._data:
            del self._data[key]
            return True
        return False

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