# D4 — Callback-стиль → asyncio

Дан callback-style API (скопируй в `solution.py` БЕЗ ИЗМЕНЕНИЙ):

```python
import asyncio

DELAY = 0.05
SAVED = []

def fetch_user(uid, cb):
    asyncio.get_event_loop().call_later(
        DELAY, lambda: cb(None, {"uid": uid, "name": f"user{uid}"}))

def fetch_orders(uid, cb):
    asyncio.get_event_loop().call_later(
        DELAY, lambda: cb(None, {"uid": uid, "orders": [uid * 10, uid * 10 + 1]}))

def save_report(report, cb):
    def done():
        SAVED.append(report)
        cb(None, "ok")
    asyncio.get_event_loop().call_later(DELAY, done)
```

## Задача

Реализуй async-обёртки и оркестрацию:

```python
async def get_user(uid) -> dict: ...
async def get_orders(uid) -> dict: ...
async def build_report(uid) -> dict: ...
async def build_report_timed(uid, timeout: float) -> dict: ...
```

- `get_user`/`get_orders` — await-версии callback-функций (err всегда None, но контракт сохрани: err не None → RuntimeError).
- `build_report`: user и orders запрашиваются КОНКУРЕНТНО, затем результат
  `{"uid": uid, "name": ..., "orders": [...]}` сохраняется через `save_report`,
  возвращается report.
- Отмена (asyncio cancellation) ДО начала save → save не стартует (SAVED не растёт).
- Отмена ВО ВРЕМЯ save → save обязан завершиться (report появляется в SAVED), при этом `CancelledError` всё равно доходит до вызывающего.
- `build_report_timed`: если не уложился в `timeout` секунд → `TimeoutError` у вызывающего, но внутренняя работа при этом НЕ отменяется (report всё равно сохраняется в SAVED позже).

## Ограничения

- Только stdlib asyncio. Callback-функции не менять, оборачивать как есть.
- DELAY/SAVED должны остаться module-level с этими именами — тесты их читают.

## Формат ответа

Ровно один блок ```python с полным содержимым `solution.py`. Без пояснений.
