import time
from typing import Callable


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    def __init__(self, failure_threshold: int, recovery_timeout: float, clock: Callable = time.monotonic):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_timeout <= 0:
            raise ValueError("recovery_timeout must be > 0")
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._clock = clock
        self._failures = 0
        self._opened_at = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        elapsed = self._clock() - self._opened_at
        if elapsed < self.recovery_timeout:
            return "open"
        return "half_open"

    def on_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def on_failure(self) -> None:
        if self._opened_at is not None and self.state == "half_open":
            self._opened_at = self._clock()
            self._failures += 1
            return
        self._failures += 1
        if self._failures >= self.failure_threshold and self._opened_at is None:
            self._opened_at = self._clock()


def with_resilience(fn, *, max_attempts=3, base_delay=0.1, multiplier=2.0,
                    retry_on=(Exception,), breaker=None, sleep=time.sleep):
    if breaker is not None:
        state = breaker.state
        if state == "open":
            raise CircuitOpen()
        if state == "half_open":
            try:
                result = fn()
            except Exception as exc:
                if isinstance(exc, retry_on):
                    breaker.on_failure()
                raise
            breaker.on_success()
            return result

    last_exc = None
    for attempt in range(max_attempts):
        try:
            result = fn()
        except Exception as exc:
            last_exc = exc
            if not isinstance(exc, retry_on):
                raise
            if attempt == max_attempts - 1:
                break
            sleep(base_delay * multiplier ** attempt)
        else:
            if breaker is not None:
                breaker.on_success()
            return result

    if breaker is not None:
        breaker.on_failure()
    raise last_exc
