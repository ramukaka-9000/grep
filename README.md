# grep

`$ grep signal /internet` — a daily signal hunt across AI, electronics,
DIY hardware, 3D printing, science, history and other unexpectedly useful
corners of the web.

Published as a static site at:

**https://grep.shantanugoel.com/**

Each daily edition has three top-level sections:

- **AI** — models, papers, tools, systems and practical engineering
- **Electronics** — DIY electronics, ESP32 and embedded projects, tools,
  hardware hacks, 3D-printing technology and open builds
- **Interesting News** — TIL-style discoveries, surprising science, history,
  space, culture and other well-sourced oddities

## How it works (the whole daily loop)

The scheduled Hermes job runs daily at **08:30 Asia/Kolkata**.

1. `python3 fetch_sources.py` — deterministic collectors pull raw candidate
   stories into `content/candidates/<date>.json`:
   - AI candidates from Hacker News, arXiv, GitHub and Hugging Face
   - Electronics leads from Hackaday, Adafruit, Arduino and 3D Printing
     Industry
   - Interesting News leads from NASA, ESA, ScienceDaily and TIL
   - Reddit is collected during curation using the authenticated browser RSS
     path first, with Degoog search/scrape as fallback

2. A Hermes agent curates the three sections independently, verifies source
   pages, deduplicates against `seen-items.json`, and writes
   `content/<date>.json`.

   The caps are:

   | Section | Limit |
   |---|---:|
   | AI | **14 total** |
   | Electronics | **6** |
   | Interesting News | **6** |

   AI's existing per-source ceilings remain unchanged:
   `hn:4`, `arxiv:2`, `github:2`, `other:4`, `reddit:4`, `hf:2`.
   Sections are not padded when fewer strong items are available.

   Each story contains a title, source, URL, author/byline, tier
   (`notable|recommended|must-read|essential`), optional discussion link,
   and a 2–4 sentence “why it matters” summary.

3. `python3 build.py` — renders every edition to static HTML, fetches and
   caches each story's OG image with a styled placeholder fallback, renders
   the section tabs, section-specific filters and archive, regenerates
   `index.html` + `archive.html`, and pushes the result to the `gh-pages`
   branch.

The agent never hand-edits HTML or layout. Pages are produced from JSON by
the templates in `templates/` and styles in `assets/`. To restyle the site,
edit `assets/style.css` once.

## Content shape

New editions use a sectioned JSON document:

```json
{
  "date": "YYYY-MM-DD",
  "summary": "one-sentence overall signal",
  "sections": [
    {"id": "ai", "title": "AI", "summary": "...", "stories": []},
    {"id": "electronics", "title": "Electronics", "summary": "...", "stories": []},
    {"id": "interesting-news", "title": "Interesting News", "summary": "...", "stories": []}
  ]
}
```

The builder remains backward-compatible with older AI-only editions in the
archive.

## Local commands

```bash
# Collect today's raw candidates
python3 fetch_sources.py

# Render locally without committing or pushing gh-pages
python3 build.py --no-deploy

# Render and deploy to gh-pages
python3 build.py
```

## Structure

```
fetch_sources.py          # deterministic source collectors -> candidates/
build.py                  # section-aware render + image cache + deploy
templates/                # edition.html, archive.html
assets/                    # style.css, app.js, favicon.svg
content/<YYYY-MM-DD>.json # curated sectioned edition (permanent archive)
content/candidates/       # raw per-day source dumps (gitignored)
seen-items.json           # 60-day deduplication ledger
```
