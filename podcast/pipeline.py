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
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent.parent
PODCAST_DIR = BASE / "podcast"
RUNS_DIR = PODCAST_DIR / "runs"
CONTENT_DIR = BASE / "content"
PAGES_DIR = BASE.parent / "grep-pages"
CONFIG_PATH = PODCAST_DIR / "config.json"
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_TIMEOUT = 180
DEFAULT_TTS_CACHE_DIR = BASE.parent / "cache" / "grep-podcast" / "tts"
DEFAULT_MIN_DURATION_SECONDS = 600.0
DEFAULT_MAX_DURATION_SECONDS = 900.0
DEFAULT_ESTIMATED_CHARS_PER_SECOND = 15.2
DEFAULT_MAX_TURN_CHARACTERS = 720
DEFAULT_MAX_PAUSE_SECONDS = 2.0
PAUSE_DURATION_TOLERANCE_SECONDS = 0.005
SCHEMA_V2_KINDS = {"intro", "quick", "deep-dive", "outro"}
SCHEMA_V2_STORY_KINDS = {"quick", "deep-dive"}
SCHEMA_V2_BEATS = {
    "hook", "setup", "question", "reaction", "answer", "challenge",
    "counterpoint", "qualification", "implication", "takeaway",
    "comparison", "transition", "guest-perspective", "outro",
}
SCHEMA_V2_RESPONSE_BEATS = {
    "question", "reaction", "answer", "challenge", "counterpoint",
    "qualification", "implication", "takeaway", "comparison",
}


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


def render_config_hash(cfg: dict) -> str:
    identity = {
        "tts_base_url": cfg["tts_base_url"],
        "tts_model": cfg["tts_model"],
        "tts_cache_dir": cfg["tts_cache_dir"],
        "speed": cfg["speed"],
        "bitrate": cfg["bitrate"],
        "voices": cfg["voices"],
        "pause_seconds": cfg["pause_seconds"],
        "min_duration_seconds": cfg["min_duration_seconds"],
        "max_duration_seconds": cfg["max_duration_seconds"],
        "estimated_chars_per_second": cfg["estimated_chars_per_second"],
        "max_turn_characters": cfg["max_turn_characters"],
        "max_pause_seconds": cfg["max_pause_seconds"],
    }
    return canonical_json_hash(identity)


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("podcast config must be a JSON object")
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
    cfg["tts_base_url"] = nonempty_string(base_url_raw, "tts_base_url").rstrip("/")
    if not cfg["tts_base_url"]:
        raise ValueError("tts_base_url must be a non-empty string")
    cfg["tts_model"] = nonempty_string(model_raw, "tts_model")
    cfg["speed"] = finite_float(speed_raw, "speed")
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
    speech_seconds = characters / cfg["estimated_chars_per_second"] / cfg["speed"]
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
    metrics = script_metrics(script, cfg)
    estimated = metrics["estimated_duration_seconds"]
    if estimated < cfg["min_duration_seconds"] or estimated > cfg["max_duration_seconds"]:
        raise RuntimeError(
            f"estimated episode duration is {estimated / 60:.2f} minutes; "
            f"it must be between {cfg['min_duration_seconds'] / 60:.0f} and "
            f"{cfg['max_duration_seconds'] / 60:.0f} minutes"
        )
    return metrics


def request_tts(text: str, voice: str, cfg: dict, dest: Path) -> None:
    payload = {
        "model": cfg["tts_model"],
        "input": text,
        "voice": voice,
        "speed": cfg["speed"],
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


def tts_cache_key(text: str, voice: str, cfg: dict) -> str:
    material = {
        "base_url": cfg["tts_base_url"],
        "model": cfg["tts_model"],
        "voice": voice,
        "speed": cfg["speed"],
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
    cache_is_current = media_is_valid(cache, "mp3")

    if not force and raw_is_current:
        if not cache_is_current:
            copy_atomic(raw, cache)
        return "run-cache"
    if not force and cache_is_current:
        copy_atomic(cache, raw)
        atomic_write(key_path(raw), cache_key + "\n")
        return "shared-cache"
    if not force and legacy_raw_is_current:
        copy_atomic(raw, cache)
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
        request_tts(text, voice, cfg, temporary)
        if not media_is_valid(temporary, "mp3"):
            raise RuntimeError(f"TTS produced invalid MP3 audio: {temporary}")
        copy_atomic(temporary, cache)
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
    # Silence is deterministic; reuse it when its actual media duration is valid,
    # including legacy pause files that predate sidecars.
    existing = try_probe_media(path)
    existing_duration = None
    if isinstance(existing, dict):
        try:
            existing_duration = float(existing.get("format", {}).get("duration", 0))
        except (AttributeError, TypeError, ValueError, OverflowError):
            existing_duration = None
    if (
        media_is_valid(path, "wav")
        and existing_duration is not None
        and math.isfinite(existing_duration)
        and math.isclose(existing_duration, seconds, abs_tol=PAUSE_DURATION_TOLERANCE_SECONDS)
    ):
        if stored_key(path) != silence_key:
            atomic_write(key_path(path), silence_key + "\n")
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
        isinstance(manifest.get("tts_speed"), bool)
        or manifest.get("tts_speed") != cfg["speed"]
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
        expected_cache_key = tts_cache_key(normalize_text(current.get("text")), voice, cfg)
        if (
            recorded.get("speaker") != speaker
            or recorded.get("voice") != voice
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
    show_notes = manifest.get("show_notes")
    if not isinstance(show_notes, str) or not Path(show_notes).is_file():
        return False
    try:
        _, duration, _ = validate_episode(episode, cfg)
        done_duration = probe_float(done.get("duration_seconds", 0), "done duration")
        manifest_duration = probe_float(
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
    for index, segment in enumerate(script["segments"], start=1):
        speaker = normalize_text(segment["speaker"])
        voice = cfg["voices"].get(speaker)
        if not voice:
            return False
        text = normalize_text(segment["text"])
        expected_cache_key = tts_cache_key(text, voice, cfg)
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
            recorded_pause_seconds = finite_float(
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
        cache_key = tts_cache_key(text, voice, cfg)
        raw = run_dir / "audio" / "raw" / f"{index:03d}_{speaker}.mp3"
        if media_is_valid(raw, "mp3") and stored_key(raw) == cache_key:
            source = "run-cache"
        elif media_is_valid(tts_cache_path(cache_key, cfg), "mp3"):
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
        cache_key = tts_cache_key(text, voice, cfg)
        raw = raw_dir / f"{index:03d}_{speaker}.mp3"
        wav = wav_dir / f"{index:03d}_{speaker}.wav"
        source = materialize_tts(
            text,
            voice,
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
        "tts_base_url": cfg["tts_base_url"],
        "tts_model": cfg["tts_model"],
        "tts_cache_dir": cfg["tts_cache_dir"],
        "voices": voices,
        "tts_speed": cfg["speed"],
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
