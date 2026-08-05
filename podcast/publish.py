#!/usr/bin/env python3
"""Publish an already-rendered podcast episode into the site's tracked assets.

This command never renders audio. It validates podcast/runs/<date>/episode.mp3,
then atomically copies it to podcast/episodes/<date>.mp3 for build.py to deploy.
"""
from __future__ import annotations

import argparse
import datetime as dt
import filecmp
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
RUNS = BASE / "podcast" / "runs"
EPISODES = BASE / "podcast" / "episodes"
CONFIG = BASE / "podcast" / "config.json"
DEFAULT_MIN_DURATION_SECONDS = 600.0
DEFAULT_MAX_DURATION_SECONDS = 900.0


def validate_date_string(date_str: str) -> str:
    """Accept a dated label without allowing traversal in publication paths."""
    if type(date_str) is not str:
        raise RuntimeError("episode date must be a string")
    match = re.fullmatch(
        r"(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<label>[A-Za-z0-9][A-Za-z0-9_-]*))?",
        date_str,
    )
    if not match:
        raise RuntimeError(f"invalid episode date: {date_str}")
    try:
        parsed = dt.date.fromisoformat(match.group("date"))
    except ValueError as exc:
        raise RuntimeError(f"invalid episode date: {date_str}") from exc
    if parsed.isoformat() != match.group("date"):
        raise RuntimeError(f"invalid episode date: {date_str}")
    return date_str


def finite_float(raw: object, name: str) -> float:
    if isinstance(raw, bool) or raw is None:
        raise RuntimeError(f"{name} must be a finite number")
    try:
        value = float(str(raw))
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(f"{name} must be a finite number") from exc
    if not math.isfinite(value):
        raise RuntimeError(f"{name} must be a finite number")
    return value


def probe_size(raw: object) -> int:
    value = finite_float(raw, "ffprobe size")
    if not value.is_integer():
        raise RuntimeError("ffprobe size must be an integer")
    return int(value)


def probe_episode(path: Path, min_duration: float, max_duration: float) -> tuple[float, int]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration,size,format_name",
                "-of", "json", str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise RuntimeError("ffprobe returned an unexpected JSON shape")
        fmt = payload.get("format", {})
        if not isinstance(fmt, dict):
            raise RuntimeError("ffprobe returned an unexpected format shape")
        duration = finite_float(fmt.get("duration", 0), "ffprobe duration")
        raw_size = fmt["size"] if "size" in fmt else path.stat().st_size
        size = probe_size(raw_size)
        formats = str(fmt.get("format_name", ""))
    except (OSError, subprocess.CalledProcessError, TypeError, ValueError, OverflowError, RuntimeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"ffprobe could not validate {path}") from exc
    if (
        "mp3" not in formats.split(",")
        or not math.isfinite(duration)
        or duration <= 0
        or size <= 0
    ):
        raise RuntimeError(f"episode is not a non-empty MP3 with positive duration: {path}")
    if duration < min_duration or duration > max_duration:
        raise RuntimeError(
            f"episode duration is {duration / 60:.2f} minutes; "
            f"it must be between {min_duration / 60:.0f} and {max_duration / 60:.0f} minutes: {path}"
        )
    return duration, size


def duration_policy() -> tuple[float, float]:
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise RuntimeError("duration policy config must be an object")
        minimum = finite_float(
            cfg.get("min_duration_seconds", DEFAULT_MIN_DURATION_SECONDS),
            "min_duration_seconds",
        )
        maximum = finite_float(
            cfg.get("max_duration_seconds", DEFAULT_MAX_DURATION_SECONDS),
            "max_duration_seconds",
        )
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read podcast duration policy: {CONFIG}") from exc
    if (
        not math.isfinite(minimum)
        or not math.isfinite(maximum)
        or minimum <= 0
        or maximum < minimum
    ):
        raise RuntimeError("podcast duration policy is invalid")
    return minimum, maximum


def publish(date_str: str) -> Path:
    validate_date_string(date_str)

    source = RUNS / date_str / "episode.mp3"
    if not source.is_file():
        raise RuntimeError(f"rendered episode is missing: {source}")
    min_duration, max_duration = duration_policy()
    duration, size = probe_episode(source, min_duration, max_duration)

    EPISODES.mkdir(parents=True, exist_ok=True)
    dest = EPISODES / f"{date_str}.mp3"
    if not dest.exists() or not filecmp.cmp(source, dest, shallow=False):
        fd, tmp_name = tempfile.mkstemp(
            prefix=dest.name + ".",
            suffix=".tmp.mp3",
            dir=dest.parent,
        )
        tmp = Path(tmp_name)
        os.close(fd)
        try:
            shutil.copy2(source, tmp)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        action = "published"
    else:
        action = "already tracked"

    print(f"[podcast] {action} {date_str}: {dest} ({size} bytes, {duration:.2f}s)")
    return dest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    try:
        publish(args.date)
    except (KeyError, RuntimeError, OSError, ValueError) as exc:
        print(f"[podcast] ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
