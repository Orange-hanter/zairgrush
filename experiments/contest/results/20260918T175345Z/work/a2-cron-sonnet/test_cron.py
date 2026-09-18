"""Hidden tests for A2 — cron next_fire."""
from datetime import datetime

import pytest

from solution import next_fire


def test_daily_same_day():
    assert next_fire("0 9 * * *", datetime(2026, 9, 18, 8, 0)) == datetime(2026, 9, 18, 9, 0)


def test_daily_strictly_after():
    assert next_fire("0 9 * * *", datetime(2026, 9, 18, 9, 0)) == datetime(2026, 9, 19, 9, 0)


def test_step_minutes():
    assert next_fire("*/15 * * * *", datetime(2026, 9, 18, 10, 7)) == datetime(2026, 9, 18, 10, 15)


def test_seconds_dropped():
    assert next_fire("* * * * *", datetime(2026, 9, 18, 10, 7, 30)) == datetime(2026, 9, 18, 10, 8)


def test_hour_range_weekdays():
    # 09:00-17:00, пн-пт (dow 1-5). Пятница 18:00 -> понедельник 09:00
    assert next_fire("0 9-17 * * 1-5", datetime(2026, 9, 18, 18, 0)) == datetime(2026, 9, 21, 9, 0)


def test_dow_sunday_is_zero():
    # 2026-09-20 — воскресенье
    assert next_fire("0 12 * * 0", datetime(2026, 9, 18, 0, 0)) == datetime(2026, 9, 20, 12, 0)


def test_dom_monthly():
    assert next_fire("30 4 1 * *", datetime(2026, 9, 18, 0, 0)) == datetime(2026, 10, 1, 4, 30)


def test_yearly():
    assert next_fire("0 0 1 1 *", datetime(2026, 9, 18, 0, 0)) == datetime(2027, 1, 1, 0, 0)


def test_feb29():
    assert next_fire("0 0 29 2 *", datetime(2026, 1, 1, 0, 0)) == datetime(2028, 2, 29, 0, 0)


def test_list_hours():
    assert next_fire("0 9,18 * * *", datetime(2026, 9, 18, 10, 0)) == datetime(2026, 9, 18, 18, 0)


def test_step_range():
    assert next_fire("0 8-20/4 * * *", datetime(2026, 9, 18, 9, 0)) == datetime(2026, 9, 18, 12, 0)


def test_mixed_list():
    assert next_fire("0 1-3,10 * * *", datetime(2026, 9, 18, 3, 30)) == datetime(2026, 9, 18, 10, 0)


def test_dom_dow_or_rule():
    # dom=15 ИЛИ понедельник. 2026-09-07 — понедельник
    assert next_fire("0 0 15 * 1", datetime(2026, 9, 1, 0, 0)) == datetime(2026, 9, 7, 0, 0)


def test_dom_dow_or_rule_picks_monday_before_15th():
    # 2026-09-14 — понедельник, он ближе 15-го
    assert next_fire("0 0 15 * 1", datetime(2026, 9, 8, 0, 0)) == datetime(2026, 9, 14, 0, 0)


def test_dow_only_and_semantics():
    # dow ограничен, dom=* — только понедельники (AND с «любым dom»)
    assert next_fire("0 0 * * 1", datetime(2026, 9, 1, 0, 0)) == datetime(2026, 9, 7, 0, 0)


def test_minute_list_with_step():
    assert next_fire("*/20,45 * * * *", datetime(2026, 9, 18, 10, 21)) == datetime(2026, 9, 18, 10, 40)


def test_month_rollover_minute_precision():
    assert next_fire("5 0 1 * *", datetime(2026, 9, 30, 23, 59)) == datetime(2026, 10, 1, 0, 5)


def test_extra_whitespace_ok():
    assert next_fire("  0   9  *  *  * ", datetime(2026, 9, 18, 8, 0)) == datetime(2026, 9, 18, 9, 0)


def test_invalid_step_zero():
    with pytest.raises(ValueError):
        next_fire("*/0 * * * *", datetime(2026, 1, 1))


def test_invalid_minute_range():
    with pytest.raises(ValueError):
        next_fire("61 * * * *", datetime(2026, 1, 1))


def test_invalid_dow_7():
    with pytest.raises(ValueError):
        next_fire("0 9 * * 7", datetime(2026, 1, 1))


def test_invalid_field_count():
    with pytest.raises(ValueError):
        next_fire("0 9 * *", datetime(2026, 1, 1))


def test_invalid_garbage():
    with pytest.raises(ValueError):
        next_fire("a b c d e", datetime(2026, 1, 1))


def test_impossible_feb31():
    with pytest.raises(ValueError):
        next_fire("0 0 31 2 *", datetime(2026, 1, 1, 0, 0))
