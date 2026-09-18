import time
from typing import Callable


class CircuitOpen(Exception):
    """Raised when the circuit breaker is open and blocks the call."""
    pass


class CircuitBreaker:
    def __init__(self, failure_threshold: int, recovery_timeout: float,
                 clock: Callable = time.monotonic):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_timeout <= 0:
            raise ValueError("recovery_timeout must be > 0")

        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.clock = clock

        self._state = "closed"   # internal: "closed" or "open"
        self._fail_count = 0
        self._opened_at = None

    @property
    def state(self) -> str:
        if self._state == "open":
            if self._opened_at is not None and (self.clock() - self._opened_at) < self.recovery_timeout:
                return "open"
            else:
                return "half_open"
        return "closed"

    def on_success(self) -> None:
        self._fail_count = 0
        self._state = "closed"
        self._opened_at = None

    def on_failure(self) -> None:
        current = self.state
        if current == "half_open":
            # trial attempt failed -> reopen, refresh the opened moment
            self._state = "open"
            self._opened_at = self.clock()
            self._fail_count = self.failure_threshold
            return

        if current == "open":
            # already open (shouldn't normally get failures recorded here,
            # but keep counting just in case)
            self._fail_count += 1
            return

        # closed
        self._fail_count += 1
        if self._fail_count >= self.failure_threshold:
            self._state = "open"
            self._opened_at = self.clock()


def with_resilience(fn, *, max_attempts=3, base_delay=0.1, multiplier=2.0,
                     retry_on=(Exception,), breaker=None, sleep=time.sleep):
    if breaker is not None:
        if breaker.state == "open":
            raise CircuitOpen()

    last_exc = None

    for attempt in range(max_attempts):
        try:
            result = fn()
        except retry_on as e:
            last_exc = e
            if attempt == max_attempts - 1:
                if breaker is not None:
                    breaker.on_failure()
                raise
            else:
                sleep(base_delay * (multiplier ** attempt))
                continue
        except Exception:
            # Not covered by retry_on: propagate immediately, no retry,
            # no breaker interaction.
            raise
        else:
            if breaker is not None:
                breaker.on_success()
            return result

    # Should not be reachable, but just in case.
    if last_exc is not None:
        raise last_exc
