from __future__ import annotations

import datetime as _dt

import pytest

from ccreport.errors import CCReportError
from ccreport.period import (
    InvalidPeriod,
    available_periods,
    clamp_period,
    format_period,
    month_bounds,
    parse_period,
    period_label,
)

NOW = _dt.datetime(2026, 8, 15, 12, 0, tzinfo=_dt.UTC)


def test_parse_and_format_round_trip() -> None:
    assert parse_period(" 2026-07 ") == (2026, 7)
    assert format_period(2026, 7) == "2026-07"


@pytest.mark.parametrize("bad", ["2026-13", "2026-00", "26-07", "2026/07", "July 2026", ""])
def test_malformed_periods_are_rejected(bad: str) -> None:
    with pytest.raises(InvalidPeriod):
        parse_period(bad)


def test_invalid_period_is_both_a_value_error_and_a_ccreport_error() -> None:
    """So parsing callers and the CLI's single error handler both work."""
    assert issubclass(InvalidPeriod, ValueError)
    assert issubclass(InvalidPeriod, CCReportError)


def test_month_bounds_are_half_open_and_utc() -> None:
    start, end = month_bounds(2026, 7)
    assert start == _dt.datetime(2026, 7, 1, tzinfo=_dt.UTC)
    assert end == _dt.datetime(2026, 8, 1, tzinfo=_dt.UTC)
    assert start.tzinfo is _dt.UTC


def test_february_in_a_leap_year() -> None:
    start, end = month_bounds(2028, 2)
    assert (end - start).days == 29


def test_available_periods_are_newest_first_and_cross_the_year() -> None:
    assert available_periods(3, NOW) == ["2026-08", "2026-07", "2026-06"]
    assert available_periods(9, _dt.datetime(2026, 2, 1, tzinfo=_dt.UTC))[-1] == "2025-06"
    assert available_periods(0, NOW) == []


def test_a_month_outside_the_window_raises_rather_than_snapping_to_a_nearby_one() -> None:
    """A user who asked for March and got July would not notice until it was wrong."""
    with pytest.raises(InvalidPeriod, match="outside the permitted window"):
        clamp_period("2025-01", 8, NOW)
    assert clamp_period("2026-07", 8, NOW) == (2026, 7)


def test_the_current_month_is_always_inside_the_window() -> None:
    assert clamp_period("2026-08", 1, NOW) == (2026, 8)


def test_labels_are_human_readable() -> None:
    assert period_label(2026, 7) == "July 2026"
