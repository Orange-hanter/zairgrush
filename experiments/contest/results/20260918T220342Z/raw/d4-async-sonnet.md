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


# --- helpers ---------------------------------------------------------------

# Держим ссылки на "фоновые" задачи, чтобы их не собрал GC до завершения,
# даже если никто больше на них не await-ит (например, после отмены/таймаута).
_background_tasks = set()


def _spawn(coro):
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)
    task.add_done_callback(lambda t: _background_tasks.discard(t))
    return task


async def _call_cb(fn, *args):
    loop = asyncio.get_event_loop()
    fut = loop.create_future()

    def cb(err, result):
        if fut.done():
            return
        if err is not None:
            fut.set_exception(RuntimeError(str(err)))
        else:
            fut.set_result(result)

    fn(*args, cb)
    return await fut


# --- async обёртки над callback-API -----------------------------------------

async def get_user(uid) -> dict:
    return await _call_cb(fetch_user, uid)


async def get_orders(uid) -> dict:
    return await _call_cb(fetch_orders, uid)


async def _save(report):
    return await _call_cb(save_report, report)


# --- оркестрация ------------------------------------------------------------

async def build_report(uid) -> dict:
    # user и orders — конкурентно
    user, orders = await asyncio.gather(get_user(uid), get_orders(uid))
    report = {"uid": uid, "name": user["name"], "orders": orders["orders"]}

    # save запускаем как отдельную задачу и защищаем shield-ом:
    # если наш coroutine отменят прямо во время save — сама задача save
    # доведётся до конца (появится запись в SAVED), а CancelledError
    # всё равно долетит до вызывающего в точке await ниже.
    save_task = _spawn(_save(report))
    await asyncio.shield(save_task)

    return report


async def build_report_timed(uid, timeout: float) -> dict:
    # build_report выполняется как отдельная задача и оборачивается shield-ом,
    # чтобы истечение таймаута не отменяло саму работу — она продолжится
    # в фоне и рано или поздно сохранит отчёт в SAVED.
    task = _spawn(build_report(uid))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"build_report_timed({uid}) exceeded timeout={timeout}s"
        )
```