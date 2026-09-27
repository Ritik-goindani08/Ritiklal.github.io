"""Date/time helpers.

Storage rules: event timestamps are UTC ISO-8601 with a trailing 'Z'; business
dates are 'YYYY-MM-DD' in Brisbane local time (fixed UTC+10, no DST).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc
_ISO_Z = "%Y-%m-%dT%H:%M:%SZ"

# Australian day-first formats seen in GCDC sheets: 22/9/26, 18/08/26, 22/9/2026
_DMY = re.compile(r"^\s*(?:[A-Za-z]+\s+)?(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\s*$")
_MON_DAY = re.compile(r"^\s*(?:[A-Za-z]{3,9}\s+)?(\d{1,2})\s+([A-Za-z]{3,9})\s*(\d{4})?\s*$")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def local_tz(offset_hours: float = 10) -> timezone:
    return timezone(timedelta(hours=offset_hours))


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_ISO_Z)


def now_iso() -> str:
    return to_iso_utc(now_utc())


def local_today(offset_hours: float = 10, at: datetime | None = None) -> date:
    return (at or now_utc()).astimezone(local_tz(offset_hours)).date()


def parse_date(value, *, default_year: int | None = None) -> date | None:
    """Parse the date formats found in GCDC data. Returns None when blank.

    Raises ValueError for non-blank values that cannot be understood, so bad
    data is reported rather than silently dropped.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    m = _DMY.match(text)
    if m:
        d, mth, y = (int(g) for g in m.groups())
        if y < 100:
            y += 2000
        return date(y, mth, d)
    m = _MON_DAY.match(text)
    if m and m.group(2)[:3].lower() in _MONTHS:
        year = int(m.group(3)) if m.group(3) else default_year
        if year is None:
            raise ValueError(f"date has no year: {value!r}")
        return date(year, _MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
    raise ValueError(f"unrecognised date: {value!r}")


def parse_datetime_utc(value, *, offset_hours: float = 10) -> str | None:
    """Parse a timestamp to UTC ISO. Naive values are treated as Brisbane local."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            d = parse_date(text)
            dt = datetime(d.year, d.month, d.day, 9, 0)  # date only: 9am local
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz(offset_hours))
    return to_iso_utc(dt)


def local_date_of(ts_utc: str | None, offset_hours: float = 10) -> str | None:
    if not ts_utc:
        return None
    dt = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
    return dt.astimezone(local_tz(offset_hours)).date().isoformat()


def is_business_day(d: date, holidays: frozenset[str]) -> bool:
    return d.weekday() < 5 and d.isoformat() not in holidays
