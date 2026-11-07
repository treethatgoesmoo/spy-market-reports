#!/usr/bin/env python3
"""Market-aware bridge from an existing generator to the Git publisher."""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import shlex
import string
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence

from market_calendar import EASTERN, CalendarOverrides, load_overrides, market_hours
from spy_report_publisher import (
    GitError,
    PublisherConfig,
    PublisherError,
    ValidationError,
    configure_logging,
    publish_report,
)


LOGGER = logging.getLogger("spy_report_scheduler")
ALLOWED_PLACEHOLDERS = {"session", "trading_date", "output"}


class ConfigurationError(PublisherError):
    """The scheduler configuration is incomplete or unsafe."""


@dataclass(frozen=True)
class RunnerConfig:
    publisher: PublisherConfig
    input_dir: Path
    generator_working_dir: Path
    generator_commands: Mapping[str, str]
    generator_timeout_seconds: int
    due_tolerance_minutes: int
    premarket_time: time
    log_file: Optional[Path]


def _required(parser: configparser.ConfigParser, section: str, option: str) -> str:
    if not parser.has_option(section, option):
        raise ConfigurationError(f"missing [{section}] {option}")
    value = parser.get(section, option).strip()
    if not value:
        raise ConfigurationError(f"empty [{section}] {option}")
    return value


def _optional_path(
    parser: configparser.ConfigParser, section: str, option: str
) -> Optional[Path]:
    value = parser.get(section, option, fallback="").strip()
    return Path(value) if value else None


def _parse_local_time(value: str, label: str) -> time:
    try:
        result = time.fromisoformat(value)
    except ValueError as exc:
        raise ConfigurationError(f"{label} must be HH:MM[:SS]") from exc
    if result.tzinfo is not None:
        raise ConfigurationError(f"{label} must be a local wall time without an offset")
    return result


def load_config(path: Path) -> RunnerConfig:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"configuration file not found: {path}") from exc

    for section in ("publisher", "generator", "schedule"):
        if not parser.has_section(section):
            raise ConfigurationError(f"missing [{section}] section")

    publisher = PublisherConfig(
        repo_dir=Path(_required(parser, "publisher", "repo_dir")),
        expected_repository=parser.get(
            "publisher",
            "expected_repository",
            fallback="treethatgoesmoo/spy-market-reports",
        ).strip(),
        remote=parser.get("publisher", "remote", fallback="origin").strip(),
        branch=parser.get("publisher", "branch", fallback="main").strip(),
        max_age_minutes=parser.getint("publisher", "max_age_minutes", fallback=120),
        clock_skew_minutes=parser.getint("publisher", "clock_skew_minutes", fallback=5),
        postmarket_tolerance_minutes=parser.getint(
            "publisher", "postmarket_tolerance_minutes", fallback=5
        ),
        overrides_file=_optional_path(parser, "publisher", "calendar_overrides"),
        git_author_name=parser.get(
            "publisher", "git_author_name", fallback="SPY Report Publisher"
        ).strip(),
        git_author_email=parser.get(
            "publisher",
            "git_author_email",
            fallback="spy-report-publisher@localhost",
        ).strip(),
        push_retries=parser.getint("publisher", "push_retries", fallback=2),
    )
    if not publisher.expected_repository:
        raise ConfigurationError("expected_repository may not be empty")

    premarket_command = _required(parser, "generator", "premarket_command")
    postmarket_command = _required(parser, "generator", "postmarket_command")
    commands = {
        "premarket": premarket_command,
        "intraday": parser.get(
            "generator", "intraday_command", fallback=postmarket_command
        ).strip(),
        "postmarket": postmarket_command,
    }
    if not commands["intraday"]:
        raise ConfigurationError("empty [generator] intraday_command")
    timeout = parser.getint("generator", "timeout_seconds", fallback=900)
    tolerance = parser.getint("schedule", "due_tolerance_minutes", fallback=20)
    if timeout <= 0 or tolerance < 0:
        raise ConfigurationError("timeouts must be positive and tolerance cannot be negative")

    return RunnerConfig(
        publisher=publisher,
        input_dir=Path(_required(parser, "generator", "input_dir")),
        generator_working_dir=Path(
            _required(parser, "generator", "working_directory")
        ),
        generator_commands=commands,
        generator_timeout_seconds=timeout,
        due_tolerance_minutes=tolerance,
        premarket_time=_parse_local_time(
            parser.get("schedule", "premarket_time", fallback="08:45"),
            "premarket_time",
        ),
        log_file=_optional_path(parser, "publisher", "log_file"),
    )


def due_time(
    session: str,
    trading_date: date,
    config: RunnerConfig,
    overrides: CalendarOverrides,
) -> Optional[datetime]:
    hours = market_hours(trading_date, overrides)
    if not hours.is_open or hours.close_at is None:
        return None
    if session == "premarket":
        return datetime.combine(trading_date, config.premarket_time, tzinfo=EASTERN)
    if session == "intraday":
        if hours.open_at is None:
            return None
        return hours.open_at + timedelta(minutes=65)
    if session == "postmarket":
        return hours.close_at + timedelta(minutes=30)
    raise ConfigurationError(f"unsupported session: {session!r}")


def intraday_due_times(
    trading_date: date,
    overrides: CalendarOverrides,
) -> list[datetime]:
    hours = market_hours(trading_date, overrides)
    if not hours.is_open or hours.open_at is None or hours.close_at is None:
        return []
    result: list[datetime] = []
    cursor = hours.open_at + timedelta(minutes=65)
    while cursor < hours.close_at:
        result.append(cursor)
        cursor += timedelta(hours=1)
    return result


def should_run(
    session: str,
    config: RunnerConfig,
    *,
    now: Optional[datetime] = None,
) -> tuple[bool, str, date]:
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ConfigurationError("now must be timezone-aware")
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_eastern = now_utc.astimezone(EASTERN)
    trading_date = now_eastern.date()
    overrides = load_overrides(config.publisher.overrides_file)
    if session == "intraday":
        slots = intraday_due_times(trading_date, overrides)
        if not slots:
            hours = market_hours(trading_date, overrides)
            return False, f"{trading_date} is not open ({hours.reason})", trading_date
        eligible = [slot for slot in slots if slot <= now_eastern + timedelta(minutes=2)]
        if not eligible:
            return (
                False,
                f"intraday is not due until {slots[0].isoformat()}",
                trading_date,
            )
        due = eligible[-1]
        delta = now_eastern - due
        if delta > timedelta(minutes=config.due_tolerance_minutes):
            return (
                False,
                f"intraday due window ended at "
                f"{(due + timedelta(minutes=config.due_tolerance_minutes)).isoformat()}",
                trading_date,
            )
        return True, f"intraday due at {due.isoformat()}", trading_date

    due = due_time(session, trading_date, config, overrides)
    if due is None:
        hours = market_hours(trading_date, overrides)
        return False, f"{trading_date} is not open ({hours.reason})", trading_date

    delta = now_eastern - due
    if delta < timedelta(minutes=-2):
        return False, f"{session} is not due until {due.isoformat()}", trading_date
    if delta > timedelta(minutes=config.due_tolerance_minutes):
        return (
            False,
            f"{session} due window ended at "
            f"{(due + timedelta(minutes=config.due_tolerance_minutes)).isoformat()}",
            trading_date,
        )
    return True, f"{session} due at {due.isoformat()}", trading_date


def _render_command(template: str, *, session: str, trading_date: date, output: Path) -> list[str]:
    try:
        tokens = shlex.split(template, posix=True)
    except ValueError as exc:
        raise ConfigurationError(f"invalid generator command quoting: {exc}") from exc
    if not tokens:
        raise ConfigurationError("generator command is empty")

    mapping = {
        "session": session,
        "trading_date": trading_date.isoformat(),
        "output": str(output),
    }
    rendered: list[str] = []
    for token in tokens:
        fields = {
            field_name
            for _, field_name, _, _ in string.Formatter().parse(token)
            if field_name is not None
        }
        unknown = fields.difference(ALLOWED_PLACEHOLDERS)
        if unknown:
            raise ConfigurationError(
                f"unsupported generator placeholders: {sorted(unknown)}"
            )
        rendered.append(token.format_map(mapping))
    return rendered


def run_generator(
    session: str,
    trading_date: date,
    output: Path,
    config: RunnerConfig,
) -> None:
    command = _render_command(
        config.generator_commands[session],
        session=session,
        trading_date=trading_date,
        output=output,
    )
    if not config.generator_working_dir.is_dir():
        raise ConfigurationError(
            f"generator working directory not found: {config.generator_working_dir}"
        )

    env = os.environ.copy()
    env.update(
        {
            "MARKET_SESSION": session,
            "TRADING_DATE": trading_date.isoformat(),
            "REPORT_OUTPUT": str(output),
        }
    )
    LOGGER.info("starting %s generator executable %s", session, command[0])
    with tempfile.TemporaryFile(mode="w+b") as generator_log:
        try:
            completed = subprocess.run(
                command,
                cwd=config.generator_working_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=generator_log,
                stderr=subprocess.STDOUT,
                timeout=config.generator_timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PublisherError(
                f"generator exceeded {config.generator_timeout_seconds} seconds"
            ) from exc
        if completed.returncode != 0:
            generator_log.seek(0, os.SEEK_END)
            length = generator_log.tell()
            generator_log.seek(max(0, length - 2000))
            detail = generator_log.read().decode("utf-8", errors="replace").strip()
            raise PublisherError(
                f"generator exited {completed.returncode}: "
                f"{detail or 'no error output'}"
            )
    if not output.is_file() or output.stat().st_size == 0:
        raise PublisherError(f"generator did not create a non-empty report at {output}")
    LOGGER.info("generator completed and wrote %d bytes", output.stat().st_size)


def run_scheduled_session(
    session: str,
    config: RunnerConfig,
    *,
    now: Optional[datetime] = None,
) -> dict[str, object]:
    due, reason, trading_date = should_run(session, config, now=now)
    if not due:
        LOGGER.info("schedule gate skipped run: %s", reason)
        return {"outcome": "skipped", "session": session, "reason": reason}

    config.input_dir.mkdir(parents=True, exist_ok=True)
    fd, output_name = tempfile.mkstemp(
        prefix=f"{trading_date:%Y%m%d}-{session}-",
        suffix=".json",
        dir=config.input_dir,
    )
    os.close(fd)
    output = Path(output_name)
    try:
        run_generator(session, trading_date, output, config)
        result = publish_report(
            output,
            session,
            config.publisher,
            now=now,
            push=True,
        )
        return result.as_dict()
    finally:
        try:
            output.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the configured report generator only when the market schedule is due"
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--session", required=True, choices=("premarket", "intraday", "postmarket")
    )
    parser.add_argument("--check-schedule", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(None, args.verbose)
    try:
        config = load_config(args.config)
        configure_logging(config.log_file, args.verbose)
        if args.check_schedule:
            due, reason, trading_date = should_run(args.session, config)
            print(
                json.dumps(
                    {
                        "due": due,
                        "reason": reason,
                        "session": args.session,
                        "trading_date": trading_date.isoformat(),
                    },
                    sort_keys=True,
                )
            )
            return 0
        result = run_scheduled_session(args.session, config)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ValidationError as exc:
        LOGGER.error("generated report rejected: %s", exc)
        return 2
    except (ConfigurationError, GitError, PublisherError, OSError) as exc:
        LOGGER.error("scheduled publication failed: %s", exc)
        return 1
    except Exception:
        LOGGER.exception("unexpected scheduled publication failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

