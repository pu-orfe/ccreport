"""Month arithmetic, in one place.

Every connector and the browse view need to agree on where a month starts and
how far back the picker may reach. Disagreeing by a timezone offset means a
receipt dated the 1st silently belongs to the previous month, which is precisely
the class of quiet error that makes faculty distrust the tool.

Months are computed in UTC. A receipt at 23:30 on the 31st in local time is a
receipt in the month the provider says it is, and the provider says UTC.
"""

from __future__ import annotations

import calendar
import datetime as _dt
import re

from .errors import CCReportError

_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")


class InvalidPeriod(CCReportError, ValueError):
    """The period string is malformed or outside the permitted window.

    Both bases are deliberate: callers that catch :class:`ValueError` around
    parsing keep working, and the CLI's single ``CCReportError`` handler turns it
    into a one-line message instead of a traceback.
    """


def parse_period(value: str) -> tuple[int, int]:
    """Parse ``"2026-07"`` into ``(2026, 7)``."""
    match = _PERIOD_RE.match(value.strip())
    if not match:
        raise InvalidPeriod(f"period must look like YYYY-MM, got {value!r}")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise InvalidPeriod(f"month must be 01-12, got {value!r}")
    return year, month


def format_period(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def month_bounds(year: int, month: int) -> tuple[_dt.datetime, _dt.datetime]:
    """Return ``[start, end)`` for a month, timezone-aware UTC."""
    start = _dt.datetime(year, month, 1, tzinfo=_dt.UTC)
    last_day = calendar.monthrange(year, month)[1]
    end = _dt.datetime(year, month, last_day, 23, 59, 59, 999999, tzinfo=_dt.UTC) + _dt.timedelta(
        microseconds=1
    )
    return start, end


def current_period(now: _dt.datetime | None = None) -> tuple[int, int]:
    now = now or _dt.datetime.now(_dt.UTC)
    return now.year, now.month


def available_periods(window: int, now: _dt.datetime | None = None) -> list[str]:
    """The most recent ``window`` months, newest first, inclusive of this one."""
    if window < 1:
        return []
    year, month = current_period(now)
    out: list[str] = []
    for _ in range(window):
        out.append(format_period(year, month))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return out


def clamp_period(value: str, window: int, now: _dt.datetime | None = None) -> tuple[int, int]:
    """Parse and validate a period against the permitted window.

    Raises rather than silently substituting a nearby month: a user who asked
    for March and got July would not notice until the report was wrong.
    """
    year, month = parse_period(value)
    allowed = available_periods(window, now)
    period = format_period(year, month)
    if period not in allowed:
        oldest = allowed[-1] if allowed else "n/a"
        newest = allowed[0] if allowed else "n/a"
        raise InvalidPeriod(
            f"{period} is outside the permitted window of {window} months "
            f"({oldest} through {newest})"
        )
    return year, month


def period_label(year: int, month: int) -> str:
    """``(2026, 7)`` → ``"July 2026"``."""
    return f"{calendar.month_name[month]} {year}"


__all__ = [
    "InvalidPeriod",
    "available_periods",
    "clamp_period",
    "current_period",
    "format_period",
    "month_bounds",
    "parse_period",
    "period_label",
]
