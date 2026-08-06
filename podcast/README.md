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

The plan estimate uses `estimated_chars_per_second`, calibrated against real
renders — the 2026-08-05 episode spoke 9,405 characters in 702 seconds of
speech, so the rate is about 13.4. Every render records
`measured_chars_per_second` in its manifest; this is speed-normalized as
`sum(characters / effective_speaker_speed) / speech_seconds`, so it remains safe
to copy into the config when speakers use different speeds. Retune the config
from those values rather than guessing. An over-optimistic rate is expensive:
it lets a script plan inside the budget and then blow the 15-minute ceiling
after the whole episode has been synthesized.

## Plan before speech

The plan command validates the script, checks the duration estimate, counts turns
and characters, runs the editorial acceptance checks, and reports shared-cache
hits. It never contacts the TTS server and never writes audio:

```bash
python3 podcast/pipeline.py --plan --date YYYY-MM-DD \
  --script podcast/runs/YYYY-MM-DD/script.json
```

A failing plan lists every problem at once, so revise the script and re-plan
rather than fixing one line per run. Planning is free; a render is not.

Only after the plan passes should the job render:

```bash
python3 podcast/pipeline.py --render --date YYYY-MM-DD \
  --script podcast/runs/YYYY-MM-DD/script.json
```

OmniVoice saved-prompt cloning is the default renderer. It runs on the local
speech service through `/upstream/omnivoice/v1/audio/clone`; the configured
voices are the server-side prompt IDs `bella` (Maya), `morgan-freeman` (Arjun),
and `shweta` (Shweta). Per-speaker speed overrides live in `speed_by_speaker`;
the current configuration keeps Maya and Shweta at `1.0` and renders Arjun at
`1.15`. Each clone response is validated as WAV and converted to the pipeline's
cached MP3, while the content-addressed cache remains outside the repository at
`/opt/data/cache/grep-podcast/tts`. A cached turn is keyed by the exact text,
voice prompt ID, clone settings, model/provider identity, effective per-speaker
speed, and endpoint.
The legacy Kokoro-compatible JSON speech path remains available only when
`tts_provider` is explicitly set to `kokoro`. Voice IDs can still be overridden
with environment variables such as `PODCAST_VOICE_HOST_FEMALE`.

The renderer also supports an optional `pause_after_seconds` on each turn. This
lets the script use shorter handoff pauses for rapid exchanges and longer pauses
around transitions without synthesizing extra speech. Turns without that field
use the configured default of 0.32 seconds. When using OmniVoice, scripts may
insert documented non-verbal tags inline, such as `[laughter]`, `[sigh]`,
`[question-en]`, `[question-ah]`, and `[surprise-oh]`. Use them sparingly—at
most one tag per sentence, positioned where the reaction belongs. These tags
make a specific sound; they do not set an overall emotion, so do not scatter
them through every turn or use undocumented tags.

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
`comparison`, `transition`, `section-transition`, `guest-perspective`,
`guest-intro`, `guest-thanks`, and `outro`.

## Speakers

`podcast/personas.json` holds the show's stable identities: the same two hosts
and the same recurring contributor every day, with their voice IDs and their
areas of interest. Read it before writing a script. The names are not
decoration — the hosts address each other, and `--plan` requires it.

All three voices are synthetic. The outro says so; that disclosure is part of
the episode, not just the show notes.

## Editorial constraints for new scripts

Structural rules, all enforced by `--plan`:

- Select about 8–10 stories across all three sections, with 2–3 genuine deep
  dives; do not pad the episode with unsupported claims or repeated facts.
  **Cutting a story is always better than padding one.**
- Keep the final episode in the 10–15 minute range; aim for 11–14 minutes in the
  plan estimate. Rough per-segment budget: intro ≤45s, quick story 40–70s, deep
  dive 2.5–3.5 min, outro ≤30s.
- A quick story needs at least two turns; a deep dive needs at least four, with
  a real exchange rather than a recitation.
- Both recurring hosts must appear in every story, and both must open a fair
  share of the stories.
- Do not let one speaker take more than two consecutive turns.
- Repeat `story_title` on every turn belonging to that story.

Rules that exist because a structurally valid script can still sound generated:

- **Vary the shape.** No two deep dives may open on the same three beats or use
  the same set of beats, and the quick stories may not all share one beat shape
  or all be the same length. The beat vocabulary is a validation label, not a
  template to recite.
- **Vary the length.** Turns run from about 45 to 720 characters. An episode
  needs at least four genuine short reactions of 110 characters or less, and
  enough spread overall that it does not read as alternating paragraphs. The
  45-character floor is not stylistic: OmniVoice renders very short clips badly.
- **Give the contributor an arc.** If the guest speaks at all, exactly one
  `guest-intro` turn brings them in with a concrete reason before they speak, a
  host questions or pushes back on them at least once, and one `guest-thanks`
  turn closes the arc. The guest may not sit at the same position in every
  story.
- **Write for the ear.** No spoken URLs, domains, or arXiv identifiers — say it
  in words and put the link in the show notes. Spell out model and version
  strings the way a person would say them, and gloss an unfamiliar acronym the
  first time. When directly addressing a person by name, use an exclamation mark
  rather than a comma — write `Maya! Shweta read the release`, not
  `Maya, Shweta read the release`; the comma makes TTS hear a list of names.
- **No production language and no cross-story thesis.** Ten unrelated stories do
  not share a theme, and claiming they do in the intro or outro is the clearest
  sign a model wrote it. Episode outlines, "let's get into it", and canned
  "not X, it's Y" contrasts are rejected. So is a title that describes how the
  episode was made rather than what is in it.
- Keep factual claims bounded by the original story and the researched sources.
  The guest may explain an argument in new words but must not fabricate
  quotations or impersonate an author.

### Expressive cues

Documented OmniVoice cues are `[laughter]`, `[sigh]`, `[question-en]`,
`[question-ah]`, and `[surprise-oh]`. Undocumented tags reach the TTS as literal
bracketed text, so the set is closed and enforced. A cue makes one non-verbal
sound; it does not set the speaker's emotion.

- At most one cue per sentence, and at most eight in an episode (3–6 is better).
- A cue must never be a turn on its own — OmniVoice handles a one-second clip
  poorly, and the turn floor applies to the text with cues stripped out.
- A cue must never trail a turn. Put it beside the words that trigger it:
  `"Really [surprise-oh] — that tripled the yield strength?"`, not
  `"That tripled the yield strength. [surprise-oh]"`.

### Reference script

`podcast/fixtures/humanized-example.json` is a complete schema-v2 script that
satisfies every check, kept as proof the constraints are jointly satisfiable and
as a shape to imitate. `podcast/fixtures/test_editorial_checks.py` asserts that
each check fires on its own defect. Both run without contacting the TTS server:

```bash
python3 podcast/fixtures/build_humanized_example.py
python3 podcast/fixtures/test_editorial_checks.py
```

Legacy schema-v1 scripts remain readable for already-existing or partially
rendered runs, but all new episodes should use schema v2. Sidecar-less raw audio
is reused only for explicitly recorded, unique segment indices when the v1 run
manifest proves the matching script, edition, complete render-configuration
identity (including TTS speed), per-turn cache key, path, and raw artifact hash.
Unlisted or unverifiable legacy files are synthesized again. Older markers are
rebuilt safely. Completion fast paths revalidate those identities, the final
episode hash, all raw/WAV/pause artifacts, and the current duration policy before
accepting `done.json`.
