# grep podcast pipeline

The daily grep edition is the source of truth. The editorial cron agent reads
`content/YYYY-MM-DD.json`, researches selected stories with Degoog, and writes a
structured script to `podcast/runs/YYYY-MM-DD/script.json`.

The deterministic renderer then turns that script into local speech:

```bash
python3 podcast/pipeline.py --render --date YYYY-MM-DD \
  --script podcast/runs/YYYY-MM-DD/script.json
```

It creates `episode.mp3`, `show-notes.md`, `manifest.json`, and `done.json`.
Kokoro is the default renderer. Voices can be overridden with environment
variables such as `PODCAST_VOICE_HOST_FEMALE`.

After the grep job has successfully deployed an edition, it should run:

```bash
python3 podcast/pipeline.py --mark-ready --date YYYY-MM-DD
```

The marker is intentionally separate from `content/YYYY-MM-DD.json`: it is only
written when the deployed `gh-pages` worktree contains the dated page and the
latest commit subject is `edition YYYY-MM-DD`.

## Script shape

```json
{
  "date": "YYYY-MM-DD",
  "title": "grep podcast — ...",
  "description": "...",
  "disclaimer": "The guest perspective is a synthetic interpretation...",
  "segments": [
    {"speaker": "host_female", "kind": "intro", "text": "..."},
    {"speaker": "host_male", "kind": "quick", "story_title": "...", "text": "..."},
    {"speaker": "guest", "kind": "deep-dive", "story_title": "...", "text": "..."}
  ],
  "show_notes": [
    {
      "title": "...",
      "url": "https://...",
      "section": "AI",
      "kind": "quick",
      "summary": "...",
      "additional_sources": [{"title": "...", "url": "https://..."}]
    }
  ]
}
```
