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

import pipeline as p  # noqa: E402

GOOD = json.loads((BASE / "humanized-example.json").read_text(encoding="utf-8"))


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
]


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
    print(f"\n{len(CASES) + 1 - failed}/{len(CASES) + 1} checks behaved as specified")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
