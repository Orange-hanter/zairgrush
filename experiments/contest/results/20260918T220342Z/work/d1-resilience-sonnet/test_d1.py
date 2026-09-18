"""Hidden tests for D1 — retry + circuit breaker."""
import pytest

from solution import CircuitBreaker, CircuitOpen, with_resilience


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeSleep:
    def __init__(self):
        self.delays = []

    def __call__(self, d):
        self.delays.append(d)


class Flaky:
    """Фейлится first_fails раз, потом отдаёт 'ok'."""
    def __init__(self, first_fails, exc=RuntimeError):
        self.left = first_fails
        self.exc = exc
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.left > 0:
            self.left -= 1
            raise self.exc("boom")
        return "ok"


def test_success_first_try():
    s = FakeSleep()
    assert with_resilience(lambda: "ok", sleep=s) == "ok"
    assert s.delays == []


def test_retry_then_success():
    f, s = Flaky(2), FakeSleep()
    assert with_resilience(f, max_attempts=3, base_delay=0.1, multiplier=2.0, sleep=s) == "ok"
    assert f.calls == 3
    assert s.delays == [0.1, 0.2]


def test_attempts_exhausted_reraises_last():
    f, s = Flaky(99), FakeSleep()
    with pytest.raises(RuntimeError):
        with_resilience(f, max_attempts=4, base_delay=0.5, sleep=s)
    assert f.calls == 4
    assert len(s.delays) == 3


def test_non_retryable_propagates_immediately():
    s = FakeSleep()
    def f():
        raise KeyError("nope")
    with pytest.raises(KeyError):
        with_resilience(f, retry_on=(ValueError,), sleep=s)
    assert s.delays == []


def test_non_retryable_not_recorded_on_breaker():
    br = CircuitBreaker(1, 10)
    def f():
        raise KeyError()
    with pytest.raises(KeyError):
        with_resilience(f, retry_on=(ValueError,), breaker=br, sleep=FakeSleep())
    assert br.state == "closed"


def test_max_attempts_one_no_sleep():
    f, s = Flaky(1), FakeSleep()
    with pytest.raises(RuntimeError):
        with_resilience(f, max_attempts=1, sleep=s)
    assert s.delays == []


def test_breaker_opens_at_threshold():
    clock = Clock()
    br = CircuitBreaker(2, 30, clock=clock)
    br.on_failure()
    assert br.state == "closed"
    br.on_failure()
    assert br.state == "open"


def test_success_resets_counter():
    br = CircuitBreaker(2, 30)
    br.on_failure()
    br.on_success()
    br.on_failure()
    assert br.state == "closed"


def test_open_breaker_blocks_call():
    clock = Clock()
    br = CircuitBreaker(1, 30, clock=clock)
    br.on_failure()
    f = Flaky(0)
    with pytest.raises(CircuitOpen):
        with_resilience(f, breaker=br, sleep=FakeSleep())
    assert f.calls == 0


def test_half_open_after_timeout_then_close():
    clock = Clock()
    br = CircuitBreaker(1, 30, clock=clock)
    br.on_failure()
    clock.advance(31)
    assert br.state == "half_open"
    assert with_resilience(lambda: "ok", breaker=br, sleep=FakeSleep()) == "ok"
    assert br.state == "closed"


def test_half_open_failure_reopens_and_refreshes():
    clock = Clock()
    br = CircuitBreaker(1, 30, clock=clock)
    br.on_failure()
    clock.advance(31)
    with pytest.raises(RuntimeError):
        with_resilience(Flaky(1), max_attempts=1, breaker=br, sleep=FakeSleep())
    assert br.state == "open"
    clock.advance(15)  # от нового момента открытия прошло 15 < 30
    assert br.state == "open"
    with pytest.raises(CircuitOpen):
        with_resilience(lambda: 1, breaker=br, sleep=FakeSleep())


def test_breaker_counts_failures_via_with_resilience():
    clock = Clock()
    br = CircuitBreaker(2, 100, clock=clock)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            with_resilience(Flaky(1), max_attempts=1, breaker=br, sleep=FakeSleep())
    assert br.state == "open"
    with pytest.raises(CircuitOpen):
        with_resilience(lambda: 1, breaker=br, sleep=FakeSleep())


def test_retry_on_tuple():
    f, s = Flaky(1, exc=ValueError), FakeSleep()
    assert with_resilience(f, retry_on=(ValueError, KeyError), sleep=s) == "ok"
    assert len(s.delays) == 1


def test_invalid_breaker_args():
    with pytest.raises(ValueError):
        CircuitBreaker(0, 10)
    with pytest.raises(ValueError):
        CircuitBreaker(1, 0)


def test_default_sleep_and_breakerless():
    assert with_resilience(lambda: 42, max_attempts=2) == 42
