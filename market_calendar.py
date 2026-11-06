#!/usr/bin/env python3
"""Small, dependency-free US equities calendar used by the publisher.

The rules cover regular NYSE holidays and the recurring 13:00 ET closes.  An
operator-supplied override file is supported because exchanges can announce
one-off closures or schedule changes after software has shipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Set
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


class CalendarError(ValueError):
    """Raised when a calendar override is invalid."""


@dataclass(frozen=True)
class CalendarOverrides:
    closed: frozenset[date] = frozenset()
    open: frozenset[date] = frozenset()
    early_close: Mapping[date, time] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.early_close is None:
            object.__setattr__(self, "early_close", {})
        overlap = self.closed.intersection(self.open)
        if overlap:
            raise CalendarError(f"dates cannot be both open and closed: {sorted(overlap)}")


@dataclass(frozen=True)
class MarketHours:
    trading_date: date
    is_open: bool
    open_at: Optional[datetime]
    close_at: Optional[datetime]
    reason: str


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise CalendarError(f"invalid ISO date in calendar override: {value!r}") from exc


def _parse_time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise CalendarError(f"invalid time in calendar override: {value!r}") from exc
    if parsed.tzinfo is not None:
        raise CalendarError("calendar override times must be local wall times without an offset")
    return parsed


def load_overrides(path: Optional[Path]) -> CalendarOverrides:
    if path is None:
        return CalendarOverrides()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CalendarError(f"calendar override file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CalendarError(f"invalid calendar override JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CalendarError("calendar override must be a JSON object")

    allowed = {"closed", "open", "early_close"}
    unknown = set(raw).difference(allowed)
    if unknown:
        raise CalendarError(f"unknown calendar override keys: {sorted(unknown)}")

    def parse_date_list(key: str) -> frozenset[date]:
        values = raw.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise CalendarError(f"{key!r} must be an array of ISO date strings")
        return frozenset(_parse_date(v) for v in values)

    early_raw = raw.get("early_close", {})
    if not isinstance(early_raw, dict):
        raise CalendarError("'early_close' must be an object mapping dates to local times")
    early: Dict[date, time] = {}
    for day_text, close_text in early_raw.items():
        if not isinstance(day_text, str) or not isinstance(close_text, str):
            raise CalendarError("early-close dates and times must be strings")
        early[_parse_date(day_text)] = _parse_time(close_text)

    return CalendarOverrides(
        closed=parse_date_list("closed"),
        open=parse_date_list("open"),
        early_close=early,
    )


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    return cursor - timedelta(days=(cursor.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Gregorian Easter using the Meeus/Jones/Butcher algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed_nearest_weekday(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def nyse_holidays(year: int) -> Set[date]:
    """Return recurring full-day NYSE holidays for a calendar year.

    NYSE does not observe New Year's Day on the preceding Friday when January
    1 falls on Saturday.  Other fixed-date holidays use the nearest weekday.
    """
    new_year = date(year, 1, 1)
    holidays: Set[date] = {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed_nearest_weekday(date(year, 6, 19)),
        _observed_nearest_weekday(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed_nearest_weekday(date(year, 12, 25)),
    }
    if new_year.weekday() != 5:
        holidays.add(new_year if new_year.weekday() != 6 else new_year + timedelta(days=1))
    return {day for day in holidays if day.year == year}


def _base_is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def nyse_early_closes(year: int) -> Set[date]:
    """Return recurring 13:00 ET closes (subject to exchange changes)."""
    result: Set[date] = set()

    # July 3 closes early only when it is itself a trading day.  The exchange
    # does not automatically move this early close to July 2 when July 3 is the
    # observed holiday or a weekend (for example, 2026 and 2027).
    independence_eve = date(year, 7, 3)
    if _base_is_trading_day(independence_eve):
        result.add(independence_eve)

    # Day after Thanksgiving.
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    black_friday = thanksgiving + timedelta(days=1)
    if _base_is_trading_day(black_friday):
        result.add(black_friday)

    # Christmas Eve only when it is itself a trading weekday.
    christmas_eve = date(year, 12, 24)
    if _base_is_trading_day(christmas_eve):
        result.add(christmas_eve)

    return result


def market_hours(day: date, overrides: Optional[CalendarOverrides] = None) -> MarketHours:
    overrides = overrides or CalendarOverrides()

    if day in overrides.closed:
        return MarketHours(day, False, None, None, "operator override: closed")

    forced_open = day in overrides.open
    if not forced_open:
        if day.weekday() >= 5:
            return MarketHours(day, False, None, None, "weekend")
        if day in nyse_holidays(day.year):
            return MarketHours(day, False, None, None, "NYSE holiday")

    close_time = overrides.early_close.get(day)
    reason = "operator override: open" if forced_open else "regular session"
    if close_time is None and day in nyse_early_closes(day.year):
        close_time = EARLY_CLOSE
        reason = "scheduled early close"
    elif close_time is not None:
        reason = "operator override: early close"
    else:
        close_time = REGULAR_CLOSE

    open_at = datetime.combine(day, REGULAR_OPEN, tzinfo=EASTERN)
    close_at = datetime.combine(day, close_time, tzinfo=EASTERN)
    if close_at <= open_at:
        raise CalendarError(f"close must be after open for {day}: {close_time}")
    return MarketHours(day, True, open_at, close_at, reason)


def format_dates(days: Iterable[date]) -> list[str]:
    return [day.isoformat() for day in sorted(days)]

