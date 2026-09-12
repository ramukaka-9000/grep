#!/usr/bin/env python3
"""Append today's edition items to seen-items.json and prune >60 days."""
import json
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
LEDGER = BASE / "seen-items.json"
TODAY = date(2026, 8, 17)
CUTOFF = TODAY - timedelta(days=60)  # keep items with last_seen >= cutoff

def norm(url: str) -> str:
    from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "source")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), urlencode(q), ""))

edition = json.load(open(BASE / "content" / f"{TODAY.isoformat()}.json", encoding="utf-8"))

new_entries = []
for sec in edition["sections"]:
    section = sec["id"]
    for st in sec["stories"]:
        new_entries.append({
            "url": norm(st["url"]),
            "title": st["title"],
            "source": st["source"],
            "section": section,
            "first_seen": TODAY.isoformat(),
            "last_seen": TODAY.isoformat(),
        })

ledger = json.load(open(LEDGER, encoding="utf-8"))
items = ledger["items"]

# prune: drop entries whose last_seen is before cutoff
before = len(items)
items = [it for it in items if it.get("last_seen", it.get("first_seen", "")) >= CUTOFF.isoformat()]
after = len(items)

# normalize existing URLs too (retrofit)
seen = {}
for it in items:
    it["url"] = norm(it.get("url", ""))
    seen[it["url"]] = it

dedupe_added = 0
for e in new_entries:
    if e["url"] in seen:
        old = seen[e["url"]]
        # update last_seen, merge section list if different
        old["last_seen"] = TODAY.isoformat()
        if old.get("section") != e["section"]:
            old["section"] = e["section"]
        dedupe_added += 1
    else:
        items.append(e)
        seen[e["url"]] = e

items.sort(key=lambda x: x.get("first_seen", ""))
ledger["items"] = items
json.dump(ledger, open(LEDGER, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print(f"pruned {before-after} old item(s); kept {len(items)}")
print(f"added {len(new_entries)-dedupe_added} new entry(ies); updated {dedupe_added} existing (should be 0)")
