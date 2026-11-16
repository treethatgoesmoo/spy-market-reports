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
VERSION = "1.6.0"
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


def _direction_label(return_pct: float, neutral_band_pct: float = 0.10) -> str:
    if return_pct > neutral_band_pct:
        return "bullish"
    if return_pct < -neutral_band_pct:
        return "bearish"
    return "neutral"


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


def _resample_intraday_bars(
    bars: Sequence[IntradayBar],
    *,
    interval_minutes: int,
    session_open: datetime,
) -> list[IntradayBar]:
    if interval_minutes <= 0:
        raise GeneratorError("resample interval must be positive")
    if interval_minutes == 1:
        return list(bars)

    grouped: dict[datetime, list[IntradayBar]] = {}
    for bar in bars:
        offset_minutes = int((bar.timestamp - session_open).total_seconds() // 60)
        bucket_start = session_open + timedelta(
            minutes=(offset_minutes // interval_minutes) * interval_minutes
        )
        grouped.setdefault(bucket_start, []).append(bar)

    result: list[IntradayBar] = []
    for bucket_start, bucket in sorted(grouped.items()):
        ordered = sorted(bucket, key=lambda item: item.timestamp)
        expected_timestamps = [
            bucket_start + timedelta(minutes=index)
            for index in range(interval_minutes)
        ]
        if [item.timestamp for item in ordered] != expected_timestamps:
            continue
        total_volume = sum(item.volume for item in ordered)
        divisor = sum(max(1, item.volume) for item in ordered)
        weighted_vwap = sum(
            item.vwap * max(1, item.volume) for item in ordered
        ) / divisor
        result.append(
            IntradayBar(
                timestamp=bucket_start,
                open=ordered[0].open,
                high=max(item.high for item in ordered),
                low=min(item.low for item in ordered),
                close=ordered[-1].close,
                volume=total_volume,
                vwap=weighted_vwap,
            )
        )
    return result


def _linear_trend_line(
    bars: Sequence[IntradayBar],
    *,
    lookback_bars: int,
    interval_minutes: int,
    neutral_slope_pct: float,
) -> dict[str, Any]:
    window = list(bars[-min(lookback_bars, len(bars)) :])
    if len(window) < 2:
        raise GeneratorError(
            f"need at least two {interval_minutes}-minute bars for a trend line"
        )
    values = [bar.close for bar in window]
    x_mean = (len(values) - 1) / 2.0
    y_mean = statistics.fmean(values)
    denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
    slope = (
        sum(
            (index - x_mean) * (value - y_mean)
            for index, value in enumerate(values)
        )
        / denominator
    )
    intercept = y_mean - slope * x_mean
    fitted = [intercept + slope * index for index in range(len(values))]
    total_variation = sum((value - y_mean) ** 2 for value in values)
    residual_variation = sum(
        (value - estimate) ** 2 for value, estimate in zip(values, fitted)
    )
    r_squared = (
        1.0 - residual_variation / total_variation
        if total_variation > 0
        else 1.0
    )
    slope_pct = slope / y_mean * 100.0
    direction = _direction_label(slope_pct, neutral_slope_pct)
    return {
        "method": "least_squares_close",
        "lookback_bars": len(window),
        "lookback_minutes": len(window) * interval_minutes,
        "start_at": _window_iso(window[0].timestamp),
        "end_at": _window_iso(
            window[-1].timestamp + timedelta(minutes=interval_minutes)
        ),
        "start_price": _rounded(fitted[0]),
        "end_price": _rounded(fitted[-1]),
        "slope_points_per_bar": _rounded(slope, 6),
        "slope_pct_per_bar": _rounded(slope_pct, 6),
        "r_squared": _rounded(max(0.0, min(1.0, r_squared))),
        "direction": direction,
    }


def _recent_volume_ratio(
    bars: Sequence[IntradayBar],
    window_bars: int = 5,
) -> Optional[float]:
    usable = min(window_bars, len(bars) // 2)
    if usable < 2:
        return None
    recent = statistics.fmean(bar.volume for bar in bars[-usable:])
    previous = statistics.fmean(bar.volume for bar in bars[-2 * usable : -usable])
    return recent / previous if previous > 0 else None


def _candle_metrics(bar: IntradayBar) -> dict[str, Any]:
    candle_range = max(bar.high - bar.low, 1e-9)
    body = abs(bar.close - bar.open)
    body_high = max(bar.open, bar.close)
    body_low = min(bar.open, bar.close)
    return {
        "direction": (
            "bullish" if bar.close > bar.open else
            "bearish" if bar.close < bar.open else "neutral"
        ),
        "body": body,
        "range": candle_range,
        "body_to_range": body / candle_range,
        "upper_shadow": bar.high - body_high,
        "lower_shadow": body_low - bar.low,
        "body_high": body_high,
        "body_low": body_low,
    }


def _pattern_trend(
    bars: Sequence[IntradayBar],
    *,
    pattern_bars: int,
    neutral_band_pct: float,
) -> str:
    history = list(bars[:-pattern_bars])
    if len(history) < 2:
        return "neutral"
    lookback = min(5, len(history) - 1)
    change = _pct_change(history[-1].close, history[-1 - lookback].close)
    return _direction_label(change, neutral_band_pct)


def _candlestick_analysis(
    bars: Sequence[IntradayBar],
    *,
    interval_minutes: int,
    trend_neutral_band_pct: float,
) -> dict[str, Any]:
    if len(bars) < 4:
        raise GeneratorError("need at least four bars for candlestick analysis")

    patterns: list[dict[str, Any]] = []

    def add_pattern(
        name: str,
        bias: str,
        contribution: float,
        bars_used: int,
        context: str,
        reason: str,
    ) -> None:
        patterns.append(
            {
                "name": name,
                "bias": bias,
                "contribution": contribution,
                "bars_used": bars_used,
                "context": context,
                "reason": reason,
            }
        )

    current = bars[-1]
    previous = bars[-2]
    third = bars[-3]
    current_metrics = _candle_metrics(current)
    previous_metrics = _candle_metrics(previous)
    third_metrics = _candle_metrics(third)
    single_trend = _pattern_trend(
        bars,
        pattern_bars=1,
        neutral_band_pct=trend_neutral_band_pct,
    )
    two_bar_trend = _pattern_trend(
        bars,
        pattern_bars=2,
        neutral_band_pct=trend_neutral_band_pct,
    )
    three_bar_trend = _pattern_trend(
        bars,
        pattern_bars=3,
        neutral_band_pct=trend_neutral_band_pct,
    )

    body_floor = current_metrics["range"] * 0.03
    effective_body = max(current_metrics["body"], body_floor)
    if current_metrics["body_to_range"] <= 0.10:
        add_pattern(
            "doji",
            "neutral",
            0.0,
            1,
            single_trend,
            "The latest candle has a very small real body, indicating indecision",
        )
    elif (
        current_metrics["body_to_range"] <= 0.25
        and current_metrics["upper_shadow"] >= current_metrics["range"] * 0.20
        and current_metrics["lower_shadow"] >= current_metrics["range"] * 0.20
    ):
        add_pattern(
            "spinning_top",
            "neutral",
            0.0,
            1,
            single_trend,
            "A small body with two-sided shadows indicates indecision",
        )

    long_lower_shadow = (
        current_metrics["body_to_range"] <= 0.45
        and current_metrics["lower_shadow"] >= effective_body * 2.0
        and current_metrics["upper_shadow"] <= max(
            effective_body * 0.75,
            current_metrics["range"] * 0.12,
        )
    )
    long_upper_shadow = (
        current_metrics["body_to_range"] <= 0.45
        and current_metrics["upper_shadow"] >= effective_body * 2.0
        and current_metrics["lower_shadow"] <= max(
            effective_body * 0.75,
            current_metrics["range"] * 0.12,
        )
    )
    if long_lower_shadow:
        if single_trend == "bearish":
            add_pattern(
                "hammer",
                "bullish",
                0.60,
                1,
                single_trend,
                "Long lower rejection after a decline is a bullish reversal candidate",
            )
        elif single_trend == "bullish":
            add_pattern(
                "hanging_man",
                "bearish",
                -0.50,
                1,
                single_trend,
                "Long lower shadow after an advance warns of possible bearish reversal",
            )
        else:
            add_pattern(
                "long_lower_shadow",
                "neutral",
                0.0,
                1,
                single_trend,
                "Lower-price rejection lacks a preceding trend for reversal confirmation",
            )
    elif long_upper_shadow:
        if single_trend == "bearish":
            add_pattern(
                "inverted_hammer",
                "bullish",
                0.50,
                1,
                single_trend,
                "Upper-price rejection after a decline is a bullish reversal candidate",
            )
        elif single_trend == "bullish":
            add_pattern(
                "shooting_star",
                "bearish",
                -0.60,
                1,
                single_trend,
                "Long upper rejection after an advance is a bearish reversal candidate",
            )
        else:
            add_pattern(
                "long_upper_shadow",
                "neutral",
                0.0,
                1,
                single_trend,
                "Higher-price rejection lacks a preceding trend for reversal confirmation",
            )

    bullish_engulfing = (
        previous.close < previous.open
        and current.close > current.open
        and current_metrics["body_low"] <= previous_metrics["body_low"]
        and current_metrics["body_high"] >= previous_metrics["body_high"]
        and current_metrics["body"] > previous_metrics["body"]
    )
    bearish_engulfing = (
        previous.close > previous.open
        and current.close < current.open
        and current_metrics["body_low"] <= previous_metrics["body_low"]
        and current_metrics["body_high"] >= previous_metrics["body_high"]
        and current_metrics["body"] > previous_metrics["body"]
    )
    if bullish_engulfing:
        contribution = 0.75 if two_bar_trend == "bearish" else 0.45
        add_pattern(
            "bullish_engulfing",
            "bullish",
            contribution,
            2,
            two_bar_trend,
            "The latest bullish body fully engulfs the preceding bearish body",
        )
    elif bearish_engulfing:
        contribution = -0.75 if two_bar_trend == "bullish" else -0.45
        add_pattern(
            "bearish_engulfing",
            "bearish",
            contribution,
            2,
            two_bar_trend,
            "The latest bearish body fully engulfs the preceding bullish body",
        )
    else:
        bullish_harami = (
            previous.close < previous.open
            and current.close > current.open
            and current_metrics["body_low"] >= previous_metrics["body_low"]
            and current_metrics["body_high"] <= previous_metrics["body_high"]
            and current_metrics["body"] < previous_metrics["body"]
        )
        bearish_harami = (
            previous.close > previous.open
            and current.close < current.open
            and current_metrics["body_low"] >= previous_metrics["body_low"]
            and current_metrics["body_high"] <= previous_metrics["body_high"]
            and current_metrics["body"] < previous_metrics["body"]
        )
        if bullish_harami and two_bar_trend == "bearish":
            add_pattern(
                "bullish_harami",
                "bullish",
                0.40,
                2,
                two_bar_trend,
                "A smaller bullish body inside a bearish body suggests reversal",
            )
        elif bearish_harami and two_bar_trend == "bullish":
            add_pattern(
                "bearish_harami",
                "bearish",
                -0.40,
                2,
                two_bar_trend,
                "A smaller bearish body inside a bullish body suggests reversal",
            )

    first_large = third_metrics["body_to_range"] >= 0.50
    middle_small = previous_metrics["body"] <= third_metrics["body"] * 0.55
    third_large = current_metrics["body_to_range"] >= 0.45
    first_midpoint = (third.open + third.close) / 2.0
    if (
        three_bar_trend == "bearish"
        and third.close < third.open
        and first_large
        and middle_small
        and current.close > current.open
        and third_large
        and current.close >= first_midpoint
    ):
        add_pattern(
            "morning_star",
            "bullish",
            1.0,
            3,
            three_bar_trend,
            "A three-candle recovery after a decline supports bullish reversal",
        )
    elif (
        three_bar_trend == "bullish"
        and third.close > third.open
        and first_large
        and middle_small
        and current.close < current.open
        and third_large
        and current.close <= first_midpoint
    ):
        add_pattern(
            "evening_star",
            "bearish",
            -1.0,
            3,
            three_bar_trend,
            "A three-candle rejection after an advance supports bearish reversal",
        )

    last_three = list(bars[-3:])
    last_three_metrics = [_candle_metrics(bar) for bar in last_three]
    substantial_bodies = all(
        metrics["body_to_range"] >= 0.50 for metrics in last_three_metrics
    )
    if (
        substantial_bodies
        and all(bar.close > bar.open for bar in last_three)
        and last_three[0].close < last_three[1].close < last_three[2].close
    ):
        add_pattern(
            "three_white_soldiers",
            "bullish",
            0.75,
            3,
            three_bar_trend,
            "Three substantial rising bullish candles confirm buying pressure",
        )
    elif (
        substantial_bodies
        and all(bar.close < bar.open for bar in last_three)
        and last_three[0].close > last_three[1].close > last_three[2].close
    ):
        add_pattern(
            "three_black_crows",
            "bearish",
            -0.75,
            3,
            three_bar_trend,
            "Three substantial falling bearish candles confirm selling pressure",
        )

    if current.high <= previous.high and current.low >= previous.low:
        add_pattern(
            "inside_bar",
            "neutral",
            0.0,
            2,
            two_bar_trend,
            "The latest range is inside the prior candle and awaits a breakout",
        )

    directional_score = max(
        -1.0,
        min(1.0, sum(float(pattern["contribution"]) for pattern in patterns)),
    )
    bias = _direction_label(directional_score, 0.25)
    return {
        "bias": bias,
        "score": round(directional_score, 2),
        "maximum_absolute_score": 1.0,
        "patterns": patterns,
        "last_candle": {
            "start_at": _window_iso(current.timestamp),
            "end_at": _window_iso(
                current.timestamp + timedelta(minutes=interval_minutes)
            ),
            "direction": current_metrics["direction"],
            "body_to_range": _rounded(current_metrics["body_to_range"]),
            "upper_shadow_to_range": _rounded(
                current_metrics["upper_shadow"] / current_metrics["range"]
            ),
            "lower_shadow_to_range": _rounded(
                current_metrics["lower_shadow"] / current_metrics["range"]
            ),
        },
        "note": (
            "Pattern direction is contextual evidence, not a standalone probability"
        ),
    }


def _timeframe_analysis(
    label: str,
    bars: Sequence[IntradayBar],
    *,
    interval_minutes: int,
    session_vwap: float,
) -> dict[str, Any]:
    settings = {
        "1m": {
            "fast": 9,
            "slow": 21,
            "momentum": 5,
            "trend_lookback": 20,
            "ema_threshold": 0.015,
            "momentum_threshold": 0.04,
            "slope_threshold": 0.005,
        },
        "5m": {
            "fast": 6,
            "slow": 12,
            "momentum": 3,
            "trend_lookback": 12,
            "ema_threshold": 0.04,
            "momentum_threshold": 0.10,
            "slope_threshold": 0.02,
        },
        "15m": {
            "fast": 3,
            "slow": 5,
            "momentum": 2,
            "trend_lookback": 8,
            "ema_threshold": 0.08,
            "momentum_threshold": 0.18,
            "slope_threshold": 0.04,
        },
    }[label]
    if len(bars) < max(4, settings["slow"]):
        raise GeneratorError(
            f"need at least {max(4, settings['slow'])} completed {label} bars; "
            f"received {len(bars)}"
        )

    closes = [bar.close for bar in bars]
    fast_ema = _ema(closes, settings["fast"])
    slow_ema = _ema(closes, settings["slow"])
    ema_spread = _pct_change(fast_ema, slow_ema)
    momentum = _momentum(closes, settings["momentum"])
    rsi = _rsi(closes, 14)
    price_vs_vwap = _pct_change(closes[-1], session_vwap)
    volume_ratio = _recent_volume_ratio(bars)
    trend_line = _linear_trend_line(
        bars,
        lookback_bars=settings["trend_lookback"],
        interval_minutes=interval_minutes,
        neutral_slope_pct=settings["slope_threshold"],
    )
    candlestick = _candlestick_analysis(
        bars,
        interval_minutes=interval_minutes,
        trend_neutral_band_pct=settings["momentum_threshold"],
    )

    prior_window = list(bars[-min(settings["trend_lookback"], len(bars)) : -1])
    if not prior_window:
        prior_window = list(bars[-1:])
    support = min(bar.low for bar in prior_window)
    resistance = max(bar.high for bar in prior_window)
    reference_price = closes[-1]
    breakout = (
        "above_resistance"
        if reference_price > resistance
        else "below_support"
        if reference_price < support
        else "inside_range"
    )
    level_tolerance_pct = {"1m": 0.05, "5m": 0.10, "15m": 0.20}[label]
    candlestick["confirmation"] = {
        "near_support": abs(_pct_change(bars[-1].low, support))
        <= level_tolerance_pct,
        "near_resistance": abs(_pct_change(bars[-1].high, resistance))
        <= level_tolerance_pct,
        "price_vs_session_vwap": (
            "above" if price_vs_vwap > 0.05 else
            "below" if price_vs_vwap < -0.05 else "near"
        ),
        "volume": (
            "expanding"
            if volume_ratio is not None and volume_ratio >= 1.20
            else "contracting"
            if volume_ratio is not None and volume_ratio <= 0.80
            else "normal"
        ),
    }

    signals: list[dict[str, Any]] = []
    score = 0.0
    ema_contribution = (
        1.0
        if ema_spread > settings["ema_threshold"]
        else -1.0
        if ema_spread < -settings["ema_threshold"]
        else 0.0
    )
    score += _add_signal(
        signals,
        "ema_fast_vs_slow_pct",
        ema_spread,
        ema_contribution,
        f"{label} fast EMA relative to slow EMA",
    )
    slope_value = float(trend_line["slope_pct_per_bar"])
    slope_contribution = (
        1.0
        if slope_value > settings["slope_threshold"]
        else -1.0
        if slope_value < -settings["slope_threshold"]
        else 0.0
    )
    score += _add_signal(
        signals,
        "trend_line_slope_pct_per_bar",
        slope_value,
        slope_contribution,
        f"{label} least-squares close trend",
    )
    momentum_contribution = (
        0.75
        if momentum is not None and momentum > settings["momentum_threshold"]
        else -0.75
        if momentum is not None and momentum < -settings["momentum_threshold"]
        else 0.0
    )
    score += _add_signal(
        signals,
        "momentum_pct",
        momentum,
        momentum_contribution,
        f"{label} recent price momentum",
    )
    rsi_contribution = (
        0.5
        if rsi is not None and rsi >= 55
        else -0.5
        if rsi is not None and rsi <= 45
        else 0.0
    )
    score += _add_signal(
        signals,
        "rsi14",
        rsi,
        rsi_contribution,
        f"{label} RSI trend confirmation",
    )
    vwap_contribution = (
        0.5
        if price_vs_vwap > 0.05
        else -0.5
        if price_vs_vwap < -0.05
        else 0.0
    )
    score += _add_signal(
        signals,
        "price_vs_session_vwap_pct",
        price_vs_vwap,
        vwap_contribution,
        "Last completed one-minute close relative to session VWAP",
    )
    pattern_names = [pattern["name"] for pattern in candlestick["patterns"]]
    pattern_score = float(candlestick["score"])
    score += _add_signal(
        signals,
        "candlestick_pattern_score",
        pattern_score,
        pattern_score,
        (
            f"{label} contextual candlestick patterns: {', '.join(pattern_names)}"
            if pattern_names
            else f"No recognized {label} candlestick pattern"
        ),
    )

    bias = "bullish" if score >= 1.25 else "bearish" if score <= -1.25 else "neutral"
    volume_signal = (
        "expanding"
        if volume_ratio is not None and volume_ratio >= 1.20
        else "contracting"
        if volume_ratio is not None and volume_ratio <= 0.80
        else "normal"
    )
    return {
        "interval_minutes": interval_minutes,
        "completed_bars": len(bars),
        "bias": bias,
        "score": round(score, 2),
        "maximum_absolute_score": 4.75,
        "trend_line": trend_line,
        "candlestick_analysis": candlestick,
        "support_resistance": {
            "lookback_bars": len(prior_window),
            "support": _rounded(support),
            "resistance": _rounded(resistance),
            "price_vs_support_pct": _rounded(_pct_change(reference_price, support)),
            "price_vs_resistance_pct": _rounded(
                _pct_change(reference_price, resistance)
            ),
            "breakout_state": breakout,
        },
        "indicators": {
            "ema_fast": _rounded(fast_ema),
            "ema_slow": _rounded(slow_ema),
            "ema_fast_vs_slow_pct": _rounded(ema_spread),
            "rsi14": _rounded(rsi),
            "momentum_pct": _rounded(momentum),
            "price_vs_session_vwap_pct": _rounded(price_vs_vwap),
            "recent_volume_ratio": _rounded(volume_ratio),
            "volume_signal": volume_signal,
        },
        "signals": signals,
    }


def build_intraday_report(
    *,
    trading_date: date,
    generated_at: datetime,
    stock_rows: Sequence[Mapping[str, Any]],
    interval_minutes: int = 1,
) -> dict[str, Any]:
    if interval_minutes != 1:
        raise GeneratorError("intraday reports require one-minute source bars")
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise GeneratorError("generated_at must be timezone-aware")
    hours = market_hours(trading_date)
    if not hours.is_open or hours.open_at is None or hours.close_at is None:
        raise GeneratorError(f"{trading_date} is not a regular US equities trading day")
    generated_eastern = generated_at.astimezone(EASTERN)
    if generated_eastern.date() != trading_date:
        raise GeneratorError("intraday generated_at must resolve to trading_date")
    if not (hours.open_at + timedelta(minutes=80) <= generated_eastern < hours.close_at):
        raise GeneratorError(
            "intraday report must be generated at least 80 minutes after the open "
            "and before the close"
        )

    completed = _completed_rows(
        stock_rows,
        trading_date=trading_date,
        generated_at=generated_at,
        interval_minutes=interval_minutes,
    )
    one_minute_bars = _intraday_bars(completed, trading_date)
    if len(one_minute_bars) < 80:
        raise GeneratorError(
            "need at least 80 completed one-minute intraday bars; "
            f"received {len(one_minute_bars)}"
        )

    last_bar_end = one_minute_bars[-1].timestamp + timedelta(minutes=1)
    if generated_eastern - last_bar_end > timedelta(minutes=3):
        raise GeneratorError(
            f"latest completed bar ended at {last_bar_end.isoformat()}, too stale for "
            f"an intraday report generated at {generated_eastern.isoformat()}"
        )

    five_minute_bars = _resample_intraday_bars(
        one_minute_bars,
        interval_minutes=5,
        session_open=hours.open_at,
    )
    fifteen_minute_bars = _resample_intraday_bars(
        one_minute_bars,
        interval_minutes=15,
        session_open=hours.open_at,
    )
    if len(fifteen_minute_bars) < 5:
        raise GeneratorError(
            "need at least five complete 15-minute bars for multi-timeframe analysis; "
            f"received {len(fifteen_minute_bars)}"
        )

    daily = aggregate_stock_rows(completed)
    if not daily or daily[-1].trading_date != trading_date:
        raise GeneratorError(f"SPY intraday data does not reach {trading_date}")
    current = daily[-1]
    previous_close = daily[-2].close if len(daily) >= 2 else None
    total_volume = sum(bar.volume for bar in one_minute_bars)
    session_vwap = (
        sum(bar.vwap * max(1, bar.volume) for bar in one_minute_bars)
        / sum(max(1, bar.volume) for bar in one_minute_bars)
    )
    reference_price = one_minute_bars[-1].close
    price_vs_vwap = _pct_change(reference_price, session_vwap)
    timeframe_analysis = {
        "1m": _timeframe_analysis(
            "1m",
            one_minute_bars,
            interval_minutes=1,
            session_vwap=session_vwap,
        ),
        "5m": _timeframe_analysis(
            "5m",
            five_minute_bars,
            interval_minutes=5,
            session_vwap=session_vwap,
        ),
        "15m": _timeframe_analysis(
            "15m",
            fifteen_minute_bars,
            interval_minutes=15,
            session_vwap=session_vwap,
        ),
    }
    bullish_timeframes = [
        label
        for label, analysis in timeframe_analysis.items()
        if analysis["bias"] == "bullish"
    ]
    bearish_timeframes = [
        label
        for label, analysis in timeframe_analysis.items()
        if analysis["bias"] == "bearish"
    ]
    neutral_timeframes = [
        label
        for label, analysis in timeframe_analysis.items()
        if analysis["bias"] == "neutral"
    ]
    weights = {"1m": 0.25, "5m": 0.35, "15m": 0.40}
    consensus_score = sum(
        weights[label]
        * float(analysis["score"])
        / float(analysis["maximum_absolute_score"])
        for label, analysis in timeframe_analysis.items()
    )
    candlestick_score = sum(
        weights[label]
        * float(analysis["candlestick_analysis"]["score"])
        for label, analysis in timeframe_analysis.items()
    )
    candlestick_bias = _direction_label(candlestick_score, 0.20)
    candlestick_patterns = {
        label: [
            pattern["name"]
            for pattern in analysis["candlestick_analysis"]["patterns"]
        ]
        for label, analysis in timeframe_analysis.items()
    }
    if (
        len(bullish_timeframes) >= 2
        and timeframe_analysis["15m"]["bias"] != "bearish"
        and price_vs_vwap >= 0
    ):
        bias = "bullish"
    elif (
        len(bearish_timeframes) >= 2
        and timeframe_analysis["15m"]["bias"] != "bullish"
        and price_vs_vwap <= 0
    ):
        bias = "bearish"
    else:
        bias = "neutral"
    agreement_ratio = max(
        len(bullish_timeframes), len(bearish_timeframes), len(neutral_timeframes)
    ) / 3.0
    signal_strength = min(
        1.0,
        abs(consensus_score) * (0.75 + 0.25 * agreement_ratio),
    )
    if bias == "neutral":
        signal_strength = min(signal_strength, 0.49)
    one_minute_volume_ratio = _recent_volume_ratio(one_minute_bars)
    if one_minute_volume_ratio is not None and one_minute_volume_ratio < 0.80:
        signal_strength *= 0.85

    window_start = last_bar_end
    window_end = min(window_start + timedelta(minutes=15), hours.close_at)
    duration_minutes = int((window_end - window_start).total_seconds() // 60)
    expected_bars = int((last_bar_end - hours.open_at).total_seconds() // 60)
    completeness = min(1.0, len(one_minute_bars) / max(1, expected_bars))
    if completeness < 0.80:
        raise GeneratorError(
            f"intraday bar completeness is only {completeness:.0%}; refusing to forecast"
        )
    conflict = bool(bullish_timeframes and bearish_timeframes)
    rationale = [
        f"{label} timeframe is {analysis['bias']} with a "
        f"{analysis['trend_line']['direction']} regression trend and "
        f"{analysis['candlestick_analysis']['bias']} candlestick evidence"
        for label, analysis in timeframe_analysis.items()
    ]
    rationale.append(
        f"Price is {'above' if price_vs_vwap > 0 else 'below' if price_vs_vwap < 0 else 'at'} "
        f"session VWAP by {abs(price_vs_vwap):.2f}%"
    )
    if conflict:
        rationale.append("Timeframes conflict, so the consensus rule reduces conviction")

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
            "last_completed_bar_start": _window_iso(one_minute_bars[-1].timestamp),
            "reference_price": _rounded(reference_price),
            "session_open": _rounded(current.open),
            "session_high": _rounded(current.high),
            "session_low": _rounded(current.low),
            "session_vwap": _rounded(session_vwap),
            "session_volume_nasdaq_basic": total_volume,
            "open_to_current_pct": _rounded(_pct_change(reference_price, current.open)),
            "previous_close_to_current_pct": (
                _rounded(_pct_change(reference_price, previous_close))
                if previous_close is not None
                else None
            ),
        },
        "analysis": {
            "method": "transparent-multitimeframe-rule-score-v2",
            "consensus_score": round(consensus_score, 2),
            "timeframe_weights": weights,
            "agreement": {
                "bullish_timeframes": bullish_timeframes,
                "bearish_timeframes": bearish_timeframes,
                "neutral_timeframes": neutral_timeframes,
                "agreement_ratio": round(agreement_ratio, 2),
                "conflict": conflict,
            },
            "candlestick_consensus": {
                "bias": candlestick_bias,
                "score": round(candlestick_score, 2),
                "patterns_by_timeframe": candlestick_patterns,
                "note": (
                    "Candlestick evidence is incorporated into the timeframe scores "
                    "and is not used as a standalone forecast"
                ),
            },
            "timeframes": timeframe_analysis,
            "market_signals": [
                {
                    "name": "timeframe_consensus",
                    "value": round(consensus_score, 2),
                    "state": bias,
                },
                {
                    "name": "candlestick_consensus",
                    "value": round(candlestick_score, 2),
                    "state": candlestick_bias,
                },
                {
                    "name": "price_vs_session_vwap_pct",
                    "value": _rounded(price_vs_vwap),
                    "state": (
                        "above" if price_vs_vwap > 0.05 else
                        "below" if price_vs_vwap < -0.05 else "near"
                    ),
                },
                {
                    "name": "one_minute_volume_ratio",
                    "value": _rounded(one_minute_volume_ratio),
                    "state": (
                        "expanding"
                        if one_minute_volume_ratio is not None
                        and one_minute_volume_ratio >= 1.20
                        else "contracting"
                        if one_minute_volume_ratio is not None
                        and one_minute_volume_ratio <= 0.80
                        else "normal"
                    ),
                },
            ],
            "indicators": {
                "price_vs_session_vwap_pct": _rounded(price_vs_vwap),
            },
        },
        "forecast": {
            "is_directional_forecast": True,
            "bias": bias,
            "signal_strength": round(signal_strength, 2),
            "calibrated_probability": None,
            "horizon": "next_15_minutes",
            "window_start": _window_iso(window_start),
            "window_end": _window_iso(window_end),
            "duration_minutes": duration_minutes,
            "target": "15-minute window-end SPY price versus reference_price",
            "reference_price": _rounded(reference_price),
            "rationale": rationale,
        },
        "data_quality": {
            "feed": "Nasdaq Basic",
            "coverage": "not consolidated UTP/CTA",
            "source_interval_minutes": 1,
            "completed_bars": {
                "1m": len(one_minute_bars),
                "5m": len(five_minute_bars),
                "15m": len(fifteen_minute_bars),
            },
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
    interval_minutes: int = 1,
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
    previous_close = stock[-2].close if len(stock) >= 2 else None
    close_to_close_return = (
        _pct_change(latest.close, previous_close)
        if previous_close is not None
        else session_return
    )
    session_direction = _direction_label(close_to_close_return)

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
            "calibrated_probability": None,
            "horizon": "none",
            "target": None,
            "rationale": [
                "Postmarket reports are descriptive; use session_direction and "
                "trend_regime instead of interpreting this object as a forecast"
            ],
        }

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
        **(
            {
                "session_direction": {
                    "bias": session_direction,
                    "basis": "previous_close_to_close",
                    "return_pct": _rounded(close_to_close_return),
                    "open_to_close_pct": _rounded(session_return),
                },
                "trend_regime": {
                    "bias": bias,
                    "signal_strength": round(strength, 2),
                    "score": round(score, 2),
                    "maximum_absolute_score": 4.5,
                    "lookback_sessions": min(20, len(stock)),
                    "rationale": rationale,
                },
            }
            if session == "postmarket"
            else {}
        ),
        "market": {
            "as_of": latest.last_timestamp,
            "open": _rounded(latest.open),
            "high": _rounded(latest.high),
            "low": _rounded(latest.low),
            "close": _rounded(latest.close),
            "vwap": _rounded(latest.vwap),
            "volume": latest.volume,
            "intraday_return_pct": _rounded(session_return),
            "previous_close_to_close_pct": _rounded(close_to_close_return),
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
    interval: Optional[str],
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
    if session == "intraday":
        if interval is not None and interval != "1m":
            raise GeneratorError("intraday reports require --interval 1m")
        previous_day = _previous_trading_day(trading_date)
        LOGGER.info(
            "requesting SPY previous-session data for %s and one-minute data for %s",
            previous_day,
            trading_date,
        )
        stock_rows = client.stock_ohlc(
            "SPY", previous_day, previous_day, interval="5m", venue=venue
        )
        stock_rows += client.stock_ohlc(
            "SPY", trading_date, trading_date, interval="1m", venue=venue
        )
        interval_minutes = 1
    else:
        requested_interval = interval or "5m"
        data_end = (
            _previous_trading_day(trading_date)
            if session == "premarket"
            else trading_date
        )
        data_start = data_end - timedelta(days=lookback_calendar_days - 1)
        LOGGER.info("requesting SPY data from %s through %s", data_start, data_end)
        stock_rows = client.stock_ohlc(
            "SPY",
            data_start,
            data_end,
            interval=requested_interval,
            venue=venue,
        )
        interval_minutes = 5
    generated = generated_at or datetime.now(timezone.utc)
    return build_report(
        session=session,
        trading_date=trading_date,
        generated_at=generated,
        stock_rows=stock_rows,
        interval_minutes=interval_minutes,
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
    parser.add_argument(
        "--interval",
        choices=("1m", "5m", "15m", "1h"),
        help="optional override; intraday reports require 1m (the default for that session)",
    )
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
        if args.session == "postmarket":
            LOGGER.info(
                "wrote %s postmarket report to %s "
                "(session_direction=%s trend_regime=%s trend_signal_strength=%s)",
                args.trading_date,
                args.output,
                report["session_direction"]["bias"],
                report["trend_regime"]["bias"],
                report["trend_regime"]["signal_strength"],
            )
        else:
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

