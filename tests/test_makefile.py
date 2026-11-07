from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"


class MakefileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = MAKEFILE.read_text(encoding="utf-8")

    def test_expected_setup_targets_exist(self) -> None:
        targets = set(
            re.findall(r"^([a-zA-Z0-9_.-]+):(?:\s|$)", self.text, flags=re.MULTILINE)
        )
        self.assertTrue(
            {
                "bootstrap",
                "configure-generator",
                "check-thetadata",
                "finish",
                "test",
                "update",
                "disable-timers",
                "clear-failures",
            }.issubset(targets)
        )

    def test_github_host_key_is_pinned(self) -> None:
        self.assertIn(
            "github.com ssh-ed25519 "
            "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
            self.text,
        )

    def test_setup_does_not_contain_recursive_delete(self) -> None:
        self.assertNotIn("rm -rf", self.text)
        self.assertNotIn("git reset --hard", self.text)

    def test_timers_are_not_enabled_during_bootstrap(self) -> None:
        bootstrap = re.search(r"^bootstrap:([^\n]+)", self.text, flags=re.MULTILINE)
        self.assertIsNotNone(bootstrap)
        self.assertNotIn("enable-timers", bootstrap.group(1))

    def test_intraday_timer_is_installed_and_managed(self) -> None:
        self.assertIn("spy-report-publisher-intraday.timer", self.text)
        timer = ROOT / "systemd" / "spy-report-publisher-intraday.timer"
        self.assertTrue(timer.is_file())
        timer_text = timer.read_text(encoding="utf-8")
        self.assertIn("10..15:35:00 America/New_York", timer_text)
        self.assertIn("spy-report-publisher@intraday.service", timer_text)

    def test_status_never_opens_a_pager_and_failures_can_be_cleared(self) -> None:
        self.assertIn(
            "systemctl list-timers 'spy-report-publisher*' --all --no-pager",
            self.text,
        )
        self.assertIn("clear-failures:", self.text)
        self.assertIn("systemctl reset-failed", self.text)


if __name__ == "__main__":
    unittest.main()

