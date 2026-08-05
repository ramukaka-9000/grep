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

After rendering and validating an episode, publish its final MP3 to the tracked
site source and rebuild the static site:

```bash
python3 podcast/publish.py --date YYYY-MM-DD
python3 build.py
git add podcast/episodes/YYYY-MM-DD.mp3
git commit -m "podcast: publish YYYY-MM-DD audio"
git push origin main
```

`publish.py` never renders audio; it only validates the existing episode and
copies it from `podcast/runs/YYYY-MM-DD/episode.mp3` to the durable
`podcast/episodes/YYYY-MM-DD.mp3` source artifact. `build.py` mirrors tracked
episodes into `gh-pages/audio/YYYY-MM-DD/episode.mp3` and adds the player to
the matching dated edition page.

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
