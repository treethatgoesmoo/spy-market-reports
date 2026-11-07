from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from spy_report_publisher import ValidationError, validate_report


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ValidationTests(unittest.TestCase):
    def test_valid_premarket(self) -> None:
        metadata = validate_report(
            fixture("valid_premarket.json"),
            expected_session="premarket",
            now=datetime(2026, 11, 6, 13, 50, tzinfo=timezone.utc),
        )
        self.assertEqual(metadata.session, "premarket")

    def test_valid_regular_postmarket(self) -> None:
        validate_report(
            fixture("valid_postmarket.json"),
            expected_session="postmarket",
            now=datetime(2026, 11, 6, 21, 35, tzinfo=timezone.utc),
        )

    def test_valid_intraday_next_hour(self) -> None:
        metadata = validate_report(
            fixture("valid_intraday.json"),
            expected_session="intraday",
            now=datetime(2026, 11, 9, 15, 36, tzinfo=timezone.utc),
        )
        self.assertEqual(metadata.session, "intraday")

    def test_intraday_rejects_probability_like_confidence(self) -> None:
        report = deepcopy(fixture("valid_intraday.json"))
        report["forecast"]["confidence"] = 0.80
        with self.assertRaisesRegex(ValidationError, "signal_strength"):
            validate_report(
                report,
                expected_session="intraday",
                now=datetime(2026, 11, 9, 15, 36, tzinfo=timezone.utc),
            )

    def test_intraday_window_cannot_cross_close(self) -> None:
        report = deepcopy(fixture("valid_intraday.json"))
        report["generated_at"] = "2026-11-09T20:35:00Z"
        report["forecast"]["window_start"] = "2026-11-09T15:35:00-05:00"
        report["forecast"]["window_end"] = "2026-11-09T16:35:00-05:00"
        with self.assertRaisesRegex(ValidationError, "end by the close"):
            validate_report(
                report,
                expected_session="intraday",
                now=datetime(2026, 11, 9, 20, 36, tzinfo=timezone.utc),
            )

    def test_late_postmarket_requires_explicit_same_day_backfill(self) -> None:
        report = deepcopy(fixture("valid_postmarket.json"))
        report["generated_at"] = "2026-11-07T02:34:00Z"
        now = datetime(2026, 11, 7, 2, 35, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValidationError, "no later than 20:00 ET"):
            validate_report(
                report,
                expected_session="postmarket",
                now=now,
            )
        metadata = validate_report(
            report,
            expected_session="postmarket",
            now=now,
            allow_same_day_postmarket_backfill=True,
        )
        self.assertEqual(metadata.session, "postmarket")

    def test_same_day_backfill_does_not_allow_next_day_label_mismatch(self) -> None:
        report = deepcopy(fixture("valid_postmarket.json"))
        report["generated_at"] = "2026-11-07T05:01:00Z"
        with self.assertRaisesRegex(ValidationError, "not trading_date"):
            validate_report(
                report,
                expected_session="postmarket",
                now=datetime(2026, 11, 7, 5, 2, tzinfo=timezone.utc),
                allow_same_day_postmarket_backfill=True,
            )

    def test_valid_early_close_postmarket(self) -> None:
        validate_report(
            fixture("valid_early_close_postmarket.json"),
            expected_session="postmarket",
            now=datetime(2026, 11, 27, 18, 35, tzinfo=timezone.utc),
        )

    def test_test_data_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "test/non-live"):
            validate_report(
                fixture("reject_test_data.json"),
                expected_session="premarket",
                now=datetime(2026, 11, 6, 13, 50, tzinfo=timezone.utc),
            )

    def test_mislabeled_session_date_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "not trading_date"):
            validate_report(
                fixture("reject_mislabeled_session.json"),
                expected_session="postmarket",
                now=datetime(2026, 11, 6, 5, 20, tzinfo=timezone.utc),
                max_age_minutes=180,
            )

    def test_expected_session_must_match(self) -> None:
        with self.assertRaisesRegex(ValidationError, "does not match"):
            validate_report(
                fixture("valid_premarket.json"),
                expected_session="postmarket",
                now=datetime(2026, 11, 6, 13, 50, tzinfo=timezone.utc),
            )

    def test_holiday_label_is_rejected(self) -> None:
        report = deepcopy(fixture("valid_premarket.json"))
        report["trading_date"] = "2026-11-26"
        report["generated_at"] = "2026-11-26T13:45:00Z"
        with self.assertRaisesRegex(ValidationError, "not a trading session"):
            validate_report(
                report,
                expected_session="premarket",
                now=datetime(2026, 11, 26, 13, 50, tzinfo=timezone.utc),
            )

    def test_stale_and_future_reports_are_rejected(self) -> None:
        report = fixture("valid_premarket.json")
        with self.assertRaisesRegex(ValidationError, "stale"):
            validate_report(
                report,
                expected_session="premarket",
                now=datetime(2026, 11, 6, 16, 0, tzinfo=timezone.utc),
            )
        with self.assertRaisesRegex(ValidationError, "future"):
            validate_report(
                report,
                expected_session="premarket",
                now=datetime(2026, 11, 6, 13, 30, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()

