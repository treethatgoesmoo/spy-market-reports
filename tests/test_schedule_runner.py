from __future__ import annotations

import unittest
import tempfile
from datetime import datetime, time, timezone
from pathlib import Path

from schedule_runner import RunnerConfig, load_config, should_run
from spy_report_publisher import PublisherConfig


def config() -> RunnerConfig:
    return RunnerConfig(
        publisher=PublisherConfig(repo_dir=Path("/unused")),
        input_dir=Path("/unused"),
        generator_working_dir=Path("/unused"),
        generator_commands={
            "premarket": "true",
            "intraday": "true",
            "postmarket": "true",
        },
        generator_timeout_seconds=60,
        due_tolerance_minutes=20,
        premarket_time=time(8, 45),
        log_file=None,
    )


class ScheduleRunnerTests(unittest.TestCase):
    def test_existing_config_without_intraday_command_uses_postmarket_command(self) -> None:
        contents = """\
[publisher]
repo_dir = /srv/spy-market-reports
[generator]
working_directory = /opt/spy-report-publisher
input_dir = /var/lib/spy-report-publisher/incoming
premarket_command = generator --session {session}
postmarket_command = generator --session {session}
[schedule]
premarket_time = 08:45
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "publisher.ini"
            path.write_text(contents, encoding="utf-8")
            loaded = load_config(path)
        self.assertEqual(
            loaded.generator_commands["intraday"],
            loaded.generator_commands["postmarket"],
        )

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

    def test_intraday_hourly_slot_is_due(self) -> None:
        due, reason, trading_date = should_run(
            "intraday",
            config(),
            now=datetime(2026, 11, 9, 15, 35, tzinfo=timezone.utc),
        )
        self.assertTrue(due)
        self.assertIn("10:35", reason)
        self.assertEqual(trading_date.isoformat(), "2026-11-09")

    def test_intraday_is_not_due_before_first_completed_hour(self) -> None:
        due, reason, _ = should_run(
            "intraday",
            config(),
            now=datetime(2026, 11, 9, 15, 20, tzinfo=timezone.utc),
        )
        self.assertFalse(due)
        self.assertIn("not due until", reason)

    def test_intraday_early_close_has_no_1335_slot(self) -> None:
        due, reason, _ = should_run(
            "intraday",
            config(),
            now=datetime(2026, 11, 27, 18, 35, tzinfo=timezone.utc),
        )
        self.assertFalse(due)
        self.assertIn("window ended", reason)

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

