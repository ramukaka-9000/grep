#!/usr/bin/env python3
"""Render a structured grep podcast script with the local speech service.

The editorial agent writes a JSON script, then invokes:

    python3 podcast/pipeline.py --plan --date YYYY-MM-DD \
        --script podcast/runs/YYYY-MM-DD/script.json
    python3 podcast/pipeline.py --render --date YYYY-MM-DD \
        --script podcast/runs/YYYY-MM-DD/script.json

The script is deliberately independent of the editor/researcher.  It validates
speaker turns, synthesizes each turn separately, assembles normalized audio,
and writes show notes plus a manifest.  It also provides --mark-ready, called
by the grep job only after a successful site deployment.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent.parent
PODCAST_DIR = BASE / "podcast"
RUNS_DIR = PODCAST_DIR / "runs"
CONTENT_DIR = BASE / "content"
PAGES_DIR = BASE.parent / "grep-pages"
CONFIG_PATH = PODCAST_DIR / "config.json"
PERSONAS_PATH = PODCAST_DIR / "personas.json"
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_TIMEOUT = 180
DEFAULT_TTS_CACHE_DIR = BASE.parent / "cache" / "grep-podcast" / "tts"
DEFAULT_MIN_DURATION_SECONDS = 600.0
DEFAULT_MAX_DURATION_SECONDS = 900.0
DEFAULT_ESTIMATED_CHARS_PER_SECOND = 13.4
DEFAULT_MAX_TURN_CHARACTERS = 720
DEFAULT_MAX_PAUSE_SECONDS = 2.0
# A story change must be audible: enough silence closing one story before the
# next story's first turn, and a longer one when the section changes too.
DEFAULT_MIN_STORY_BOUNDARY_PAUSE_SECONDS = 0.55
DEFAULT_MIN_SECTION_BOUNDARY_PAUSE_SECONDS = 0.8
# OmniVoice degrades on very short clips, so a turn has a hard character floor
# even though short reactions are exactly what makes the dialogue sound real.
DEFAULT_MIN_TURN_CHARACTERS = 45
# A turn at or below this length counts as a short reaction for variety checks.
DEFAULT_SHORT_TURN_CHARACTERS = 110
DEFAULT_MIN_SHORT_TURNS = 4
DEFAULT_MIN_TURN_LENGTH_STDEV = 55.0
DEFAULT_MAX_EXPRESSIVE_TAGS = 8
PAUSE_DURATION_TOLERANCE_SECONDS = 0.005
SCHEMA_V2_KINDS = {"intro", "quick", "deep-dive", "outro"}
SCHEMA_V2_STORY_KINDS = {"quick", "deep-dive"}
SCHEMA_V2_BEATS = {
    "hook", "setup", "question", "reaction", "answer", "challenge",
    "counterpoint", "qualification", "implication", "takeaway",
    "comparison", "transition", "guest-perspective", "outro",
    "guest-intro", "guest-thanks", "section-transition",
}
SCHEMA_V2_RESPONSE_BEATS = {
    "question", "reaction", "answer", "challenge", "counterpoint",
    "qualification", "implication", "takeaway", "comparison",
}
# Beats that can open a fresh story: they frame a subject instead of continuing
# Beats that can open a fresh story. Every one of them must name the story's
# subject in speech and start cleanly (no unanchored pronoun, conjunction, or
# definite reference), so the listener always hears what the new story is.
SCHEMA_V2_OPENING_BEATS = {
    "setup", "guest-intro", "transition", "section-transition",
    "reaction", "comparison", "implication",
}
# Beats that frame the guest as a participant rather than a citation dispenser.
SCHEMA_V2_GUEST_FRAME_BEATS = {"guest-intro", "guest-thanks"}
# A host turn that actually engages with what the guest just said.
SCHEMA_V2_GUEST_ENGAGEMENT_BEATS = {
    "question", "challenge", "counterpoint", "qualification",
}

# Documented OmniVoice non-verbal cues. Anything else reaches the TTS request
# as literal bracketed text, so the set is closed.
EXPRESSIVE_TAGS = {
    "laughter", "sigh", "question-en", "question-ah", "surprise-oh",
}
TAG_PATTERN = re.compile(r"\[([^\[\]]*)\]")
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+")

# Producer-facing dialogue and canned hooks. These are rejected outright: the
# prose prohibitions in the cron prompt were followed and the output still used
# the same moves in different words, so they are enforced here instead.
BANNED_PHRASE_PATTERNS = [
    (r"\blet'?s get into it\b", "producer-facing filler"),
    (r"\blet'?s dive (?:in|into)\b", "producer-facing filler"),
    (r"\bin today'?s (?:episode|show)\b", "episode outline in dialogue"),
    (r"\b(?:we'?ll|we are going to|we're going to|we will) (?:cover|walk through|move from|start with|treat these)\b",
     "episode outline in dialogue"),
    (r"\bhere'?s what we(?:'| a)?(?:re|ll)?\b", "episode outline in dialogue"),
    (r"\bcoming up (?:on|after)\b", "broadcast promo filler"),
    (r"\bwithout further ado\b", "broadcast promo filler"),
    (r"\bstay tuned\b", "broadcast promo filler"),
    (r"\bmost items are quick hits\b", "format explanation in dialogue"),
    (r"\bit'?s not (?:just )?[^,.;]{1,60}[,;]\s*it'?s\b", "canned contrast hook"),
    (r"\bthis is'?nt [^,.;]{1,60}[,;]\s*it'?s\b", "canned contrast hook"),
    (r"\bnot (?:just )?[^,.;]{1,60}, but (?:rather )?\b", "canned contrast hook"),
]

# Cross-story theme assertions. Ten unrelated stories do not share a thesis, and
# claiming they do is the tell that a model wrote the intro and outro.
THEME_ASSERTION_PATTERNS = [
    (r"\bdifferent [a-z]+s?, same\b", "cross-story theme assertion"),
    (r"\bsame (?:question|problem|story|idea|pattern)\b", "cross-story theme assertion"),
    (r"\bwhat (?:they|these) (?:all )?have in common\b", "cross-story theme assertion"),
    (r"\bthe (?:pattern|theme|thread|through-?line) (?:is|here|running)\b",
     "cross-story theme assertion"),
    (r"\b(?:common|connecting) thread\b", "cross-story theme assertion"),
    (r"\bif there'?s a (?:theme|pattern|lesson)\b", "cross-story theme assertion"),
    (r"\bhard to miss\b", "cross-story theme assertion"),
    (r"\bput [^.]{1,60} beside [^.]{1,60} and\b", "cross-story theme assertion"),
]

# Sentence-initial words that make a fresh story sound like a continuation.
# A story's first turn must not assume context the listener never heard, so
# these are flagged when they open a sentence in that turn. The definite
# reference list is deliberately narrow to avoid false positives.
UNANCHORED_OPENING_PATTERNS = [
    (r"^(?:it|that|this|those|these|they|he|she)\b", "an unanchored pronoun"),
    (r"^(?:but|and|so|also|meanwhile|then|because)\b", "a continuation conjunction"),
    (r"^the (?:audit|report|study|paper|article|team|researchers?|company|project|release|listing|finding|result|model|system|method|experiment|data|announcement)\b",
     "an unanchored definite reference"),
]

# Title words too generic to identify a story's subject in speech.
STORY_TITLE_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "with",
    "from", "into", "by", "as", "is", "are", "was", "were", "has", "have", "had",
    "its", "their", "his", "her", "this", "that", "these", "those", "how", "why",
    "what", "when", "where", "who", "whom", "new", "latest", "first", "last",
    "next", "over", "under", "after", "before", "between", "about", "against",
    "across", "through", "during", "via", "per", "but", "not", "no", "so", "if",
    "then", "than", "too", "very", "just", "can", "could", "will", "would",
    "should", "may", "might", "must", "do", "does", "did", "get", "gets", "got",
    "make", "makes", "made", "use", "uses", "used", "using", "say", "says",
    "said", "see", "sees", "seen", "look", "looks", "looking", "up", "down",
    "out", "off", "onto", "more", "most", "much", "many", "some", "any", "all",
    "both", "each", "few", "other", "another", "same", "such", "being", "been",
    "method", "result", "finding", "model", "system", "study", "report",
    "project", "release", "article", "paper", "research", "data", "work",
    "story", "week", "year", "time", "way", "part", "thing", "stuff", "number",
    "numbers", "news", "test", "tests", "testing", "build", "builds", "built",
    "building", "give", "gives", "given", "show", "shows", "shown", "help",
    "helps", "need", "needs", "want", "wants", "take", "takes", "taken", "full",
    "open", "real", "best", "top", "big", "small", "high", "low", "old", "free",
    "fast", "slow", "back", "still", "even", "just", "like", "well", "good",
    "great", "really", "actually", "finally", "already", "today", "now", "also",
})

# Meta-vocabulary that means the production instructions leaked into the artifact.
TITLE_META_PATTERNS = [
    r"\bhuman(?:ized|ised|-sounding)?\b",
    r"\bconversation(?:al)?\b",
    r"\bnatural\b",
    r"\bscript\b",
    r"\btwo[- ]host\b",
]

# Text that the TTS will mispronounce or read as characters.
SPOKEN_TEXT_ERROR_PATTERNS = [
    (r"https?://\S+", "a spoken URL"),
    (r"\bwww\.\S+", "a spoken URL"),
    (r"\b\d{4}\.\d{4,5}(?:v\d+)?\b", "a raw arXiv identifier"),
    (r"\b[a-z0-9][a-z0-9-]*\.(?:com|org|net|io|ai|dev|edu|gov)\b", "a spoken domain name"),
]
# Acronyms every listener already hears as words; flagging them is pure noise.
SPOKEN_ACRONYM_ALLOWLIST = {
    "NASA", "ESA", "NOAA", "MIT", "USB", "HDMI", "JSON", "HTML", "HTTP", "HTTPS",
    "JPEG", "MPEG", "LIDAR", "RADAR", "LASER", "SCUBA", "PDF", "SQL", "GPU", "CPU",
    "RAM", "ROM", "SSD", "USA", "UK", "EU", "AI", "ML", "API", "CLI", "GUI", "TIL",
}
SPOKEN_TEXT_WARN_PATTERNS = [
    (r"\b\w*\d+\.\d+\s?[A-Za-z]\b", "a model/version string that should be spelled out"),
    (r"\b[A-Za-z]+\d[\w.]*-[\w.]*\d\w*\b", "a model/version string that should be spelled out"),
    (r"\b[A-Z]{4,}\b", "an acronym that may need a gloss on first use"),
]


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def iso_now() -> str:
    return now_ist().isoformat(timespec="seconds")


def today_ist() -> str:
    return now_ist().date().isoformat()


def validate_date_string(date_str: str) -> str:
    """Accept a dated run label without allowing traversal in repository paths."""
    if type(date_str) is not str:
        raise RuntimeError("episode date must be a string")
    match = re.fullmatch(
        r"(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<label>[A-Za-z0-9][A-Za-z0-9_-]*))?",
        date_str,
    )
    if not match:
        raise RuntimeError(f"invalid episode date: {date_str}")
    try:
        parsed = dt.date.fromisoformat(match.group("date"))
    except ValueError as exc:
        raise RuntimeError(f"invalid episode date: {date_str}") from exc
    if parsed.isoformat() != match.group("date"):
        raise RuntimeError(f"invalid episode date: {date_str}")
    return date_str


def canonical_json_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_file_sha256(path: Path) -> str | None:
    try:
        return file_sha256(path)
    except OSError:
        return None


def script_schema_version(script: dict) -> int:
    raw = script.get("schema_version", 1)
    if type(raw) is not int:  # bool is intentionally rejected too.
        raise RuntimeError("schema_version must be an integer")
    if raw not in {1, 2}:
        raise RuntimeError("unsupported podcast script schema_version")
    return raw


def finite_float(raw: object, name: str) -> float:
    """Parse a finite numeric setting without accepting bool/null values."""
    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"{name} must be a finite number")
    try:
        value = float(str(raw))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def nonempty_string(raw: object, name: str) -> str:
    """Require a real, non-empty string for identity-bearing settings."""
    if type(raw) is not str or not raw.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return raw.strip()


def speaker_speed(speaker: str, cfg: dict) -> float:
    """Return the effective TTS speed for a speaker."""
    return cfg.get("speed_by_speaker", {}).get(speaker, cfg["speed"])


def strict_number_equal(raw: object, expected: float) -> bool:
    """Compare a manifest number without accepting bool/string coercion."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return False
    if isinstance(raw, float) and not math.isfinite(raw):
        return False
    try:
        return raw == expected
    except (OverflowError, TypeError, ValueError):
        return False


def strict_json_number(raw: object, name: str) -> float:
    """Parse a JSON number from a marker without accepting string coercion."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        value = float(raw)
    except (OverflowError, ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def strict_speed_map_equal(raw: object, expected: dict[str, float]) -> bool:
    """Compare per-speaker speeds with strict JSON-number semantics."""
    if not isinstance(raw, dict) or set(raw) != set(expected):
        return False
    return all(strict_number_equal(raw[speaker], speed) for speaker, speed in expected.items())


def render_config_hash(cfg: dict) -> str:
    identity = {
        "tts_provider": cfg["tts_provider"],
        "tts_base_url": cfg["tts_base_url"],
        "tts_model": cfg["tts_model"],
        "tts_cache_dir": cfg["tts_cache_dir"],
        "omnivoice_num_steps": cfg["omnivoice_num_steps"],
        "omnivoice_postprocess_output": cfg["omnivoice_postprocess_output"],
        "speed": cfg["speed"],
        "speed_by_speaker": cfg["speed_by_speaker"],
        "bitrate": cfg["bitrate"],
        "voices": cfg["voices"],
        "pause_seconds": cfg["pause_seconds"],
        "min_duration_seconds": cfg["min_duration_seconds"],
        "max_duration_seconds": cfg["max_duration_seconds"],
        "estimated_chars_per_second": cfg["estimated_chars_per_second"],
        "max_turn_characters": cfg["max_turn_characters"],
        "max_pause_seconds": cfg["max_pause_seconds"],
        "min_story_boundary_pause": cfg["min_story_boundary_pause"],
        "min_section_boundary_pause": cfg["min_section_boundary_pause"],
    }
    return canonical_json_hash(identity)


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("podcast config must be a JSON object")
    provider_raw = (
        os.environ["PODCAST_TTS_PROVIDER"]
        if "PODCAST_TTS_PROVIDER" in os.environ
        else cfg.get("tts_provider", "kokoro")
    )
    base_url_raw = (
        os.environ["PODCAST_TTS_BASE_URL"]
        if "PODCAST_TTS_BASE_URL" in os.environ
        else cfg.get("tts_base_url")
    )
    model_raw = (
        os.environ["PODCAST_TTS_MODEL"]
        if "PODCAST_TTS_MODEL" in os.environ
        else cfg.get("tts_model")
    )
    speed_raw = os.environ.get("PODCAST_TTS_SPEED") if "PODCAST_TTS_SPEED" in os.environ else cfg.get("speed", 1.0)
    pause_raw = os.environ.get("PODCAST_PAUSE_SECONDS") if "PODCAST_PAUSE_SECONDS" in os.environ else cfg.get("pause_seconds", 0.32)
    bitrate_raw = (
        os.environ["PODCAST_BITRATE"]
        if "PODCAST_BITRATE" in os.environ
        else cfg.get("bitrate")
    )
    cache_dir_raw = (
        os.environ["PODCAST_TTS_CACHE_DIR"]
        if "PODCAST_TTS_CACHE_DIR" in os.environ
        else cfg.get("tts_cache_dir")
    )
    min_duration = os.environ.get("PODCAST_MIN_DURATION_SECONDS") if "PODCAST_MIN_DURATION_SECONDS" in os.environ else cfg.get("min_duration_seconds", DEFAULT_MIN_DURATION_SECONDS)
    max_duration = os.environ.get("PODCAST_MAX_DURATION_SECONDS") if "PODCAST_MAX_DURATION_SECONDS" in os.environ else cfg.get("max_duration_seconds", DEFAULT_MAX_DURATION_SECONDS)
    chars_per_second = os.environ.get("PODCAST_ESTIMATED_CHARS_PER_SECOND") if "PODCAST_ESTIMATED_CHARS_PER_SECOND" in os.environ else cfg.get("estimated_chars_per_second", DEFAULT_ESTIMATED_CHARS_PER_SECOND)
    max_turn_chars = os.environ.get("PODCAST_MAX_TURN_CHARACTERS") if "PODCAST_MAX_TURN_CHARACTERS" in os.environ else cfg.get("max_turn_characters", DEFAULT_MAX_TURN_CHARACTERS)
    max_pause = os.environ.get("PODCAST_MAX_PAUSE_SECONDS") if "PODCAST_MAX_PAUSE_SECONDS" in os.environ else cfg.get("max_pause_seconds", DEFAULT_MAX_PAUSE_SECONDS)
    min_turn_chars = os.environ.get("PODCAST_MIN_TURN_CHARACTERS") if "PODCAST_MIN_TURN_CHARACTERS" in os.environ else cfg.get("min_turn_characters", DEFAULT_MIN_TURN_CHARACTERS)
    short_turn_chars = os.environ.get("PODCAST_SHORT_TURN_CHARACTERS") if "PODCAST_SHORT_TURN_CHARACTERS" in os.environ else cfg.get("short_turn_characters", DEFAULT_SHORT_TURN_CHARACTERS)
    min_short_turns_raw = os.environ.get("PODCAST_MIN_SHORT_TURNS") if "PODCAST_MIN_SHORT_TURNS" in os.environ else cfg.get("min_short_turns", DEFAULT_MIN_SHORT_TURNS)
    min_stdev_raw = cfg.get("min_turn_length_stdev", DEFAULT_MIN_TURN_LENGTH_STDEV)
    max_tags_raw = cfg.get("max_expressive_tags", DEFAULT_MAX_EXPRESSIVE_TAGS)
    omnivoice_num_steps_raw = cfg.get("omnivoice_num_steps", 32)
    omnivoice_postprocess_raw = cfg.get("omnivoice_postprocess_output", True)
    cfg["tts_provider"] = nonempty_string(provider_raw, "tts_provider")
    if cfg["tts_provider"] not in {"kokoro", "omnivoice"}:
        raise ValueError("tts_provider must be kokoro or omnivoice")
    cfg["tts_base_url"] = nonempty_string(base_url_raw, "tts_base_url").rstrip("/")
    if not cfg["tts_base_url"]:
        raise ValueError("tts_base_url must be a non-empty string")
    cfg["tts_model"] = nonempty_string(model_raw, "tts_model")
    cfg["speed"] = finite_float(speed_raw, "speed")
    raw_speed_by_speaker = cfg.get("speed_by_speaker", {})
    if not isinstance(raw_speed_by_speaker, dict):
        raise ValueError("speed_by_speaker must be a JSON object")
    speed_by_speaker: dict[str, float] = {}
    allowed_speakers = ("host_female", "host_male", "guest")
    for speaker, raw_speaker_speed in raw_speed_by_speaker.items():
        if (
            type(speaker) is not str
            or not speaker
            or speaker != speaker.strip()
            or speaker not in allowed_speakers
        ):
            raise ValueError(
                "speed_by_speaker keys must be exact speaker names from "
                f"{', '.join(allowed_speakers)}"
            )
        value = finite_float(raw_speaker_speed, f"speed_by_speaker[{speaker}]")
        if value <= 0:
            raise ValueError(f"speed_by_speaker[{speaker}] must be positive")
        speed_by_speaker[speaker] = value
    if "PODCAST_TTS_SPEED" in os.environ:
        # Preserve the legacy global override for every configured speaker;
        # per-speaker environment overrides below remain more specific.
        speed_by_speaker = {
            speaker: cfg["speed"] for speaker in speed_by_speaker
        }
    for speaker in ("host_female", "host_male", "guest"):
        env_name = "PODCAST_TTS_SPEED_" + speaker.upper()
        if env_name in os.environ:
            value = finite_float(os.environ[env_name], env_name)
            if value <= 0:
                raise ValueError(f"{env_name} must be positive")
            speed_by_speaker[speaker] = value
    cfg["speed_by_speaker"] = speed_by_speaker
    cfg["pause_seconds"] = finite_float(pause_raw, "pause_seconds")
    cfg["bitrate"] = nonempty_string(bitrate_raw, "bitrate")
    cfg["tts_cache_dir"] = nonempty_string(cache_dir_raw, "tts_cache_dir")
    cfg["min_duration_seconds"] = finite_float(min_duration, "min_duration_seconds")
    cfg["max_duration_seconds"] = finite_float(max_duration, "max_duration_seconds")
    cfg["estimated_chars_per_second"] = finite_float(
        chars_per_second, "estimated_chars_per_second"
    )
    max_turn_characters = finite_float(max_turn_chars, "max_turn_characters")
    if not max_turn_characters.is_integer():
        raise ValueError("max_turn_characters must be a finite integer")
    cfg["max_turn_characters"] = int(max_turn_characters)
    cfg["max_pause_seconds"] = finite_float(max_pause, "max_pause_seconds")
    min_story_pause_raw = cfg.get(
        "min_story_boundary_pause", DEFAULT_MIN_STORY_BOUNDARY_PAUSE_SECONDS
    )
    min_section_pause_raw = cfg.get(
        "min_section_boundary_pause", DEFAULT_MIN_SECTION_BOUNDARY_PAUSE_SECONDS
    )
    cfg["min_story_boundary_pause"] = finite_float(
        min_story_pause_raw, "min_story_boundary_pause"
    )
    cfg["min_section_boundary_pause"] = finite_float(
        min_section_pause_raw, "min_section_boundary_pause"
    )
    for name, raw in (
        ("min_turn_characters", min_turn_chars),
        ("short_turn_characters", short_turn_chars),
        ("min_short_turns", min_short_turns_raw),
        ("max_expressive_tags", max_tags_raw),
    ):
        value = finite_float(raw, name)
        if not value.is_integer() or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        cfg[name] = int(value)
    cfg["min_turn_length_stdev"] = finite_float(min_stdev_raw, "min_turn_length_stdev")
    if cfg["min_turn_length_stdev"] < 0:
        raise ValueError("min_turn_length_stdev must not be negative")
    if type(omnivoice_num_steps_raw) is not int or not 1 <= omnivoice_num_steps_raw <= 128:
        raise ValueError("omnivoice_num_steps must be an integer between 1 and 128")
    if type(omnivoice_postprocess_raw) is not bool:
        raise ValueError("omnivoice_postprocess_output must be boolean")
    cfg["omnivoice_num_steps"] = omnivoice_num_steps_raw
    cfg["omnivoice_postprocess_output"] = omnivoice_postprocess_raw
    if cfg["speed"] <= 0:
        raise ValueError("TTS speed must be positive")
    if cfg["pause_seconds"] < 0:
        raise ValueError("pause_seconds must not be negative")
    if cfg["min_duration_seconds"] <= 0 or cfg["max_duration_seconds"] < cfg["min_duration_seconds"]:
        raise ValueError("podcast duration bounds are invalid")
    if cfg["estimated_chars_per_second"] <= 0:
        raise ValueError("estimated_chars_per_second must be positive")
    if cfg["max_turn_characters"] < 2 or cfg["max_pause_seconds"] < 0:
        raise ValueError("podcast turn/pause limits are invalid")
    if not 0 <= cfg["min_story_boundary_pause"] <= cfg["min_section_boundary_pause"] <= cfg["max_pause_seconds"]:
        raise ValueError(
            "boundary pauses must satisfy "
            "0 <= min_story_boundary_pause <= min_section_boundary_pause <= max_pause_seconds"
        )
    if not 2 <= cfg["min_turn_characters"] <= cfg["short_turn_characters"] <= cfg["max_turn_characters"]:
        raise ValueError(
            "turn length bands must satisfy "
            "2 <= min_turn_characters <= short_turn_characters <= max_turn_characters"
        )
    raw_voices = cfg.get("voices", {})
    if not isinstance(raw_voices, dict):
        raise ValueError("voices must be a JSON object")
    for speaker, voice in raw_voices.items():
        if type(speaker) is not str or type(voice) is not str or not voice.strip():
            raise ValueError("every configured voice must be a non-empty string")
    voices = dict(raw_voices)
    for speaker in ("host_female", "host_male", "guest"):
        if speaker not in voices:
            raise ValueError(f"missing configured voice for {speaker}")
        env_name = "PODCAST_VOICE_" + speaker.upper()
        if env_name in os.environ:
            voices[speaker] = nonempty_string(os.environ[env_name], env_name)
    cfg["voices"] = voices
    return cfg


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def run(cmd: list[str], *, cwd: Path | None = None, capture: bool = False) -> subprocess.CompletedProcess:
    print("[$] " + " ".join(str(x) for x in cmd), flush=True)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=True,
        text=True,
        capture_output=capture,
    )


def content_path(date_str: str) -> Path:
    validate_date_string(date_str)
    return CONTENT_DIR / f"{date_str}.json"


def success_marker(date_str: str) -> Path:
    validate_date_string(date_str)
    return RUNS_DIR / date_str / "grep-success.json"


def mark_ready(date_str: str) -> Path:
    """Record a post-deploy handoff only when the gh-pages commit is present."""
    content = content_path(date_str)
    page = PAGES_DIR / f"{date_str}.html"
    if not content.is_file():
        raise RuntimeError(f"missing curated edition: {content}")
    if not page.is_file():
        raise RuntimeError(f"missing deployed worktree page: {page}")
    try:
        commit = run(["git", "-C", str(PAGES_DIR), "rev-parse", "HEAD"], capture=True).stdout.strip()
        subject = run(["git", "-C", str(PAGES_DIR), "log", "-1", "--pretty=%s"], capture=True).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("could not inspect the gh-pages worktree") from exc
    if subject != f"edition {date_str}":
        raise RuntimeError(
            f"latest gh-pages commit is not this edition: expected 'edition {date_str}', got {subject!r}"
        )
    edition_sha = hashlib.sha256(content.read_bytes()).hexdigest()
    marker = success_marker(date_str)
    payload = {
        "date": date_str,
        "content": str(content),
        "content_sha256": edition_sha,
        "deployed_page": str(page),
        "gh_pages_commit": commit,
        "gh_pages_subject": subject,
        "marked_at": iso_now(),
    }
    atomic_write(marker, json.dumps(payload, indent=2) + "\n")
    print(f"[podcast] grep success marker written: {marker}")
    return marker


def load_edition(date_str: str) -> dict:
    path = content_path(date_str)
    if not path.is_file():
        raise RuntimeError(f"edition does not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise RuntimeError(f"unexpected edition shape: {path}")
    return data


def normalize_text(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def pause_after_seconds(segment: dict, cfg: dict) -> float:
    """Return a bounded pause for a turn handoff."""
    raw = segment.get("pause_after_seconds", cfg["pause_seconds"])
    try:
        value = finite_float(raw, "pause_after_seconds")
    except ValueError as exc:
        raise RuntimeError("pause_after_seconds must be numeric") from exc
    if not math.isfinite(value) or value < 0 or value > cfg["max_pause_seconds"]:
        raise RuntimeError(
            f"pause_after_seconds must be between 0 and {cfg['max_pause_seconds']:.2f} seconds"
        )
    return round(value, 3)


def script_metrics(script: dict, cfg: dict) -> dict:
    """Calculate the budget used by planning and final duration validation."""
    segments = script["segments"]
    characters = sum(len(normalize_text(segment["text"])) for segment in segments)
    pause_seconds = sum(
        pause_after_seconds(segment, cfg)
        for index, segment in enumerate(segments)
        if index < len(segments) - 1
    )
    speech_seconds = sum(
        len(normalize_text(segment["text"]))
        / cfg["estimated_chars_per_second"]
        / speaker_speed(normalize_text(segment["speaker"]), cfg)
        for segment in segments
    )
    stories = {
        normalize_text(segment.get("story_title"))
        for segment in segments
        if normalize_text(segment.get("story_title"))
    }
    return {
        "schema_version": script_schema_version(script),
        "segments": len(segments),
        "stories": len(stories),
        "characters": characters,
        "pause_seconds": pause_seconds,
        "speech_seconds_estimate": speech_seconds,
        "estimated_duration_seconds": speech_seconds + pause_seconds,
    }


def strip_expressive_tags(text: str) -> str:
    """Return the words a listener actually hears, without the cue markers."""
    return normalize_text(TAG_PATTERN.sub(" ", text))


def check_expressive_tags(segments: list[dict], cfg: dict, errors: list[str]) -> int:
    """Validate OmniVoice cue markers.

    A cue is one non-verbal sound embedded in a sentence. It must never be a
    turn on its own and must never trail a turn: OmniVoice renders very short
    clips poorly, and a cue at the very end of a turn lands after the line it is
    supposed to react to.
    """
    total = 0
    for i, seg in enumerate(segments, start=1):
        text = normalize_text(seg.get("text"))
        matches = list(TAG_PATTERN.finditer(text))
        total += len(matches)
        for match in matches:
            name = match.group(1).strip()
            if name not in EXPRESSIVE_TAGS:
                errors.append(
                    f"segment {i} uses undocumented cue [{name}]; allowed cues are "
                    + ", ".join(f"[{t}]" for t in sorted(EXPRESSIVE_TAGS))
                )
        if matches and not strip_expressive_tags(text):
            errors.append(
                f"segment {i} is only a cue marker; a cue must sit inside a spoken sentence"
            )
        for match in matches:
            if not text[match.end():].strip(" .!?,;:—-"):
                errors.append(
                    f"segment {i} ends on the cue [{match.group(1).strip()}]; move it beside "
                    "the words that trigger it so the sound does not trail the turn"
                )
        for sentence in SENTENCE_SPLIT_PATTERN.split(text):
            if len(TAG_PATTERN.findall(sentence)) > 1:
                errors.append(f"segment {i} stacks more than one cue in a single sentence")
                break
    if total > cfg["max_expressive_tags"]:
        errors.append(
            f"the script uses {total} expressive cues; keep it to at most "
            f"{cfg['max_expressive_tags']} so they stay meaningful"
        )
    return total


def check_turn_lengths(segments: list[dict], cfg: dict, errors: list[str]) -> dict:
    """Enforce a spoken-length floor and require real variation in turn length.

    Uniform paragraph-length turns are the strongest signal that an episode is
    two readers rather than two people, so an episode must contain genuine short
    reactions - but never shorter than OmniVoice renders cleanly.
    """
    lengths: list[int] = []
    for i, seg in enumerate(segments, start=1):
        spoken = strip_expressive_tags(seg.get("text"))
        lengths.append(len(spoken))
        if len(spoken) < cfg["min_turn_characters"]:
            errors.append(
                f"segment {i} has {len(spoken)} spoken characters; the floor is "
                f"{cfg['min_turn_characters']} because OmniVoice renders very short clips poorly"
            )
    short_turns = [n for n in lengths if n <= cfg["short_turn_characters"]]
    if len(short_turns) < cfg["min_short_turns"]:
        errors.append(
            f"only {len(short_turns)} turns are at or below {cfg['short_turn_characters']} "
            f"characters; write at least {cfg['min_short_turns']} genuine short reactions "
            "so the episode does not sound like alternating paragraphs"
        )
    stdev = statistics.pstdev(lengths) if len(lengths) > 1 else 0.0
    if stdev < cfg["min_turn_length_stdev"]:
        errors.append(
            f"turn lengths vary too little (stdev {stdev:.1f} < {cfg['min_turn_length_stdev']:.0f}); "
            "mix short reactions with longer explanations"
        )
    return {
        "min": min(lengths) if lengths else 0,
        "median": int(statistics.median(lengths)) if lengths else 0,
        "max": max(lengths) if lengths else 0,
        "stdev": round(stdev, 1),
        "short_turns": len(short_turns),
    }


def story_signature(story_segments: list[dict]) -> tuple:
    return tuple(
        (normalize_text(seg.get("speaker")), normalize_text(seg.get("beat")))
        for seg in story_segments
    )


def check_story_variety(
    ordered_stories: list[tuple[str, list[dict]]], errors: list[str]
) -> None:
    """Reject scripts that walk the same skeleton through every story.

    The beat vocabulary is a validation label, not a template to recite. Without
    this check a script can satisfy every structural rule and still repeat one
    identical exchange shape ten times.
    """
    deep_prefixes: dict[tuple, str] = {}
    deep_multisets: dict[tuple, str] = {}
    quick_beat_shapes: dict[tuple, int] = {}
    quick_turn_counts: list[int] = []
    openers: dict[str, int] = {}
    for title, segs in ordered_stories:
        kind = normalize_text(segs[0].get("kind"))
        openers[normalize_text(segs[0].get("speaker"))] = (
            openers.get(normalize_text(segs[0].get("speaker")), 0) + 1
        )
        beats = tuple(beat for _, beat in story_signature(segs))
        if kind == "deep-dive":
            # Two dives that open the same way and use the same beats are the
            # same skeleton, even when a couple of middle beats are swapped.
            prefix = beats[:3]
            if prefix in deep_prefixes:
                errors.append(
                    f"deep dives '{deep_prefixes[prefix]}' and '{title}' open on the identical "
                    f"beat sequence {' -> '.join(prefix)}; vary who opens and where the guest, "
                    "question, and pushback land"
                )
            else:
                deep_prefixes[prefix] = title
            multiset = tuple(sorted(beats))
            if multiset in deep_multisets:
                errors.append(
                    f"deep dives '{deep_multisets[multiset]}' and '{title}' use the identical "
                    "set of beats; give one of them a different shape, not a reordering"
                )
            else:
                deep_multisets[multiset] = title
        elif kind == "quick":
            quick_beat_shapes[beats] = quick_beat_shapes.get(beats, 0) + 1
            quick_turn_counts.append(len(segs))
    quick_total = sum(quick_beat_shapes.values())
    if quick_total >= 3 and quick_beat_shapes:
        most_common = max(quick_beat_shapes.values())
        if most_common > max(2, round(quick_total * 0.6)):
            errors.append(
                f"{most_common} of {quick_total} quick stories use the same beat shape; "
                "let some open with a reaction or a question, or run to a third turn"
            )
    if quick_total >= 5 and len(set(quick_turn_counts)) == 1:
        errors.append(
            f"all {quick_total} quick stories are exactly {quick_turn_counts[0]} turns; "
            "give at least one of them a different length"
        )
    story_total = len(ordered_stories)
    if story_total >= 4:
        for host in ("host_female", "host_male"):
            if openers.get(host, 0) < max(1, round(story_total * 0.25)):
                errors.append(
                    f"{host} opens only {openers.get(host, 0)} of {story_total} stories; "
                    "both hosts should start a fair share"
                )


def _story_sections(script: dict) -> dict[str, str]:
    """Map each normalized story title to its edition section from show notes."""
    sections: dict[str, str] = {}
    notes = script.get("show_notes")
    if not isinstance(notes, list):
        return sections
    for note in notes:
        if not isinstance(note, dict):
            continue
        title = normalize_text(note.get("title"))
        section = normalize_text(note.get("section"))
        if title and section:
            sections[title] = section
    return sections


def _check_boundary_pauses(
    segments: list[dict], sections: dict[str, str], cfg: dict, errors: list[str]
) -> None:
    """A new story must be heard arriving: enough silence before its first turn.

    The pause that counts is the one on whichever segment immediately precedes
    a new story's first turn, so an untagged interstitial between two stories
    cannot smuggle the boundary past the check. When a section is unknown the
    check fails closed toward the longer section pause.
    """
    last_story_title: str | None = None
    for i, seg in enumerate(segments):
        title = normalize_text(seg.get("story_title"))
        if not title:
            continue
        if last_story_title is None:
            if i > 0:
                pause = pause_after_seconds(segments[i - 1], cfg)
                required = cfg["min_story_boundary_pause"]
                if pause < required:
                    errors.append(
                        f"segment {i} precedes the first story '{title}' with only a "
                        f"{pause:.2f}s pause; leave at least {required:.2f}s before "
                        "the first story so its arrival is audible"
                    )
        elif title != last_story_title:
            prev = segments[i - 1]
            pause = pause_after_seconds(prev, cfg)
            prev_section = sections.get(last_story_title)
            next_section = sections.get(title)
            if prev_section and next_section and prev_section != next_section:
                required = cfg["min_section_boundary_pause"]
                boundary = "a section boundary"
            elif not prev_section or not next_section:
                # Fail closed: an unknown section must not silently weaken the
                # boundary into the shorter story pause.
                required = cfg["min_section_boundary_pause"]
                boundary = "a section boundary"
            else:
                required = cfg["min_story_boundary_pause"]
                boundary = "a story boundary"
            if pause < required:
                errors.append(
                    f"segment {i} closes story '{last_story_title}' with only a "
                    f"{pause:.2f}s pause before '{title}'; leave at least "
                    f"{required:.2f}s at {boundary} so the move is audible"
                )
        last_story_title = title


def _stem(token: str) -> str:
    """A few surface forms of a word, so 'printing' and 'printed' both stem."""
    for suffix in ("ing", "ed", "es", "s", "ly", "er", "est"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def _subject_tokens(title: str) -> set[str]:
    """Stems of the distinctive words in a story title.

    Generic words are filtered on their stem, so plural forms cannot smuggle a
    generic word past the stopword list ('Methods' stems to 'method' and is
    dropped like the singular).
    """
    tokens: set[str] = set()
    for token in re.findall(r"\w+", normalize_text(title).lower()):
        if len(token) < 4:
            continue
        stemmed = _stem(token)
        if stemmed in STORY_TITLE_STOPWORDS:
            continue
        tokens.add(stemmed)
    return tokens


def _story_subject_mentioned(title: str, opener_text: str) -> bool:
    """True when the opener names the story's subject in speech.

    Any distinct title word that also appears in the opener counts, stemmed on
    both sides ('printed' matches a title containing 'Prints'). Generic words
    never count because STORY_TITLE_STOPWORDS removes them from the title side,
    so 'one method is worth watching' cannot stand in for a story whose title
    happens to contain the word Method.
    """
    title_stems = _subject_tokens(title)
    if not title_stems:
        return True
    opener_stems = {_stem(t) for t in re.findall(r"\w+", opener_text.lower())}
    return bool(title_stems & opener_stems)


def _opener_problems(text: str) -> list[str]:
    """Return unanchored-opener labels, or [] when the opener stands alone.

    Only the first sentence of a story's first turn is examined. A pronoun or
    conjunction that opens a later sentence is ordinary dialogue, and the turn
    can establish its own antecedent; but the very first thing a listener hears
    from a new story must not assume context they never got.
    """
    problems: list[str] = []
    sentences = SENTENCE_SPLIT_PATTERN.split(strip_expressive_tags(text))
    if not sentences:
        return problems
    first_sentence = sentences[0]
    # Strip speech punctuation/parenthetical staging before testing the first
    # lexical token. This catches ``—it's...``, ``(It)...`` and ``…the...``
    # in addition to ordinary quoted openers.
    stripped = first_sentence.strip().lstrip('"\'“”‘’«»—–…([{,;:-.!')
    lowered = stripped.lower()
    # Discourse markers and vocatives hide the real first token: "Well, it
    # changes." and "Arjun, it changes." must both flag "it". Strip at most a
    # few leading discourse fillers or capitalized vocative names; stop at the
    # first word that is neither, so a phrase like "Speaking of machines doing
    # more than they should" keeps its anchored pronoun out of the scan.
    discourse_markers = {
        "well", "hmm", "hm", "oh", "uh", "um", "yeah", "yep", "yup",
        "no", "wait", "okay", "ok", "right", "look", "listen", "hey", "honestly",
        "actually", "basically", "anyway", "first", "next", "up", "last",
    }
    persona_names = {name.lower() for name in load_personas().values()}
    # "Maya! It changes..." splits at the exclamation, isolating the vocative
    # as its own sentence and hiding the pronoun from the scan. Rejoin a lone
    # leading vocative with the next sentence so the same defect is caught
    # whether the writer uses "Maya, " or the "!" address style.
    if (
        len(sentences) > 1
        and re.fullmatch(r"\s*[a-zA-Z]+[!…]?\s*", sentences[0])
        and sentences[0].strip().rstrip("!…").strip().lower() in persona_names
    ):
        sentences[0] = f"{sentences[0].strip()} {sentences[1].strip()}"
        first_sentence = sentences[0]
        lowered = first_sentence.strip().lower()
    for _ in range(3):
        m = re.match(r"^([a-zA-Z]+)[,\s\-—–…!]+", lowered)
        if not m:
            break
        word = m.group(1).lower()
        if word in discourse_markers or word in persona_names:
            lowered = lowered[m.end():].lstrip('"\'“”‘’«»—–…([{,;:-.!')
        else:
            break
    for pattern, label in UNANCHORED_OPENING_PATTERNS:
        if re.match(pattern, lowered):
            problems.append(label)
            break
    return problems


def check_story_openers(
    segments: list[dict], script: dict, cfg: dict, errors: list[str]
) -> None:
    """Every story must announce itself in speech, not only in metadata.

    The listener cannot see story_title, so the first turn of a story has to
    sound like a beginning, and the pause before it has to be long enough to
    register as a boundary. Three rules:

    A. The turn that closes a story leaves a boundary pause before the next
       story's first turn; longer when the section changes, and unknown
       sections fail closed toward the longer pause.
    B. A story may not open on a beat that implies a prior conversation
       (question, answer, challenge, counterpoint, qualification, takeaway).
       Every allowed opener beat -- setup, guest-intro, transition,
       section-transition, reaction, comparison, implication -- must name the
       story's subject in speech.
    C. No opener may start with an unanchored pronoun, a continuation
       conjunction, or an unanchored definite reference.
    """
    sections = _story_sections(script)
    _check_boundary_pauses(segments, sections, cfg, errors)
    first_of_story: dict[str, int] = {}
    for i, seg in enumerate(segments):
        title = normalize_text(seg.get("story_title"))
        if title and title not in first_of_story:
            first_of_story[title] = i
    for title, i in first_of_story.items():
        first = segments[i]
        beat = normalize_text(first.get("beat"))
        text = strip_expressive_tags(first.get("text"))
        problems = _opener_problems(text)
        if beat in SCHEMA_V2_OPENING_BEATS:
            if problems:
                errors.append(
                    f"story '{title}' opens on beat '{beat}' but starts with "
                    f"{problems[0]}; make the opener self-contained and name "
                    f"the subject, e.g. 'Next up, we have <story>'"
                )
            elif not _story_subject_mentioned(title, text):
                errors.append(
                    f"story '{title}' opens on beat '{beat}' without naming the "
                    f"story's subject; open with a line that names it, e.g. "
                    f"'Next up, we have <story>'"
                )
        else:
            errors.append(
                f"story '{title}' opens on beat '{beat}'; a fresh story cannot "
                f"start on '{beat}' because it implies a conversation the "
                f"listener never heard - open with a 'setup' that names the "
                f"subject and move the '{beat}' to a later turn"
            )


def check_intro_introduces(
    segments: list[dict], story_titles: list[str], errors: list[str]
) -> None:
    """The episode must not start inside the first story.

    The intro turns have to identify the speakers (both hosts, plus the guest
    when one appears) and line up a few of the stories coming up, so the
    listener knows who is talking and what to expect.
    """
    intro = [seg for seg in segments if normalize_text(seg.get("kind")) == "intro"]
    if not intro:
        errors.append("the script needs an intro turn before the first story")
        return
    first_story_index = next(
        (i for i, seg in enumerate(segments) if normalize_text(seg.get("story_title"))),
        None,
    )
    misplaced = [
        i + 1 for i, seg in enumerate(segments)
        if normalize_text(seg.get("kind")) == "intro"
        and first_story_index is not None
        and i >= first_story_index
    ]
    if misplaced:
        errors.append(
            "intro turns must come before the first story; intro segments at "
            f"positions {', '.join(map(str, misplaced))} appear inside or after the stories"
        )
    spoken = " ".join(
        strip_expressive_tags(seg.get("text") or "") for seg in intro
    )
    names = load_personas()
    if names:
        guest_present = any(
            normalize_text(seg.get("speaker")) == "guest" for seg in segments
        )
        required = {
            speaker: name
            for speaker, name in names.items()
            if speaker != "guest" or guest_present
        }
        missing = [
            name for name in required.values()
            if re.search(rf"\b{re.escape(name)}\b", spoken) is None
        ]
        if missing:
            errors.append(
                "the intro does not introduce every speaker; name "
                + ", ".join(missing)
                + " in the opening turns so the listener knows who is talking"
            )
    lined_up = {
        title for title in story_titles
        if _story_subject_mentioned(title, spoken)
    }
    if len(lined_up) < 2:
        errors.append(
            "the intro does not line up the topics; name at least two of the "
            "episode's stories before the first one starts"
        )


def check_guest_arc(segments: list[dict], errors: list[str]) -> None:
    """Require the guest to be a participant with an entrance and an exit.

    Without this the guest appears mid-episode with no framing, delivers one
    sourced paragraph per deep dive, and vanishes - which reads as a third
    narrator rather than a person in the conversation.
    """
    guest_indices = [
        i for i, seg in enumerate(segments)
        if normalize_text(seg.get("speaker")) == "guest"
    ]
    beats = [normalize_text(seg.get("beat")) for seg in segments]
    intro_indices = [i for i, beat in enumerate(beats) if beat == "guest-intro"]
    thanks_indices = [i for i, beat in enumerate(beats) if beat == "guest-thanks"]
    if not guest_indices:
        if intro_indices or thanks_indices:
            errors.append("guest-intro/guest-thanks beats used without any guest turn")
        return
    first_guest, last_guest = guest_indices[0], guest_indices[-1]
    if len(intro_indices) != 1:
        errors.append(
            "the script needs exactly one 'guest-intro' turn where a host brings the "
            "guest in and says why they are here"
        )
    elif intro_indices[0] > first_guest:
        errors.append("the 'guest-intro' turn must come before the guest first speaks")
    if not thanks_indices:
        errors.append("the script needs a 'guest-thanks' turn after the guest's last contribution")
    elif thanks_indices[-1] < last_guest:
        errors.append("the 'guest-thanks' turn must come after the guest's last contribution")
    engaged = any(
        normalize_text(segments[i + 1].get("beat")) in SCHEMA_V2_GUEST_ENGAGEMENT_BEATS
        for i in guest_indices
        if i + 1 < len(segments)
        and normalize_text(segments[i + 1].get("speaker")) != "guest"
    )
    if not engaged:
        errors.append(
            "no host ever questions, qualifies, or pushes back on the guest; the guest "
            "should be part of the exchange, not a source read aloud"
        )
    positions: set[int] = set()
    for i in guest_indices:
        title = normalize_text(segments[i].get("story_title"))
        within = [
            j for j, seg in enumerate(segments)
            if normalize_text(seg.get("story_title")) == title
        ]
        positions.add(within.index(i))
    if len(guest_indices) >= 2 and len(positions) == 1:
        errors.append(
            "every guest turn sits at the same position inside its story; let the guest "
            "enter earlier or later in at least one deep dive"
        )


def check_beat_coherence(segments: list[dict], errors: list[str]) -> None:
    """Keep beat labels honest so they stay useful as validation signal."""
    for i, seg in enumerate(segments, start=1):
        beat = normalize_text(seg.get("beat"))
        kind = normalize_text(seg.get("kind"))
        text = strip_expressive_tags(seg.get("text"))
        if beat == "question" and "?" not in text:
            errors.append(f"segment {i} is labelled beat 'question' but asks nothing")
        if beat == "hook" and kind != "intro":
            errors.append(f"segment {i} uses beat 'hook' outside the intro")
        if beat == "outro" and kind != "outro":
            errors.append(f"segment {i} uses beat 'outro' outside the outro")


def check_prose(script: dict, segments: list[dict], errors: list[str], warnings: list[str]) -> None:
    """Reject production language, cross-story theme claims, and unspeakable text."""
    title = normalize_text(script.get("title"))
    for pattern in TITLE_META_PATTERNS:
        if re.search(pattern, title, re.IGNORECASE):
            errors.append(
                f"the title {title!r} describes how the episode was made; name the stories instead"
            )
            break
    for i, seg in enumerate(segments, start=1):
        text = strip_expressive_tags(seg.get("text"))
        for pattern, label in BANNED_PHRASE_PATTERNS + THEME_ASSERTION_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                errors.append(f"segment {i} contains {label}: {match.group(0)!r}")
        for pattern, label in SPOKEN_TEXT_ERROR_PATTERNS:
            match = re.search(pattern, text)
            if match:
                errors.append(
                    f"segment {i} would have the host speak {label}: {match.group(0)!r}; "
                    "say it in words and put the link in show notes"
                )
        for pattern, label in SPOKEN_TEXT_WARN_PATTERNS:
            for match in re.finditer(pattern, text):
                if match.group(0) in SPOKEN_ACRONYM_ALLOWLIST:
                    continue
                warnings.append(f"segment {i} contains {label}: {match.group(0)!r}")
                break


def load_personas() -> dict:
    """Return the stable show personas, or an empty mapping when unconfigured."""
    try:
        data = json.loads(PERSONAS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    speakers = data.get("speakers")
    if not isinstance(speakers, dict):
        return {}
    names = {}
    for speaker, entry in speakers.items():
        if isinstance(entry, dict) and type(entry.get("name")) is str and entry["name"].strip():
            names[speaker] = entry["name"].strip()
    return names


def check_direct_address(segments: list[dict], errors: list[str]) -> int:
    """Require the hosts to talk to each other, not merely take turns.

    Two unnamed voices that never address each other is the strongest remaining
    signal that an episode is narration rather than conversation.
    """
    names = load_personas()
    if not names:
        return 0
    uses = 0
    for seg in segments:
        speaker = normalize_text(seg.get("speaker"))
        text = strip_expressive_tags(seg.get("text"))
        for other, name in names.items():
            if other == speaker:
                continue
            uses += len(re.findall(rf"\b{re.escape(name)}\b", text))
    if uses < 2:
        listed = ", ".join(sorted(names.values()))
        errors.append(
            f"the speakers address each other by name {uses} time(s); use the show's names "
            f"({listed}) two to four times so the listener learns who is who"
        )
    return uses


def check_vocative_address(segments: list[dict], errors: list[str]) -> None:
    """Addresses must use '!' rather than ',' so TTS hears a name as an address.

    ``Maya, Shweta read the release`` is synthesized as though the speaker were
    reading a list of names; ``Maya! Shweta read the release`` is heard as an
    exclamation of address. Only clause-initial vocatives are checked: a comma
    before a name ("first, Maya") and genuine enumerations ("Maya, Arjun, and
    Shweta") do not have the list-confusion problem.
    """
    names = load_personas()
    if not names:
        return
    name_pattern = "|".join(re.escape(name) for name in names.values())
    name_atom = rf"(?:{name_pattern})"
    address_fillers = (
        "well|hmm|hm|oh|uh|um|yeah|yep|yup|no|wait|okay|ok|right|look|"
        "listen|hey|honestly|actually|basically|anyway|first|next|up|last"
    )
    opener = r"[\"'“”‘’«»(\[]*"
    pattern = re.compile(
        rf"(?:^|(?:[.!?…;:]\s*|(?:{address_fillers})[,;:]?\s+)){opener}\s*"
        rf"({name_atom}),\s",
        re.IGNORECASE,
    )
    enumeration = re.compile(
        rf"^{name_atom}(?:(?:,\s+{name_atom})+|(?:,?\s+and\s+{name_atom}))\b",
        re.IGNORECASE,
    )
    for index, segment in enumerate(segments, start=1):
        text = strip_expressive_tags(segment.get("text") or "")
        for match in pattern.finditer(text):
            if enumeration.match(text[match.end():]):
                continue
            errors.append(
                f"segment {index} addresses {match.group(1)} with a comma; "
                f"use '!' (e.g. '{match.group(1)}!') so the name is heard as "
                "an address rather than a list item"
            )


def check_show_notes(script: dict, errors: list[str]) -> None:
    """Show notes must be unambiguous enough for boundary decisions.

    A story's section decides whether a boundary needs the longer pause, so a
    section that is not a plain non-empty string, or a duplicate note that can
    silently relabel a story, must be rejected rather than silently used.
    """
    notes = script.get("show_notes", [])
    if not isinstance(notes, list):
        return
    seen: set[str] = set()
    for note in notes:
        if not isinstance(note, dict):
            continue
        title = normalize_text(note.get("title"))
        if not title:
            continue
        if title in seen:
            errors.append(f"show_notes has more than one entry for '{title}'")
        seen.add(title)
        section = note.get("section")
        if not isinstance(section, str) or not normalize_text(section):
            errors.append(
                f"show_notes entry for '{title}' needs a plain string section"
            )


def check_editorial(script: dict, cfg: dict) -> dict:
    """Run every schema-v2 editorial acceptance check and report all failures.

    Failures are collected rather than raised one at a time so a single --plan
    run tells the editor everything that needs rewriting.
    """
    segments = script["segments"]
    errors: list[str] = []
    warnings: list[str] = []
    ordered_stories: list[tuple[str, list[dict]]] = []
    seen: dict[str, list[dict]] = {}
    for seg in segments:
        title = normalize_text(seg.get("story_title"))
        if not title:
            continue
        if title not in seen:
            seen[title] = []
            ordered_stories.append((title, seen[title]))
        seen[title].append(seg)

    tag_total = check_expressive_tags(segments, cfg, errors)
    lengths = check_turn_lengths(segments, cfg, errors)
    check_story_variety(ordered_stories, errors)
    check_story_openers(segments, script, cfg, errors)
    check_intro_introduces(segments, [t for t, _ in ordered_stories], errors)
    check_show_notes(script, errors)
    check_guest_arc(segments, errors)
    check_beat_coherence(segments, errors)
    check_prose(script, segments, errors, warnings)
    direct_address = check_direct_address(segments, errors)
    check_vocative_address(segments, errors)

    if errors:
        raise RuntimeError(
            "editorial checks failed:\n  - " + "\n  - ".join(errors)
        )
    return {
        "turn_lengths": lengths,
        "expressive_tags": tag_total,
        "direct_address": direct_address,
        "warnings": warnings,
    }


def validate_script(script: dict, date_str: str, cfg: dict) -> dict:
    if not isinstance(script, dict):
        raise RuntimeError("script must be a JSON object")
    schema_version = script_schema_version(script)
    if schema_version >= 2:
        if type(script.get("date")) is not str:
            raise RuntimeError("schema v2 script needs a string date")
        if type(script.get("title")) is not str:
            raise RuntimeError("schema v2 script needs a string title")
    if normalize_text(script.get("date")) != date_str:
        raise RuntimeError(f"script date must be {date_str}")
    if not normalize_text(script.get("title")):
        raise RuntimeError("script needs a title")
    segments = script.get("segments")
    if not isinstance(segments, list) or not segments:
        raise RuntimeError("script needs a non-empty segments array")
    voices = {normalize_text(s.get("speaker")) for s in segments if isinstance(s, dict)}
    if "host_female" not in voices or "host_male" not in voices:
        raise RuntimeError("script must contain both host_female and host_male turns")
    allowed_speakers = {"host_female", "host_male", "guest"}
    story_groups: dict[str, list[dict]] = {}
    previous_speaker = ""
    consecutive_speaker_turns = 0
    for i, seg in enumerate(segments, start=1):
        if not isinstance(seg, dict):
            raise RuntimeError(f"segment {i} is not an object")
        if schema_version >= 2:
            if type(seg.get("speaker")) is not str:
                raise RuntimeError(f"schema v2 segment {i} needs a string speaker")
            if type(seg.get("text")) is not str:
                raise RuntimeError(f"schema v2 segment {i} needs string text")
            if "pause_after_seconds" in seg and type(seg.get("pause_after_seconds")) not in (int, float):
                raise RuntimeError(f"schema v2 segment {i} needs a numeric pause_after_seconds")
        speaker = normalize_text(seg.get("speaker"))
        if speaker not in allowed_speakers:
            raise RuntimeError(f"segment {i} has unsupported speaker {speaker!r}")
        text = normalize_text(seg.get("text"))
        if len(text) < 2:
            raise RuntimeError(f"segment {i} has empty/short text")
        max_turn_characters = cfg["max_turn_characters"] if schema_version >= 2 else 5000
        if len(text) > max_turn_characters:
            raise RuntimeError(
                f"segment {i} is too long ({len(text)} characters); split it into shorter turns"
            )
        pause_after_seconds(seg, cfg)
        kind = normalize_text(seg.get("kind")) or "dialogue"
        story_title = normalize_text(seg.get("story_title"))
        if schema_version >= 2:
            if type(seg.get("kind")) is not str:
                raise RuntimeError(f"schema v2 segment {i} needs a string kind")
            if type(seg.get("beat")) is not str:
                raise RuntimeError(f"schema v2 segment {i} needs a string beat")
            if "story_title" in seg and type(seg.get("story_title")) is not str:
                raise RuntimeError(f"schema v2 segment {i} needs a string story_title")
            if kind not in SCHEMA_V2_KINDS:
                raise RuntimeError(
                    f"schema v2 segment {i} needs kind from: {', '.join(sorted(SCHEMA_V2_KINDS))}"
                )
            if kind in SCHEMA_V2_STORY_KINDS and not story_title:
                raise RuntimeError(f"story segment {i} needs story_title")
            if kind not in SCHEMA_V2_STORY_KINDS and story_title:
                raise RuntimeError(f"non-story segment {i} must not have story_title")
        if story_title:
            story_groups.setdefault(story_title, []).append(seg)
        if schema_version >= 2:
            beat = normalize_text(seg.get("beat"))
            if beat not in SCHEMA_V2_BEATS:
                raise RuntimeError(
                    f"schema v2 segment {i} needs beat from: {', '.join(sorted(SCHEMA_V2_BEATS))}"
                )
            if speaker == "guest" and kind != "deep-dive":
                raise RuntimeError("guest turns are limited to deep-dive stories")
            if speaker == previous_speaker:
                consecutive_speaker_turns += 1
            else:
                previous_speaker = speaker
                consecutive_speaker_turns = 1
            if consecutive_speaker_turns > 2:
                raise RuntimeError("no speaker may have more than two consecutive turns")
    notes = script.get("show_notes", [])
    if not isinstance(notes, list):
        raise RuntimeError("show_notes must be an array")
    for i, note in enumerate(notes, start=1):
        if not isinstance(note, dict):
            raise RuntimeError(f"show_notes item {i} needs a title")
        if schema_version >= 2:
            if type(note.get("title")) is not str:
                raise RuntimeError(f"schema v2 show_notes item {i} needs a string title")
            if type(note.get("url")) is not str:
                raise RuntimeError(f"schema v2 show_notes item {i} needs a string url")
        if not normalize_text(note.get("title")):
            raise RuntimeError(f"show_notes item {i} needs a title")
        if not normalize_text(note.get("url")):
            raise RuntimeError(f"show_notes item {i} needs a primary URL")
    if schema_version >= 2:
        for story_title, story_segments in story_groups.items():
            story_speakers = {normalize_text(segment.get("speaker")) for segment in story_segments}
            if "host_female" not in story_speakers or "host_male" not in story_speakers:
                raise RuntimeError(f"story '{story_title}' must include both recurring hosts")
            story_kinds = {normalize_text(segment.get("kind")) for segment in story_segments}
            if not story_kinds.issubset(SCHEMA_V2_STORY_KINDS) or len(story_kinds) != 1:
                raise RuntimeError(f"story '{story_title}' has inconsistent story kinds")
            if "quick" in story_kinds and len(story_segments) < 2:
                raise RuntimeError(f"quick story '{story_title}' needs at least two conversational turns")
            if "deep-dive" in story_kinds and len(story_segments) < 4:
                raise RuntimeError(f"deep-dive story '{story_title}' needs at least four conversational turns")
            if not any(normalize_text(segment.get("beat")) in SCHEMA_V2_RESPONSE_BEATS for segment in story_segments):
                raise RuntimeError(f"story '{story_title}' needs a question, reaction, or qualification")
        notes_by_title = {
            normalize_text(note.get("title")): note
            for note in notes
            if isinstance(note, dict)
        }
        for story_title in story_groups:
            note = notes_by_title.get(story_title)
            if note is None:
                raise RuntimeError(f"story '{story_title}' has no show_notes entry")
            if not normalize_text(note.get("section")):
                raise RuntimeError(
                    f"show_notes entry for story '{story_title}' needs a section "
                    "so story-boundary pauses can tell a section change"
                )
    metrics = script_metrics(script, cfg)
    if schema_version >= 2:
        metrics["editorial"] = check_editorial(script, cfg)
    estimated = metrics["estimated_duration_seconds"]
    if estimated < cfg["min_duration_seconds"] or estimated > cfg["max_duration_seconds"]:
        raise RuntimeError(
            f"estimated episode duration is {estimated / 60:.2f} minutes; "
            f"it must be between {cfg['min_duration_seconds'] / 60:.0f} and "
            f"{cfg['max_duration_seconds'] / 60:.0f} minutes"
        )
    return metrics


def multipart_form(fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = "----HermesOmniVoice" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode("ascii"),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def request_omnivoice_clone(
    text: str,
    prompt_id: str,
    cfg: dict,
    dest: Path,
    speed: float,
) -> None:
    body, content_type = multipart_form({
        "text": text,
        "prompt_id": prompt_id,
        "speed": str(speed),
        "num_step": str(cfg["omnivoice_num_steps"]),
        "preprocess_prompt": "true",
        "postprocess_output": str(cfg["omnivoice_postprocess_output"]).lower(),
        "response_format": "wav",
    })
    request = urllib.request.Request(
        cfg["tts_base_url"] + "/v1/audio/clone",
        data=body,
        headers={"Content-Type": content_type, "Accept": "audio/wav"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"OmniVoice clone HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OmniVoice clone connection failed: {exc.reason}") from exc
    if len(data) < 500:
        raise RuntimeError(f"OmniVoice returned suspiciously small audio ({len(data)} bytes)")

    fd, source_name = tempfile.mkstemp(
        prefix=dest.name + ".", suffix=".clone.tmp.wav", dir=dest.parent
    )
    source = Path(source_name)
    os.close(fd)
    try:
        source.write_bytes(data)
        if not media_is_valid(source, "wav"):
            raise RuntimeError(f"OmniVoice returned invalid WAV audio: {source}")
        run([
            "ffmpeg", "-y", "-v", "error", "-i", str(source),
            "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame",
            "-b:a", cfg["bitrate"], str(dest),
        ])
    finally:
        source.unlink(missing_ok=True)


def request_tts(text: str, voice: str, cfg: dict, dest: Path, speed: float) -> None:
    if cfg["tts_provider"] == "omnivoice":
        request_omnivoice_clone(text, voice, cfg, dest, speed)
        return
    payload = {
        "model": cfg["tts_model"],
        "input": text,
        "voice": voice,
        "speed": speed,
    }
    request = urllib.request.Request(
        cfg["tts_base_url"] + "/v1/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "audio/mpeg"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"TTS HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"TTS connection failed: {exc.reason}") from exc
    if len(data) < 500:
        raise RuntimeError(f"TTS returned suspiciously small audio ({len(data)} bytes)")
    dest.write_bytes(data)


def tts_cache_key(text: str, voice: str, speed: float, cfg: dict) -> str:
    material = {
        "provider": cfg["tts_provider"],
        "base_url": cfg["tts_base_url"],
        "model": cfg["tts_model"],
        "voice": voice,
        "speed": speed,
        "omnivoice_num_steps": cfg["omnivoice_num_steps"],
        "omnivoice_postprocess_output": cfg["omnivoice_postprocess_output"],
        "bitrate": cfg["bitrate"],
        "text": normalize_text(text),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def tts_cache_path(cache_key: str, cfg: dict) -> Path:
    root = Path(cfg["tts_cache_dir"])
    return root / cache_key[:2] / f"{cache_key}.audio"


def silence_cache_key(seconds: float) -> str:
    return canonical_json_hash({
        "kind": "silence",
        "seconds": round(seconds, 3),
        "sample_rate": 44100,
        "channels": 1,
        "codec": "pcm_s16le",
    })


def key_path(audio_path: Path) -> Path:
    return audio_path.with_suffix(".key")


def nonempty_audio(path: Path, *, minimum_bytes: int = 500) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= minimum_bytes
    except OSError:
        return False


def key_sidecar(audio_path: Path) -> tuple[bool, str]:
    try:
        return True, key_path(audio_path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return False, ""
    except (OSError, UnicodeError):
        return True, ""


def stored_key(audio_path: Path) -> str:
    return key_sidecar(audio_path)[1]


def key_sidecar_missing(audio_path: Path) -> bool:
    return not key_sidecar(audio_path)[0]


def probe_float(raw: object, name: str) -> float:
    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"{name} must be a finite number")
    try:
        value = float(str(raw))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def probe_size(raw: object, name: str) -> int:
    value = probe_float(raw, name)
    if not value.is_integer():
        raise ValueError(f"{name} must be an integer")
    return int(value)


def copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    tmp = Path(tmp_name)
    os.close(fd)
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def materialize_tts(
    text: str,
    voice: str,
    speed: float,
    cfg: dict,
    raw: Path,
    cache_key: str,
    force: bool,
    allow_legacy_raw: bool = False,
) -> str:
    """Materialize a turn from the run cache, shared cache, or TTS service."""
    cache = tts_cache_path(cache_key, cfg)
    raw_is_current = media_is_valid(raw, "mp3") and stored_key(raw) == cache_key
    legacy_raw_is_current = (
        allow_legacy_raw and media_is_valid(raw, "mp3") and key_sidecar_missing(raw)
    )
    cache_is_current = (
        media_is_valid(cache, "mp3") and stored_key(cache) == cache_key
    )

    if not force and raw_is_current:
        if not cache_is_current:
            copy_atomic(raw, cache)
            atomic_write(key_path(cache), cache_key + "\n")
        return "run-cache"
    if not force and cache_is_current:
        copy_atomic(cache, raw)
        atomic_write(key_path(raw), cache_key + "\n")
        return "shared-cache"
    if not force and legacy_raw_is_current:
        copy_atomic(raw, cache)
        atomic_write(key_path(cache), cache_key + "\n")
        atomic_write(key_path(raw), cache_key + "\n")
        return "run-cache"

    print(f"[podcast] TTS: {voice} ({len(text)} chars)", flush=True)
    raw.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=raw.name + ".",
        suffix=".tts.tmp.mp3",
        dir=raw.parent,
    )
    temporary = Path(tmp_name)
    os.close(fd)
    try:
        request_tts(text, voice, cfg, temporary, speed)
        if not media_is_valid(temporary, "mp3"):
            raise RuntimeError(f"TTS produced invalid MP3 audio: {temporary}")
        copy_atomic(temporary, cache)
        atomic_write(key_path(cache), cache_key + "\n")
        os.replace(temporary, raw)
    finally:
        temporary.unlink(missing_ok=True)
    atomic_write(key_path(raw), cache_key + "\n")
    return "tts"


def materialize_wav(raw: Path, wav: Path, cache_key: str, force: bool) -> None:
    if not force and media_is_valid(wav, "wav") and stored_key(wav) == cache_key:
        return
    wav.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=wav.name + ".", suffix=".tmp.wav", dir=wav.parent)
    tmp = Path(tmp_name)
    os.close(fd)
    try:
        run([
            "ffmpeg", "-y", "-v", "error", "-i", str(raw),
            "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", str(tmp),
        ])
        if not media_is_valid(tmp, "wav"):
            raise RuntimeError(f"ffmpeg produced invalid WAV audio: {tmp}")
        os.replace(tmp, wav)
        atomic_write(key_path(wav), cache_key + "\n")
    finally:
        tmp.unlink(missing_ok=True)


def silence_file(wav_dir: Path, seconds: float, force: bool) -> Path | None:
    if seconds <= 0:
        return None
    milliseconds = int(round(seconds * 1000))
    path = wav_dir / f"pause-{milliseconds:04d}.wav"
    silence_key = silence_cache_key(seconds)
    # Silence is deterministic, but existing bytes are trusted only when their
    # exact content-identity sidecar is present. A missing or malformed sidecar
    # must regenerate rather than relabeling arbitrary WAV bytes as silence.
    existing = try_probe_media(path)
    existing_duration = None
    if isinstance(existing, dict):
        try:
            existing_duration = float(existing.get("format", {}).get("duration", 0))
        except (AttributeError, TypeError, ValueError, OverflowError):
            existing_duration = None
    if (
        media_is_valid(path, "wav")
        and stored_key(path) == silence_key
        and existing_duration is not None
        and math.isfinite(existing_duration)
        and math.isclose(existing_duration, seconds, abs_tol=PAUSE_DURATION_TOLERANCE_SECONDS)
    ):
        return path

    wav_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp.wav", dir=wav_dir)
    tmp = Path(tmp_name)
    os.close(fd)
    try:
        run([
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
            "-i", "anullsrc=r=44100:cl=mono", "-t", f"{seconds:.3f}",
            "-c:a", "pcm_s16le", str(tmp),
        ])
        generated = try_probe_media(tmp)
        try:
            generated_format = generated.get("format", {}) if isinstance(generated, dict) else {}
            generated_duration = probe_float(
                generated_format.get("duration", 0), "generated pause duration"
            ) if isinstance(generated_format, dict) else 0.0
        except (TypeError, ValueError, OverflowError):
            generated_duration = 0.0
        if (
            not media_is_valid(tmp, "wav")
            or not math.isfinite(generated_duration)
            or not math.isclose(generated_duration, seconds, abs_tol=PAUSE_DURATION_TOLERANCE_SECONDS)
        ):
            raise RuntimeError(f"ffmpeg produced invalid pause audio: {tmp}")
        os.replace(tmp, path)
        atomic_write(key_path(path), silence_key + "\n")
    finally:
        tmp.unlink(missing_ok=True)
    return path


def try_probe_media(path: Path) -> dict | None:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration,size,format_name",
                "-of", "json", str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        return payload if isinstance(payload, dict) else None
    except (OSError, subprocess.CalledProcessError, TypeError, ValueError, json.JSONDecodeError):
        return None


def media_is_valid(path: Path, expected_format: str | None = None) -> bool:
    minimum_bytes = 44 if expected_format == "wav" else 500
    if not nonempty_audio(path, minimum_bytes=minimum_bytes):
        return False
    payload = try_probe_media(path)
    if not isinstance(payload, dict):
        return False
    fmt = payload.get("format", {})
    if not isinstance(fmt, dict):
        return False
    try:
        duration = probe_float(fmt.get("duration", 0), "media duration")
        size = probe_size(fmt.get("size", 0), "media size")
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(duration) or duration <= 0 or size <= 0:
        return False
    if expected_format and expected_format not in str(fmt.get("format_name", "")).split(","):
        return False
    return True


def media_duration(path: Path) -> float | None:
    payload = try_probe_media(path)
    if not isinstance(payload, dict):
        return None
    fmt = payload.get("format", {})
    if not isinstance(fmt, dict):
        return None
    try:
        duration = probe_float(fmt.get("duration", 0), "media duration")
    except (TypeError, ValueError, OverflowError):
        return None
    return duration if math.isfinite(duration) and duration > 0 else None


def probe_media(path: Path) -> dict:
    try:
        probe = run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration,size,format_name",
            "-of", "json", str(path),
        ], capture=True).stdout
        payload = json.loads(probe)
        if not isinstance(payload, dict):
            raise RuntimeError("ffprobe returned an unexpected JSON shape")
        return payload
    except (subprocess.CalledProcessError, TypeError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"ffprobe validation failed for {path}") from exc


def write_show_notes(script: dict, run_dir: Path) -> Path:
    lines = [
        f"# {normalize_text(script['title'])}",
        "",
        f"Date: {script['date']}",
        "",
        normalize_text(script.get("description")),
        "",
        "## Stories and sources",
        "",
    ]
    for note in script.get("show_notes", []):
        title = normalize_text(note.get("title"))
        url = normalize_text(note.get("url"))
        section = normalize_text(note.get("section"))
        kind = normalize_text(note.get("kind"))
        label = " · ".join(x for x in (section, kind) if x)
        lines.append(f"- **[{title}]({url})**" + (f" — {label}" if label else ""))
        summary = normalize_text(note.get("summary"))
        if summary:
            lines.append(f"  {summary}")
        for extra in note.get("additional_sources", []) or []:
            if isinstance(extra, dict):
                extra_title = normalize_text(extra.get("title")) or normalize_text(extra.get("url"))
                extra_url = normalize_text(extra.get("url"))
                if extra_url:
                    lines.append(f"  - Additional: [{extra_title}]({extra_url})")
            elif normalize_text(extra):
                lines.append(f"  - Additional: {normalize_text(extra)}")
    disclaimer = normalize_text(script.get("disclaimer"))
    if disclaimer:
        lines.extend(["", "## Disclosure", "", disclaimer])
    path = run_dir / "show-notes.md"
    atomic_write(path, "\n".join(lines).rstrip() + "\n")
    return path


def check_gate(date_str: str, allow_unready: bool) -> Path | None:
    load_edition(date_str)
    marker = success_marker(date_str)
    if not allow_unready and not marker.is_file():
        raise RuntimeError(f"grep success marker is missing: {marker}")
    return marker if marker.exists() else None


def load_script(script_path: Path, date_str: str, cfg: dict) -> tuple[dict, dict]:
    try:
        script = json.loads(script_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"could not read script: {script_path}") from exc
    metrics = validate_script(script, date_str, cfg)
    return script, metrics


def legacy_raw_audio_compatible(
    run_dir: Path,
    script: dict,
    date_str: str,
    cfg: dict,
    edition_sha256: str,
) -> set[int]:
    """Return explicitly recorded indices eligible for sidecar-less recovery.

    A legacy raw file is reusable only when the old manifest already contains the
    same script and render-configuration identities used by the current renderer.
    Older markers without those identities are deliberately rebuilt rather than
    making an unverifiable synthesis claim.
    """
    stored_script_path = run_dir / "script.json"
    manifest_path = run_dir / "manifest.json"
    if not stored_script_path.is_file() or not manifest_path.is_file():
        return set()
    try:
        stored_script = json.loads(stored_script_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return set()
    if not isinstance(stored_script, dict) or not isinstance(manifest, dict):
        return set()
    try:
        if script_schema_version(script) != 1 or script_schema_version(stored_script) != 1:
            return set()
    except RuntimeError:
        return set()
    script_sha256 = canonical_json_hash(script)
    if canonical_json_hash(stored_script) != script_sha256:
        return set()
    if not isinstance(script.get("segments"), list):
        return set()
    current_segments = script["segments"]
    if (
        manifest.get("date") != date_str
        or manifest.get("edition") != str(content_path(date_str))
        or manifest.get("edition_sha256") != edition_sha256
        or manifest.get("script_sha256") != script_sha256
        or manifest.get("render_config_sha256") != render_config_hash(cfg)
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or type(manifest.get("segment_count")) is not int
        or manifest.get("segment_count") != len(current_segments)
    ):
        return set()
    if normalize_text(manifest.get("title")) != normalize_text(script.get("title")):
        return set()
    if manifest.get("tts_base_url") != cfg["tts_base_url"]:
        return set()
    if manifest.get("tts_model") != cfg["tts_model"]:
        return set()
    if (
        not strict_number_equal(manifest.get("tts_speed"), cfg["speed"])
        or not strict_speed_map_equal(
            manifest.get("speed_by_speaker"), cfg["speed_by_speaker"]
        )
    ):
        return set()
    if manifest.get("voices") != cfg["voices"]:
        return set()
    recorded_segments = manifest.get("segments")
    if not isinstance(recorded_segments, list) or not recorded_segments:
        return set()
    if len(recorded_segments) > len(current_segments):
        return set()
    permitted_indices: set[int] = set()
    previous_index = 0
    for recorded in recorded_segments:
        if not isinstance(recorded, dict):
            return set()
        index_raw = recorded.get("index")
        if type(index_raw) is not int:
            return set()
        index = index_raw
        if (
            index <= previous_index
            or index < 1
            or index > len(current_segments)
            or index in permitted_indices
        ):
            return set()
        previous_index = index
        current = current_segments[index - 1]
        speaker = normalize_text(current.get("speaker"))
        voice = cfg["voices"].get(speaker)
        expected_cache_key = tts_cache_key(
            normalize_text(current.get("text")),
            voice,
            speaker_speed(speaker, cfg),
            cfg,
        )
        if (
            recorded.get("speaker") != speaker
            or recorded.get("voice") != voice
            or not strict_number_equal(
                recorded.get("speed"),
                speaker_speed(speaker, cfg),
            )
            or recorded.get("kind") != (normalize_text(current.get("kind")) or "dialogue")
            or recorded.get("story_title") != normalize_text(current.get("story_title"))
            or recorded.get("characters") != len(normalize_text(current.get("text")))
            or recorded.get("cache_key") != expected_cache_key
        ):
            return set()
        raw = run_dir / "audio" / "raw" / f"{index:03d}_{speaker}.mp3"
        if (
            recorded.get("audio") != str(raw)
            or not raw.is_file()
            or recorded.get("raw_sha256") != safe_file_sha256(raw)
        ):
            continue
        permitted_indices.add(index)
    return permitted_indices


def validate_episode(path: Path, cfg: dict) -> tuple[dict, float, int]:
    ffprobe = probe_media(path)
    if not isinstance(ffprobe, dict):
        raise RuntimeError("ffprobe returned an unexpected JSON shape")
    format_data = ffprobe.get("format", {})
    if not isinstance(format_data, dict):
        raise RuntimeError("ffprobe returned an unexpected format shape")
    try:
        duration = probe_float(format_data.get("duration", 0), "episode duration")
        size = probe_size(format_data.get("size", 0), "episode size")
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("ffprobe returned invalid episode metadata") from exc
    formats = str(format_data.get("format_name", ""))
    if (
        "mp3" not in formats.split(",")
        or not math.isfinite(duration)
        or duration <= 0
        or size <= 0
    ):
        raise RuntimeError("rendered episode is not a non-empty MP3 with positive duration")
    if duration < cfg["min_duration_seconds"] or duration > cfg["max_duration_seconds"]:
        raise RuntimeError(
            f"rendered episode duration is {duration / 60:.2f} minutes; "
            f"it must be between {cfg['min_duration_seconds'] / 60:.0f} and "
            f"{cfg['max_duration_seconds'] / 60:.0f} minutes"
        )
    return ffprobe, duration, size


def audio_directories_are_exact(
    run_dir: Path,
    script: dict,
    cfg: dict,
) -> bool:
    """Reject completion claims with unlisted raw, WAV, pause, or temp files."""
    expected_raw: set[str] = set()
    expected_wav: set[str] = set()
    expected_pause: set[str] = set()
    for index, segment in enumerate(script["segments"], start=1):
        speaker = normalize_text(segment["speaker"])
        raw_name = f"{index:03d}_{speaker}.mp3"
        wav_name = f"{index:03d}_{speaker}.wav"
        expected_raw.update({raw_name, raw_name.removesuffix(".mp3") + ".key"})
        expected_wav.update({wav_name, wav_name.removesuffix(".wav") + ".key"})
        if index < len(script["segments"]):
            pause_seconds = pause_after_seconds(segment, cfg)
            if pause_seconds > 0:
                pause_name = f"pause-{int(round(pause_seconds * 1000)):04d}.wav"
                expected_pause.update({
                    pause_name,
                    pause_name.removesuffix(".wav") + ".key",
                })

    def names(path: Path) -> set[str] | None:
        try:
            if not path.is_dir():
                return None
            return {entry.name for entry in path.iterdir()}
        except OSError:
            return None

    raw_names = names(run_dir / "audio" / "raw")
    wav_names = names(run_dir / "audio" / "wav")
    return (
        raw_names is not None
        and wav_names is not None
        and raw_names <= expected_raw
        and wav_names <= expected_wav | expected_pause
    )


def completed_render_is_valid(
    run_dir: Path,
    episode: Path,
    done_path: Path,
    script: dict,
    metrics: dict,
    cfg: dict,
    date_str: str,
    edition_sha256: str,
) -> bool:
    """Validate a completion claim before taking the no-op render path."""
    manifest_path = run_dir / "manifest.json"
    if not episode.is_file() or not done_path.is_file() or not manifest_path.is_file():
        return False
    try:
        done = json.loads(done_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(done, dict) or not isinstance(manifest, dict):
        return False
    if (
        type(done.get("schema_version")) is not int
        or type(done.get("segment_count")) is not int
        or type(manifest.get("schema_version")) is not int
        or type(manifest.get("segment_count")) is not int
    ):
        return False
    script_sha256 = canonical_json_hash(script)
    config_sha256 = render_config_hash(cfg)
    episode_sha256 = safe_file_sha256(episode)
    if episode_sha256 is None:
        return False
    expected = {
        "date": date_str,
        "episode": str(episode),
        "episode_sha256": episode_sha256,
        "manifest": str(manifest_path),
        "script_sha256": script_sha256,
        "edition_sha256": edition_sha256,
        "render_config_sha256": config_sha256,
        "schema_version": metrics["schema_version"],
        "segment_count": len(script["segments"]),
    }
    if any(done.get(key) != value for key, value in expected.items()):
        return False
    manifest_sha256 = safe_file_sha256(manifest_path)
    if manifest_sha256 is None or done.get("manifest_sha256") != manifest_sha256:
        return False
    for key, value in expected.items():
        if key == "manifest":
            continue
        if manifest.get(key) != value:
            return False
    if (
        manifest.get("episode") != str(episode)
        or manifest.get("show_notes") != str(run_dir / "show-notes.md")
    ):
        return False
    if manifest.get("edition") != str(content_path(date_str)):
        return False
    if (
        manifest.get("voices") != cfg["voices"]
        or not strict_number_equal(manifest.get("tts_speed"), cfg["speed"])
        or not strict_speed_map_equal(
            manifest.get("speed_by_speaker"), cfg["speed_by_speaker"]
        )
    ):
        return False
    show_notes = manifest.get("show_notes")
    if not isinstance(show_notes, str) or not Path(show_notes).is_file():
        return False
    try:
        _, duration, _ = validate_episode(episode, cfg)
        done_duration = strict_json_number(
            done.get("duration_seconds", 0), "done duration"
        )
        manifest_duration = strict_json_number(
            manifest.get("actual_duration_seconds", 0), "manifest duration"
        )
    except (RuntimeError, TypeError, ValueError, OverflowError):
        return False
    if (
        not math.isfinite(done_duration)
        or not math.isfinite(manifest_duration)
        or not math.isclose(done_duration, duration, abs_tol=0.05)
        or not math.isclose(manifest_duration, duration, abs_tol=0.05)
    ):
        return False
    manifest_segments = manifest.get("segments")
    if not isinstance(manifest_segments, list) or len(manifest_segments) != len(script["segments"]):
        return False
    wav_dir = run_dir / "audio" / "wav"
    raw_dir = run_dir / "audio" / "raw"
    if not audio_directories_are_exact(run_dir, script, cfg):
        return False
    for index, segment in enumerate(script["segments"], start=1):
        speaker = normalize_text(segment["speaker"])
        voice = cfg["voices"].get(speaker)
        if not voice:
            return False
        text = normalize_text(segment["text"])
        expected_cache_key = tts_cache_key(
            text,
            voice,
            speaker_speed(speaker, cfg),
            cfg,
        )
        raw = raw_dir / f"{index:03d}_{speaker}.mp3"
        wav = wav_dir / f"{index:03d}_{speaker}.wav"
        recorded = manifest_segments[index - 1]
        if not isinstance(recorded, dict):
            return False
        if (
            type(recorded.get("index")) is not int
            or recorded.get("index") != index
            or recorded.get("speaker") != speaker
            or recorded.get("voice") != voice
            or not strict_number_equal(
                recorded.get("speed"),
                speaker_speed(speaker, cfg),
            )
            or recorded.get("kind") != (normalize_text(segment.get("kind")) or "dialogue")
            or recorded.get("beat") != normalize_text(segment.get("beat"))
            or recorded.get("story_title") != normalize_text(segment.get("story_title"))
            or recorded.get("characters") != len(text)
            or recorded.get("cache_key") != expected_cache_key
            or recorded.get("audio") != str(raw)
            or stored_key(raw) != expected_cache_key
            or stored_key(wav) != expected_cache_key
        ):
            return False
        if not media_is_valid(raw, "mp3") or not media_is_valid(wav, "wav"):
            return False
        raw_sha256 = safe_file_sha256(raw)
        wav_sha256 = safe_file_sha256(wav)
        if (
            raw_sha256 is None
            or wav_sha256 is None
            or recorded.get("raw_sha256") != raw_sha256
            or recorded.get("wav_sha256") != wav_sha256
        ):
            return False
        expected_pause_seconds = (
            pause_after_seconds(segment, cfg) if index < len(script["segments"]) else 0.0
        )
        try:
            recorded_pause_seconds = strict_json_number(
                recorded.get("pause_after_seconds", -1),
                "recorded pause_after_seconds",
            )
        except ValueError:
            return False
        if not math.isclose(recorded_pause_seconds, expected_pause_seconds, abs_tol=0.001):
            return False
        if index < len(script["segments"]):
            if expected_pause_seconds <= 0:
                if "pause_sha256" not in recorded or recorded["pause_sha256"] is not None:
                    return False
            else:
                pause = wav_dir / f"pause-{int(round(expected_pause_seconds * 1000)):04d}.wav"
                if not media_is_valid(pause, "wav"):
                    return False
                pause_sha256 = safe_file_sha256(pause)
                pause_duration = media_duration(pause)
                if (
                    pause_sha256 is None
                    or stored_key(pause) != silence_cache_key(expected_pause_seconds)
                    or recorded.get("pause_sha256") != pause_sha256
                    or pause_duration is None
                    or not math.isclose(
                        pause_duration,
                        expected_pause_seconds,
                        abs_tol=PAUSE_DURATION_TOLERANCE_SECONDS,
                    )
                ):
                    return False
        elif "pause_sha256" not in recorded or recorded["pause_sha256"] is not None:
            return False
    return True


def plan(date_str: str, script_path: Path, allow_unready: bool) -> dict:
    cfg = load_config()
    check_gate(date_str, allow_unready)
    script, metrics = load_script(script_path, date_str, cfg)
    run_dir = RUNS_DIR / date_str
    cache_hits = 0
    cache_misses = 0
    cache_sources: dict[str, int] = {"run-cache": 0, "shared-cache": 0, "tts": 0}
    voices = cfg["voices"]
    for index, segment in enumerate(script["segments"], start=1):
        speaker = normalize_text(segment["speaker"])
        voice = voices.get(speaker)
        if not voice:
            raise RuntimeError(f"no configured voice for {speaker}")
        text = normalize_text(segment["text"])
        cache_key = tts_cache_key(
            text,
            voice,
            speaker_speed(speaker, cfg),
            cfg,
        )
        raw = run_dir / "audio" / "raw" / f"{index:03d}_{speaker}.mp3"
        if media_is_valid(raw, "mp3") and stored_key(raw) == cache_key:
            source = "run-cache"
        elif (
            media_is_valid(tts_cache_path(cache_key, cfg), "mp3")
            and stored_key(tts_cache_path(cache_key, cfg)) == cache_key
        ):
            source = "shared-cache"
        else:
            source = "tts"
        cache_sources[source] += 1
        if source == "tts":
            cache_misses += 1
        else:
            cache_hits += 1
    result = {
        "date": date_str,
        "title": normalize_text(script["title"]),
        "duration_policy_seconds": {
            "min": cfg["min_duration_seconds"],
            "max": cfg["max_duration_seconds"],
        },
        "estimated_duration_seconds": round(metrics["estimated_duration_seconds"], 2),
        "estimated_duration_minutes": round(metrics["estimated_duration_seconds"] / 60, 2),
        "characters": metrics["characters"],
        "segments": metrics["segments"],
        "stories": metrics["stories"],
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "cache_sources": cache_sources,
        "tts_cache_dir": cfg["tts_cache_dir"],
        "tts_contacted": False,
    }
    editorial = metrics.get("editorial")
    if editorial:
        result["turn_lengths"] = editorial["turn_lengths"]
        result["expressive_tags"] = editorial["expressive_tags"]
        result["warnings"] = editorial["warnings"]
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def render(date_str: str, script_path: Path, allow_unready: bool, force: bool) -> Path:
    cfg = load_config()
    marker = check_gate(date_str, allow_unready)
    script, metrics = load_script(script_path, date_str, cfg)

    run_dir = RUNS_DIR / date_str
    episode = run_dir / "episode.mp3"
    done = run_dir / "done.json"
    edition_sha256 = file_sha256(content_path(date_str))
    if episode.exists() and done.exists() and not force:
        if completed_render_is_valid(
            run_dir,
            episode,
            done,
            script,
            metrics,
            cfg,
            date_str,
            edition_sha256,
        ):
            print(f"[podcast] episode already exists and is still valid: {episode}")
            return episode
        print(f"[podcast] completion marker is stale or incomplete; rebuilding: {episode}")
        done.unlink(missing_ok=True)
    if force:
        done.unlink(missing_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = run_dir / "audio"
    raw_dir = audio_dir / "raw"
    wav_dir = audio_dir / "wav"
    raw_dir.mkdir(parents=True, exist_ok=True)
    wav_dir.mkdir(parents=True, exist_ok=True)
    stored_script = run_dir / "script.json"
    legacy_raw_indices = legacy_raw_audio_compatible(
        run_dir,
        script,
        date_str,
        cfg,
        edition_sha256,
    )
    if script_path.resolve() != stored_script.resolve():
        atomic_write(stored_script, json.dumps(script, indent=2, ensure_ascii=False) + "\n")

    concat_items: list[Path] = []
    manifest_segments: list[dict] = []
    voices = cfg["voices"]
    cache_sources: dict[str, int] = {"run-cache": 0, "shared-cache": 0, "tts": 0}
    for index, segment in enumerate(script["segments"], start=1):
        speaker = normalize_text(segment["speaker"])
        text = normalize_text(segment["text"])
        voice = voices.get(speaker)
        if not voice:
            raise RuntimeError(f"no configured voice for {speaker}")
        cache_key = tts_cache_key(
            text,
            voice,
            speaker_speed(speaker, cfg),
            cfg,
        )
        raw = raw_dir / f"{index:03d}_{speaker}.mp3"
        wav = wav_dir / f"{index:03d}_{speaker}.wav"
        source = materialize_tts(
            text,
            voice,
            speaker_speed(speaker, cfg),
            cfg,
            raw,
            cache_key,
            force,
            allow_legacy_raw=index in legacy_raw_indices,
        )
        cache_sources[source] += 1
        materialize_wav(raw, wav, cache_key, force)
        concat_items.append(wav)
        pause: Path | None = None
        if index < len(script["segments"]):
            pause = silence_file(wav_dir, pause_after_seconds(segment, cfg), force)
            if pause is not None:
                concat_items.append(pause)
        pause_path = pause if index < len(script["segments"]) else None
        manifest_segments.append({
            "index": index,
            "speaker": speaker,
            "voice": voice,
            "speed": speaker_speed(speaker, cfg),
            "kind": normalize_text(segment.get("kind")) or "dialogue",
            "beat": normalize_text(segment.get("beat")),
            "story_title": normalize_text(segment.get("story_title")),
            "characters": len(text),
            "pause_after_seconds": pause_after_seconds(segment, cfg) if index < len(script["segments"]) else 0,
            "cache_key": cache_key,
            "tts_source": source,
            "audio": str(raw),
            "raw_sha256": file_sha256(raw),
            "wav_sha256": file_sha256(wav),
            "pause_sha256": file_sha256(pause_path) if pause_path else None,
        })

    def concat_line(path: Path) -> str:
        escaped = str(path).replace("'", "'\\\\''")
        return "file '" + escaped + "'"

    concat_text = "\n".join(concat_line(p) for p in concat_items) + "\n"
    fd, concat_tmp_name = tempfile.mkstemp(
        prefix="concat.", suffix=".txt", dir=audio_dir
    )
    concat_list = Path(concat_tmp_name)
    os.close(fd)
    try:
        atomic_write(concat_list, concat_text)
        fd, episode_tmp_name = tempfile.mkstemp(
            prefix="episode.", suffix=".tmp.mp3", dir=run_dir
        )
        episode_tmp = Path(episode_tmp_name)
        os.close(fd)
        try:
            run([
                "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", str(concat_list), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-b:a", cfg["bitrate"],
                "-metadata", f"title={normalize_text(script['title'])}",
                "-metadata", f"date={date_str}",
                str(episode_tmp),
            ])
            ffprobe, duration, size = validate_episode(episode_tmp, cfg)
            os.replace(episode_tmp, episode)
        finally:
            episode_tmp.unlink(missing_ok=True)
    finally:
        concat_list.unlink(missing_ok=True)
    episode_sha256 = file_sha256(episode)
    notes_path = write_show_notes(script, run_dir)
    manifest = {
        "date": date_str,
        "schema_version": metrics["schema_version"],
        "title": normalize_text(script["title"]),
        "description": normalize_text(script.get("description")),
        "episode": str(episode),
        "episode_sha256": episode_sha256,
        "show_notes": str(notes_path),
        "edition": str(content_path(date_str)),
        "edition_sha256": edition_sha256,
        "script_sha256": canonical_json_hash(script),
        "render_config_sha256": render_config_hash(cfg),
        "success_marker": str(marker) if marker else None,
        "tts_provider": cfg["tts_provider"],
        "tts_base_url": cfg["tts_base_url"],
        "tts_model": cfg["tts_model"],
        "tts_cache_dir": cfg["tts_cache_dir"],
        "omnivoice_num_steps": cfg["omnivoice_num_steps"],
        "omnivoice_postprocess_output": cfg["omnivoice_postprocess_output"],
        "voices": voices,
        "tts_speed": cfg["speed"],
        "speed_by_speaker": cfg["speed_by_speaker"],
        "bitrate": cfg["bitrate"],
        "pause_seconds": cfg["pause_seconds"],
        "max_pause_seconds": cfg["max_pause_seconds"],
        "duration_policy_seconds": {
            "min": cfg["min_duration_seconds"],
            "max": cfg["max_duration_seconds"],
        },
        "estimated_duration_seconds": metrics["estimated_duration_seconds"],
        "actual_duration_seconds": duration,
        "characters": metrics["characters"],
        # Recorded so estimated_chars_per_second can be retuned from real
        # renders instead of guessed. Speech time excludes the inserted pauses.
        # The numerator is the speed-adjusted baseline (characters/speed), so
        # the recorded rate can be copied straight into the config even though
        # speakers now synthesize at different speeds.
        "measured_chars_per_second": (
            round(
                sum(
                    len(normalize_text(segment["text"]))
                    / speaker_speed(normalize_text(segment["speaker"]), cfg)
                    for segment in script["segments"]
                )
                / (duration - metrics["pause_seconds"]),
                2,
            )
            if duration > metrics["pause_seconds"]
            else None
        ),
        "cache_sources": cache_sources,
        "segments": manifest_segments,
        "segment_count": len(script["segments"]),
        "rendered_at": iso_now(),
        "ffprobe": ffprobe,
    }
    manifest_path = run_dir / "manifest.json"
    atomic_write(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    manifest_sha256 = file_sha256(manifest_path)
    atomic_write(done, json.dumps({
        "date": date_str,
        "episode": str(episode),
        "episode_sha256": episode_sha256,
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "schema_version": metrics["schema_version"],
        "script_sha256": canonical_json_hash(script),
        "edition_sha256": edition_sha256,
        "render_config_sha256": render_config_hash(cfg),
        "segment_count": len(script["segments"]),
        "duration_seconds": duration,
        "completed_at": iso_now(),
    }, indent=2) + "\n")
    print(f"[podcast] rendered and validated: {episode} ({duration:.2f}s)")
    return episode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=today_ist())
    parser.add_argument("--mark-ready", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--script", type=Path)
    parser.add_argument("--allow-unready", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        if args.mark_ready:
            mark_ready(args.date)
        if args.plan:
            if not args.script:
                parser.error("--plan requires --script")
            plan(args.date, args.script, args.allow_unready)
        if args.render:
            if not args.script:
                parser.error("--render requires --script")
            render(args.date, args.script, args.allow_unready, args.force)
        if not args.mark_ready and not args.plan and not args.render:
            parser.error("choose --mark-ready, --plan, or --render")
        return 0
    except (KeyError, RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[podcast] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
