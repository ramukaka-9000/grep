#!/usr/bin/env python3
"""
grep build.py — render the static site from content/*.json and deploy to gh-pages.

The morning agent writes content/<YYYY-MM-DD>.json (the curated edition), then this
script turns every edition into a static page, auto-fetches/caches each story's
OG image (generating a styled placeholder when none exists or for arXiv), and
pushes the result to the gh-pages branch.

    python3 build.py              # render all editions + deploy
    python3 build.py --no-deploy  # render only (for testing)

Pure stdlib. No third-party dependencies.
"""
from __future__ import annotations

import concurrent.futures as cf
import html as H
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent          # main-branch worktree (source)
PAGES_DIR = BASE.parent / "grep-pages"          # gh-pages worktree (build target)
TEMPLATES = BASE / "templates"
CONTENT = BASE / "content"

SITE_URL = "https://ramukaka-9000.github.io/grep"
SITE_NAME = "grep"
TAGLINE = "a daily signal hunt — HN · arXiv · GitHub · Reddit · the web"
UA = "grep-daily-read build.py (+https://ramukaka-9000.github.io/grep)"
IMG_KEEP_DAYS = 60          # prune cached story images older than this
MAX_OG_BYTES = 2_500_000

SOURCES = {
    "hn":     {"label": "HN",     "color": "#ff8000", "short": "HN"},
    "arxiv":  {"label": "arXiv",  "color": "#ff5a5f", "short": "Ax"},
    "github": {"label": "GitHub", "color": "#8b5cf6", "short": "Gh"},
    "other":  {"label": "Web",    "color": "#22d3ee", "short": "WB"},
    "reddit": {"label": "Reddit", "color": "#ff4500", "short": "r/"},
}

TIER_CHIP = {"essential": "Must-Read", "must-read": "Must-Read",
             "recommended": "Recommended", "notable": "Recommended"}
TIER_GROUP = {"essential": "must", "must-read": "must",
              "recommended": "rec", "notable": "rec"}


# ----------------------------------------------------------------------------- utilities

def _http_get(url: str, timeout: int = 15, max_bytes: int = MAX_OG_BYTES) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,image/*;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(max_bytes + 1)


def og_image(url: str) -> str | None:
    """Return the og:image URL for a page, or None."""
    if not url:
        return None
    try:
        data = _http_get(url)
    except Exception:
        return None
    if len(data) > MAX_OG_BYTES:
        return None
    for pat in (
        br'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        br'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        br'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        br'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
    ):
        m = re.search(pat, data, re.I)
        if m:
            cand = m.group(1)
            cand = cand.decode("utf-8", "replace").strip()
            if cand.startswith("//"):
                cand = "https:" + cand
            if cand.startswith(("http://", "https://")):
                return cand
    return None


def _slugify(title: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", title.lower()).strip("-")
    return s[:48] or "story"


def _sniff_ext(data: bytes) -> str | None:
    """Determine image type from magic bytes, not the (often missing) URL ext."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"GIF8":
        return ".gif"
    if b"<svg" in data[:512].lower():
        return ".svg"
    return None


def save_image(url: str, dest: Path) -> Path | None:
    """Download an image and save it with the correct extension (magic sniff)."""
    try:
        data = _http_get(url, timeout=20, max_bytes=6_000_000)
    except Exception:
        return None
    if len(data) < 500:
        return None
    ext = _sniff_ext(data)
    if not ext:
        return None
    dest = dest.with_suffix(ext)
    dest.write_bytes(data)
    return dest


def placeholder_svg(date_dir: Path, idx: int, source: str, title: str) -> str:
    """Deterministic styled placeholder when no usable image exists."""
    meta = SOURCES.get(source, SOURCES["other"])
    color = meta["color"]
    words = re.findall(r"[\w']+", title)
    line_words = words[:4]
    if len(words) > 4:
        line_words.append("…")
    lines = " ".join(line_words)
    lines = H.escape(lines, quote=True)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="560" height="418"
  viewBox="0 0 560 418" role="img" aria-label="{H.escape(title[:80], quote=True)}">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#101418"/>
      <stop offset="1" stop-color="{color}"/>
    </linearGradient>
  </defs>
  <rect width="560" height="418" fill="url(#g)"/>
  <text x="28" y="60" font-family="ui-monospace,monospace" font-size="22"
    fill="{color}" font-weight="700">{meta["short"]} · grep</text>
  <text x="28" y="300" font-family="ui-monospace,monospace" font-size="30"
    fill="#f2f4f8" font-weight="600">{lines}</text>
  <text x="28" y="382" font-family="ui-monospace,monospace" font-size="15"
    fill="rgba(242,244,248,.5)">{meta["label"].upper()} · no preview image</text>
</svg>'''
    dest = date_dir / f"{idx:02d}-{source}-placeholder.svg"
    dest.write_text(svg)
    return dest.name


def build_story_images(date_dir: Path, date_str: str, stories: list[dict]) -> dict[int, str]:
    """Fetch + cache one image per story; returns {index: relative_path}.

    Idempotent: if an edition's images already exist on disk they are reused
    (no re-download), so archived editions never change. Fresh editions fetch.
    """
    date_dir.mkdir(parents=True, exist_ok=True)
    existing = {
        i: sorted(date_dir.glob(f"{i:02d}-*"))
        for i in range(1, len(stories) + 1)
    }

    def one(i: int, s: dict) -> tuple[int, str]:
        hits = existing.get(i) or []
        if hits:  # cached from an earlier run
            return i, f"images/{date_str}/{hits[0].name}"
        rel = f"images/{date_str}/{i:02d}-{_slugify(s.get('title', 'story'))}.jpg"
        dest = date_dir / f"{i:02d}-{_slugify(s.get('title', 'story'))}.jpg"
        explicit = (s.get("image") or "").strip() or None
        # arXiv abstracts have no useful OG image -> straight to placeholder.
        if explicit:
            saved = save_image(explicit, dest)
            if saved:
                return i, f"images/{date_str}/{saved.name}"
        elif s.get("source") != "arxiv":
            og = og_image(s.get("url", ""))
            if og:
                saved = save_image(og, dest)
                if saved:
                    return i, f"images/{date_str}/{saved.name}"
        pl = placeholder_svg(date_dir, i, s.get("source", "other"), s.get("title", ""))
        return i, f"images/{date_str}/{pl}"

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(one, i, s) for i, s in enumerate(stories, start=1)]
        results = {}
        for f in futures:
            i, rel = f.result()
            results[i] = rel
    return results


def load_editions() -> list[dict]:
    eds = []
    for p in sorted(CONTENT.glob("*.json")):
        if p.parent.name != CONTENT.name or p.name.startswith("."):
            continue
        try:
            data = json.loads(p.read_text())
        except Exception as e:
            print(f"[build] skip {p.name}: {e}", file=sys.stderr)
            continue
        if not data.get("stories"):
            continue
        data["_file"] = p
        eds.append(data)
    eds.sort(key=lambda d: d["date"])
    return eds


# ----------------------------------------------------------------------------- rendering

def render_story_card(story: dict, idx: int, date_str: str, img_rel: str) -> str:
    src = story.get("source", "other")
    meta = SOURCES.get(src, SOURCES["other"])
    tier = story.get("tier", "recommended")
    chip = TIER_CHIP.get(tier, "Recommended")
    group = TIER_GROUP.get(tier, "rec")
    url = story.get("url", "#")
    title = H.escape(story.get("title", "Untitled"), quote=False)
    author = H.escape(story.get("byline") or story.get("author") or "", quote=False)
    discuss = H.escape(story.get("discuss_url") or "", quote=False)
    desc = story.get("summary") or ""
    paras = "".join(f"<p>{H.escape(p)}</p>" for p in str(desc).split("\n") if p.strip())

    meta_links = f'<a class="orig" href="{url}" target="_blank" rel="noopener">{H.escape(meta["label"])} ›</a>'
    if discuss:
        meta_links += f'<a class="discuss" href="{discuss}" target="_blank" rel="noopener">Discuss ›</a>'

    return f"""
    <article class="story" data-source="{src}" data-tier-group="{group}" data-tier="{H.escape(tier)}">
      <a class="thumb" href="{url}" target="_blank" rel="noopener" aria-hidden="true" tabindex="-1">
        <img src="{img_rel}" alt="{H.escape(title, quote=True)}" loading="lazy">
      </a>
      <div class="story-body">
        <div class="story-meta">
          <span class="src-tag" style="--c:{meta['color']}">{meta['short']}</span>
          <span class="by">{author}</span>
          {meta_links}
          <span class="tier-chip tier-{group}">{chip}</span>
        </div>
        <h2 class="story-title"><a href="{url}" target="_blank" rel="noopener">{title}</a></h2>
        <div class="story-summary">{paras}</div>
      </div>
    </article>"""


def render_edition_page(edition: dict, stories_html: str, prev_href: str,
                        next_href: str, filter_buttons: str, target: Path) -> None:
    tpl = (TEMPLATES / "edition.html").read_text()
    date_iso = edition["date"]
    dt = os.path.getmtime(edition["_file"]) if edition.get("_file") else 0
    count = len(edition.get("stories", []))
    page = (
        tpl
        .replace("{{SITE_NAME}}", SITE_NAME)
        .replace("{{TAGLINE}}", H.escape(TAGLINE, quote=True))
        .replace("{{DATE_ISO}}", date_iso)
        .replace("{{DATE_DISPLAY}}", _fmt_date(date_iso))
        .replace("{{COUNT}}", str(count))
        .replace("{{FILTER_SOURCES}}", filter_buttons)
        .replace("{{STORY_CARDS}}", stories_html)
        .replace("{{PREV_LINK}}", prev_href)
        .replace("{{NEXT_LINK}}", next_href)
        .replace("{{CANONICAL}}", f"{SITE_URL}/{date_iso}.html")
    )
    target.write_text(page)


def _fmt_date(iso: str) -> str:
    try:
        from datetime import datetime as _d
        return _d.strptime(iso, "%Y-%m-%d").strftime("%A, %B %-d, %Y").replace(" %-", " ")
    except Exception:
        return iso


def render_archive(editions: list[dict]) -> None:
    tpl = (TEMPLATES / "archive.html").read_text()
    rows = []
    for e in reversed(editions):
        d = e["date"]
        href = f"{d}.html"
        if d == editions[-1]["date"]:
            href = "./"
        rows.append(
            f'<li class="arch-row"><a href="{href}">{d}</a>'
            f'<span class="arch-meta">{len(e.get("stories", []))} stories</span></li>'
        )
    page = tpl.replace("{{SITE_NAME}}", SITE_NAME).replace("{{ARCHIVE_ROWS}}", "\n".join(rows))
    (PAGES_DIR / "archive.html").write_text(page)


# ----------------------------------------------------------------------------- deploy

def deploy(date_str: str, no_deploy: bool) -> None:
    if no_deploy:
        print("[build] --no-deploy: skipping git push")
        return
    subprocess.run(["git", "-C", str(PAGES_DIR), "add", "-A"], check=True)
    try:
        subprocess.run(
            ["git", "-C", str(PAGES_DIR), "commit", "-m", f"edition {date_str}",
             "--allow-empty"],
            capture_output=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[build] commit failed: {e}", file=sys.stderr)
        return
    subprocess.run(["git", "-C", str(PAGES_DIR), "push", "-q", "origin", "gh-pages"], check=True)
    print(f"[build] deployed {date_str} -> gh-pages")


def prune_stale_images() -> None:
    cutoff = None
    try:
        import datetime as _dt
        cutoff = _dt.date.today() - _dt.timedelta(days=IMG_KEEP_DAYS)
    except Exception:
        return
    img_root = PAGES_DIR / "images"
    if not img_root.exists():
        return
    for child in img_root.iterdir():
        if not child.is_dir():
            continue
        try:
            d = _dt.date.fromisoformat(child.name)
        except Exception:
            continue
        if d < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            print(f"[build] pruned stale images for {child.name}")


def main() -> None:
    no_deploy = "--no-deploy" in sys.argv
    if not PAGES_DIR.is_dir():
        print("[build] gh-pages worktree missing; create it: "
              "cd grep-main && git worktree add ../grep-pages gh-pages", file=sys.stderr)
        sys.exit(1)
    for d in (PAGES_DIR / "assets", PAGES_DIR / "images"):
        d.mkdir(parents=True, exist_ok=True)

    # static assets live in main, mirrored into the build target
    for name in ("style.css", "app.js", "favicon.svg"):
        src = BASE / "assets" / name
        if src.exists():
            shutil.copy2(src, PAGES_DIR / "assets" / name)

    editions = load_editions()
    if not editions:
        print("[build] no editions found under content/", file=sys.stderr)
        sys.exit(2)

    filter_buttons = _filter_buttons(editions[-1])
    for i, ed in enumerate(editions):
        d = ed["date"]
        img_dir = PAGES_DIR / "images" / d
        rels = build_story_images(img_dir, d, ed["stories"])
        cards = [
            render_story_card(s, idx, d, rels[idx])
            for idx, s in enumerate(ed["stories"], start=1)
        ]
        prev_el, next_el = "", ""
        if i > 0:
            prev_el = f'<a class="navlink" href="{editions[i - 1]["date"]}.html">‹ Prev</a>'
        else:
            prev_el = '<span class="navlink disabled">‹ Prev</span>'
        if i < len(editions) - 1:
            next_el = f'<a class="navlink" href="{editions[i + 1]["date"]}.html">Next ›</a>'
        else:
            next_el = '<span class="navlink disabled">Next ›</span>'
        target = PAGES_DIR / f"{d}.html"
        render_edition_page(ed, "\n".join(cards), prev_el, next_el, filter_buttons, target)
        print(f"[build] rendered {d}.html ({len(cards)} stories)")

    # index = latest edition
    shutil.copy2(PAGES_DIR / f"{editions[-1]['date']}.html", PAGES_DIR / "index.html")
    render_archive(editions)
    prune_stale_images()
    print(f"[build] rendered index.html + archive.html")
    deploy(editions[-1]["date"], no_deploy)


def _filter_buttons(latest: dict) -> str:
    present = [src for src in SOURCES if any(s.get("source") == src for s in latest.get("stories", []))]
    parts = ['<button class="chip active" data-filter-source="all">All</button>']
    for src in present:
        m = SOURCES[src]
        parts.append(
            f'<button class="chip" data-filter-source="{src}" style="--c:{m["color"]}">{H.escape(m["label"])}</button>'
        )
    parts.append('<span class="chip-sep"></span>')
    parts.append('<button class="chip active" data-filter-tier="all">All</button>')
    parts.append('<button class="chip" data-filter-tier="rec">Recommended</button>')
    parts.append('<button class="chip" data-filter-tier="must">Must-Read</button>')
    return "\n".join(parts)


if __name__ == "__main__":
    main()
