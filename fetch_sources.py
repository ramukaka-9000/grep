#!/usr/bin/env python3
"""
grep fetch_sources.py — deterministic candidate collectors.

Pulls raw candidate stories from Hacker News, arXiv and GitHub (created/
updated in the last ~7 days) and writes them to:
    content/candidates/<YYYY-MM-DD>.json

Reddit candidates are collected during curation rather than by this
standalone collector: the cron agent uses the authenticated persistent browser
RSS path first and Degoog search/scrape as fallback. The bucket stays empty in
this deterministic candidate file.

Pure stdlib. No third-party dependencies.
"""
from __future__ import annotations

import html as html_mod
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

# AI's existing per-source ceilings are intentionally unchanged. The two new
# sections have their own section-level caps and are curated independently.
CAPS = {"hn": 4, "arxiv": 2, "github": 2, "other": 4, "reddit": 4, "hf": 2,
        "total": 14}
SECTION_CAPS = {"ai": 14, "electronics": 6, "interesting-news": 6}

HN_API = "https://hacker-news.firebaseio.com/v0"
ARXIV_API = "https://export.arxiv.org/api/query"
GITHUB_STAR_FLOOR = 50

HF_API = "https://huggingface.co/api"
HF_SMALL_PARAMS = 4_000_000_000   # <=4B params: runs on a 3060/3070 within
                                  # ~5-6GB VRAM quantized, or performantly on CPU
HF_LIMIT = 300
# Repo ids matching these are almost certainly junk (test/draft/throwaway uploads).
# Lookarounds treat '_' as a boundary too (underscore is a \w char, so \b misses
# e.g. 'act_clean_table_test_100').
HF_JUNK = re.compile(
    r"(testrepo|my-?awesome|scratch|throwaway|deleteme|delete-me|junk\b|"
    r"bob-the-builder|(?<![A-Za-z0-9])test(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])tmp(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])wip(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])draft(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])staging(?![A-Za-z0-9]))",
    re.I,
)

# These are candidate feeds, not an allow-list. The cron curator still uses
# Degoog/primary-source verification before publishing anything.
ELECTRONICS_FEEDS = (
    ("hackaday", "https://hackaday.com/feed/"),
    ("adafruit", "https://blog.adafruit.com/feed/"),
    ("arduino", "https://blog.arduino.cc/feed/"),
    ("3dprinting", "https://3dprintingindustry.com/feed/"),
)

INTERESTING_FEEDS = (
    ("nasa", "https://www.nasa.gov/rss/dyn/breaking_news.rss"),
    ("esa", "https://www.esa.int/rssfeed/Our_Activities/Space_Science"),
    ("science", "https://www.sciencedaily.com/rss/top/science.xml"),
    ("reddit", "https://www.reddit.com/r/todayilearned/.rss?limit=40"),
)


def get(url: str, timeout: int = 20, raw: bool = False, headers: dict | None = None):
    hdrs = dict(HEADERS)
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
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


def _strip_markup(s: str | None) -> str:
    """Turn feed HTML snippets into compact plain text candidates."""
    text = html_mod.unescape(s or "")
    return _clean(re.sub(r"<[^>]+>", " ", text))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node: ET.Element, names: set[str]) -> str:
    for child in node.iter():
        if child is node or _local_name(child.tag) not in names:
            continue
        value = _clean(child.text)
        if value:
            return value
    return ""


def _child_link(node: ET.Element) -> str:
    for child in node.iter():
        if child is node or _local_name(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or "").strip()
        if href:
            return href
        value = _clean(child.text)
        if value:
            return value
    return ""


def fetch_feed(url: str, source: str, section: str) -> list[dict]:
    """Fetch a small RSS/Atom feed into the common candidate shape."""
    root = ET.fromstring(get(url, timeout=20))
    root_name = _local_name(root.tag)
    if root_name == "feed":
        nodes = [n for n in root.iter() if _local_name(n.tag) == "entry"]
    else:
        nodes = [n for n in root.iter() if _local_name(n.tag) == "item"]

    items: list[dict] = []
    for node in nodes[:30]:
        title = _strip_markup(_child_text(node, {"title"}))
        link = _child_link(node)
        if not title or not link or not link.startswith(("http://", "https://")):
            continue
        summary = _strip_markup(
            _child_text(node, {"description", "summary", "content", "encoded"})
        )
        published = _child_text(node, {"pubDate", "published", "updated", "date"})
        items.append(
            {
                "title": title,
                "url": link,
                "source": source,
                "summary": summary[:700],
                "published": published,
                "section": section,
            }
        )
    return items


def fetch_feeds(feeds: tuple[tuple[str, str], ...], section: str) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for source, url in feeds:
        try:
            found = fetch_feed(url, source, section)
            print(f"[fetch_sources] {source}: {len(found)} feed candidates")
        except Exception as e:
            print(f"[fetch_sources] {source} feed failed: {e}", file=sys.stderr)
            continue
        for item in found:
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            items.append(item)
    return items


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


def fetch_hf() -> list[dict]:
    """Newly created + trending Hugging Face model repos whose size we can VERIFY
    is small (safetensors param count <= HF_SMALL_PARAMS, or parameter count from
    the trending feed) — i.e. models that plausibly run on a 3060/3070 within
    ~5-6GB VRAM once quantized, or on CPU, across text/image/video/audio uses.

    Two HTTP calls merged by repo id (HF's `expand[]=safetensors` returns a
    minimal projection, so metadata and sizes must be fetched separately):
      1. /api/models?sort=createdAt&direction=-1  -> rich metadata
      2. ...&expand[]=safetensors                  -> verified param counts
      3. /api/trending                             -> named small models (incl.
         GGUF-only repos the create-stream would miss)
    Only repos with a *verified* small param count are returned — a repo with no
    metadata can't be proven small, so it is excluded (never surface a 70B as
    "fits on a 3060").
    """
    query = urllib.parse.urlencode(
        {"sort": "createdAt", "direction": "-1", "limit": HF_LIMIT}
    )
    meta = {}
    sizes: dict[str, int | None] = {}
    try:
        meta = {m["id"]: m for m in json.loads(get(f"{HF_API}/models?{query}"))}
    except Exception as e:
        print(f"[fetch_sources] HF metadata fetch failed: {e}", file=sys.stderr)
    try:
        exp_query = urllib.parse.urlencode(
            {"sort": "createdAt", "direction": "-1", "limit": HF_LIMIT,
             "expand[]": "safetensors"}
        )
        sizes = {
            m["id"]: (m.get("safetensors") or {}).get("total")
            for m in json.loads(get(f"{HF_API}/models?{exp_query}"))
        }
    except Exception as e:
        print(f"[fetch_sources] HF size fetch failed: {e}", file=sys.stderr)

    trend: dict[str, dict] = {}
    try:
        t = json.loads(get(f"{HF_API}/trending", headers={"Accept": "application/json"}))
        for r in t.get("recentlyTrending", []):
            if r.get("repoType") != "model":
                continue
            rd = r.get("repoData") or {}
            if not rd.get("id"):
                continue
            trend[rd["id"]] = {
                "pipeline_tag": rd.get("pipeline_tag"),
                "downloads": rd.get("downloads"),
                "likes": rd.get("likes"),
                "numParameters": rd.get("numParameters"),
                "lastModified": str(rd.get("lastModified") or "")[:10],
            }
    except Exception as e:
        print(f"[fetch_sources] HF trending fetch failed: {e}", file=sys.stderr)

    blank = {"pipeline_tag": None, "downloads": 0, "likes": 0, "lastModified": ""}
    items: list[dict] = []
    for rid in dict.fromkeys([*sizes.keys(), *meta.keys(), *trend.keys()]):
        if HF_JUNK.search(rid):
            continue
        params = sizes.get(rid)
        if not isinstance(params, int) or params <= 0:
            # fall back to the trending param count (covers GGUF-only smalls)
            params = trend.get(rid, {}).get("numParameters")  # type: ignore
        if not isinstance(params, int) or not (1_000_000 <= params <= HF_SMALL_PARAMS):
            continue
        md = meta.get(rid) or {}
        tr = {**blank, **trend.get(rid, {})}
        # embedded pipeline tags: e.g. 'text-generation', extractors aren't useful here
        pipeline = md.get("pipeline_tag") or tr["pipeline_tag"]
        items.append(
            {
                "id": rid,
                "url": f"https://huggingface.co/{rid}",
                "model": rid,
                "params": params,
                "size_tier": "tiny" if params < 1_000_000_000 else "small",
                "pipeline_tag": pipeline,
                "tags": (md.get("tags") or [])[:8],
                "downloads": md.get("downloads", 0) or tr["downloads"] or 0,
                "likes": md.get("likes", 0) or tr["likes"] or 0,
                "created_at": (
                    str(md.get("createdAt") or "")[:10] or tr["lastModified"]
                ),
                "stream": "new" if rid in sizes else "trending",
            }
        )
    # newest first, then by community attention (a like signals real interest,
    # so it out-weights a download)
    items.sort(
        key=lambda x: (x["created_at"] or "", x["likes"] * 10 + x["downloads"]),
        reverse=True,
    )
    return items[:40]


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
    print("[fetch_sources] fetching Hugging Face ...")
    hf = fetch_hf()
    print(f"[fetch_sources] fetched {len(hf)} small-model HF candidates")
    print("[fetch_sources] fetching Electronics feeds ...")
    electronics = fetch_feeds(ELECTRONICS_FEEDS, "electronics")
    print(f"[fetch_sources] fetched {len(electronics)} Electronics candidates")
    print("[fetch_sources] fetching Interesting News feeds ...")
    interesting = fetch_feeds(INTERESTING_FEEDS, "interesting-news")
    print(f"[fetch_sources] fetched {len(interesting)} Interesting News candidates")

    payload = {
        "fetched_at": datetime.now().astimezone().isoformat(),
        "date": today,
        "caps": {
            "ai": CAPS,
            "electronics": SECTION_CAPS["electronics"],
            "interesting-news": SECTION_CAPS["interesting-news"],
            "edition_total": sum(SECTION_CAPS.values()),
        },
        "reddit_note": (
            "Reddit candidates are filled during curation: use authenticated "
            "persistent-browser Atom RSS first for the AI, Electronics, and "
            "Interesting News subreddits; use Degoog search/scrape as fallback."
        ),
        "section_notes": {
            "electronics": (
                "Use the feed candidates as leads, then prioritize reproducible "
                "DIY builds, ESP32/embedded work, tools, hacks, open hardware, "
                "and meaningful 3D-printing technology. Curate at most 6."
            ),
            "interesting-news": (
                "Use feed candidates as leads, but verify surprising TIL-style "
                "claims against a primary or authoritative source. Curate at most 6."
            ),
        },
        "hf_note": (
            "HF candidates are new/trending model repos with VERIFIED small "
            "param counts (safetensors total, else trending numParameters) of "
            "1M-4B params — small enough to run on a 3060/3070 within ~5-6GB "
            "VRAM once quantized, or performantly on CPU. Curate the strongest "
            "2 (cap hf=2), prioritizing NOVEL small models for interesting "
            "text/image/video/audio use-cases. 'size_tier': tiny=<1B, "
            "small=1-4B. GGUF-only smalls appear via the trending stream."
        ),
        "hn": hn,
        "arxiv": arxiv,
        "github": github,
        "reddit": [],
        "hf": hf,
        "electronics": electronics,
        "interesting-news": interesting,
        "other": [],
    }
    out = CAND_DIR / f"{today}.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[fetch_sources] wrote {out}")


if __name__ == "__main__":
    main()
