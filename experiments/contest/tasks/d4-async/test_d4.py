"""Hidden tests for D4 — callbacks → asyncio с семантикой отмены."""
import asyncio
import time

import pytest

from solution import (DELAY, SAVED, build_report, build_report_timed,
                      get_orders, get_user)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_saved():
    SAVED.clear()
    yield
    SAVED.clear()


def test_get_user():
    assert run(get_user(7)) == {"uid": 7, "name": "user7"}


def test_get_orders():
    assert run(get_orders(3)) == {"uid": 3, "orders": [30, 31]}


def test_build_report_shape():
    r = run(build_report(5))
    assert r == {"uid": 5, "name": "user5", "orders": [50, 51]}
    assert r in SAVED


def test_build_report_concurrent():
    t0 = time.monotonic()
    run(build_report(1))
    dur = time.monotonic() - t0
    # последовательно было бы 3*DELAY; конкурентно — 2*DELAY + запас
    assert dur < 2.8 * DELAY


def test_cancel_before_save():
    async def main():
        task = asyncio.ensure_future(build_report(2))
        await asyncio.sleep(DELAY * 0.2)  # ещё фетчим, save не начат
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(DELAY * 3)
    run(main())
    assert SAVED == []


def test_cancel_during_save_still_saves():
    async def main():
        task = asyncio.ensure_future(build_report(4))
        await asyncio.sleep(DELAY * 1.5)  # фетчи кончились, save идёт
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(DELAY * 3)
    run(main())
    assert any(r.get("uid") == 4 for r in SAVED)


def test_timed_success():
    r = run(build_report_timed(6, 5.0))
    assert r["uid"] == 6


def test_timed_timeout_raises_but_saves():
    async def main():
        with pytest.raises(TimeoutError):
            await build_report_timed(8, DELAY * 0.5)
        await asyncio.sleep(DELAY * 5)
    run(main())
    assert any(r.get("uid") == 8 for r in SAVED)
