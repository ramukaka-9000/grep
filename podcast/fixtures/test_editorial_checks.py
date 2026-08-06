#!/usr/bin/env python3
"""Assert each editorial check fires on its own defect and not otherwise.

The good fixture must pass; every mutation of it must fail with the matching
message. Run with no TTS and no rendering:

    python3 podcast/fixtures/test_editorial_checks.py
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
sys.path.insert(0, str(BASE))

import pipeline as p  # noqa: E402
import build_humanized_example as builder  # noqa: E402

# Build the reference script in memory (no writes) so the harness is read-only.
GOOD = builder.build()
# Drift guard: the checked-in fixture must match the builder's output exactly.
CHECKED_IN = json.loads((BASE / "humanized-example.json").read_text(encoding="utf-8"))
if p.canonical_json_hash(GOOD) != p.canonical_json_hash(CHECKED_IN):
    print("FAIL: checked-in humanized-example.json is stale; run the builder to regenerate it")
    raise SystemExit(1)


def failure(script: dict) -> str:
    cfg = p.load_config()
    try:
        p.check_editorial(script, cfg)
    except RuntimeError as exc:
        return str(exc)
    return ""


def mutate(fn):
    script = copy.deepcopy(GOOD)
    fn(script)
    return script


def first_of(script: dict, **match) -> dict:
    for seg in script["segments"]:
        if all(seg.get(k) == v for k, v in match.items()):
            return seg
    raise AssertionError(f"no segment matching {match}")


def set_trailing_cue(script):
    seg = first_of(script, beat="takeaway")
    seg["text"] = seg["text"] + " [laughter]"


def set_standalone_cue(script):
    first_of(script, beat="reaction")["text"] = "[laughter]"


def set_undocumented_cue(script):
    seg = first_of(script, beat="reaction")
    seg["text"] = "Wow [surprise] that is a lot of parameters to move around."


def set_stacked_cues(script):
    seg = first_of(script, beat="reaction")
    seg["text"] = "Really [surprise-oh] though [laughter] that number seems high to me."


def set_short_turn(script):
    first_of(script, beat="reaction")["text"] = "Huh."


def set_uniform_lengths(script):
    filler = (
        "The measurement holds up under the conditions they describe in the paper, "
        "though the sample is small and the follow-up work is not published yet."
    )
    for seg in script["segments"]:
        seg["text"] = filler


def set_identical_deep_dives(script):
    dives = [s for s in script["segments"] if s.get("kind") == "deep-dive"]
    titles = []
    for seg in dives:
        if seg["story_title"] not in titles:
            titles.append(seg["story_title"])
    a = [s for s in dives if s["story_title"] == titles[0]]
    b = [s for s in dives if s["story_title"] == titles[-1]]
    for src, dst in zip(a, b):
        dst["beat"] = src["beat"]
        dst["speaker"] = src["speaker"]


def set_uniform_quick_shape(script):
    for seg in script["segments"]:
        if seg.get("kind") == "quick":
            seg["beat"] = "setup" if seg["speaker"] == "host_female" else "reaction"
            seg["speaker"] = seg["speaker"]


def drop_guest_intro(script):
    for seg in script["segments"]:
        if seg.get("beat") == "guest-intro":
            seg["beat"] = "setup"


def drop_guest_thanks(script):
    for seg in script["segments"]:
        if seg.get("beat") == "guest-thanks":
            seg["beat"] = "outro"


def drop_guest_engagement(script):
    for i, seg in enumerate(script["segments"]):
        if seg.get("speaker") == "guest" and i + 1 < len(script["segments"]):
            nxt = script["segments"][i + 1]
            if nxt.get("beat") in p.SCHEMA_V2_GUEST_ENGAGEMENT_BEATS:
                nxt["beat"] = "takeaway"


def set_questionless_question(script):
    seg = first_of(script, beat="question")
    seg["text"] = "That number is arrival-time accuracy rather than warning time entirely."


def set_meta_title(script):
    script["title"] = "grep podcast — a more human conversation about systems"


def set_theme_assertion(script):
    first_of(script, beat="hook")["text"] = (
        "Welcome back to grep. Three very different systems today, and what they have in "
        "common is a better representation for the person using them."
    )


def set_producer_dialogue(script):
    first_of(script, beat="hook")["text"] = (
        "Welcome back to grep. In today's episode we'll cover ten stories across AI, "
        "electronics, and science, so let's get into it."
    )


def set_spoken_url(script):
    seg = first_of(script, beat="takeaway")
    seg["text"] = "You can read the whole thing at grep.shantanugoel.com if you want the detail."


def set_arxiv_id(script):
    seg = first_of(script, beat="takeaway")
    seg["text"] = "The paper is arXiv 2508.14321 if you want to check the benchmark tables."


def set_short_story_pause(script):
    seg = first_of(script, story_title="LLMs reward expertise", beat="answer")
    seg["pause_after_seconds"] = 0.2


def set_short_first_story_pause(script):
    script["segments"][1]["pause_after_seconds"] = 0.2


def set_short_section_pause(script):
    seg = first_of(script, story_title="Can LLMs Test Terminal User Interfaces?", beat="implication")
    seg["pause_after_seconds"] = 0.3


def set_question_opener(script):
    first_of(script, story_title="LLMs reward expertise", beat="setup")["beat"] = "question"


def set_unanchored_opener(script):
    seg = first_of(script, story_title="LLMs reward expertise", beat="setup")
    seg["text"] = "The report claims expertise is what makes the model useful, Arjun."


def set_continuation_opener(script):
    seg = first_of(script, story_title="LLMs reward expertise", beat="setup")
    seg["text"] = "So, the report claims expertise is what makes the model useful."


def set_subjectless_pivot_opener(script):
    seg = first_of(script, story_title="Strengthening 3D Prints With A Carbon-Fiber Epidermis", beat="setup")
    seg["beat"] = "reaction"
    seg["text"] = "The geometry is doing more work than the material in that test."


def set_subjectless_setup_opener(script):
    seg = first_of(script, story_title="LLMs reward expertise", beat="setup")
    seg["text"] = ("Sean Goedecke has an uncomfortable observation about who these models "
                   "actually help. They are most useful when the person at the keyboard "
                   "already knows enough to doubt them.")


def set_no_speaker_intro(script):
    intro = script["segments"][0]
    intro["text"] = "Welcome back to grep. Today we have a packed show."


def set_no_topic_lineup(script):
    intro = script["segments"][0]
    intro["text"] = ("Welcome back to grep. It's your hosts Maya and Arjun again, with "
                     "Shweta here for the deep dives. Today we are going to talk about "
                     "Mistral's Shieldstral guard model.")


def set_ascii_dash_pronoun(script):
    seg = first_of(script, story_title="LLMs reward expertise", beat="setup")
    seg["text"] = "-It changes how users work with these models."


def set_vocative_pronoun(script):
    seg = first_of(script, story_title="LLMs reward expertise", beat="setup")
    seg["text"] = "Arjun, it changes how users work."


def set_discourse_pronoun(script):
    seg = first_of(script, story_title="LLMs reward expertise", beat="setup")
    seg["text"] = "Well, it changes how users work."


def set_intro_after_story(script):
    intro = [s for s in script["segments"] if s.get("kind") == "intro"]
    script["segments"] = [s for s in script["segments"] if s.get("kind") != "intro"] + intro


def set_duplicate_section_note(script):
    for note in script["show_notes"]:
        if note.get("title") == "A Full Motion Video Codec For The Atari ST":
            break
    else:
        raise AssertionError("no Atari note")
    script["show_notes"].append({"title": note["title"], "url": note["url"], "section": "AI"})
    seg = first_of(script, story_title="Can LLMs Test Terminal User Interfaces?", beat="implication")
    seg["pause_after_seconds"] = 0.6


def set_numeric_section(script):
    for note in script["show_notes"]:
        if note.get("title") == "A Full Motion Video Codec For The Atari ST":
            note["section"] = 0.8
            return
    raise AssertionError("no Atari note")


def set_interstitial_bypass(script):
    for i, seg in enumerate(script["segments"]):
        if seg.get("story_title") == "LLMs reward expertise" and seg.get("beat") == "answer":
            script["segments"].insert(i + 1, {
                "speaker": "host_female", "kind": "intro", "beat": "hook",
                "text": "Quick pause for context.", "pause_after_seconds": 0.0,
            })
            return
    raise AssertionError("no LLMs answer turn found")


def set_section_gap(script):
    for note in script["show_notes"]:
        if note.get("title") == "A Full Motion Video Codec For The Atari ST":
            note.pop("section", None)
            break
    seg = first_of(script, story_title="Can LLMs Test Terminal User Interfaces?", beat="implication")
    seg["pause_after_seconds"] = 0.6


def set_transition_opener_subjectless(script):
    seg = first_of(script, story_title="Strengthening 3D Prints With A Carbon-Fiber Epidermis", beat="setup")
    seg["beat"] = "section-transition"
    seg["text"] = "Speaking of hardware builds this week."


def set_quoted_pronoun_opener(script):
    seg = first_of(script, story_title="LLMs reward expertise", beat="setup")
    seg["text"] = "\u201cIt changes how users work with these models,\u201d Goedecke writes."


def set_generic_single_token_opener(script):
    seg = first_of(script, story_title="New Operando X-Ray Method Could Give Metal 3D Printing a Real-Time Control Lever", beat="setup")
    seg["beat"] = "reaction"
    seg["text"] = "One method is worth watching here."


CASES = [
    (set_trailing_cue, "ends on the cue"),
    (set_standalone_cue, "only a cue marker"),
    (set_undocumented_cue, "undocumented cue"),
    (set_stacked_cues, "stacks more than one cue"),
    (set_short_turn, "spoken characters; the floor is"),
    (set_uniform_lengths, "vary too little"),
    (set_identical_deep_dives, "beat sequence"),
    (set_uniform_quick_shape, "same beat shape"),
    (drop_guest_intro, "exactly one 'guest-intro'"),
    (drop_guest_thanks, "'guest-thanks' turn after"),
    (drop_guest_engagement, "no host ever questions"),
    (set_questionless_question, "asks nothing"),
    (set_meta_title, "describes how the episode was made"),
    (set_theme_assertion, "cross-story theme assertion"),
    (set_producer_dialogue, "episode outline in dialogue"),
    (set_spoken_url, "spoken domain name"),
    (set_arxiv_id, "raw arXiv identifier"),
    (set_short_story_pause, "a story boundary"),
    (set_short_first_story_pause, "before the first story"),
    (set_short_section_pause, "a section boundary"),
    (set_question_opener, "cannot start on 'question'"),
    (set_unanchored_opener, "unanchored definite reference"),
    (set_continuation_opener, "continuation conjunction"),
    (set_subjectless_pivot_opener, "without naming the story's subject"),
    (set_subjectless_setup_opener, "without naming the story's subject"),
    (set_no_speaker_intro, "does not introduce every speaker"),
    (set_no_topic_lineup, "does not line up the topics"),
    (set_interstitial_bypass, "at a story boundary"),
    (set_section_gap, "a section boundary"),
    (set_transition_opener_subjectless, "without naming the story's subject"),
    (set_quoted_pronoun_opener, "unanchored pronoun"),
    (set_ascii_dash_pronoun, "unanchored pronoun"),
    (set_vocative_pronoun, "unanchored pronoun"),
    (set_discourse_pronoun, "unanchored pronoun"),
    (set_intro_after_story, "intro turns must come before the first story"),
    (set_duplicate_section_note, "more than one entry"),
    (set_numeric_section, "needs a plain string section"),
    (set_generic_single_token_opener, "without naming the story's subject"),
]


def test_helpers() -> int:
    failed = 0

    def ok(cond, label):
        nonlocal failed
        if cond:
            print(f"ok   {label}")
        else:
            failed += 1
            print(f"FAIL {label}")

    cfg = p.load_config()
    ok(
        cfg["min_story_boundary_pause"] == 0.55 and cfg["min_section_boundary_pause"] == 0.8,
        "boundary thresholds load",
    )
    cfg2 = dict(cfg)
    cfg2["min_section_boundary_pause"] = 1.0
    ok(
        p.render_config_hash(cfg) != p.render_config_hash(cfg2),
        "thresholds join the render config hash",
    )
    ok(
        p._story_subject_mentioned(
            "Strengthening 3D Prints With A Carbon-Fiber Epidermis",
            "one printed shell of a different shape",
        ),
        "morphology match: printed matches Prints",
    )
    ok(
        not p._story_subject_mentioned(
            "New Operando X-Ray Method Could Give Metal 3D Printing a Real-Time Control Lever",
            "one method is worth watching",
        ),
        "single generic title token is not a subject",
    )
    ok(
        p._story_subject_mentioned(
            "Muse Code and Muse Spark 1.2",
            "First up, we have the Muse Code release.",
        ),
        "explicit lead-in names the story",
    )
    ok(
        p._opener_problems("\u201cIt changes how users work.\u201d") == ["an unanchored pronoun"],
        "quoted pronoun opener flagged",
    )
    ok(
        p._opener_problems("\u2014It changes how users work.") == ["an unanchored pronoun"],
        "leading speech punctuation does not bypass pronoun scan",
    )
    ok(
        p._opener_problems("Maya, Perseverance caught Earth as a pixel behind Phobos.") == [],
        "vocative opener stays clean",
    )
    ok(
        p._opener_problems("-It changes how users work.") == ["an unanchored pronoun"],
        "ASCII dash does not bypass pronoun scan",
    )
    ok(
        p._opener_problems("...It changes how users work.") == ["an unanchored pronoun"],
        "ASCII ellipsis does not bypass pronoun scan",
    )
    ok(
        p._opener_problems("Well, it changes how users work.") == ["an unanchored pronoun"],
        "discourse marker does not bypass pronoun scan",
    )
    ok(
        p._opener_problems("Arjun, it changes how users work.") == ["an unanchored pronoun"],
        "vocative does not bypass pronoun scan",
    )
    ok(
        p._opener_problems("So, the report claims expertise is what makes the model useful.")
        == ["a continuation conjunction"],
        "conjunction stays detectable behind a filler",
    )
    ok(
        p._opener_problems("After that, we have TurnSight.") == [],
        "transition anchor stays clean",
    )
    ok(
        not p._story_subject_mentioned(
            "New Methods for Safety", "One method is worth watching."
        ),
        "plural generic title word is not a subject",
    )
    ok(
        not p._story_subject_mentioned(
            "A Full Motion Video Codec For The Atari ST",
            "Speaking of machines doing more than they should - two hardware builds this week.",
        ),
        "transition teaser without subject rejected",
    )
    return failed


def main() -> int:
    failed = 0
    baseline = failure(GOOD)
    if baseline:
        print("FAIL: the good fixture does not pass\n" + baseline)
        return 1
    print("ok   good fixture passes")
    for fn, expected in CASES:
        message = failure(mutate(fn))
        if expected in message:
            print(f"ok   {fn.__name__}")
        else:
            failed += 1
            print(f"FAIL {fn.__name__}: expected {expected!r}, got:\n{message or '(passed)'}")
    failed += test_helpers()
    helper_checks = 16
    total_checks = len(CASES) + 1 + helper_checks
    print(f"\n{total_checks - failed}/{total_checks} checks behaved as specified")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
