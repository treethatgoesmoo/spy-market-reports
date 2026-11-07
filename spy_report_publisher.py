#!/usr/bin/env python3
"""Validate, version, commit, and push SPY market reports.

This module intentionally uses only the Python standard library.  It is
designed for an unattended Raspberry Pi and treats validation failures as a
hard stop before any repository file is changed.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from market_calendar import CalendarOverrides, EASTERN, load_overrides, market_hours


LOGGER = logging.getLogger("spy_report_publisher")
SESSION_RANK = {"premarket": 0, "intraday": 1, "postmarket": 2}
TEST_VALUES = {
    "test",
    "testing",
    "mock",
    "demo",
    "sample",
    "synthetic",
    "dry-run",
    "dry_run",
    "integration-test",
    "integration_test",
    "paper",
    "paper-trading",
    "paper_trading",
}
TEST_PHRASES = (
    "no live market data",
    "test data",
    "test only",
    "integration test",
    "mock data",
    "sample data",
    "synthetic data",
    "paper trading",
)
MODE_KEYS = {
    "status",
    "mode",
    "environment",
    "data_mode",
    "data_status",
    "run_mode",
}
BOOLEAN_TEST_KEYS = {"test", "is_test", "test_data", "dry_run", "mock", "synthetic"}


class PublisherError(RuntimeError):
    """Base class for expected publishing errors."""


class ValidationError(PublisherError):
    """The report is not safe to publish."""


class GitError(PublisherError):
    """A Git precondition or operation failed."""


class DuplicateReport(PublisherError):
    """Internal no-op signal; exposed as a successful result."""


@dataclass(frozen=True)
class ReportMetadata:
    trading_date: date
    session: str
    generated_at: datetime
    status: str


@dataclass(frozen=True)
class PublisherConfig:
    repo_dir: Path
    expected_repository: Optional[str] = "treethatgoesmoo/spy-market-reports"
    remote: str = "origin"
    branch: str = "main"
    max_age_minutes: int = 120
    clock_skew_minutes: int = 5
    postmarket_tolerance_minutes: int = 5
    allow_same_day_postmarket_backfill: bool = False
    overrides_file: Optional[Path] = None
    git_author_name: str = "SPY Report Publisher"
    git_author_email: str = "spy-report-publisher@localhost"
    push_retries: int = 2


@dataclass(frozen=True)
class PublishResult:
    outcome: str
    report_path: str
    commit: Optional[str]
    digest: str
    message: str

    def as_dict(self) -> dict[str, Optional[str]]:
        return {
            "outcome": self.outcome,
            "report_path": self.report_path,
            "commit": self.commit,
            "sha256": self.digest,
            "message": self.message,
        }


def configure_logging(log_file: Optional[Path], verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=2_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        handlers.append(file_handler)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    for handler in handlers:
        handler.setLevel(level)
        handler.setFormatter(formatter)
        root.addHandler(handler)


def _parse_generated_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("generated_at must be a non-empty ISO-8601 string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"generated_at is not valid ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("generated_at must include a UTC offset or Z")
    return parsed.astimezone(timezone.utc)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise ValidationError(f"report file not found: {path}") from exc
    if size == 0:
        raise ValidationError(f"report file is empty: {path}")
    if size > 10 * 1024 * 1024:
        raise ValidationError("report exceeds the 10 MiB safety limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ValidationError("report must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"report is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError("report root must be a JSON object")
    return value


def _walk_json(value: Any, path: str = "$") -> Iterator[tuple[str, Optional[str], Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            yield child_path, key_text.casefold(), child
            yield from _walk_json(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            yield child_path, None, child
            yield from _walk_json(child, child_path)


def _reject_test_markers(report: Mapping[str, Any]) -> None:
    for path, key, value in _walk_json(report):
        if key in BOOLEAN_TEST_KEYS and value is True:
            raise ValidationError(f"test marker {path}=true is not publishable")
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if key in MODE_KEYS and normalized in TEST_VALUES:
                raise ValidationError(f"test/non-live marker at {path}: {value!r}")
            if any(phrase in normalized for phrase in TEST_PHRASES):
                raise ValidationError(f"test/non-live phrase at {path}: {value!r}")


def extract_metadata(report: Mapping[str, Any]) -> ReportMetadata:
    symbol = report.get("symbol")
    if not isinstance(symbol, str) or symbol.strip().upper() != "SPY":
        raise ValidationError("symbol must be exactly SPY (case-insensitive)")

    status = report.get("status")
    if not isinstance(status, str) or not status.strip():
        raise ValidationError("status must be a non-empty string")
    _reject_test_markers(report)
    if status.strip().casefold() != "live":
        raise ValidationError("status must be exactly 'live' for publication")

    market_timezone = report.get("market_timezone")
    if market_timezone != "America/New_York":
        raise ValidationError("market_timezone must be 'America/New_York'")

    session = report.get("session")
    if session not in SESSION_RANK:
        raise ValidationError("session must be 'premarket', 'intraday', or 'postmarket'")

    trading_date_raw = report.get("trading_date")
    if not isinstance(trading_date_raw, str):
        raise ValidationError("trading_date must be an ISO date string")
    try:
        trading_date = date.fromisoformat(trading_date_raw)
    except ValueError as exc:
        raise ValidationError(f"trading_date is invalid: {trading_date_raw!r}") from exc
    if trading_date_raw != trading_date.isoformat():
        raise ValidationError("trading_date must use YYYY-MM-DD format")

    generated_at = _parse_generated_at(report.get("generated_at"))
    return ReportMetadata(trading_date, session, generated_at, status.strip())


def validate_report(
    report: Mapping[str, Any],
    *,
    expected_session: str,
    now: Optional[datetime] = None,
    overrides: Optional[CalendarOverrides] = None,
    max_age_minutes: int = 120,
    clock_skew_minutes: int = 5,
    postmarket_tolerance_minutes: int = 5,
    allow_same_day_postmarket_backfill: bool = False,
) -> ReportMetadata:
    if expected_session not in SESSION_RANK:
        raise ValidationError(f"unsupported expected session: {expected_session!r}")
    if max_age_minutes <= 0:
        raise ValidationError("max_age_minutes must be positive")

    metadata = extract_metadata(report)
    if metadata.session != expected_session:
        raise ValidationError(
            f"report session {metadata.session!r} does not match scheduled session "
            f"{expected_session!r}"
        )

    hours = market_hours(metadata.trading_date, overrides)
    if not hours.is_open or hours.open_at is None or hours.close_at is None:
        raise ValidationError(
            f"{metadata.trading_date} is not a trading session ({hours.reason})"
        )

    generated_eastern = metadata.generated_at.astimezone(EASTERN)
    if generated_eastern.date() != metadata.trading_date:
        raise ValidationError(
            "generated_at resolves to "
            f"{generated_eastern.date()} in America/New_York, not trading_date "
            f"{metadata.trading_date}"
        )

    if metadata.session == "premarket":
        earliest = datetime.combine(metadata.trading_date, time(4, 0), tzinfo=EASTERN)
        if not (earliest <= generated_eastern < hours.open_at):
            raise ValidationError(
                "premarket generated_at must be between 04:00 ET and the 09:30 ET open"
            )
    elif metadata.session == "intraday":
        earliest = hours.open_at + timedelta(minutes=60)
        if not (earliest <= generated_eastern < hours.close_at):
            raise ValidationError(
                "intraday generated_at must be at least 60 minutes after the open "
                "and before the close"
            )
        forecast = report.get("forecast")
        if not isinstance(forecast, dict):
            raise ValidationError("intraday forecast must be an object")
        if forecast.get("is_directional_forecast") is not True:
            raise ValidationError("intraday forecast must be explicitly directional")
        if forecast.get("horizon") != "next_market_interval":
            raise ValidationError("intraday forecast horizon must be next_market_interval")
        if "confidence" in forecast:
            raise ValidationError(
                "intraday forecast must use signal_strength, not uncalibrated confidence"
            )
        strength = forecast.get("signal_strength")
        if (
            isinstance(strength, bool)
            or not isinstance(strength, (int, float))
            or not 0 <= float(strength) <= 1
        ):
            raise ValidationError("intraday signal_strength must be between 0 and 1")
        if forecast.get("calibrated_probability") is not None:
            raise ValidationError(
                "intraday calibrated_probability must be null until a calibrated model exists"
            )
        window_start = _parse_generated_at(forecast.get("window_start"))
        window_end = _parse_generated_at(forecast.get("window_end"))
        window_start_eastern = window_start.astimezone(EASTERN)
        window_end_eastern = window_end.astimezone(EASTERN)
        if abs(window_start - metadata.generated_at) > timedelta(minutes=5):
            raise ValidationError(
                "intraday forecast window_start must be within five minutes of generated_at"
            )
        if not (
            window_start_eastern.date() == metadata.trading_date
            and window_start_eastern < window_end_eastern <= hours.close_at
            and window_end_eastern - window_start_eastern <= timedelta(hours=1)
        ):
            raise ValidationError(
                "intraday forecast window must remain on trading_date, end by the close, "
                "and be no longer than 60 minutes"
            )
        duration = forecast.get("duration_minutes")
        actual_minutes = int((window_end - window_start).total_seconds() // 60)
        if isinstance(duration, bool) or not isinstance(duration, int) or duration != actual_minutes:
            raise ValidationError("intraday duration_minutes does not match its forecast window")
    else:
        earliest = hours.close_at + timedelta(
            minutes=max(0, 30 - postmarket_tolerance_minutes)
        )
        latest_time = time.max if allow_same_day_postmarket_backfill else time(20, 0)
        latest = datetime.combine(metadata.trading_date, latest_time, tzinfo=EASTERN)
        if not (earliest <= generated_eastern <= latest):
            latest_label = (
                "the end of the labeled trading date"
                if allow_same_day_postmarket_backfill
                else "20:00 ET"
            )
            raise ValidationError(
                "postmarket generated_at must be at least approximately 30 minutes after "
                f"the {hours.close_at:%H:%M} ET close and no later than {latest_label}"
            )

    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ValidationError("now must be timezone-aware")
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = now_utc - metadata.generated_at
    if age < -timedelta(minutes=clock_skew_minutes):
        raise ValidationError(
            f"generated_at is more than {clock_skew_minutes} minutes in the future"
        )
    if age > timedelta(minutes=max_age_minutes):
        raise ValidationError(
            f"report is stale ({age.total_seconds() / 60:.1f} minutes old; "
            f"maximum is {max_age_minutes})"
        )
    return metadata


def canonical_json(report: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            report,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"report contains a value that strict JSON cannot encode: {exc}") from exc
    return (text + "\n").encode("utf-8")


def _metadata_if_valid(payload: Optional[bytes]) -> Optional[ReportMetadata]:
    if payload is None:
        return None
    try:
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            return None
        return extract_metadata(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        return None


def _semantically_equal(existing: Optional[bytes], candidate: bytes) -> bool:
    if existing is None:
        return False
    if existing == candidate:
        return True
    try:
        value = json.loads(existing.decode("utf-8"))
        return isinstance(value, dict) and canonical_json(value) == candidate
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        return False


def _version_key(metadata: ReportMetadata) -> tuple[date, int, datetime]:
    return (
        metadata.trading_date,
        SESSION_RANK[metadata.session],
        metadata.generated_at,
    )


def _report_paths(metadata: ReportMetadata) -> tuple[Path, Optional[Path]]:
    daily = Path("reports") / f"{metadata.trading_date:%Y%m%d}.json"
    if metadata.session != "intraday":
        return daily, None
    generated_eastern = metadata.generated_at.astimezone(EASTERN)
    archive = (
        Path("reports")
        / f"{metadata.trading_date:%Y%m%d}"
        / f"intraday-{generated_eastern:%H%M%S}.json"
    )
    return daily, archive


def _reject_regression(
    existing: Optional[bytes],
    candidate: ReportMetadata,
    *,
    label: str,
) -> None:
    current = _metadata_if_valid(existing)
    if current is not None and _version_key(current) >= _version_key(candidate):
        raise ValidationError(
            f"refusing to replace {label}: existing report is the same version or newer "
            f"({current.trading_date} {current.session} {current.generated_at.isoformat()})"
        )


def _safe_read(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


def _redact(text: str) -> str:
    # Do not expose credentials if a misconfigured HTTPS remote appears in Git output.
    return re.sub(r"https://[^/@\s]+@", "https://[REDACTED]@", text)


class GitRepository:
    def __init__(self, config: PublisherConfig) -> None:
        self.config = config
        self.root = config.repo_dir.resolve()

    def run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        extra_config: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = ["git"]
        if extra_config:
            command.extend(
                [
                    "-c",
                    f"user.name={self.config.git_author_name}",
                    "-c",
                    f"user.email={self.config.git_author_email}",
                ]
            )
        command.extend(args)
        LOGGER.debug("running Git command: %s", " ".join(command[:2] + ["..."]))
        completed = subprocess.run(
            command,
            cwd=self.root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
        if check and completed.returncode != 0:
            detail = _redact((completed.stderr or completed.stdout).strip())
            raise GitError(f"git {' '.join(args[:2])} failed: {detail or 'unknown error'}")
        return completed

    def preflight(self) -> None:
        if shutil.which("git") is None:
            raise GitError("git executable not found")
        if not self.root.is_dir():
            raise GitError(f"repository directory not found: {self.root}")
        top = Path(self.run(["rev-parse", "--show-toplevel"]).stdout.strip()).resolve()
        if top != self.root:
            raise GitError(f"repo_dir must be the Git top level: expected {top}, got {self.root}")
        branch = self.run(["branch", "--show-current"]).stdout.strip()
        if branch != self.config.branch:
            raise GitError(
                f"refusing to publish from branch {branch!r}; expected {self.config.branch!r}"
            )
        self.verify_remote()
        self.require_clean()

    def verify_remote(self) -> None:
        remote_url = self.run(
            ["remote", "get-url", "--push", self.config.remote]
        ).stdout.strip()
        if re.match(r"^https://[^/]+@", remote_url):
            raise GitError("credentials must not be embedded in the Git remote URL")
        expected = self.config.expected_repository
        if expected is None:
            return
        allowed = {
            f"git@github.com:{expected}.git",
            f"git@github.com:{expected}",
            f"ssh://git@github.com/{expected}.git",
            f"ssh://git@github.com/{expected}",
            f"https://github.com/{expected}.git",
            f"https://github.com/{expected}",
        }
        if remote_url not in allowed:
            # Also reject deceptive URL userinfo explicitly before showing it.
            parsed = urlsplit(remote_url) if "://" in remote_url else None
            if parsed is not None and parsed.password:
                raise GitError("credentials must not be embedded in the Git remote URL")
            raise GitError(
                f"remote {self.config.remote!r} does not exactly identify "
                f"github.com/{expected}"
            )

    def require_clean(self) -> None:
        status = self.run(
            ["status", "--porcelain=v1", "--untracked-files=all"]
        ).stdout.strip()
        if status:
            preview = "; ".join(status.splitlines()[:5])
            raise GitError(f"repository is not clean; refusing to mix changes: {preview}")

    def sync(self) -> None:
        self.run(["fetch", "--prune", self.config.remote, self.config.branch])
        self.run(["merge", "--ff-only", f"{self.config.remote}/{self.config.branch}"])
        self.require_clean()

    def head(self) -> str:
        return self.run(["rev-parse", "HEAD"]).stdout.strip()

    def push_with_retry(self) -> None:
        last_error: Optional[GitError] = None
        for attempt in range(self.config.push_retries + 1):
            try:
                self.run(
                    ["push", self.config.remote, f"HEAD:{self.config.branch}"]
                )
                return
            except GitError as exc:
                last_error = exc
                if attempt >= self.config.push_retries:
                    break
                LOGGER.warning("push failed; fetching and rebasing before retry %d", attempt + 1)
                self.run(["fetch", self.config.remote, self.config.branch])
                rebase = self.run(
                    ["rebase", f"{self.config.remote}/{self.config.branch}"],
                    check=False,
                )
                if rebase.returncode != 0:
                    self.run(["rebase", "--abort"], check=False)
                    detail = _redact((rebase.stderr or rebase.stdout).strip())
                    raise GitError(f"remote update conflicts with report publication: {detail}")
        assert last_error is not None
        raise last_error


class RepositoryLock:
    """Advisory process lock stored inside .git (fcntl on Linux, msvcrt on Windows)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Optional[Any] = None

    def __enter__(self) -> "RepositoryLock":
        if not self.path.parent.is_dir():
            raise GitError(f"Git metadata directory not found: {self.path.parent}")
        self.handle = self.path.open("a+", encoding="ascii")
        self.handle.seek(0)
        if self.handle.read(1) == "":
            self.handle.write("0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            self.handle.close()
            self.handle = None
            raise PublisherError("another publisher process is already running") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n")
        self.handle.flush()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is None:
            return
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def publish_report(
    report_file: Path,
    expected_session: str,
    config: PublisherConfig,
    *,
    now: Optional[datetime] = None,
    push: bool = True,
) -> PublishResult:
    report = _read_json_object(report_file)
    overrides = load_overrides(config.overrides_file)
    metadata = validate_report(
        report,
        expected_session=expected_session,
        now=now,
        overrides=overrides,
        max_age_minutes=config.max_age_minutes,
        clock_skew_minutes=config.clock_skew_minutes,
        postmarket_tolerance_minutes=config.postmarket_tolerance_minutes,
        allow_same_day_postmarket_backfill=config.allow_same_day_postmarket_backfill,
    )
    payload = canonical_json(report)
    digest = hashlib.sha256(payload).hexdigest()
    relative_report, relative_archive = _report_paths(metadata)
    result_path = relative_archive or relative_report

    repository = GitRepository(config)
    lock_path = config.repo_dir.resolve() / ".git" / "spy-report-publisher.lock"
    with RepositoryLock(lock_path):
        repository.preflight()
        if push:
            repository.sync()

        report_path = repository.root / relative_report
        latest_path = repository.root / "latest.json"
        archive_path = repository.root / relative_archive if relative_archive else None
        old_report = _safe_read(report_path)
        old_latest = _safe_read(latest_path)
        old_archive = _safe_read(archive_path) if archive_path else None

        report_same = _semantically_equal(old_report, payload)
        latest_same = _semantically_equal(old_latest, payload)
        archive_same = archive_path is None or _semantically_equal(old_archive, payload)
        if report_same and latest_same and archive_same:
            LOGGER.info("duplicate report; repository already contains sha256=%s", digest)
            return PublishResult(
                "duplicate",
                result_path.as_posix(),
                None,
                digest,
                "identical report and latest.json already exist; no commit created",
            )

        if not report_same:
            _reject_regression(old_report, metadata, label=relative_report.as_posix())
        if not latest_same:
            _reject_regression(old_latest, metadata, label="latest.json")
        if archive_path is not None and old_archive is not None and not archive_same:
            raise ValidationError(
                f"refusing to overwrite intraday archive collision: {relative_archive}"
            )

        baseline = repository.head()
        wrote_files = False
        commit_created = False
        try:
            if not report_same:
                _atomic_write(report_path, payload)
            if not latest_same:
                _atomic_write(latest_path, payload)
            if archive_path is not None and not archive_same:
                _atomic_write(archive_path, payload)
            wrote_files = True

            commit_paths = [relative_report.as_posix(), "latest.json"]
            if relative_archive is not None:
                commit_paths.append(relative_archive.as_posix())
            repository.run(["add", "--", *commit_paths])
            staged = repository.run(["diff", "--cached", "--quiet"], check=False)
            if staged.returncode == 0:
                repository.run(["reset", "--hard", baseline])
                return PublishResult(
                    "duplicate",
                    result_path.as_posix(),
                    None,
                    digest,
                    "canonical report produced no Git change; no commit created",
                )
            if staged.returncode != 1:
                raise GitError("unable to inspect staged report changes")

            if metadata.session == "intraday":
                message = (
                    f"Publish SPY intraday report for {metadata.trading_date.isoformat()} "
                    f"{metadata.generated_at.astimezone(EASTERN):%H:%M} ET"
                )
            else:
                message = (
                    f"Publish SPY {metadata.session} report for "
                    f"{metadata.trading_date.isoformat()}"
                )
            repository.run(
                [
                    "commit",
                    "--no-gpg-sign",
                    "-m",
                    message,
                    "--",
                    *commit_paths,
                ],
                extra_config=True,
            )
            commit_created = True
            commit = repository.head()
            if push:
                repository.push_with_retry()
                # A push race may have rebased the publication and changed its SHA.
                commit = repository.head()
            LOGGER.info(
                "published %s (%s, sha256=%s)", result_path, commit, digest
            )
            return PublishResult(
                "published",
                result_path.as_posix(),
                commit,
                digest,
                "report committed and pushed" if push else "report committed locally",
            )
        except Exception:
            if wrote_files:
                # The repository was required to be clean at entry, so this resets only
                # changes and commits created by this transaction.
                rollback_ref = baseline
                if commit_created and push:
                    repository.run(
                        ["fetch", config.remote, config.branch], check=False
                    )
                    remote_ref = repository.run(
                        ["rev-parse", f"{config.remote}/{config.branch}"], check=False
                    )
                    if remote_ref.returncode == 0:
                        rollback_ref = remote_ref.stdout.strip()
                repository.run(["reset", "--hard", rollback_ref], check=False)
            raise


def validate_file(
    report_file: Path,
    expected_session: str,
    config: PublisherConfig,
    *,
    now: Optional[datetime] = None,
) -> PublishResult:
    report = _read_json_object(report_file)
    metadata = validate_report(
        report,
        expected_session=expected_session,
        now=now,
        overrides=load_overrides(config.overrides_file),
        max_age_minutes=config.max_age_minutes,
        clock_skew_minutes=config.clock_skew_minutes,
        postmarket_tolerance_minutes=config.postmarket_tolerance_minutes,
        allow_same_day_postmarket_backfill=config.allow_same_day_postmarket_backfill,
    )
    payload = canonical_json(report)
    digest = hashlib.sha256(payload).hexdigest()
    relative_report, relative_archive = _report_paths(metadata)
    return PublishResult(
        "valid",
        (relative_archive or relative_report).as_posix(),
        None,
        digest,
        "validation passed; Git was not accessed",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and publish a generated SPY JSON report"
    )
    parser.add_argument("--report", required=True, type=Path, help="generated JSON report")
    parser.add_argument(
        "--session", required=True, choices=sorted(SESSION_RANK), help="scheduled session"
    )
    parser.add_argument("--repo", required=True, type=Path, help="clean Git checkout")
    parser.add_argument(
        "--expected-repository",
        default="treethatgoesmoo/spy-market-reports",
        help="required owner/name for the push remote",
    )
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--max-age-minutes", type=int, default=120)
    parser.add_argument(
        "--allow-same-day-postmarket-backfill",
        action="store_true",
        help=(
            "allow a fresh postmarket report after 20:00 ET, but never after the "
            "labeled trading date ends; intended only for manual recovery"
        ),
    )
    parser.add_argument("--calendar-overrides", type=Path)
    parser.add_argument("--git-author-name", default="SPY Report Publisher")
    parser.add_argument(
        "--git-author-email", default="spy-report-publisher@localhost"
    )
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    configure_logging(args.log_file, args.verbose)
    config = PublisherConfig(
        repo_dir=args.repo,
        expected_repository=args.expected_repository,
        remote=args.remote,
        branch=args.branch,
        max_age_minutes=args.max_age_minutes,
        allow_same_day_postmarket_backfill=args.allow_same_day_postmarket_backfill,
        overrides_file=args.calendar_overrides,
        git_author_name=args.git_author_name,
        git_author_email=args.git_author_email,
    )
    try:
        if args.validate_only:
            result = validate_file(args.report, args.session, config)
        else:
            result = publish_report(args.report, args.session, config)
        print(json.dumps(result.as_dict(), sort_keys=True))
        return 0
    except ValidationError as exc:
        LOGGER.error("validation rejected report: %s", exc)
        return 2
    except (GitError, PublisherError, OSError, subprocess.SubprocessError) as exc:
        LOGGER.error("publication failed: %s", exc)
        return 1
    except Exception:
        LOGGER.exception("unexpected publication failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

