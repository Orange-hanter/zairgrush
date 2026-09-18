```python
from datetime import datetime, timedelta, date
import calendar


def _parse_field(token: str, lo: int, hi: int) -> list:
    """Parse one cron field and return a sorted list of unique allowed values."""
    if not token:
        raise ValueError

    values = set()

    for part in token.split(","):
        if not part:
            raise ValueError

        if "/" in part:
            range_part, step_part = part.split("/", 1)
            try:
                step = int(step_part)
            except ValueError:
                raise ValueError
            if step < 1:
                raise ValueError

            if range_part == "*":
                start, end = lo, hi
            elif "-" in range_part:
                a, b = range_part.split("-", 1)
                start = int(a)
                end = int(b)
            else:
                raise ValueError
        elif "-" in part:
            a, b = part.split("-", 1)
            start = int(a)
            end = int(b)
            step = 1
        else:
            v = int(part)
            if v < lo or v > hi:
                raise ValueError
            values.add(v)
            continue

        if start < lo or end > hi or start > end:
            raise ValueError

        for v in range(start, end + 1, step):
            values.add(v)

    return sorted(values)


def next_fire(expr: str, after: datetime) -> datetime:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError

    minute_vals = _parse_field(fields[0], 0, 59)
    hour_vals = _parse_field(fields[1], 0, 23)
    dom_set = set(_parse_field(fields[2], 1, 31))
    month_set = set(_parse_field(fields[3], 1, 12))
    dow_set = set(_parse_field(fields[4], 0, 6))

    dom_restricted = fields[2] != "*"
    dow_restricted = fields[4] != "*"

    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    first_day = start.date()
    start_h = start.hour
    start_m = start.minute

    # 4-year limit from `after`, keeping the same month/day (clamp Feb 29)
    limit_year = after.year + 4
    limit_month = after.month
    limit_day = after.day
    max_day = calendar.monthrange(limit_year, limit_month)[1]
    if limit_day > max_day:
        limit_day = max_day
    limit_date = date(limit_year, limit_month, limit_day)

    def day_matches(d: date) -> bool:
        dom_ok = d.day in dom_set
        cron_dow = (d.weekday() + 1) % 7
        dow_ok = cron_dow in dow_set
        if dom_restricted and dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    cur_day = first_day
    one_day = timedelta(days=1)

    while cur_day <= limit_date:
        if cur_day.month not in month_set or not day_matches(cur_day):
            cur_day += one_day
            continue

        sh = start_h if cur_day == first_day else 0
        sm = start_m if cur_day == first_day else 0

        for h in hour_vals:
            if h < sh:
                continue
            if h > sh:
                return datetime(cur_day.year, cur_day.month, cur_day.day,
                                h, minute_vals[0], 0, 0)
            for m in minute_vals:
                if m >= sm:
                    return datetime(cur_day.year, cur_day.month, cur_day.day,
                                    h, m, 0, 0)

        cur_day += one_day

    raise ValueError
```