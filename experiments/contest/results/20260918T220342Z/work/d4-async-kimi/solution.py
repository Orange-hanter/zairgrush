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


async def _await_callback(fetch, *args):
    fut = asyncio.get_running_loop().create_future()

    def cb(err, res):
        if err is not None:
            fut.set_exception(RuntimeError(err))
        else:
            fut.set_result(res)

    fetch(*args, cb)
    return await fut


async def get_user(uid) -> dict:
    return await _await_callback(fetch_user, uid)


async def get_orders(uid) -> dict:
    return await _await_callback(fetch_orders, uid)


async def _async_save_report(report):
    return await _await_callback(save_report, report)


async def build_report(uid) -> dict:
    user, orders = await asyncio.gather(
        asyncio.shield(get_user(uid)),
        asyncio.shield(get_orders(uid)),
    )
    report = {"uid": uid, "name": user["name"], "orders": orders["orders"]}
    await asyncio.sleep(0)
    await _async_save_report(report)
    return report


async def build_report_timed(uid, timeout: float) -> dict:
    try:
        return await asyncio.wait_for(
            asyncio.shield(build_report(uid)), timeout)
    except asyncio.TimeoutError:
        raise TimeoutError
