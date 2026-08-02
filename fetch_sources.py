#!/usr/bin/env python3
"""
grep fetch_sources.py — deterministic candidate collectors.

Pulls raw candidate stories from Hacker News, arXiv and GitHub (created/
updated in the last ~7 days) and writes them to:
    content/candidates/<YYYY-MM-DD>.json

Reddit is network-blocked on this host, so its bucket is left empty and the
number of reddit picks (max REDDIT_CAP) is fulfilled by the agent via web
search during curation instead.

Pure stdlib. No third-party dependencies.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
CAND_DIR = BASE / "content" / "candidates"

UA = "grep-daily-read/1.0 (hermes agent, personal daily digest)"
HEADERS = {"User-Agent": UA}

# Per-source ceilings; the TOTAL cap is enforced at curation time.
CAPS = {"hn": 4, "arxiv": 2, "github": 2, "other": 4, "reddit": 2, "total": 12}

HN_API = "https://hacker-news.firebaseio.com/v0"
ARXIV_API = "https://export.arxiv.org/api/query"
GITHUB_STAR_FLOOR = 50


def get(url: str, timeout: int = 20, raw: bool = False):
    req = urllib.request.Request(url, headers=dict(HEADERS))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if raw else data.decode("utf-8", "replace")


def fetch_hn() -> list[dict]:
    ids: list[int] = []
    for feed in ("topstories", "beststories"):
        try:
            ids += json.loads(get(f"{HN_API}/{feed}.json"))[:50]
        except Exception:
            continue
    seen = set()
    items = []
    for i in ids:
        if i in seen:
            continue
        seen.add(i)
        if len(items) >= 50:
            break
        try:
            it = json.loads(get(f"{HN_API}/item/{i}.json", timeout=12))
        except Exception:
            continue
        if not it or it.get("type") != "story" or not it.get("url"):
            continue
        if it.get("dead") or it.get("deleted"):
            continue
        items.append(
            {
                "id": i,
                "title": (it.get("title") or "").strip(),
                "url": it.get("url", ""),
                "author": it.get("by", ""),
                "score": it.get("score", 0),
                "comments": it.get("descendants", 0),
                "time": it.get("time", 0),
                "discuss_url": f"https://news.ycombinator.com/item?id={i}",
            }
        )
        time.sleep(0.04)
    items.sort(key=lambda x: (x["score"], x["comments"]), reverse=True)
    return items[:45]


def _clean(s: str | None) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s


def _txt(entry: ET.Element, ns: dict, path: str) -> str:
    el = entry.find(path, ns)
    return _clean(el.text if el is not None else "")


def fetch_arxiv() -> list[dict]:
    ns = {
        "a": "http://www.w3.org/2005/Atom",
        "ar": "http://arxiv.org/schemas/atom",
    }
    results = {}
    for cat in ("cs.AI", "cs.LG", "cs.CL", "cs.SE", "cs.CR"):
        q = urllib.parse.urlencode(
            {
                "search_query": f"cat:{cat}",
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": 8,
            }
        )
        try:
            xml = get(f"{ARXIV_API}?{q}")
        except Exception:
            continue
        root = ET.fromstring(xml)
        for entry in root.findall("a:entry", ns):
            aid = _txt(entry, ns, "a:id").rsplit("/abs/", 1)[-1]
            if not aid or aid in results:  # a paper often appears under several cats
                continue
            summary = _txt(entry, ns, "a:summary")
            results[aid] = {
                "arxiv_id": aid,
                "title": _txt(entry, ns, "a:title"),
                "url": f"https://arxiv.org/abs/{aid}",
                "authors": [
                    _txt(a, ns, "a:name")
                    for a in entry.findall("a:author", ns)
                    if a.find("a:name", ns) is not None
                ],
                "published": _txt(entry, ns, "a:published")[:10],
                "abstract": summary[:700],
                "comment": _txt(entry, ns, "ar:comment"),
            }
    items = list(results.values())
    items.sort(key=lambda x: x["published"], reverse=True)
    return items[:30]


def fetch_github() -> list[dict]:
    since = (date.today() - timedelta(days=7)).isoformat()
    q = f"created:>{since} pushed:>{since} stars:>50"
    try:
        out = subprocess.run(
            [
                "gh", "api", "-X", "GET", "search/repositories",
                "-f", f"q={q}", "-f", "sort=stars", "-f", "order=desc",
                "-f", "per_page=20",
            ],
            capture_output=True, text=True, timeout=45, check=True,
        ).stdout
    except Exception as e:
        print(f"[fetch_sources] github search failed: {e}", file=sys.stderr)
        return []
    items = []
    for it in json.loads(out).get("items", []):
        items.append(
            {
                "full_name": it["full_name"],
                "url": it["html_url"],
                "description": _clean(it.get("description") or ""),
                "stars": it.get("stargazers_count", 0),
                "forks": it.get("forks_count", 0),
                "language": it.get("language") or "",
                "pushed_at": it.get("pushed_at", "")[:10],
                "topics": it.get("topics", [])[:6],
            }
        )
    return items[:25]


def main() -> None:
    today = date.today().isoformat()
    CAND_DIR.mkdir(parents=True, exist_ok=True)
    print("[fetch_sources] fetching HN ...")
    hn = fetch_hn()
    print(f"[fetch_sources] fetched {len(hn)} HN candidates")
    print("[fetch_sources] fetching arXiv ...")
    arxiv = fetch_arxiv()
    print(f"[fetch_sources] fetched {len(arxiv)} arXiv candidates")
    print("[fetch_sources] fetching GitHub ...")
    github = fetch_github()
    print(f"[fetch_sources] fetched {len(github)} GitHub candidates")

    payload = {
        "fetched_at": datetime.now().astimezone().isoformat(),
        "date": today,
        "caps": CAPS,
        "reddit_note": (
            "Reddit JSON/RSS is blocked on this host; the reddit bucket is "
            "filled during curation via web search (r/MachineLearning, "
            "r/LocalLLaMA, r/programming), capped at 2."
        ),
        "hn": hn,
        "arxiv": arxiv,
        "github": github,
        "reddit": [],
        "other": [],
    }
    out = CAND_DIR / f"{today}.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[fetch_sources] wrote {out}")


if __name__ == "__main__":
    main()
