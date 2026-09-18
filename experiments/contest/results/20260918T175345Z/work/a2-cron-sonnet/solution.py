from datetime import datetime, timedelta
from bisect import bisect_left


def _parse_int(s: str) -> int:
    if not s or not s.isdigit():
        raise ValueError(f"Invalid integer literal: {s!r}")
    return int(s)


def _parse_field(field: str, lo: int, hi: int) -> set:
    if field == "":
        raise ValueError("Empty cron field")

    result = set()

    for part in field.split(","):
        if part == "":
            raise ValueError("Empty item in cron field list")

        if part == "*":
            result.update(range(lo, hi + 1))
            continue

        if "/" in part:
            base, step_s = part.split("/", 1)
            step = _parse_int(step_s)
            if step < 1:
                raise ValueError("Step must be >= 1")

            if base == "*":
                start, end = lo, hi
            elif "-" in base:
                a_s, b_s = base.split("-", 1)
                a = _parse_int(a_s)
                b = _parse_int(b_s)
                if a > b:
                    raise ValueError("Invalid range: start > end")
                start, end = a, b
            else:
                a = _parse_int(base)
                start, end = a, hi

            if start < lo or end > hi or start > hi or end < lo:
                raise ValueError("Range out of bounds")

            for v in range(start, end + 1, step):
                result.add(v)

        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            a = _parse_int(a_s)
            b = _parse_int(b_s)
            if a > b:
                raise ValueError("Invalid range: start > end")
            if a < lo or b > hi:
                raise ValueError("Range out of bounds")
            for v in range(a, b + 1):
                result.add(v)

        else:
            a = _parse_int(part)
            if a < lo or a > hi:
                raise ValueError("Value out of bounds")
            result.add(a)

    if not result:
        raise ValueError("Empty resulting set for cron field")

    return result


def _parse_cron(expr: str):
    if not isinstance(expr, str):
        raise ValueError("Cron expression must be a string")

    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("Cron expression must have exactly 5 fields")

    minute_f, hour_f, dom_f, month_f, dow_f = fields

    minute_set = _parse_field(minute_f, 0, 59)
    hour_set = _parse_field(hour_f, 0, 23)
    dom_set = _parse_field(dom_f, 1, 31)
    month_set = _parse_field(month_f, 1, 12)
    dow_set = _parse_field(dow_f, 0, 6)

    dom_restricted = dom_f != "*"
    dow_restricted = dow_f != "*"

    return minute_set, hour_set, dom_set, month_set, dow_set, dom_restricted, dow_restricted


def _date_matches(date_obj, dom_set, dow_set, dom_restricted, dow_restricted):
    dom_ok = date_obj.day in dom_set
    dow_val = (date_obj.weekday() + 1) % 7  # cron: 0=Sunday..6=Saturday
    dow_ok = dow_val in dow_set

    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    elif dom_restricted:
        return dom_ok
    elif dow_restricted:
        return dow_ok
    else:
        return True


def _find_earliest_time(hours_sorted, minutes_sorted, threshold_h, threshold_m):
    for h in hours_sorted:
        if h < threshold_h:
            continue
        if h == threshold_h:
            pos = bisect_left(minutes_sorted, threshold_m)
            if pos < len(minutes_sorted):
                return h, minutes_sorted[pos]
            else:
                continue
        else:
            return h, minutes_sorted[0]
    return None


def next_fire(expr: str, after: datetime) -> datetime:
    (minute_set, hour_set, dom_set, month_set, dow_set,
     dom_restricted, dow_restricted) = _parse_cron(expr)

    hours_sorted = sorted(hour_set)
    minutes_sorted = sorted(minute_set)

    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    start_date = start.date()

    cutoff_date = start_date + timedelta(days=4 * 366)

    cur_date = start_date
    while cur_date <= cutoff_date:
        if cur_date.month in month_set and _date_matches(
                cur_date, dom_set, dow_set, dom_restricted, dow_restricted):
            if cur_date == start_date:
                threshold_h, threshold_m = start.hour, start.minute
            else:
                threshold_h, threshold_m = 0, 0

            result = _find_earliest_time(hours_sorted, minutes_sorted, threshold_h, threshold_m)
            if result is not None:
                h, m = result
                return datetime(cur_date.year, cur_date.month, cur_date.day, h, m)

        cur_date += timedelta(days=1)

    raise ValueError("No matching fire time found within 4 years")
