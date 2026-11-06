from __future__ import annotations

import json
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from market_calendar import market_hours
from spy_report_publisher import validate_report
from thetadata_report_generator import (
    GeneratorError,
    ThetaDataClient,
    build_report,
    health_check,
    write_report,
)


ROOT = Path(__file__).resolve().parents[1]


def trading_days_ending(end: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if market_hours(cursor).is_open:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def stock_rows(days: list[date]) -> list[dict]:
    rows: list[dict] = []
    for index, day in enumerate(days):
        base = 500.0 + index * 1.2
        rows.extend(
            [
                {
                    "timestamp": f"{day.isoformat()}T09:30:00.000",
                    "open": base,
                    "high": base + 0.8,
                    "low": base - 0.4,
                    "close": base + 0.5,
                    "volume": 1_000_000 + index * 10_000,
                    "count": 1000,
                    "vwap": base + 0.3,
                },
                {
                    "timestamp": f"{day.isoformat()}T15:55:00.000",
                    "open": base + 0.5,
                    "high": base + 1.4,
                    "low": base + 0.2,
                    "close": base + 1.2,
                    "volume": 1_200_000 + index * 10_000,
                    "count": 1200,
                    "vwap": base + 0.9,
                },
            ]
        )
    return rows


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": str(len(self.payload))}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


class GeneratorTests(unittest.TestCase):
    def test_builds_publishable_premarket_report(self) -> None:
        days = trading_days_ending(date(2026, 11, 5), 22)
        generated = datetime(2026, 11, 6, 13, 45, tzinfo=timezone.utc)
        report = build_report(
            session="premarket",
            trading_date=date(2026, 11, 6),
            generated_at=generated,
            stock_rows=stock_rows(days),
        )
        self.assertEqual(report["status"], "live")
        self.assertEqual(report["data_through"], "2026-11-05")
        self.assertEqual(report["forecast"]["bias"], "bullish")
        self.assertEqual(report["data_quality"]["inputs"], ["SPY stock OHLC"])
        self.assertNotIn("indices", report)
        self.assertFalse(
            any(
                signal["name"].startswith(("spx_", "vix_"))
                for signal in report["analysis"]["signals"]
            )
        )
        validate_report(
            report,
            expected_session="premarket",
            now=generated + timedelta(minutes=1),
        )

    def test_builds_publishable_postmarket_report(self) -> None:
        days = trading_days_ending(date(2026, 11, 6), 22)
        generated = datetime(2026, 11, 6, 21, 30, tzinfo=timezone.utc)
        report = build_report(
            session="postmarket",
            trading_date=date(2026, 11, 6),
            generated_at=generated,
            stock_rows=stock_rows(days),
        )
        self.assertEqual(report["data_through"], "2026-11-06")
        validate_report(
            report,
            expected_session="postmarket",
            now=generated + timedelta(minutes=1),
        )

    def test_client_uses_documented_v3_endpoint_and_json_format(self) -> None:
        captured: list[str] = []

        def opener(request, timeout):
            captured.append(request.full_url)
            return FakeResponse(
                {
                    "response": [
                        {
                            "timestamp": "2026-11-05T15:55:00.000",
                            "open": 500,
                            "high": 501,
                            "low": 499,
                            "close": 500.5,
                            "volume": 100,
                            "count": 10,
                            "vwap": 500.2,
                        }
                    ]
                }
            )

        client = ThetaDataClient(opener=opener, retries=0)
        rows = client.stock_ohlc(
            "SPY", date(2026, 11, 5), date(2026, 11, 5), interval="5m"
        )
        self.assertEqual(len(rows), 1)
        parsed = urllib.parse.urlsplit(captured[0])
        self.assertEqual(parsed.path, "/v3/stock/history/ohlc")
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["symbol"], ["SPY"])
        self.assertEqual(query["format"], ["json"])

    def test_health_check_requests_only_spy_stock_data(self) -> None:
        captured: list[str] = []

        def opener(request, timeout):
            captured.append(request.full_url)
            return FakeResponse({"response": [{"timestamp": "2026-11-05T15:00:00.000"}]})

        result = health_check(ThetaDataClient(opener=opener, retries=0))
        self.assertTrue(result["ok"])
        self.assertEqual(result["data_scope"], "SPY stock OHLC only")
        self.assertEqual(len(captured), 1)
        self.assertEqual(
            urllib.parse.urlsplit(captured[0]).path,
            "/v3/stock/history/ohlc",
        )

    def test_remote_thetadata_url_is_rejected_by_default(self) -> None:
        with self.assertRaisesRegex(GeneratorError, "loopback"):
            ThetaDataClient("http://example.com:25503/v3")

    def test_atomic_report_write(self) -> None:
        days = trading_days_ending(date(2026, 11, 5), 22)
        report = build_report(
            session="premarket",
            trading_date=date(2026, 11, 6),
            generated_at=datetime(2026, 11, 6, 13, 45, tzinfo=timezone.utc),
            stock_rows=stock_rows(days),
        )
        test_temp_root = ROOT / ".test-tmp"
        test_temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_temp_root) as temp:
            output = Path(temp) / "report.json"
            write_report(output, report)
            loaded = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(loaded["forecast"]["bias"], "bullish")


if __name__ == "__main__":
    unittest.main()

