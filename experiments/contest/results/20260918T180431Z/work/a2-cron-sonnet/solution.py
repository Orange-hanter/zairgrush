import re
from datetime import datetime, timedelta


class CronParseError(ValueError):
    pass


def _parse_int(s: str) -> int:
    if not re.fullmatch(r'-?\d+', s):
        raise ValueError(f"Invalid integer: {s!r}")
    return int(s)


def _parse_range(s: str, lo: int, hi: int):
    parts = s.split('-')
    if len(parts) != 2:
        raise ValueError(f"Invalid range: {s!r}")
    a_str, b_str = parts
    a = _parse_int(a_str)
    b = _parse_int(b_str)
    if a > b:
        raise ValueError(f"Invalid range (start > end): {s!r}")
    if a < lo or b > hi:
        raise ValueError(f"Range out of bounds: {s!r}")
    return a, b


def _parse_single(s: str, lo: int, hi: int):
    v = _parse_int(s)
    if v < lo or v > hi:
        raise ValueError(f"Value out of bounds: {s!r}")
    return v, v


def _parse_item(item: str, lo: int, hi: int):
    if item == '':
        raise ValueError("Empty field item")

    if '/' in item:
        parts = item.split('/')
        if len(parts) != 2:
            raise ValueError(f"Invalid item: {item!r}")
        base, step_s = parts
        if not re.fullmatch(r'\d+', step_s):
            raise ValueError(f"Invalid step: {step_s!r}")
        step = int(step_s)
        if step < 1:
            raise ValueError(f"Step must be >= 1: {step_s!r}")

        if base == '*':
            start, end = lo, hi
        elif '-' in base:
            start, end = _parse_range(base, lo, hi)
        else:
            # 'a/n' form is not allowed by spec
            raise ValueError(f"Invalid item with step: {item!r}")
    else:
        step = 1
        if item == '*':
            start, end = lo, hi
        elif '-' in item:
            start, end = _parse_range(item, lo, hi)
        else:
            start, end = _parse_single(item, lo, hi)

    return set(range(start, end + 1, step))


def _parse_field(field_str: str, lo: int, hi: int):
    if field_str == '':
        raise ValueError("Empty field")
    items = field_str.split(',')
    result = set()
    for item in items:
        result |= _parse_item(item, lo, hi)
    if not result:
        raise ValueError("Field resulted in empty set")
    return result


def _parse_cron(expr: str):
    if expr is None:
        raise ValueError("Expression is None")
    stripped = expr.strip()
    if stripped == '':
        raise ValueError("Empty cron expression")
    parts = stripped.split()
    if len(parts) != 5:
        raise ValueError(f"Expected 5 fields, got {len(parts)}")

    minute_set = _parse_field(parts[0], 0, 59)
    hour_set = _parse_field(parts[1], 0, 23)
    dom_set = _parse_field(parts[2], 1, 31)
    month_set = _parse_field(parts[3], 1, 12)
    dow_set = _parse_field(parts[4], 0, 6)

    dom_star = parts[2] == '*'
    dow_star = parts[4] == '*'

    return minute_set, hour_set, dom_set, month_set, dow_set, dom_star, dow_star


def _day_matches(date_obj, dom_set, dow_set, dom_star, dow_star) -> bool:
    dom_ok = date_obj.day in dom_set
    # Python: Monday=0 ... Sunday=6
    # Cron:   Sunday=0 ... Saturday=6
    cron_dow = (date_obj.weekday() + 1) % 7
    dow_ok = cron_dow in dow_set

    if dom_star and dow_star:
        return True
    elif dom_star:
        return dow_ok
    elif dow_star:
        return dom_ok
    else:
        return dom_ok or dow_ok


def next_fire(expr: str, after: datetime) -> datetime:
    minute_set, hour_set, dom_set, month_set, dow_set, dom_star, dow_star = _parse_cron(expr)

    start_minute = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    # 4-year search horizon (generous, includes leap-year margin)
    max_date = after.date() + timedelta(days=365 * 4 + 2)

    sorted_hours = sorted(hour_set)
    sorted_minutes = sorted(minute_set)

    current_date = start_minute.date()
    first_day = True

    while current_date <= max_date:
        if current_date.month in month_set and _day_matches(
            current_date, dom_set, dow_set, dom_star, dow_star
        ):
            for h in sorted_hours:
                for m in sorted_minutes:
                    candidate = datetime(
                        current_date.year, current_date.month, current_date.day, h, m
                    )
                    if first_day and candidate < start_minute:
                        continue
                    return candidate
        first_day = False
        current_date += timedelta(days=1)

    raise ValueError("No matching fire time found within 4 years")
