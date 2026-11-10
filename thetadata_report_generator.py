#!/usr/bin/env python3
"""Generate a transparent SPY market report from a local ThetaData v3 terminal.

The generator deliberately uses only the Python standard library. It retrieves SPY
OHLC bars, calculates conventional trend/momentum/risk indicators, and writes the
strict JSON contract consumed by spy_report_publisher.py. It never requests an index
endpoint, so it works with a stock STANDARD subscription and FREE index access.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import os
import statistics
import sys
import tempfile
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from market_calendar import EASTERN, market_hours


LOGGER = logging.getLogger("thetadata_report_generator")
VERSION = "1.3.2"
DEFAULT_BASE_URL = "http://127.0.0.1:25503/v3"
MAX_RESPONSE_BYTES = 50 * 1024 * 1024


class GeneratorError(RuntimeError):
    """Expected generator or data failure."""


class ThetaDataError(GeneratorError):
    """Theta Terminal request or response failure."""


@dataclass(frozen=True)
class DailyBar:
    trading_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float
    bar_count: int
    last_timestamp: str


@dataclass(frozen=True)
class IntradayBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float


def _validate_base_url(value: str, allow_remote: bool = False) -> str:
    parsed = urllib.parse.urlsplit(value.rstrip("/"))
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise GeneratorError(
            "ThetaData base URL must be an unauthenticated http:// URL"
        )
    if parsed.query or parsed.fragment:
        raise GeneratorError("ThetaData base URL cannot contain a query or fragment")
    if not parsed.path.rstrip("/").endswith("/v3"):
        raise GeneratorError("ThetaData base URL must end in /v3")
    if not allow_remote:
        hostname = parsed.hostname.casefold()
        loopback = hostname == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = False
        if not loopback:
            raise GeneratorError(
                "ThetaData must use a loopback address unless --allow-remote-thetadata is set"
            )
    return value.rstrip("/")


class ThetaDataClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_seconds: float = 30.0,
        retries: int = 3,
        allow_remote: bool = False,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.base_url = _validate_base_url(base_url, allow_remote=allow_remote)
        if timeout_seconds <= 0:
            raise GeneratorError("timeout must be positive")
        if retries < 0 or retries > 10:
            raise GeneratorError("retries must be between 0 and 10")
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self._opener = opener or urllib.request.urlopen

    def _rows(self, path: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{self.base_url}{path}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": f"spy-market-report-generator/{VERSION}",
            },
            method="GET",
        )
        last_error: Optional[BaseException] = None
        for attempt in range(self.retries + 1):
            try:
                with self._opener(request, timeout=self.timeout_seconds) as response:
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > MAX_RESPONSE_BYTES:
                        raise ThetaDataError("ThetaData response exceeds 50 MiB limit")
                    payload = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise ThetaDataError("ThetaData response exceeds 50 MiB limit")
                try:
                    decoded = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ThetaDataError(
                        f"ThetaData returned invalid JSON for {path}: {exc}"
                    ) from exc
                if isinstance(decoded, list):
                    rows = decoded
                elif isinstance(decoded, dict):
                    rows = next(
                        (
                            decoded[key]
                            for key in ("response", "data", "result")
                            if isinstance(decoded.get(key), list)
                        ),
                        None,
                    )
                    if rows is None:
                        detail = decoded.get("error") or decoded.get("message")
                        raise ThetaDataError(
                            f"ThetaData response did not contain rows"
                            + (f": {detail}" if detail else "")
                        )
                else:
                    raise ThetaDataError("ThetaData response root must be an array or object")
                if not all(isinstance(row, dict) for row in rows):
                    raise ThetaDataError("ThetaData response contains a non-object row")
                return rows
            except urllib.error.HTTPError as exc:
                last_error = exc
                try:
                    detail = exc.read(2000).decode("utf-8", errors="replace").strip()
                except Exception:
                    detail = ""
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise ThetaDataError(
                        f"ThetaData HTTP {exc.code} for {path}: {detail or exc.reason}"
                    ) from exc
            except (urllib.error.URLError, TimeoutError, OSError, ThetaDataError) as exc:
                last_error = exc
                if isinstance(exc, ThetaDataError) and "50 MiB" in str(exc):
                    raise
            if attempt < self.retries:
                time_module.sleep(min(4.0, 0.5 * (2**attempt)))
        raise ThetaDataError(
            "Theta Terminal request failed after retries; confirm that Theta Terminal v3 "
            f"is running at {self.base_url}: {last_error}"
        ) from last_error

    def stock_ohlc(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        *,
        interval: str = "5m",
        venue: str = "nqb",
    ) -> list[dict[str, Any]]:
        return self._rows(
            "/stock/history/ohlc",
            {
                "symbol": symbol,
                "start_date": start_date.strftime("%Y%m%d"),
                "end_date": end_date.strftime("%Y%m%d"),
                "interval": interval,
                "start_time": "09:30:00.000",
                # ThetaData treats end_time as inclusive. 16:00 is the market-close
                # boundary, not the start of a regular-session bar, and some feeds
                # return a synthetic/mixed-value row at that timestamp.
                "end_time": "15:59:59.999",
                "venue": venue,
                "format": "json",
            },
        )

def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise GeneratorError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise GeneratorError(f"{label} must be numeric") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise GeneratorError(f"{label} is outside the accepted range")
    return result


def _stock_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise GeneratorError("stock timestamp is missing or invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GeneratorError(f"stock timestamp is invalid: {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=EASTERN)
    return parsed.astimezone(EASTERN)


def _is_empty_stock_bar(row: Mapping[str, Any]) -> bool:
    """Return true only for ThetaData's all-zero, no-trade placeholder bars."""
    values: list[float] = []
    for field in ("open", "high", "low", "close", "volume", "count"):
        try:
            value = float(row.get(field))
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value):
            return False
        values.append(value)
    raw_vwap = row.get("vwap")
    if raw_vwap is not None:
        try:
            vwap = float(raw_vwap)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(vwap) or vwap != 0:
            return False
    return all(value == 0 for value in values)


def aggregate_stock_rows(rows: Sequence[Mapping[str, Any]]) -> list[DailyBar]:
    grouped: dict[date, list[Mapping[str, Any]]] = {}
    empty_bars = 0
    outside_session_bars = 0
    for row in rows:
        timestamp = _stock_timestamp(row.get("timestamp"))
        day = timestamp.date()
        hours = market_hours(day)
        if (
            hours.is_open
            and hours.open_at is not None
            and hours.close_at is not None
            and not (hours.open_at <= timestamp < hours.close_at)
        ):
            outside_session_bars += 1
            continue
        if _is_empty_stock_bar(row):
            empty_bars += 1
            continue
        grouped.setdefault(day, []).append(row)
    if outside_session_bars:
        LOGGER.warning(
            "ignored %d SPY bars outside regular market hours",
            outside_session_bars,
        )
    if empty_bars:
        LOGGER.warning("ignored %d all-zero no-trade SPY bars", empty_bars)
    daily: list[DailyBar] = []
    for day, day_rows in sorted(grouped.items()):
        ordered = sorted(day_rows, key=lambda row: str(row.get("timestamp", "")))
        opens = [
            _number(
                row.get("open"),
                f"stock open at {row.get('timestamp')}",
                positive=True,
            )
            for row in ordered
        ]
        highs = [
            _number(
                row.get("high"),
                f"stock high at {row.get('timestamp')}",
                positive=True,
            )
            for row in ordered
        ]
        lows = [
            _number(
                row.get("low"),
                f"stock low at {row.get('timestamp')}",
                positive=True,
            )
            for row in ordered
        ]
        closes = [
            _number(
                row.get("close"),
                f"stock close at {row.get('timestamp')}",
                positive=True,
            )
            for row in ordered
        ]
        volumes = [max(0, int(_number(row.get("volume", 0), "stock volume"))) for row in ordered]
        if any(high < low for high, low in zip(highs, lows)):
            raise GeneratorError(f"stock high is below low on {day}")
        total_volume = sum(volumes)
        weighted_vwap = 0.0
        for row, close, volume in zip(ordered, closes, volumes):
            raw_vwap = row.get("vwap")
            vwap = close if raw_vwap is None else _number(raw_vwap, "stock vwap", positive=True)
            weighted_vwap += vwap * max(1, volume)
        divisor = sum(max(1, volume) for volume in volumes)
        daily.append(
            DailyBar(
                trading_date=day,
                open=opens[0],
                high=max(highs),
                low=min(lows),
                close=closes[-1],
                volume=total_volume,
                vwap=weighted_vwap / divisor,
                bar_count=len(ordered),
                last_timestamp=str(ordered[-1]["timestamp"]),
            )
        )
    return daily


def _previous_trading_day(day: date) -> date:
    cursor = day - timedelta(days=1)
    for _ in range(10):
        if market_hours(cursor).is_open:
            return cursor
        cursor -= timedelta(days=1)
    raise GeneratorError(f"could not find a prior trading day before {day}")


def _sma(values: Sequence[float], period: int) -> float:
    window = values[-min(period, len(values)) :]
    return statistics.fmean(window)


def _ema(values: Sequence[float], period: int) -> float:
    window = values[-max(period * 3, period) :]
    alpha = 2.0 / (period + 1)
    result = window[0]
    for value in window[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def _pct_change(current: float, previous: float) -> float:
    if previous == 0:
        raise GeneratorError("cannot calculate percentage change from zero")
    return (current / previous - 1.0) * 100.0


def _momentum(values: Sequence[float], periods: int) -> Optional[float]:
    if len(values) <= periods:
        return None
    return _pct_change(values[-1], values[-1 - periods])


def _rsi(values: Sequence[float], period: int = 14) -> Optional[float]:
    if len(values) < 2:
        return None
    changes = [current - previous for previous, current in zip(values, values[1:])]
    window = changes[-min(period, len(changes)) :]
    gains = statistics.fmean(max(change, 0.0) for change in window)
    losses = statistics.fmean(max(-change, 0.0) for change in window)
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    relative_strength = gains / losses
    return 100.0 - 100.0 / (1.0 + relative_strength)


def _atr(bars: Sequence[DailyBar], period: int = 14) -> Optional[float]:
    if not bars:
        return None
    ranges: list[float] = []
    for index, bar in enumerate(bars):
        previous_close = bars[index - 1].close if index else bar.open
        ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )
    return statistics.fmean(ranges[-min(period, len(ranges)) :])


def _realized_volatility(values: Sequence[float], period: int = 20) -> Optional[float]:
    if len(values) < 3:
        return None
    returns = [current / previous - 1.0 for previous, current in zip(values, values[1:])]
    window = returns[-min(period, len(returns)) :]
    if len(window) < 2:
        return None
    return statistics.stdev(window) * math.sqrt(252) * 100.0


def _rounded(value: Optional[float], places: int = 4) -> Optional[float]:
    return None if value is None else round(value, places)


def _add_signal(
    signals: list[dict[str, Any]],
    name: str,
    value: Optional[float],
    contribution: float,
    reason: str,
) -> float:
    signals.append(
        {
            "name": name,
            "value": _rounded(value),
            "contribution": contribution,
            "reason": reason,
        }
    )
    return contribution


def _interval_minutes(interval: str) -> int:
    if not interval.endswith("m"):
        raise GeneratorError("intraday reports require a minute-based interval")
    try:
        minutes = int(interval[:-1])
    except ValueError as exc:
        raise GeneratorError(f"invalid minute interval: {interval!r}") from exc
    if minutes <= 0 or 60 % minutes != 0:
        raise GeneratorError("intraday interval must divide evenly into 60 minutes")
    return minutes


def _completed_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    trading_date: date,
    generated_at: datetime,
    interval_minutes: int,
) -> list[Mapping[str, Any]]:
    generated_eastern = generated_at.astimezone(EASTERN)
    interval = timedelta(minutes=interval_minutes)
    completed: list[Mapping[str, Any]] = []
    for row in rows:
        timestamp = _stock_timestamp(row.get("timestamp"))
        if timestamp.date() == trading_date and timestamp + interval > generated_eastern:
            continue
        completed.append(row)
    return completed


def _intraday_bars(
    rows: Sequence[Mapping[str, Any]],
    trading_date: date,
) -> list[IntradayBar]:
    result: list[IntradayBar] = []
    for row in rows:
        timestamp = _stock_timestamp(row.get("timestamp"))
        if timestamp.date() != trading_date:
            continue
        hours = market_hours(trading_date)
        if (
            not hours.is_open
            or hours.open_at is None
            or hours.close_at is None
            or not (hours.open_at <= timestamp < hours.close_at)
            or _is_empty_stock_bar(row)
        ):
            continue
        open_price = _number(
            row.get("open"), f"stock open at {row.get('timestamp')}", positive=True
        )
        high = _number(
            row.get("high"), f"stock high at {row.get('timestamp')}", positive=True
        )
        low = _number(
            row.get("low"), f"stock low at {row.get('timestamp')}", positive=True
        )
        close = _number(
            row.get("close"), f"stock close at {row.get('timestamp')}", positive=True
        )
        if high < low:
            raise GeneratorError(f"stock high is below low at {row.get('timestamp')}")
        volume = max(0, int(_number(row.get("volume", 0), "stock volume")))
        raw_vwap = row.get("vwap")
        vwap = close if raw_vwap is None else _number(raw_vwap, "stock vwap", positive=True)
        result.append(
            IntradayBar(timestamp, open_price, high, low, close, volume, vwap)
        )
    return sorted(result, key=lambda bar: bar.timestamp)


def _window_iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def build_intraday_report(
    *,
    trading_date: date,
    generated_at: datetime,
    stock_rows: Sequence[Mapping[str, Any]],
    interval_minutes: int = 5,
) -> dict[str, Any]:
    if interval_minutes != 5:
        raise GeneratorError("intraday reports currently require five-minute bars")
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise GeneratorError("generated_at must be timezone-aware")
    hours = market_hours(trading_date)
    if not hours.is_open or hours.open_at is None or hours.close_at is None:
        raise GeneratorError(f"{trading_date} is not a regular US equities trading day")
    generated_eastern = generated_at.astimezone(EASTERN)
    if generated_eastern.date() != trading_date:
        raise GeneratorError("intraday generated_at must resolve to trading_date")
    if not (hours.open_at + timedelta(minutes=60) <= generated_eastern < hours.close_at):
        raise GeneratorError(
            "intraday report must be generated at least 60 minutes after the open "
            "and before the close"
        )

    completed = _completed_rows(
        stock_rows,
        trading_date=trading_date,
        generated_at=generated_at,
        interval_minutes=interval_minutes,
    )
    bars = _intraday_bars(completed, trading_date)
    bars_per_hour = 60 // interval_minutes
    if len(bars) < bars_per_hour:
        raise GeneratorError(
            f"need at least {bars_per_hour} completed intraday bars; received {len(bars)}"
        )

    last_bar_end = bars[-1].timestamp + timedelta(minutes=interval_minutes)
    if generated_eastern - last_bar_end > timedelta(minutes=interval_minutes + 2):
        raise GeneratorError(
            f"latest completed bar ended at {last_bar_end.isoformat()}, too stale for "
            f"an intraday report generated at {generated_eastern.isoformat()}"
        )

    daily = aggregate_stock_rows(completed)
    if not daily or daily[-1].trading_date != trading_date:
        raise GeneratorError(f"SPY intraday data does not reach {trading_date}")
    current = daily[-1]
    previous_close = daily[-2].close if len(daily) >= 2 else None
    closes = [bar.close for bar in bars]
    ema6 = _ema(closes, min(6, len(closes)))
    ema12 = _ema(closes, min(12, len(closes)))
    momentum15 = _momentum(closes, 3)
    momentum30 = _momentum(closes, 6)
    momentum60 = _momentum(closes, bars_per_hour)
    total_volume = sum(bar.volume for bar in bars)
    session_vwap = (
        sum(bar.vwap * max(1, bar.volume) for bar in bars)
        / sum(max(1, bar.volume) for bar in bars)
    )
    price_vs_vwap = _pct_change(bars[-1].close, session_vwap)
    ema_spread = _pct_change(ema6, ema12)

    signals: list[dict[str, Any]] = []
    score = 0.0
    contribution = 1.0 if ema_spread > 0.05 else -1.0 if ema_spread < -0.05 else 0.0
    score += _add_signal(
        signals,
        "ema6_vs_ema12_5m",
        ema_spread,
        contribution,
        "Short intraday trend using completed five-minute bars",
    )
    contribution = (
        1.0
        if momentum30 is not None and momentum30 > 0.15
        else -1.0
        if momentum30 is not None and momentum30 < -0.15
        else 0.0
    )
    score += _add_signal(
        signals,
        "momentum_30m_pct",
        momentum30,
        contribution,
        "Thirty-minute price momentum",
    )
    contribution = 0.75 if price_vs_vwap > 0.10 else -0.75 if price_vs_vwap < -0.10 else 0.0
    score += _add_signal(
        signals,
        "price_vs_session_vwap_pct",
        price_vs_vwap,
        contribution,
        "Last completed-bar close relative to Nasdaq Basic session VWAP",
    )
    contribution = (
        0.5
        if momentum15 is not None and momentum15 > 0.08
        else -0.5
        if momentum15 is not None and momentum15 < -0.08
        else 0.0
    )
    score += _add_signal(
        signals,
        "momentum_15m_pct",
        momentum15,
        contribution,
        "Fifteen-minute price momentum",
    )

    bias = "bullish" if score >= 1.25 else "bearish" if score <= -1.25 else "neutral"
    signal_strength = min(1.0, abs(score) / 3.25)
    window_start = last_bar_end
    window_end = min(window_start + timedelta(hours=1), hours.close_at)
    duration_minutes = int((window_end - window_start).total_seconds() // 60)
    expected_bars = int((last_bar_end - hours.open_at).total_seconds() // 60) // interval_minutes
    completeness = min(1.0, len(bars) / max(1, expected_bars))
    if completeness < 0.80:
        raise GeneratorError(
            f"intraday bar completeness is only {completeness:.0%}; refusing to forecast"
        )
    rationale = [
        f"{item['name']} contributed {item['contribution']:+g}"
        for item in signals
        if item["contribution"] != 0
    ] or ["No configured short-horizon signal crossed its threshold"]

    return {
        "symbol": "SPY",
        "status": "live",
        "market_timezone": "America/New_York",
        "session": "intraday",
        "trading_date": trading_date.isoformat(),
        "generated_at": generated_at.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "source": "thetadata-local-v3",
        "source_venue": "nqb",
        "data_through": trading_date.isoformat(),
        "market": {
            "as_of": _window_iso(last_bar_end),
            "last_completed_bar_start": _window_iso(bars[-1].timestamp),
            "reference_price": _rounded(bars[-1].close),
            "session_open": _rounded(current.open),
            "session_high": _rounded(current.high),
            "session_low": _rounded(current.low),
            "session_vwap": _rounded(session_vwap),
            "session_volume_nasdaq_basic": total_volume,
            "open_to_current_pct": _rounded(_pct_change(bars[-1].close, current.open)),
            "previous_close_to_current_pct": (
                _rounded(_pct_change(bars[-1].close, previous_close))
                if previous_close is not None
                else None
            ),
        },
        "analysis": {
            "method": "transparent-intraday-rule-score-v1",
            "score": round(score, 2),
            "maximum_absolute_score": 3.25,
            "signals": signals,
            "indicators": {
                "ema6_5m": _rounded(ema6),
                "ema12_5m": _rounded(ema12),
                "momentum_15m_pct": _rounded(momentum15),
                "momentum_30m_pct": _rounded(momentum30),
                "momentum_60m_pct": _rounded(momentum60),
                "price_vs_session_vwap_pct": _rounded(price_vs_vwap),
            },
        },
        "forecast": {
            "is_directional_forecast": True,
            "bias": bias,
            "signal_strength": round(signal_strength, 2),
            "calibrated_probability": None,
            "horizon": "next_market_interval",
            "window_start": _window_iso(window_start),
            "window_end": _window_iso(window_end),
            "duration_minutes": duration_minutes,
            "target": "window-end SPY price versus reference_price",
            "reference_price": _rounded(bars[-1].close),
            "rationale": rationale,
        },
        "data_quality": {
            "feed": "Nasdaq Basic",
            "coverage": "not consolidated UTP/CTA",
            "completed_bars": len(bars),
            "interval_minutes": interval_minutes,
            "completeness": round(completeness, 2),
            "inputs": ["SPY stock OHLC (Nasdaq Basic)"],
        },
        "generator": {"name": "thetadata_report_generator", "version": VERSION},
        "disclaimer": (
            "Uncalibrated rule-based signal strength for research; not a probability "
            "or investment advice."
        ),
    }


def build_report(
    *,
    session: str,
    trading_date: date,
    generated_at: datetime,
    stock_rows: Sequence[Mapping[str, Any]],
    interval_minutes: int = 5,
) -> dict[str, Any]:
    if session == "intraday":
        return build_intraday_report(
            trading_date=trading_date,
            generated_at=generated_at,
            stock_rows=stock_rows,
            interval_minutes=interval_minutes,
        )
    if session not in {"premarket", "postmarket"}:
        raise GeneratorError("session must be premarket, intraday, or postmarket")
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise GeneratorError("generated_at must be timezone-aware")
    expected_data_date = (
        _previous_trading_day(trading_date) if session == "premarket" else trading_date
    )
    stock = aggregate_stock_rows(stock_rows)
    if len(stock) < 10:
        raise GeneratorError(f"need at least 10 SPY sessions; received {len(stock)}")
    if stock[-1].trading_date != expected_data_date:
        raise GeneratorError(
            f"SPY data ends on {stock[-1].trading_date}; expected {expected_data_date} "
            f"for {session}"
        )

    closes = [bar.close for bar in stock]
    sma5 = _sma(closes, 5)
    sma20 = _sma(closes, 20)
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    rsi14 = _rsi(closes, 14)
    atr14 = _atr(stock, 14)
    realized20 = _realized_volatility(closes, 20)
    momentum1 = _momentum(closes, 1)
    momentum5 = _momentum(closes, 5)
    momentum20 = _momentum(closes, 20)
    latest = stock[-1]
    average_volume20 = statistics.fmean(bar.volume for bar in stock[-20:])
    volume_ratio = latest.volume / average_volume20 if average_volume20 > 0 else None

    signals: list[dict[str, Any]] = []
    score = 0.0
    distance_sma20 = _pct_change(latest.close, sma20)
    contribution = 1.0 if distance_sma20 > 0.15 else -1.0 if distance_sma20 < -0.15 else 0.0
    score += _add_signal(
        signals,
        "spy_close_vs_sma20",
        distance_sma20,
        contribution,
        "SPY close relative to its available 20-session simple moving average",
    )

    crossover = _pct_change(sma5, sma20)
    contribution = 1.0 if crossover > 0.10 else -1.0 if crossover < -0.10 else 0.0
    score += _add_signal(
        signals,
        "spy_sma5_vs_sma20",
        crossover,
        contribution,
        "Short trend relative to the medium trend",
    )

    contribution = 0.0
    if momentum5 is not None:
        contribution = 1.0 if momentum5 > 0.50 else -1.0 if momentum5 < -0.50 else 0.0
    score += _add_signal(
        signals,
        "spy_momentum_5d_pct",
        momentum5,
        contribution,
        "Five-session SPY price momentum",
    )

    contribution = 0.0
    if rsi14 is not None:
        if 55 <= rsi14 <= 70:
            contribution = 0.5
        elif 30 <= rsi14 <= 45:
            contribution = -0.5
        elif rsi14 > 70:
            contribution = -0.25
        elif rsi14 < 30:
            contribution = 0.25
    score += _add_signal(
        signals,
        "spy_rsi14",
        rsi14,
        contribution,
        "RSI trend confirmation with a small mean-reversion adjustment at extremes",
    )

    session_return = _pct_change(latest.close, latest.open)
    contribution = 0.5 if session_return > 0.35 else -0.5 if session_return < -0.35 else 0.0
    score += _add_signal(
        signals,
        "latest_session_return_pct",
        session_return,
        contribution,
        "Most recent completed regular session direction",
    )

    bias = "bullish" if score >= 1.25 else "bearish" if score <= -1.25 else "neutral"
    data_completeness = min(1.0, len(stock) / 20.0)
    strength = min(1.0, abs(score) / 4.5)

    rationale = [
        f"{item['name']} contributed {item['contribution']:+g}"
        for item in signals
        if item["contribution"] != 0
    ]
    if not rationale:
        rationale = ["No configured signal crossed its directional threshold"]

    hours = market_hours(trading_date)
    if hours.open_at is None or hours.close_at is None:
        raise GeneratorError(f"market hours unavailable for {trading_date}")
    if session == "premarket":
        forecast = {
            "is_directional_forecast": True,
            "bias": bias,
            "signal_strength": round(strength, 2),
            "calibrated_probability": None,
            "horizon": "first_regular_session_hour",
            "window_start": _window_iso(hours.open_at),
            "window_end": _window_iso(min(hours.open_at + timedelta(hours=1), hours.close_at)),
            "target": "window-end SPY price versus regular-session open",
            "rationale": rationale,
        }
    else:
        forecast = {
            "is_directional_forecast": False,
            "bias": bias,
            "signal_strength": round(strength, 2),
            "calibrated_probability": None,
            "horizon": "completed_session_trend_context",
            "target": None,
            "rationale": rationale,
        }

    previous_close = stock[-2].close if len(stock) >= 2 else None
    return {
        "symbol": "SPY",
        "status": "live",
        "market_timezone": "America/New_York",
        "session": session,
        "trading_date": trading_date.isoformat(),
        "generated_at": generated_at.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "source": "thetadata-local-v3",
        "source_venue": "nqb",
        "data_through": expected_data_date.isoformat(),
        "market": {
            "as_of": latest.last_timestamp,
            "open": _rounded(latest.open),
            "high": _rounded(latest.high),
            "low": _rounded(latest.low),
            "close": _rounded(latest.close),
            "vwap": _rounded(latest.vwap),
            "volume": latest.volume,
            "intraday_return_pct": _rounded(session_return),
            "previous_close_to_close_pct": (
                _rounded(_pct_change(latest.close, previous_close))
                if previous_close is not None
                else None
            ),
        },
        "analysis": {
            "method": "transparent-stock-rule-score-v1",
            "score": round(score, 2),
            "signals": signals,
            "indicators": {
                "sma5": _rounded(sma5),
                "sma20": _rounded(sma20),
                "ema12": _rounded(ema12),
                "ema26": _rounded(ema26),
                "rsi14": _rounded(rsi14),
                "atr14": _rounded(atr14),
                "realized_volatility_20d_pct": _rounded(realized20),
                "momentum_1d_pct": _rounded(momentum1),
                "momentum_5d_pct": _rounded(momentum5),
                "momentum_20d_pct": _rounded(momentum20),
                "volume_vs_20d_average": _rounded(volume_ratio),
            },
        },
        "forecast": forecast,
        "data_quality": {
            "spy_sessions": len(stock),
            "spy_bars_latest_session": latest.bar_count,
            "completeness": round(data_completeness, 2),
            "feed": "Nasdaq Basic",
            "coverage": "not consolidated UTP/CTA",
            "inputs": ["SPY stock OHLC (Nasdaq Basic)"],
        },
        "generator": {
            "name": "thetadata_report_generator",
            "version": VERSION,
        },
        "disclaimer": (
            "Uncalibrated rule-based signal strength for research; not a probability "
            "or investment advice."
        ),
    }


def generate_report(
    client: ThetaDataClient,
    *,
    session: str,
    trading_date: date,
    interval: str,
    venue: str,
    lookback_calendar_days: int,
    generated_at: Optional[datetime] = None,
) -> dict[str, Any]:
    if lookback_calendar_days < 14 or lookback_calendar_days > 30:
        raise GeneratorError("lookback-calendar-days must be between 14 and 30")
    if not market_hours(trading_date).is_open:
        raise GeneratorError(f"{trading_date} is not a regular US equities trading day")
    if session not in {"premarket", "intraday", "postmarket"}:
        raise GeneratorError("session must be premarket, intraday, or postmarket")
    data_end = _previous_trading_day(trading_date) if session == "premarket" else trading_date
    data_start = data_end - timedelta(days=lookback_calendar_days - 1)
    LOGGER.info("requesting SPY data from %s through %s", data_start, data_end)
    stock_rows = client.stock_ohlc(
        "SPY", data_start, data_end, interval=interval, venue=venue
    )
    generated = generated_at or datetime.now(timezone.utc)
    return build_report(
        session=session,
        trading_date=trading_date,
        generated_at=generated,
        stock_rows=stock_rows,
        interval_minutes=_interval_minutes(interval) if session == "intraday" else 5,
    )


def health_check(client: ThetaDataClient) -> dict[str, Any]:
    today = datetime.now(EASTERN).date()
    # Always use the last completed session so setup works before the open and does
    # not depend on whether a current hourly bar has completed yet.
    day = _previous_trading_day(today)
    stock = client.stock_ohlc("SPY", day, day, interval="1h", venue="nqb")
    if not stock:
        raise ThetaDataError(f"ThetaData returned no SPY rows for {day}")
    usable_sessions = aggregate_stock_rows(stock)
    if not usable_sessions:
        raise ThetaDataError(f"ThetaData returned no usable SPY bars for {day}")
    return {
        "ok": True,
        "base_url": client.base_url,
        "checked_date": day.isoformat(),
        "spy_rows": len(stock),
        "usable_sessions": len(usable_sessions),
        "data_scope": "SPY stock OHLC only",
    }


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    payload = (
        json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a SPY analysis report from a local ThetaData v3 terminal"
    )
    parser.add_argument("--session", choices=("premarket", "intraday", "postmarket"))
    parser.add_argument("--trading-date", type=date.fromisoformat)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("THETADATA_BASE_URL", DEFAULT_BASE_URL),
    )
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--venue", choices=("nqb", "utp_cta"), default="nqb")
    parser.add_argument("--lookback-calendar-days", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--allow-remote-thetadata", action="store_true")
    parser.add_argument("--health-check", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        client = ThetaDataClient(
            args.base_url,
            timeout_seconds=args.timeout_seconds,
            retries=args.retries,
            allow_remote=args.allow_remote_thetadata,
        )
        if args.health_check:
            print(json.dumps(health_check(client), indent=2, sort_keys=True))
            return 0
        missing = [
            name
            for name, value in (
                ("--session", args.session),
                ("--trading-date", args.trading_date),
                ("--output", args.output),
            )
            if value is None
        ]
        if missing:
            raise GeneratorError(
                f"required unless --health-check is used: {', '.join(missing)}"
            )
        report = generate_report(
            client,
            session=args.session,
            trading_date=args.trading_date,
            interval=args.interval,
            venue=args.venue,
            lookback_calendar_days=args.lookback_calendar_days,
        )
        write_report(args.output, report)
        LOGGER.info(
            "wrote %s %s report to %s (bias=%s signal_strength=%s)",
            args.trading_date,
            args.session,
            args.output,
            report["forecast"]["bias"],
            report["forecast"]["signal_strength"],
        )
        return 0
    except GeneratorError as exc:
        LOGGER.error("report generation failed: %s", exc)
        return 1
    except Exception:
        LOGGER.exception("unexpected report generation failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


