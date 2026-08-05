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


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def iso_now() -> str:
    return now_ist().isoformat(timespec="seconds")


def today_ist() -> str:
    return now_ist().date().isoformat()


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    base_url = os.environ.get("PODCAST_TTS_BASE_URL") or str(cfg["tts_base_url"])
    model = os.environ.get("PODCAST_TTS_MODEL") or str(cfg["tts_model"])
    speed_raw = os.environ.get("PODCAST_TTS_SPEED") or cfg.get("speed") or 1.0
    pause_raw = os.environ.get("PODCAST_PAUSE_SECONDS") or cfg.get("pause_seconds") or 0.32
    bitrate = os.environ.get("PODCAST_BITRATE") or str(cfg.get("bitrate") or "64k")
    cache_dir = os.environ.get("PODCAST_TTS_CACHE_DIR") or cfg.get("tts_cache_dir") or str(DEFAULT_TTS_CACHE_DIR)
    min_duration = os.environ.get("PODCAST_MIN_DURATION_SECONDS") or cfg.get("min_duration_seconds") or DEFAULT_MIN_DURATION_SECONDS
    max_duration = os.environ.get("PODCAST_MAX_DURATION_SECONDS") or cfg.get("max_duration_seconds") or DEFAULT_MAX_DURATION_SECONDS
    chars_per_second = os.environ.get("PODCAST_ESTIMATED_CHARS_PER_SECOND") or cfg.get("estimated_chars_per_second") or DEFAULT_ESTIMATED_CHARS_PER_SECOND
    max_turn_chars = os.environ.get("PODCAST_MAX_TURN_CHARACTERS") or cfg.get("max_turn_characters") or DEFAULT_MAX_TURN_CHARACTERS
    max_pause = os.environ.get("PODCAST_MAX_PAUSE_SECONDS") or cfg.get("max_pause_seconds") or DEFAULT_MAX_PAUSE_SECONDS
    cfg["tts_base_url"] = base_url.rstrip("/")
    cfg["tts_model"] = model
    cfg["speed"] = float(speed_raw)
    cfg["pause_seconds"] = float(pause_raw)
    cfg["bitrate"] = bitrate
    cfg["tts_cache_dir"] = str(cache_dir)
    cfg["min_duration_seconds"] = float(min_duration)
    cfg["max_duration_seconds"] = float(max_duration)
    cfg["estimated_chars_per_second"] = float(chars_per_second)
    cfg["max_turn_characters"] = int(max_turn_chars)
    cfg["max_pause_seconds"] = float(max_pause)
    if cfg["speed"] <= 0:
        raise ValueError("TTS speed must be positive")
    if cfg["min_duration_seconds"] <= 0 or cfg["max_duration_seconds"] < cfg["min_duration_seconds"]:
        raise ValueError("podcast duration bounds are invalid")
    if cfg["estimated_chars_per_second"] <= 0:
        raise ValueError("estimated_chars_per_second must be positive")
    if cfg["max_turn_characters"] < 2 or cfg["max_pause_seconds"] < 0:
        raise ValueError("podcast turn/pause limits are invalid")
    voices = dict(cfg.get("voices", {}))
    for speaker in ("host_female", "host_male", "guest"):
        env_name = "PODCAST_VOICE_" + speaker.upper()
        if os.environ.get(env_name):
            voices[speaker] = os.environ[env_name]
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
    return CONTENT_DIR / f"{date_str}.json"


def success_marker(date_str: str) -> Path:
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
        value = float(raw)
    except (TypeError, ValueError) as exc:
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
        "schema_version": int(script.get("schema_version", 1)),
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
    if normalize_text(script.get("date")) != date_str:
        raise RuntimeError(f"script date must be {date_str}")
    if not normalize_text(script.get("title")):
        raise RuntimeError("script needs a title")
    segments = script.get("segments")
    if not isinstance(segments, list) or not segments:
        raise RuntimeError("script needs a non-empty segments array")
    try:
        schema_version = int(script.get("schema_version", 1))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("schema_version must be an integer") from exc
    if schema_version not in {1, 2}:
        raise RuntimeError("unsupported podcast script schema_version")
    voices = {normalize_text(s.get("speaker")) for s in segments if isinstance(s, dict)}
    if "host_female" not in voices or "host_male" not in voices:
        raise RuntimeError("script must contain both host_female and host_male turns")
    allowed_speakers = {"host_female", "host_male", "guest"}
    allowed_beats = {
        "hook", "setup", "question", "reaction", "answer", "challenge",
        "counterpoint", "qualification", "implication", "takeaway",
        "transition", "guest-perspective", "outro",
    }
    story_groups: dict[str, list[dict]] = {}
    previous_speaker = ""
    consecutive_speaker_turns = 0
    for i, seg in enumerate(segments, start=1):
        if not isinstance(seg, dict):
            raise RuntimeError(f"segment {i} is not an object")
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
        if kind in {"quick", "deep-dive"} and not story_title:
            raise RuntimeError(f"story segment {i} needs story_title")
        if story_title:
            story_groups.setdefault(story_title, []).append(seg)
        if schema_version >= 2:
            beat = normalize_text(seg.get("beat"))
            if beat not in allowed_beats:
                raise RuntimeError(
                    f"schema v2 segment {i} needs beat from: {', '.join(sorted(allowed_beats))}"
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
        if not isinstance(note, dict) or not normalize_text(note.get("title")):
            raise RuntimeError(f"show_notes item {i} needs a title")
        if not normalize_text(note.get("url")):
            raise RuntimeError(f"show_notes item {i} needs a primary URL")
    if schema_version >= 2:
        response_beats = {"question", "reaction", "challenge", "counterpoint", "qualification"}
        for story_title, story_segments in story_groups.items():
            story_speakers = {normalize_text(segment.get("speaker")) for segment in story_segments}
            if "host_female" not in story_speakers or "host_male" not in story_speakers:
                raise RuntimeError(f"story '{story_title}' must include both recurring hosts")
            story_kinds = {normalize_text(segment.get("kind")) for segment in story_segments}
            if "quick" in story_kinds and len(story_segments) < 2:
                raise RuntimeError(f"quick story '{story_title}' needs at least two conversational turns")
            if "deep-dive" in story_kinds and len(story_segments) < 4:
                raise RuntimeError(f"deep-dive story '{story_title}' needs at least four conversational turns")
            if not any(normalize_text(segment.get("beat")) in response_beats for segment in story_segments):
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


def key_path(audio_path: Path) -> Path:
    return audio_path.with_suffix(".key")


def nonempty_audio(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= 500
    except OSError:
        return False


def stored_key(audio_path: Path) -> str:
    try:
        return key_path(audio_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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
) -> str:
    """Materialize a turn from the run cache, shared cache, or TTS service."""
    cache = tts_cache_path(cache_key, cfg)
    raw_is_current = nonempty_audio(raw) and stored_key(raw) == cache_key
    cache_is_current = nonempty_audio(cache)

    if not force and raw_is_current:
        if not cache_is_current:
            copy_atomic(raw, cache)
        return "run-cache"
    if not force and cache_is_current:
        copy_atomic(cache, raw)
        atomic_write(key_path(raw), cache_key + "\n")
        return "shared-cache"

    print(f"[podcast] TTS: {voice} ({len(text)} chars)", flush=True)
    raw.parent.mkdir(parents=True, exist_ok=True)
    temporary = raw.with_name(raw.name + ".tts.tmp")
    temporary.unlink(missing_ok=True)
    try:
        request_tts(text, voice, cfg, temporary)
        copy_atomic(temporary, cache)
        os.replace(temporary, raw)
    finally:
        temporary.unlink(missing_ok=True)
    atomic_write(key_path(raw), cache_key + "\n")
    return "tts"


def materialize_wav(raw: Path, wav: Path, cache_key: str, force: bool) -> None:
    if not force and nonempty_audio(wav) and stored_key(wav) == cache_key:
        return
    run([
        "ffmpeg", "-y", "-v", "error", "-i", str(raw),
        "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", str(wav),
    ])
    atomic_write(key_path(wav), cache_key + "\n")


def silence_file(wav_dir: Path, seconds: float, force: bool) -> Path | None:
    if seconds <= 0:
        return None
    milliseconds = int(round(seconds * 1000))
    path = wav_dir / f"pause-{milliseconds:04d}.wav"
    # Silence is deterministic; do not regenerate the same pause once per turn
    # during a forced render.
    if not nonempty_audio(path):
        run([
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
            "-i", "anullsrc=r=44100:cl=mono", "-t", f"{seconds:.3f}",
            "-c:a", "pcm_s16le", str(path),
        ])
    return path


def probe_media(path: Path) -> dict:
    try:
        probe = run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration,size,format_name",
            "-of", "json", str(path),
        ], capture=True).stdout
        return json.loads(probe)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
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
        if nonempty_audio(raw) and stored_key(raw) == cache_key:
            source = "run-cache"
        elif nonempty_audio(tts_cache_path(cache_key, cfg)):
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
    if episode.exists() and done.exists() and not force:
        print(f"[podcast] episode already exists; use --force to re-render: {episode}")
        return episode
    if force:
        done.unlink(missing_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = run_dir / "audio"
    raw_dir = audio_dir / "raw"
    wav_dir = audio_dir / "wav"
    raw_dir.mkdir(parents=True, exist_ok=True)
    wav_dir.mkdir(parents=True, exist_ok=True)
    stored_script = run_dir / "script.json"
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
        source = materialize_tts(text, voice, cfg, raw, cache_key, force)
        cache_sources[source] += 1
        materialize_wav(raw, wav, cache_key, force)
        concat_items.append(wav)
        if index < len(script["segments"]):
            pause = silence_file(wav_dir, pause_after_seconds(segment, cfg), force)
            if pause is not None:
                concat_items.append(pause)
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
        })

    concat_list = run_dir / "audio" / "concat.txt"

    def concat_line(path: Path) -> str:
        escaped = str(path).replace("'", "'\\\\''")
        return "file '" + escaped + "'"

    concat_text = "\n".join(concat_line(p) for p in concat_items) + "\n"
    atomic_write(concat_list, concat_text)
    episode_tmp = run_dir / "episode.tmp.mp3"
    run([
        "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-b:a", cfg["bitrate"],
        "-metadata", f"title={normalize_text(script['title'])}",
        "-metadata", f"date={date_str}",
        str(episode_tmp),
    ])
    ffprobe = probe_media(episode_tmp)
    format_data = ffprobe.get("format", {})
    try:
        duration = float(format_data.get("duration", 0))
        size = int(float(format_data.get("size", 0)))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("ffprobe returned invalid episode metadata") from exc
    formats = str(format_data.get("format_name", ""))
    if "mp3" not in formats.split(",") or duration <= 0 or size <= 0:
        raise RuntimeError("rendered episode is not a non-empty MP3 with positive duration")
    if duration < cfg["min_duration_seconds"] or duration > cfg["max_duration_seconds"]:
        raise RuntimeError(
            f"rendered episode duration is {duration / 60:.2f} minutes; "
            f"it must be between {cfg['min_duration_seconds'] / 60:.0f} and "
            f"{cfg['max_duration_seconds'] / 60:.0f} minutes"
        )
    os.replace(episode_tmp, episode)
    notes_path = write_show_notes(script, run_dir)
    manifest = {
        "date": date_str,
        "schema_version": metrics["schema_version"],
        "title": normalize_text(script["title"]),
        "description": normalize_text(script.get("description")),
        "episode": str(episode),
        "show_notes": str(notes_path),
        "edition": str(content_path(date_str)),
        "edition_sha256": hashlib.sha256(content_path(date_str).read_bytes()).hexdigest(),
        "success_marker": str(marker) if marker else None,
        "tts_base_url": cfg["tts_base_url"],
        "tts_model": cfg["tts_model"],
        "tts_cache_dir": cfg["tts_cache_dir"],
        "voices": voices,
        "duration_policy_seconds": {
            "min": cfg["min_duration_seconds"],
            "max": cfg["max_duration_seconds"],
        },
        "estimated_duration_seconds": metrics["estimated_duration_seconds"],
        "actual_duration_seconds": duration,
        "characters": metrics["characters"],
        "cache_sources": cache_sources,
        "segments": manifest_segments,
        "rendered_at": iso_now(),
        "ffprobe": ffprobe,
    }
    atomic_write(run_dir / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    atomic_write(done, json.dumps({
        "date": date_str,
        "episode": str(episode),
        "manifest": str(run_dir / "manifest.json"),
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
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[podcast] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
