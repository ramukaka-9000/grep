# grep

`$ grep signal /internet` — a daily signal hunt across Hacker News, arXiv,
GitHub, Reddit and the wider web, published as a static site:

**https://ramukaka-9000.github.io/grep/**

## How it works (the whole daily loop)

1. `python3 fetch_sources.py` — deterministic collectors pull raw candidate
   stories from HN, arXiv and GitHub into `content/candidates/<date>.json`.
   (Reddit is network-blocked on this host, so reddit picks are sourced via
   web search during curation; honest per-day caps are enforced at curation.)
2. A Hermes agent curates: picks up to `hn:4 arxiv:2 github:2 other:4
   reddit:2`, **max 12 total**, dedups against the seen-items ledger, and
   writes `content/<date>.json` — one entry per story with title, url,
   author, source, tier (`notable|recommended|must-read|essential`), a
   discuss link, and a 2–4 sentence "why it matters" summary.
3. `python3 build.py` — renders every edition to static HTML, fetches/caches
   each story's OG image (styled placeholder fallback), regenerates
   `index.html` + `archive.html`, and pushes to the `gh-pages` branch.

The agent never hand-edits HTML or layout — that's all produced from JSON by
the templates in `templates/` and styles in `assets/`. To restyle, edit
`assets/style.css` once.

## Structure

```
fetch_sources.py          # HN / arXiv / GitHub collectors -> content/candidates/
build.py                  # render + image cache + deploy -> gh-pages
templates/                # edition.html, archive.html (tokens substituted by build.py)
assets/                   # style.css, app.js, favicon.svg
content/<YYYY-MM-DD>.json # curated edition (permanent archive)
content/candidates/       # raw per-day source dumps (gitignored)
```
