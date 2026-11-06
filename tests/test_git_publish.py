from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from spy_report_publisher import PublisherConfig, ValidationError, publish_report


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({completed.returncode}): "
            f"{completed.stderr or completed.stdout}"
        )
    return completed.stdout.strip()


@unittest.skipUnless(shutil.which("git"), "git is required")
class GitPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        test_temp_root = ROOT / ".test-tmp"
        test_temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_temp_root)
        self.base = Path(self.temp.name)
        self.remote = self.base / "remote.git"
        self.repo = self.base / "work"
        self.input = self.base / "input"
        self.input.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(self.remote)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        (self.repo / "latest.json").write_text(
            json.dumps(
                {
                    "symbol": "SPY",
                    "status": "test",
                    "message": "Integration test only. No live market data.",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        git(self.repo, "add", "latest.json")
        git(
            self.repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "initial",
        )
        git(self.repo, "remote", "add", "origin", self.remote.as_uri())
        self.can_test_local_push = os.name != "nt"
        if self.can_test_local_push:
            git(self.repo, "push", "-u", "origin", "main")
        self.config = PublisherConfig(
            repo_dir=self.repo,
            expected_repository=None,
            push_retries=0,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def copy_fixture(self, name: str) -> Path:
        target = self.input / name
        shutil.copy2(FIXTURES / name, target)
        return target

    def test_publish_push_and_duplicate_detection(self) -> None:
        report = self.copy_fixture("valid_premarket.json")
        now = datetime(2026, 11, 6, 13, 50, tzinfo=timezone.utc)
        first = publish_report(
            report,
            "premarket",
            self.config,
            now=now,
            push=self.can_test_local_push,
        )
        self.assertEqual(first.outcome, "published")
        self.assertTrue((self.repo / "reports" / "20261106.json").is_file())
        self.assertEqual(
            (self.repo / "reports" / "20261106.json").read_bytes(),
            (self.repo / "latest.json").read_bytes(),
        )
        if self.can_test_local_push:
            remote_head = git(self.repo, "rev-parse", "origin/main")
            self.assertEqual(first.commit, remote_head)
        else:
            self.assertEqual(first.commit, git(self.repo, "rev-parse", "HEAD"))

        count_before = git(self.repo, "rev-list", "--count", "HEAD")
        second = publish_report(
            report,
            "premarket",
            self.config,
            now=now,
            push=self.can_test_local_push,
        )
        self.assertEqual(second.outcome, "duplicate")
        self.assertEqual(count_before, git(self.repo, "rev-list", "--count", "HEAD"))

        # Whitespace/key-order changes are also a duplicate, not a new report.
        latest_path = self.repo / "latest.json"
        same_value = json.loads(latest_path.read_text(encoding="utf-8"))
        latest_path.write_text(
            json.dumps(same_value, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        git(self.repo, "add", "latest.json")
        git(
            self.repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "reformat latest",
        )
        if self.can_test_local_push:
            git(self.repo, "push", "origin", "main")
        semantic_count = git(self.repo, "rev-list", "--count", "HEAD")
        third = publish_report(
            report,
            "premarket",
            self.config,
            now=now,
            push=self.can_test_local_push,
        )
        self.assertEqual(third.outcome, "duplicate")
        self.assertEqual(semantic_count, git(self.repo, "rev-list", "--count", "HEAD"))

    def test_postmarket_replaces_same_day_and_premarket_cannot_regress(self) -> None:
        pre = self.copy_fixture("valid_premarket.json")
        post = self.copy_fixture("valid_postmarket.json")
        publish_report(
            pre,
            "premarket",
            self.config,
            now=datetime(2026, 11, 6, 13, 50, tzinfo=timezone.utc),
            push=self.can_test_local_push,
        )
        result = publish_report(
            post,
            "postmarket",
            self.config,
            now=datetime(2026, 11, 6, 21, 35, tzinfo=timezone.utc),
            push=self.can_test_local_push,
        )
        self.assertEqual(result.outcome, "published")
        latest = json.loads((self.repo / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["session"], "postmarket")

        with self.assertRaisesRegex(ValidationError, "same version or newer"):
            publish_report(
                pre,
                "premarket",
                self.config,
                now=datetime(2026, 11, 6, 13, 50, tzinfo=timezone.utc),
                push=self.can_test_local_push,
            )


if __name__ == "__main__":
    unittest.main()

