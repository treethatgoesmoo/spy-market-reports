from __future__ import annotations

import unittest
from datetime import datetime, time, timezone
from pathlib import Path

from schedule_runner import RunnerConfig, should_run
from spy_report_publisher import PublisherConfig


def config() -> RunnerConfig:
    return RunnerConfig(
        publisher=PublisherConfig(repo_dir=Path("/unused")),
        input_dir=Path("/unused"),
        generator_working_dir=Path("/unused"),
        generator_commands={"premarket": "true", "postmarket": "true"},
        generator_timeout_seconds=60,
        due_tolerance_minutes=20,
        premarket_time=time(8, 45),
        log_file=None,
    )


class ScheduleRunnerTests(unittest.TestCase):
    def test_premarket_due(self) -> None:
        due, _, trading_date = should_run(
            "premarket",
            config(),
            now=datetime(2026, 11, 6, 13, 45, tzinfo=timezone.utc),
        )
        self.assertTrue(due)
        self.assertEqual(trading_date.isoformat(), "2026-11-06")

    def test_early_close_postmarket_due_at_1330_et(self) -> None:
        due, _, _ = should_run(
            "postmarket",
            config(),
            now=datetime(2026, 11, 27, 18, 30, tzinfo=timezone.utc),
        )
        self.assertTrue(due)

    def test_regular_day_skips_early_timer(self) -> None:
        due, reason, _ = should_run(
            "postmarket",
            config(),
            now=datetime(2026, 11, 6, 18, 30, tzinfo=timezone.utc),
        )
        self.assertFalse(due)
        self.assertIn("not due", reason)

    def test_holiday_skips(self) -> None:
        due, reason, _ = should_run(
            "premarket",
            config(),
            now=datetime(2026, 11, 26, 13, 45, tzinfo=timezone.utc),
        )
        self.assertFalse(due)
        self.assertIn("not open", reason)


if __name__ == "__main__":
    unittest.main()

