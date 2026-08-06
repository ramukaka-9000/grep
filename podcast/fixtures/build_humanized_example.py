#!/usr/bin/env python3
"""Build the reference schema-v2 script that satisfies every editorial check.

This exists so the acceptance checks in pipeline.py are provably satisfiable:
a lint that no realistic episode can pass would deadlock the daily job. It
repairs the 2026-08-05 production script - which fails seventeen checks - into
the shape the checks are asking for, and writes it to humanized-example.json.

Run: python3 podcast/fixtures/build_humanized_example.py
"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
SOURCE = BASE.parent / "runs" / "2026-08-05" / "script.json"
TARGET = BASE / "humanized-example.json"


def seg(speaker, kind, beat, text, story=None, pause=None):
    out = {"speaker": speaker, "kind": kind, "beat": beat, "text": text}
    if story:
        out["story_title"] = story
    if pause is not None:
        out["pause_after_seconds"] = pause
    return out


def main() -> int:
    script = json.loads(SOURCE.read_text(encoding="utf-8"))
    old = script["segments"]

    def find(story_fragment, index):
        matches = [s for s in old if story_fragment in (s.get("story_title") or "")]
        return matches[index]

    SHIELD = "Mistral's Shieldstral: 3B open-weights model for multimodal moderation"
    TURN = "TurnSight: Turn-Level Hindsight Self-Distillation for Tool-Integrated Reasoning"
    PUNCH = "NASA's PUNCH Sharpens Solar Storm Forecasting in First Test"

    segments = [
        # Concrete hook, no cross-story thesis, no lineup.
        seg("host_female", "intro", "hook",
            "Welcome back to grep. Mistral shipped a moderation model you argue with in "
            "plain English - you hand it the rule you care about at inference time, and it "
            "scores against that rule instead of whatever was baked into the weights.",
            pause=0.25),
        seg("host_male", "intro", "reaction",
            "Three billion parameters, and the policy lives in the request.", pause=0.55),

        # Quick story: opens on a reaction, three turns, host_male first.
        seg("host_male", "quick", "setup",
            "Sean Goedecke has an uncomfortable observation about who these models actually "
            "help. They are most useful when the person at the keyboard already knows enough "
            "to doubt them.",
            story="LLMs reward expertise", pause=0.2),
        seg("host_female", "quick", "challenge",
            "Which is the opposite of how they get sold, Arjun. [question-en] Isn't the whole "
            "pitch that you don't need the expertise?",
            story="LLMs reward expertise", pause=0.22),
        seg("host_male", "quick", "answer",
            "Right, and that's the gap he's poking at. The model supplies pieces of an answer; "
            "the expertise supplies the error signal. A specialist spots the missing "
            "assumption, rejects the plausible dead end, and notices when fluent prose has "
            "quietly gone off the rails - and none of that shows up in a benchmark score, "
            "because the benchmark never had a bad day in production.",
            story="LLMs reward expertise", pause=0.55),

        # Deep dive 1: guest introduced, guest enters at position 3.
        seg("host_female", "deep-dive", "setup",
            "Back to Shieldstral, because the mechanism is stranger than the headline. The "
            "request carries three things: an instruction, a yes-or-no query, and the document "
            "being judged - text, an image, or a whole prompt-and-response pair. So one "
            "checkpoint can police violence for a games product, privacy for a health app, and "
            "something much narrower for a classroom tool, without anyone fine-tuning it.",
            story=SHIELD, pause=0.28),
        seg("host_male", "deep-dive", "guest-intro",
            "Ravi read the technical report end to end this week, so let's get the part that "
            "isn't in the announcement.",
            story=SHIELD, pause=0.35),
        seg("guest", "deep-dive", "guest-perspective",
            "The part that isn't in the announcement is how the score comes out. It reads the "
            "yes and no logits and turns them into a continuous value, so you don't get a "
            "verdict, you get a ranking. That's a different thing to build a review queue on - "
            "you can tune where the human looks without retraining anything. And the report "
            "puts it ahead of considerably larger guard models, including on the multimodal "
            "tests, which is the claim I'd want reproduced first.",
            story=SHIELD, pause=0.3),
        seg("host_male", "deep-dive", "challenge",
            "That also moves responsibility onto the operator, though. Wording, strictness, "
            "and the threshold all become part of the safety system.",
            story=SHIELD, pause=0.28),
        seg("guest", "deep-dive", "qualification",
            "It does, and the report is fairly candid about that. A score ranks the borderline "
            "cases; it can't tell you whether the policy was sensible in the first place, or "
            "what the team should do once the model says yes.",
            story=SHIELD, pause=0.3),
        seg("host_female", "deep-dive", "takeaway",
            "So: a compact configurable filter, not an oracle. The test is whether teams keep "
            "the policy explicit and reviewable.",
            story=SHIELD, pause=0.55),

        # Quick pair, two turns, female opens.
        seg("host_female", "quick", "setup",
            "A survey of 197 real terminal user interfaces found only 12 percent of test code "
            "actually exercises the interface, and 45 percent of those tests never send input "
            "beyond a static frame. In the benchmark, finding the right launch inputs mattered "
            "more than exploring harder once you were inside.",
            story="Can LLMs Test Terminal User Interfaces?", pause=0.2),
        seg("host_male", "quick", "implication",
            "So a screenshot looks perfect while focus, keybindings, resizing, and every error "
            "path stay untouched. An agent can help with the exploring, but it still needs a "
            "real way into the application - otherwise it's very diligently testing the blank "
            "terminal around the program.",
            story="Can LLMs Test Terminal User Interfaces?", pause=0.8),

        # Section transition into Electronics.
        seg("host_male", "quick", "section-transition",
            "Speaking of machines doing more than they should - the Atari ST finally has a "
            "full-motion video codec that runs in eight megahertz.",
            story="A Full Motion Video Codec For The Atari ST", pause=0.9),
        seg("host_female", "quick", "setup",
            "The Atari ST had an eight-megahertz sixty-eight-thousand, planar graphics, and "
            "nowhere near the bandwidth for anything resembling modern video. This codec gets "
            "around that with codebook-addressed blocks, and it keeps streaming palette and "
            "codebook updates as it plays, so the sixteen colours on screen aren't the same "
            "sixteen colours a second later.",
            story="A Full Motion Video Codec For The Atari ST", pause=0.22),
        seg("host_male", "quick", "reaction",
            "The format follows the machine's memory layout, so the old hardware moves "
            "something it can actually handle.",
            story="A Full Motion Video Codec For The Atari ST", pause=0.55),

        seg("host_male", "quick", "setup",
            "The carbon-fibre print goes the other way - physical, not clever. Print a core and "
            "shell, leave a shallow gap, epoxy carbon-fibre cloth into the finished part.",
            story="Strengthening 3D Prints With A Carbon-Fiber Epidermis", pause=0.2),
        seg("host_female", "quick", "reaction",
            "And in the load-cell tests that more than tripled yield strength. [surprise-oh] "
            "The geometry is doing as much work as the material there.",
            story="Strengthening 3D Prints With A Carbon-Fiber Epidermis", pause=0.22),
        seg("host_male", "quick", "qualification",
            "For that hook, anyway. The joint, the epoxy, and the load direction all decide "
            "whether it travels to another part.",
            story="Strengthening 3D Prints With A Carbon-Fiber Epidermis", pause=0.8),

        # Deep dive 2: different opening beats, guest enters at position 4.
        seg("host_male", "deep-dive", "setup",
            "NASA's PUNCH mission narrowed a solar-storm forecast window to about 30 minutes "
            "in its first operational test. Four spacecraft in low Earth orbit keep the inner "
            "solar system under continuous three-dimensional observation, with a fresh image "
            "roughly every four minutes.",
            story=PUNCH, pause=0.25),
        seg("host_female", "deep-dive", "question",
            "So is that 30 minutes warning time, or arrival-time accuracy?",
            story=PUNCH, pause=0.28),
        seg("host_male", "deep-dive", "answer",
            "Accuracy, and the distinction matters a lot. A coronal mass ejection stays "
            "visible much farther from the Sun than it used to, so the model gets a long "
            "track to work from rather than a departure point and a guess.",
            story=PUNCH, pause=0.25),
        seg("host_female", "deep-dive", "reaction",
            "So the storm is already travelling by the time any of this helps.",
            story=PUNCH, pause=0.25),
        seg("guest", "deep-dive", "guest-perspective",
            "Travelling, but trackable. On a May 2025 ejection the model watched the leading "
            "edge evolve and, about 12 hours in, put arrival eight hours out. It landed within "
            "roughly half an hour, against a five-hour window for the current method.",
            story=PUNCH, pause=0.3),
        seg("host_female", "deep-dive", "qualification",
            "With the caveat that this is one proof-of-concept event under journal review. It "
            "doesn't buy an extra day of warning; it makes the forecast less blurry.",
            story=PUNCH, pause=0.28),
        seg("host_male", "deep-dive", "implication",
            "Half an hour still changes a decision if the uncertainty was five hours before. "
            "Grid operators, satellite teams, and anyone with people on orbit aren't really "
            "asking whether a storm is coming - they know that part. They're asking whether it "
            "lands during a window where they'd have to do something expensive, and a "
            "five-hour smear covers an awful lot of expensive windows.",
            story=PUNCH, pause=0.3),
        seg("host_female", "deep-dive", "takeaway",
            "The next test is repetition - forecasters will want to see it across many "
            "eruptions before they lean on it.",
            story=PUNCH, pause=0.8),

        # Deep dive 3: no guest, distinct shape.
        seg("host_female", "deep-dive", "setup",
            "TurnSight is about a training problem that shows up the moment an agent uses "
            "tools. One run contains a dozen decisions, and the reward arrives once, at the end.",
            story=TURN, pause=0.28),
        seg("host_male", "deep-dive", "comparison",
            "Token-level feedback doesn't fix that either - a tool call is a decision followed "
            "by an observation, so the reward is precise about the wrong unit.",
            story=TURN, pause=0.25),
        seg("host_female", "deep-dive", "answer",
            "Their answer is to stop forcing every rollout to imitate a reference path and "
            "evaluate the states the policy actually visited. It builds hindsight views at "
            "several lookahead horizons, keeps the supervision where those views agree, "
            "normalizes across sibling rollouts, and uses what survives to shape the "
            "reinforcement-learning advantages.",
            story=TURN, pause=0.28),
        seg("host_male", "deep-dive", "reaction",
            "Which respects the fact that the world changed. Once the agent makes a different "
            "call, the reference trajectory is describing a world that no longer exists.",
            story=TURN, pause=0.25),
        seg("host_female", "deep-dive", "qualification",
            "It's a training method, not a reliability guarantee. Three benchmarks, code "
            "released, and the hard part is still choosing environments that reflect real "
            "tool behaviour.",
            story=TURN, pause=0.3),
        seg("host_male", "deep-dive", "takeaway",
            "Clean principle, though: the unit of learning should look like the unit of "
            "interaction.",
            story=TURN, pause=0.8),

        # Interesting News block.
        seg("host_male", "quick", "setup",
            "A used Falcon 9 upper stage from the January 2025 Blue Ghost launch is expected to "
            "hit the Moon near the Einstein and Bell craters. NASA plans ground observations "
            "plus before-and-after views from Lunar Reconnaissance Orbiter and South Korea's "
            "ShadowCam. Call it a sixty-foot crater, and no danger to anything down here.",
            story="NASA Will Attempt to Observe Rocket Part's Lunar Impact", pause=0.2),
        seg("host_female", "quick", "reaction",
            "A planned crash is oddly useful - a known event to check crater models against.",
            story="NASA Will Attempt to Observe Rocket Part's Lunar Impact", pause=0.22),
        seg("host_male", "quick", "qualification",
            "The live view may well be underwhelming. Weather, lighting, and orbital timing "
            "could leave the follow-up science more interesting than the moment.",
            story="NASA Will Attempt to Observe Rocket Part's Lunar Impact", pause=0.55),

        seg("host_female", "quick", "reaction",
            "A rove beetle named after One Piece's Monkey D. Luffy - [laughter] that is a "
            "first. Two species from Yunnan and northern Laos, with unusually long mandibles "
            "and palps.",
            story="New Beetle Genus Named After One Piece's Monkey D. Luffy", pause=0.2),
        seg("host_male", "quick", "implication",
            "The name makes the specimen memorable and the anatomy still has to justify the "
            "genus. A joke opens the door; the classification carries the weight.",
            story="New Beetle Genus Named After One Piece's Monkey D. Luffy", pause=0.8),

        seg("host_male", "quick", "setup",
            "Last one, and it's a measurement story. Metal printing is usually described "
            "through temperature and cooling rate, because that's what you can instrument. "
            "This Nature Communications group used synchrotron X-ray scattering and rapid "
            "pair-distribution analysis to watch Inconel 718 form, tracking short- and "
            "medium-range atomic ordering while the laser was still working.",
            story="New Operando X-Ray Method Could Give Metal 3D Printing a Real-Time Control Lever",
            pause=0.2),
        seg("host_female", "quick", "reaction",
            "Watching atomic ordering happen, not inferring it afterwards.",
            story="New Operando X-Ray Method Could Give Metal 3D Printing a Real-Time Control Lever",
            pause=0.22),
        seg("host_male", "quick", "implication",
            "Which turns it into a feedback problem - adjust the laser before the defect sets. "
            "Single-track conditions so far, but that's a richer signal than another "
            "temperature reading.",
            story="New Operando X-Ray Method Could Give Metal 3D Printing a Real-Time Control Lever",
            pause=0.6),

        # Guest exit, then a sign-off that claims nothing about a theme.
        seg("host_female", "outro", "guest-thanks",
            "Thanks Ravi - the Shieldstral report and the PUNCH numbers were the two we'd have "
            "got wrong on our own.",
            pause=0.4),
        seg("host_male", "outro", "outro",
            "That's grep for today. Every source and the extra reading are in the show notes, "
            "and the guest turns are synthetic interpretations rather than quotations. See you "
            "tomorrow.",
            pause=0),
    ]

    script["title"] = "grep podcast — Shieldstral, PUNCH forecasting, and a beetle named Luffy"
    script["description"] = (
        "The 2026-08-05 grep edition as a two-host discussion with a guest on the "
        "Shieldstral and PUNCH deep dives."
    )
    script["segments"] = segments
    TARGET.write_text(
        json.dumps(script, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {TARGET} ({len(segments)} segments)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
