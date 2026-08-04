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

SITE_URL = "https://grep.shantanugoel.com"
SITE_NAME = "grep"
TAGLINE = "a daily signal hunt — AI · Electronics · Interesting News"
UA = "grep-daily-read build.py (+https://grep.shantanugoel.com)"
IMG_KEEP_DAYS = 60          # prune cached story images older than this
MAX_OG_BYTES = 2_500_000

SECTION_ORDER = ("ai", "electronics", "interesting-news")
SECTIONS = {
    "ai": {"label": "AI", "color": "#ff8000"},
    "electronics": {"label": "Electronics", "color": "#22d3ee"},
    "interesting-news": {"label": "Interesting News", "color": "#f472b6"},
}

SOURCES = {
    "hn":         {"label": "HN",          "color": "#ff8000", "short": "HN"},
    "arxiv":      {"label": "arXiv",       "color": "#ff5a5f", "short": "Ax"},
    "github":     {"label": "GitHub",      "color": "#8b5cf6", "short": "Gh"},
    "hf":         {"label": "HuggingFace", "color": "#ffd21e", "short": "HF"},
    "other":      {"label": "Web",         "color": "#22d3ee", "short": "WB"},
    "reddit":     {"label": "Reddit",      "color": "#ff4500", "short": "r/"},
    "hackaday":   {"label": "Hackaday",    "color": "#f97316", "short": "HA"},
    "adafruit":   {"label": "Adafruit",    "color": "#7c3aed", "short": "Ad"},
    "arduino":    {"label": "Arduino",     "color": "#00979d", "short": "Ar"},
    "espressif":  {"label": "Espressif",   "color": "#e7352c", "short": "Es"},
    "3dprinting": {"label": "3D Print",    "color": "#14b8a6", "short": "3D"},
    "nasa":       {"label": "NASA",        "color": "#fc3d21", "short": "NA"},
    "esa":        {"label": "ESA",         "color": "#4f8ef7", "short": "ESA"},
    "science":    {"label": "Science",     "color": "#10b981", "short": "Sci"},
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


def placeholder_svg(date_dir: Path, key: str, source: str, title: str) -> str:
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
    dest = date_dir / f"{key}-{source}-placeholder.svg"
    dest.write_text(svg)
    return dest.name


def build_story_images(date_dir: Path, date_str: str,
                       entries: list[tuple[str, dict]]) -> dict[str, str]:
    """Fetch + cache one image per section story; returns {key: relative_path}."""
    date_dir.mkdir(parents=True, exist_ok=True)
    existing = {key: sorted(date_dir.glob(f"{key}-*")) for key, _ in entries}

    def one(key: str, s: dict) -> tuple[str, str]:
        hits = existing.get(key) or []
        # Reuse images from pre-section AI editions on the first section-aware
        # build, so archived editions do not needlessly redownload every image.
        if not hits and key.startswith("ai-"):
            hits = sorted(date_dir.glob(f"{key.split('-', 1)[1]}-*"))
        if hits:
            return key, f"images/{date_str}/{hits[0].name}"
        rel = f"images/{date_str}/{key}-{_slugify(s.get('title', 'story'))}.jpg"
        dest = date_dir / f"{key}-{_slugify(s.get('title', 'story'))}.jpg"
        explicit = (s.get("image") or "").strip() or None
        # arXiv abstracts have no useful OG image -> straight to placeholder.
        if explicit:
            saved = save_image(explicit, dest)
            if saved:
                return key, f"images/{date_str}/{saved.name}"
        elif s.get("source") != "arxiv":
            og = og_image(s.get("url", ""))
            if og:
                saved = save_image(og, dest)
                if saved:
                    return key, f"images/{date_str}/{saved.name}"
        pl = placeholder_svg(date_dir, key, s.get("source", "other"), s.get("title", ""))
        return key, f"images/{date_str}/{pl}"

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(one, key, story) for key, story in entries]
        results = {}
        for f in futures:
            key, rel = f.result()
            results[key] = rel
    return results


def _normalize_sections(data: dict) -> list[dict]:
    """Return the section list while keeping old AI-only editions readable."""
    raw = data.get("sections")
    if isinstance(raw, dict):
        raw_sections = [
            {"id": sid, **(value if isinstance(value, dict) else {})}
            for sid, value in raw.items()
        ]
    elif isinstance(raw, list):
        raw_sections = [s for s in raw if isinstance(s, dict)]
    else:
        raw_sections = [{
            "id": "ai",
            "title": "AI",
            "summary": data.get("summary", ""),
            "stories": data.get("stories", []),
        }]

    order = {sid: i for i, sid in enumerate(SECTION_ORDER)}
    sections: list[dict] = []
    for section in raw_sections:
        sid = str(section.get("id") or section.get("section") or "").strip()
        if not sid:
            continue
        meta = SECTIONS.get(sid, {"label": sid.replace("-", " ").title(), "color": "#7ee787"})
        stories = section.get("stories")
        if not isinstance(stories, list):
            stories = []
        sections.append({
            "id": sid,
            "title": section.get("title") or meta["label"],
            "summary": section.get("summary") or "",
            "stories": stories,
        })
    sections.sort(key=lambda s: (order.get(s["id"], len(order)), s["id"]))
    return sections


def _story_count(edition: dict) -> int:
    return sum(len(section.get("stories", [])) for section in edition.get("_sections", []))


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
        data["_sections"] = _normalize_sections(data)
        if not data["_sections"] or _story_count(data) == 0:
            continue
        data["_file"] = p
        eds.append(data)
    eds.sort(key=lambda d: d["date"])
    return eds


# ----------------------------------------------------------------------------- rendering

def render_story_card(story: dict, section_id: str, idx: int, img_rel: str) -> str:
    src = story.get("source", "other")
    meta = SOURCES.get(src, SOURCES["other"])
    tier = story.get("tier", "recommended")
    chip = TIER_CHIP.get(tier, "Recommended")
    group = TIER_GROUP.get(tier, "rec")
    url = str(story.get("url") or "#")
    url_html = H.escape(url, quote=True)
    title_raw = str(story.get("title") or "Untitled")
    title = H.escape(title_raw, quote=False)
    author = H.escape(str(story.get("byline") or story.get("author") or ""), quote=False)
    discuss = str(story.get("discuss_url") or "")
    discuss_html = H.escape(discuss, quote=True)
    desc = story.get("summary") or ""
    paras = "".join(f"<p>{H.escape(p)}</p>" for p in str(desc).split("\n") if p.strip())

    meta_links = f'<a class="orig" href="{url_html}" target="_blank" rel="noopener">{H.escape(meta["label"])} ›</a>'
    if discuss:
        meta_links += f'<a class="discuss" href="{discuss_html}" target="_blank" rel="noopener">Discuss ›</a>'

    return f"""
    <article class="story" data-section="{H.escape(section_id, quote=True)}" data-source="{H.escape(src, quote=True)}" data-tier-group="{group}" data-tier="{H.escape(tier, quote=True)}">
      <a class="thumb" href="{url_html}" target="_blank" rel="noopener" aria-hidden="true" tabindex="-1">
        <img src="{img_rel}" alt="{H.escape(title_raw, quote=True)}" loading="lazy">
      </a>
      <div class="story-body">
        <div class="story-meta">
          <span class="src-tag" style="--c:{meta['color']}">{meta['short']}</span>
          <span class="by">{author}</span>
          {meta_links}
          <span class="tier-chip tier-{group}">{chip}</span>
        </div>
        <h2 class="story-title"><a href="{url_html}" target="_blank" rel="noopener">{title}</a></h2>
        <div class="story-summary">{paras}</div>
      </div>
    </article>"""


def _filter_buttons(section: dict) -> str:
    present = [
        src for src in SOURCES
        if any(s.get("source") == src for s in section.get("stories", []))
    ]
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


def _section_nav(sections: list[dict]) -> str:
    parts = []
    for i, section in enumerate(sections):
        sid = H.escape(section["id"], quote=True)
        meta = SECTIONS.get(section["id"], {"label": section["title"], "color": "#7ee787"})
        count = len(section.get("stories", []))
        active = " active" if i == 0 else ""
        parts.append(
            f'<a class="section-tab{active}" href="#{sid}" data-section-tab="{sid}" '
            f'style="--section-color:{meta["color"]}">{H.escape(section["title"])} '
            f'<span class="section-tab-count">{count}</span></a>'
        )
    return "\n".join(parts)


def _section_block(section: dict, date_str: str, image_rels: dict[str, str]) -> str:
    sid = section["id"]
    sid_html = H.escape(sid, quote=True)
    meta = SECTIONS.get(sid, {"label": section["title"], "color": "#7ee787"})
    stories = section.get("stories", [])
    cards = []
    for idx, story in enumerate(stories, start=1):
        key = f"{sid}-{idx:02d}"
        cards.append(render_story_card(story, sid, idx, image_rels[key]))
    summary = str(section.get("summary") or "")
    summary_html = f'<p class="section-summary">{H.escape(summary)}</p>' if summary else ""
    body = "\n".join(cards)
    if not body:
        body = '<p class="empty-state">No strong items cleared the bar today.</p>'
    return f"""
  <section class="section-panel" id="{sid_html}" data-section-panel="{sid_html}" style="--section-color:{meta['color']}" aria-labelledby="heading-{sid_html}">
    <div class="section-heading">
      <div>
        <p class="section-kicker">daily section</p>
        <h1 id="heading-{sid_html}">{H.escape(section["title"])}</h1>
        {summary_html}
      </div>
      <span class="section-count" id="count-{sid_html}">{len(stories)} stories</span>
    </div>
    <section class="filters" data-section-filters="{sid_html}" aria-label="{H.escape(section["title"])} filters">
      <span class="filter-label">Source</span>
      {_filter_buttons(section)}
    </section>
    <section class="stories" data-section-stories="{sid_html}">
      {body}
    </section>
  </section>"""


def render_edition_page(edition: dict, section_nav: str, section_blocks: str,
                        prev_href: str, next_href: str, target: Path) -> None:
    tpl = (TEMPLATES / "edition.html").read_text()
    date_iso = edition["date"]
    count = _story_count(edition)
    page = (
        tpl
        .replace("{{SITE_NAME}}", SITE_NAME)
        .replace("{{TAGLINE}}", H.escape(TAGLINE, quote=True))
        .replace("{{DATE_ISO}}", date_iso)
        .replace("{{DATE_DISPLAY}}", _fmt_date(date_iso))
        .replace("{{COUNT}}", str(count))
        .replace("{{SECTION_NAV}}", section_nav)
        .replace("{{SECTION_BLOCKS}}", section_blocks)
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
        href = f"{d}.html#ai"
        if d == editions[-1]["date"]:
            href = "./#ai"
        counts = " · ".join(
            f"{H.escape(section['title'])} {len(section.get('stories', []))}"
            for section in e.get("_sections", [])
            if section.get("stories")
        )
        rows.append(
            f'<li class="arch-row"><a href="{href}">{d}</a>'
            f'<span class="arch-meta">{_story_count(e)} stories · {counts}</span></li>'
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

    for i, ed in enumerate(editions):
        d = ed["date"]
        entries = [
            (f"{section['id']}-{idx:02d}", story)
            for section in ed["_sections"]
            for idx, story in enumerate(section.get("stories", []), start=1)
        ]
        img_dir = PAGES_DIR / "images" / d
        rels = build_story_images(img_dir, d, entries)
        blocks = [
            _section_block(section, d, rels)
            for section in ed["_sections"]
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
        render_edition_page(
            ed, _section_nav(ed["_sections"]), "\n".join(blocks),
            prev_el, next_el, target,
        )
        print(f"[build] rendered {d}.html ({_story_count(ed)} stories, "
              f"{len(ed['_sections'])} sections)")

    # index = latest edition
    shutil.copy2(PAGES_DIR / f"{editions[-1]['date']}.html", PAGES_DIR / "index.html")
    render_archive(editions)
    prune_stale_images()
    print(f"[build] rendered index.html + archive.html")
    deploy(editions[-1]["date"], no_deploy)


if __name__ == "__main__":
    main()
