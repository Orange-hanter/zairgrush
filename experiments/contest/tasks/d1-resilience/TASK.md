# D1 — Retry + Circuit Breaker

Напиши модуль `solution.py`, реализующий два паттерна устойчивости: retry с экспоненциальным backoff и circuit breaker.

## API

```python
class CircuitOpen(Exception): ...

class CircuitBreaker:
    def __init__(self, failure_threshold: int, recovery_timeout: float, clock: Callable = time.monotonic): ...
    @property
    def state(self) -> str: ...   # "closed" | "open" | "half_open"
    def on_success(self) -> None: ...
    def on_failure(self) -> None: ...

def with_resilience(fn, *, max_attempts=3, base_delay=0.1, multiplier=2.0,
                    retry_on=(Exception,), breaker=None, sleep=time.sleep): ...
```

## Семантика

**Retry:**
- `fn()` вызывается не более `max_attempts` раз суммарно. Между попытками `sleep(base_delay * multiplier**k)`, где k=0 для первого retry.
- Исключение, не подходящее под `retry_on`, пробрасывается немедленно: без retry, без sleep, НЕ записывается в breaker.
- Если попытки исчерпаны — пробрасывается ПОСЛЕДНЕЕ исключение; это записывается в breaker как failure (один раз).
- Успех → `breaker.on_success()` (если breaker передан) и возврат значения.

**Circuit breaker:**
- `failure_threshold >= 1`, `recovery_timeout > 0`, иначе `ValueError`.
- `on_failure`: счётчик подряд-фейлов +1; при достижении threshold → open (запоминается момент открытия).
- `on_success`: счётчик сбрасывается, состояние closed.
- `state`: `"open"`, пока с момента открытия прошло меньше `recovery_timeout`; после — `"half_open"`; иначе `"closed"`.
- `with_resilience` с breaker'ом в состоянии open (таймаут не вышел) → `CircuitOpen` немедленно, `fn` не вызывается, sleep не вызывается.
- В half_open допускается одна пробная попытка: успех → closed; фейл → снова open, момент открытия обновляется.
- Время — только из параметра `clock`; задержки — только через параметр `sleep`. Прямые вызовы `time.*` внутри запрещены.

## Формат ответа

Ровно один блок ```python с полным содержимым `solution.py`. Без пояснений.
