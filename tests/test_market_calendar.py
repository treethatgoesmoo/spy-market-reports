from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, time
from pathlib import Path

from market_calendar import (
    CalendarError,
    load_overrides,
    market_hours,
    nyse_early_closes,
    nyse_holidays,
)


class MarketCalendarTests(unittest.TestCase):
    def test_2026_holidays_match_key_nyse_dates(self) -> None:
        holidays = nyse_holidays(2026)
        self.assertIn(date(2026, 1, 1), holidays)
        self.assertIn(date(2026, 4, 3), holidays)  # Good Friday
        self.assertIn(date(2026, 7, 3), holidays)  # Independence Day observed
        self.assertIn(date(2026, 11, 26), holidays)
        self.assertIn(date(2026, 12, 25), holidays)

    def test_2026_early_closes(self) -> None:
        early = nyse_early_closes(2026)
        self.assertNotIn(date(2026, 7, 2), early)
        self.assertIn(date(2026, 11, 27), early)
        self.assertIn(date(2026, 12, 24), early)
        hours = market_hours(date(2026, 11, 27))
        self.assertEqual(hours.close_at.time(), time(13, 0))

    def test_july_3_early_close_when_it_is_a_trading_day(self) -> None:
        self.assertIn(date(2028, 7, 3), nyse_early_closes(2028))

    def test_regular_and_closed_sessions(self) -> None:
        self.assertTrue(market_hours(date(2026, 11, 6)).is_open)
        self.assertFalse(market_hours(date(2026, 11, 26)).is_open)
        self.assertFalse(market_hours(date(2026, 11, 7)).is_open)

    def test_operator_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "overrides.json"
            path.write_text(
                json.dumps(
                    {
                        "closed": ["2026-11-06"],
                        "open": [],
                        "early_close": {"2026-11-09": "14:00"},
                    }
                ),
                encoding="utf-8",
            )
            overrides = load_overrides(path)
            self.assertFalse(market_hours(date(2026, 11, 6), overrides).is_open)
            self.assertEqual(
                market_hours(date(2026, 11, 9), overrides).close_at.time(),
                time(14, 0),
            )

    def test_bad_override_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "overrides.json"
            path.write_text('{"surprise": []}', encoding="utf-8")
            with self.assertRaises(CalendarError):
                load_overrides(path)


if __name__ == "__main__":
    unittest.main()

