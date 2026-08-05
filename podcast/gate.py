#!/usr/bin/env python3
"""Cheap pre-run gate for the daily grep podcast cron job.

Hermes invokes this from /opt/data/grep-main. It emits the cron gate JSON
format: wakeAgent=false when today's grep edition is not ready or the podcast
is already complete; otherwise wakeAgent=true with compact context.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
BASE = Path(__file__).resolve().parent.parent
RUNS = BASE / "podcast" / "runs"
CONTENT = BASE / "content"


def today() -> str:
    return dt.datetime.now(IST).date().isoformat()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=today())
    args = parser.parse_args()
    date = args.date
    marker = RUNS / date / "grep-success.json"
    done = RUNS / date / "done.json"
    script = RUNS / date / "script.json"
    edition = CONTENT / f"{date}.json"

    if done.is_file():
        print(json.dumps({"wakeAgent": False, "context": {"date": date, "reason": "podcast already complete"}}))
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
