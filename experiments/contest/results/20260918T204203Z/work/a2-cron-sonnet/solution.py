from datetime import datetime, timedelta, time, date


def _is_digits(s: str) -> bool:
    return len(s) > 0 and s.isdigit()


def _parse_range_base(item: str, lo: int, hi: int):
    parts = item.split('-')
    if len(parts) != 2:
        raise ValueError(f"Invalid range: {item}")
    a_str, b_str = parts
    if not (_is_digits(a_str) and _is_digits(b_str)):
        raise ValueError(f"Invalid range: {item}")
    a, b = int(a_str), int(b_str)
    if a > b:
        raise ValueError(f"Invalid range (start > end): {item}")
    if a < lo or b > hi:
        raise ValueError(f"Range out of bounds: {item}")
    return a, b


def _parse_item(item: str, lo: int, hi: int):
    if item == '':
        raise ValueError("Empty field item")

    if item == '*':
        return set(range(lo, hi + 1))

    if '/' in item:
        base, step_str = item.split('/', 1)
        if not _is_digits(step_str):
            raise ValueError(f"Invalid step: {step_str}")
        step = int(step_str)
        if step < 1:
            raise ValueError("Step must be >= 1")

        if base == '*':
            start, end = lo, hi
        elif '-' in base:
            start, end = _parse_range_base(base, lo, hi)
        else:
            if not _is_digits(base):
                raise ValueError(f"Invalid base: {base}")
            start = int(base)
            if start < lo or start > hi:
                raise ValueError(f"Value out of bounds: {base}")
            end = hi
        return set(range(start, end + 1, step))

    elif '-' in item:
        start, end = _parse_range_base(item, lo, hi)
        return set(range(start, end + 1))

    else:
        if not _is_digits(item):
            raise ValueError(f"Invalid value: {item}")
        val = int(item)
        if val < lo or val > hi:
            raise ValueError(f"Value out of bounds: {item}")
        return {val}


def _parse_field(field_str: str, lo: int, hi: int):
    if field_str == '':
        raise ValueError("Empty field")
    items = field_str.split(',')
    result = set()
    for item in items:
        result |= _parse_item(item, lo, hi)
    return result


def _parse_cron(expr: str):
    if not isinstance(expr, str):
        raise ValueError("Expression must be a string")
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("Cron expression must have exactly 5 fields")

    minute_f, hour_f, dom_f, month_f, dow_f = parts

    minutes = _parse_field(minute_f, 0, 59)
    hours = _parse_field(hour_f, 0, 23)
    doms = _parse_field(dom_f, 1, 31)
    months = _parse_field(month_f, 1, 12)
    dows = _parse_field(dow_f, 0, 6)

    dom_is_star = (dom_f == '*')
    dow_is_star = (dow_f == '*')

    return minutes, hours, doms, months, dows, dom_is_star, dow_is_star


def _day_matches(d: date, doms, dows, dom_star: bool, dow_star: bool) -> bool:
    dom = d.day
    # Python weekday(): Monday=0 ... Sunday=6
    # cron dow: Sunday=0, Monday=1, ..., Saturday=6
    cron_dow = (d.weekday() + 1) % 7

    if dom_star and dow_star:
        return True
    if dom_star:
        return cron_dow in dows
    if dow_star:
        return dom in doms
    return (dom in doms) or (cron_dow in dows)


def next_fire(expr: str, after: datetime) -> datetime:
    minutes, hours, doms, months, dows, dom_star, dow_star = _parse_cron(expr)

    sorted_minutes = sorted(minutes)
    sorted_hours = sorted(hours)

    start_dt = after.replace(second=0, microsecond=0)
    candidate = start_dt + timedelta(minutes=1)

    current_day = candidate.date()
    max_days = 4 * 366 + 10  # safety buffer covering 4 years incl. leap years
    limit_day = after.date() + timedelta(days=max_days)

    day = current_day
    is_first_day = True

    while day <= limit_day:
        if day.month in months and _day_matches(day, doms, dows, dom_star, dow_star):
            if is_first_day and day == current_day:
                found = None
                for h in sorted_hours:
                    if h < candidate.hour:
                        continue
                    if h == candidate.hour:
                        for m in sorted_minutes:
                            if m >= candidate.minute:
                                found = time(h, m)
                                break
                        if found is not None:
                            break
                        else:
                            continue
                    else:
                        found = time(h, sorted_minutes[0])
                        break
                if found is not None:
                    return datetime.combine(day, found)
            else:
                return datetime.combine(day, time(sorted_hours[0], sorted_minutes[0]))

        is_first_day = False
        day = day + timedelta(days=1)

    raise ValueError("No matching fire time found within 4 years")
