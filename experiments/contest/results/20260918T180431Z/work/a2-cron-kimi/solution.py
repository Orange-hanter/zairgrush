from datetime import datetime, timedelta
import calendar

MINUTE_RANGE = (0, 59)
HOUR_RANGE = (0, 23)
DOM_RANGE = (1, 31)
MONTH_RANGE = (1, 12)
DOW_RANGE = (0, 6)

RANGES = [MINUTE_RANGE, HOUR_RANGE, DOM_RANGE, MONTH_RANGE, DOW_RANGE]


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    if not field:
        raise ValueError("empty field")
    values = set()
    parts = field.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            raise ValueError("empty subfield")
        if "/" in part:
            base, step_str = part.split("/", 1)
            if not step_str or not step_str.isdigit():
                raise ValueError("invalid step")
            step = int(step_str)
            if step <= 0:
                raise ValueError("step must be >= 1")
        else:
            base = part
            step = 1

        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a_str, b_str = base.split("-", 1)
            if not a_str or not a_str.isdigit() or not b_str or not b_str.isdigit():
                raise ValueError("invalid range")
            start = int(a_str)
            end = int(b_str)
        else:
            if not base.isdigit():
                raise ValueError("invalid number")
            start = end = int(base)

        if start < lo or end > hi or start > end:
            raise ValueError("range out of bounds")

        for v in range(start, end + 1, step):
            values.add(v)

    if not values:
        raise ValueError("no values")
    return values


def _parse(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    expr = expr.strip()
    if not expr:
        raise ValueError("empty expression")
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("must have 5 fields")
    parsed = []
    for i, field in enumerate(fields):
        lo, hi = RANGES[i]
        parsed.append(_parse_field(field, lo, hi))
    return tuple(parsed)


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def next_fire(expr: str, after: datetime) -> datetime:
    minutes, hours, doms, months, dows = _parse(expr)

    # Determine if dom and dow are restricted (not '*')
    # We need to know whether the original field was a wildcard.
    # _parse loses that info, so re-parse to detect.
    expr_stripped = expr.strip()
    fields = expr_stripped.split()
    dom_restricted = fields[2].strip() != "*"
    dow_restricted = fields[4].strip() != "*"

    start = after.replace(second=0, microsecond=0)
    # We need strictly after, so start searching from start + 1 minute
    # because seconds/microseconds are discarded for comparison.
    current = start + timedelta(minutes=1)

    # Limit search to 4 years from after (exclusive)
    limit = after + timedelta(days=4 * 366)

    while current < limit:
        if current.month not in months:
            # Move to first day of next month in allowed months
            current = _next_month_start(current, months)
            continue
        if current.hour not in hours:
            current = current.replace(minute=0) + timedelta(hours=1)
            continue
        if current.minute not in minutes:
            current += timedelta(minutes=1)
            continue

        # Check day condition
        dom_ok = current.day in doms
        dow_ok = current.weekday() in dows  # Monday=0 in Python; cron Sunday=0
        # Convert Python weekday to cron dow
        cron_dow = (current.weekday() + 1) % 7
        dow_ok = cron_dow in dows

        day_match = False
        if dom_restricted and dow_restricted:
            day_match = dom_ok or dow_ok
        else:
            day_match = dom_ok and dow_ok

        if not day_match:
            current += timedelta(days=1)
            current = current.replace(hour=0, minute=0)
            continue

        return current.replace(second=0, microsecond=0)

    raise ValueError("no matching time in next 4 years")


def _next_month_start(current: datetime, months: set[int]) -> datetime:
    year = current.year
    month = current.month + 1
    if month > 12:
        month = 1
        year += 1
    # If next month not allowed, keep advancing years until we find one
    # But months set is within 1-12, so we can find next allowed month
    # in the same year or next.
    allowed = sorted(months)
    # Find first allowed month >= current month (but we want strictly next month)
    # Simpler: advance month by month until allowed.
    while month not in months:
        month += 1
        if month > 12:
            month = 1
            year += 1
    return datetime(year, month, 1, 0, 0, 0)
