# grep podcast pipeline

The daily grep edition is the source of truth. The editorial cron agent reads
`content/YYYY-MM-DD.json`, researches selected stories with Degoog, and writes a
structured conversational script to `podcast/runs/YYYY-MM-DD/script.json`.

New scripts use schema version 2. The renderer requires both recurring hosts to
participate in every story, keeps quick stories as short exchanges, and reserves
longer multi-turn conversations for deep dives. The guest is a synthetic
interpretation of a cited article's argument, never an impersonation or invented
quotation.

The episode budget is deliberate: the final MP3 must be between **10 and 15
minutes**. New scripts should aim for roughly 11–14 minutes so the estimate and
actual speech duration have some margin. The renderer rejects an episode outside
that range and never writes `done.json` for it.

## Plan before speech

The plan command validates the script, checks the duration estimate, counts turns
and characters, and reports shared-cache hits. It never contacts the TTS server
and never writes audio:

```bash
python3 podcast/pipeline.py --plan --date YYYY-MM-DD \
  --script podcast/runs/YYYY-MM-DD/script.json
```

Only after the plan passes should the job render:

```bash
python3 podcast/pipeline.py --render --date YYYY-MM-DD \
  --script podcast/runs/YYYY-MM-DD/script.json
```

Kokoro is the default renderer. It runs on the local speech service and is
CPU-only, so the renderer uses a content-addressed cache outside the repository
at `/opt/data/cache/grep-podcast/tts`. A cached turn is keyed by the exact text,
voice, speed, model, and endpoint. This prevents duplicate TTS work on retries
or script revisions and avoids reusing stale same-index audio. Voices can be
overridden with environment variables such as
`PODCAST_VOICE_HOST_FEMALE`.

The renderer also supports an optional `pause_after_seconds` on each turn. This
lets the script use shorter handoff pauses for rapid exchanges and longer pauses
around transitions without synthesizing extra speech. Turns without that field
use the configured default of 0.32 seconds.

It creates `episode.mp3`, `show-notes.md`, `manifest.json`, and `done.json`.
The manifest records the estimated and actual duration, cache sources, turn
beats, script/edition/config identity hashes, per-artifact hashes, the final
episode hash, and the final ffprobe data. A completion marker is accepted only
when those identities, the current duration policy, and every numbered
raw/WAV/pause artifact still validate. Preview runs may use a safe label such as
`YYYY-MM-DD-preview`; path separators and traversal components are rejected.

## Gated handoff and publication

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

`publish.py` never renders audio; it validates the existing episode against the
10–15 minute policy and only then copies it from
`podcast/runs/YYYY-MM-DD/episode.mp3` to the durable
`podcast/episodes/YYYY-MM-DD.mp3` source artifact. `build.py` mirrors tracked
episodes into `gh-pages/audio/YYYY-MM-DD/episode.mp3` and adds the player to the
matching dated edition page.

## Script shape

```json
{
  "schema_version": 2,
  "date": "YYYY-MM-DD",
  "title": "grep podcast — ...",
  "description": "...",
  "disclaimer": "The guest perspective is a synthetic interpretation...",
  "segments": [
    {
      "speaker": "host_female",
      "kind": "intro",
      "beat": "hook",
      "pause_after_seconds": 0.25,
      "text": "..."
    },
    {
      "speaker": "host_male",
      "kind": "quick",
      "beat": "reaction",
      "story_title": "...",
      "pause_after_seconds": 0.20,
      "text": "..."
    },
    {
      "speaker": "guest",
      "kind": "deep-dive",
      "beat": "guest-perspective",
      "story_title": "...",
      "pause_after_seconds": 0.55,
      "text": "..."
    }
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

Allowed schema-v2 beats are `hook`, `setup`, `question`, `reaction`, `answer`,
`challenge`, `counterpoint`, `qualification`, `implication`, `takeaway`,
`comparison`, `transition`, `guest-perspective`, and `outro`.

Editorial constraints for new scripts:

- Select about 8–10 stories across all three sections, with 2–3 genuine deep
  dives; do not pad the episode with unsupported claims or repeated facts.
- Keep the final episode in the 10–15 minute range; aim for 11–14 minutes in the
  plan estimate.
- A quick story needs at least two turns and should move from setup to a host
  reaction, question, comparison, or takeaway.
- A deep dive needs at least four turns and should contain real exchange:
  question, answer, qualification, implication, or substantive counterpoint.
- Both recurring hosts must appear in every story. Use the guest only for
  selected deep dives.
- Keep individual turns short enough to sound conversational; the renderer caps
  schema-v2 turns at 720 characters, with roughly 120–350 characters preferred.
- Do not let one speaker take more than two consecutive turns.
- Repeat `story_title` on every turn belonging to that story so validation and
  show-note auditing can group the conversation correctly.
- Keep factual claims bounded by the original story and the researched sources.
  A guest may explain an article's argument in new words but must not fabricate
  quotations or impersonate its author.

Legacy schema-v1 scripts remain readable for already-existing or partially
rendered runs, but all new episodes should use schema v2. Sidecar-less raw audio
is reused only for explicitly recorded, unique segment indices when the v1 run
manifest proves the matching script, edition, complete render-configuration
identity (including TTS speed), per-turn cache key, path, and raw artifact hash.
Unlisted or unverifiable legacy files are synthesized again. Older markers are
rebuilt safely. Completion fast paths revalidate those identities, the final
episode hash, all raw/WAV/pause artifacts, and the current duration policy before
accepting `done.json`.
