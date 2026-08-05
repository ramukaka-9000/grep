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
import os
import shutil
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
RUNS = BASE / "podcast" / "runs"
EPISODES = BASE / "podcast" / "episodes"


def probe_episode(path: Path) -> tuple[float, int]:
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
        fmt = payload.get("format", {})
        duration = float(fmt.get("duration", 0))
        size = int(float(fmt.get("size", path.stat().st_size)))
        formats = str(fmt.get("format_name", ""))
    except (OSError, subprocess.CalledProcessError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"ffprobe could not validate {path}") from exc
    if "mp3" not in formats.split(",") or duration <= 0 or size <= 0:
        raise RuntimeError(f"episode is not a non-empty MP3 with positive duration: {path}")
    return duration, size


def publish(date_str: str) -> Path:
    try:
        dt.date.fromisoformat(date_str)
    except ValueError as exc:
        raise RuntimeError(f"invalid episode date: {date_str}") from exc

    source = RUNS / date_str / "episode.mp3"
    if not source.is_file():
        raise RuntimeError(f"rendered episode is missing: {source}")
    duration, size = probe_episode(source)

    EPISODES.mkdir(parents=True, exist_ok=True)
    dest = EPISODES / f"{date_str}.mp3"
    if not dest.exists() or not filecmp.cmp(source, dest, shallow=False):
        tmp = dest.with_name(dest.name + ".tmp")
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
    except (RuntimeError, OSError) as exc:
        print(f"[podcast] ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
