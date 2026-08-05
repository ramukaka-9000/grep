#!/usr/bin/env python3
"""Cheap pre-run gate for the daily grep podcast cron job.

Hermes invokes this from /workspace/grep-main. It emits the cron gate JSON
format: wakeAgent=false when today's grep edition is not ready or the podcast
is already complete; otherwise wakeAgent=true with compact context.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from .pipeline import validate_date_string
except ImportError:  # pragma: no cover - direct script execution
    from pipeline import validate_date_string

IST = ZoneInfo("Asia/Kolkata")
BASE = Path(__file__).resolve().parent.parent
RUNS = BASE / "podcast" / "runs"
CONTENT = BASE / "content"
EPISODES = BASE / "podcast" / "episodes"
PAGES = BASE.parent / "grep-pages"


def today() -> str:
    return dt.datetime.now(IST).date().isoformat()


def site_audio_ready(date: str) -> bool:
    """Return whether the rendered episode is present in the deployed worktree."""
    source = EPISODES / f"{date}.mp3"
    deployed = PAGES / "audio" / date / "episode.mp3"
    page = PAGES / f"{date}.html"
    if not source.is_file() or source.stat().st_size <= 0:
        return False
    if not deployed.is_file() or deployed.stat().st_size <= 0:
        return False
    return page.is_file() and f"audio/{date}/episode.mp3" in page.read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=today())
    args = parser.parse_args()
    try:
        date = validate_date_string(args.date)
    except RuntimeError as exc:
        print(f"[podcast-gate] ERROR: {exc}", file=sys.stderr)
        return 1
    marker = RUNS / date / "grep-success.json"
    done = RUNS / date / "done.json"
    script = RUNS / date / "script.json"
    edition = CONTENT / f"{date}.json"

    if done.is_file() and site_audio_ready(date):
        print(json.dumps({"wakeAgent": False, "context": {"date": date, "reason": "podcast complete and site audio published"}}))
        return 0
    if done.is_file():
        print(json.dumps({
            "wakeAgent": True,
            "context": {
                "date": date,
                "existing_episode": str(RUNS / date / "episode.mp3"),
                "reason": "podcast rendered but site audio is not published",
                "site_audio_ready": False,
            },
        }))
        return 0
    if not marker.is_file() or not edition.is_file():
        print(json.dumps({"wakeAgent": False, "context": {"date": date, "reason": "grep edition not ready"}}))
        return 0
    print(json.dumps({
        "wakeAgent": True,
        "context": {
            "date": date,
            "edition": str(edition),
            "grep_success_marker": str(marker),
            "existing_script": str(script) if script.is_file() else None,
        },
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
