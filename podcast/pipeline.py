#!/usr/bin/env python3
"""Render a structured grep podcast script with the local speech service.

The editorial agent writes a JSON script, then invokes:

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
    cfg["tts_base_url"] = base_url.rstrip("/")
    cfg["tts_model"] = model
    cfg["speed"] = float(speed_raw)
    cfg["pause_seconds"] = float(pause_raw)
    cfg["bitrate"] = bitrate
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


def validate_script(script: dict, date_str: str) -> None:
    if not isinstance(script, dict):
        raise RuntimeError("script must be a JSON object")
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
    for i, seg in enumerate(segments, start=1):
        if not isinstance(seg, dict):
            raise RuntimeError(f"segment {i} is not an object")
        speaker = normalize_text(seg.get("speaker"))
        if speaker not in {"host_female", "host_male", "guest"}:
            raise RuntimeError(f"segment {i} has unsupported speaker {speaker!r}")
        text = normalize_text(seg.get("text"))
        if len(text) < 2:
            raise RuntimeError(f"segment {i} has empty/short text")
        if len(text) > 5000:
            raise RuntimeError(f"segment {i} is too long; split it into smaller turns")
    notes = script.get("show_notes", [])
    if not isinstance(notes, list):
        raise RuntimeError("show_notes must be an array")
    for i, note in enumerate(notes, start=1):
        if not isinstance(note, dict) or not normalize_text(note.get("title")):
            raise RuntimeError(f"show_notes item {i} needs a title")
        if not normalize_text(note.get("url")):
            raise RuntimeError(f"show_notes item {i} needs a primary URL")


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


def render(date_str: str, script_path: Path, allow_unready: bool, force: bool) -> Path:
    cfg = load_config()
    edition = load_edition(date_str)
    marker = success_marker(date_str)
    if not allow_unready and not marker.is_file():
        raise RuntimeError(f"grep success marker is missing: {marker}")
    script = json.loads(script_path.read_text(encoding="utf-8"))
    validate_script(script, date_str)

    run_dir = RUNS_DIR / date_str
    if run_dir.exists() and (run_dir / "episode.mp3").exists() and not force:
        print(f"[podcast] episode already exists; use --force to re-render: {run_dir / 'episode.mp3'}")
        return run_dir / "episode.mp3"
    run_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = run_dir / "audio"
    raw_dir = audio_dir / "raw"
    wav_dir = audio_dir / "wav"
    raw_dir.mkdir(parents=True, exist_ok=True)
    wav_dir.mkdir(parents=True, exist_ok=True)
    stored_script = run_dir / "script.json"
    if script_path.resolve() != stored_script.resolve():
        atomic_write(stored_script, json.dumps(script, indent=2, ensure_ascii=False) + "\n")

    silence = wav_dir / "pause.wav"
    run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
        "-i", "anullsrc=r=44100:cl=mono", "-t", str(cfg["pause_seconds"]),
        "-c:a", "pcm_s16le", str(silence),
    ])

    concat_items: list[Path] = []
    manifest_segments: list[dict] = []
    voices = cfg["voices"]
    for index, segment in enumerate(script["segments"], start=1):
        speaker = normalize_text(segment["speaker"])
        text = normalize_text(segment["text"])
        voice = voices.get(speaker)
        if not voice:
            raise RuntimeError(f"no configured voice for {speaker}")
        raw = raw_dir / f"{index:03d}_{speaker}.mp3"
        wav = wav_dir / f"{index:03d}_{speaker}.wav"
        if not raw.exists() or force:
            print(f"[podcast] TTS {index}/{len(script['segments'])}: {speaker} ({len(text)} chars)", flush=True)
            request_tts(text, voice, cfg, raw)
        if not wav.exists() or force:
            run([
                "ffmpeg", "-y", "-v", "error", "-i", str(raw),
                "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", str(wav),
            ])
        concat_items.append(wav)
        if index < len(script["segments"]):
            concat_items.append(silence)
        manifest_segments.append({
            "index": index,
            "speaker": speaker,
            "voice": voice,
            "kind": normalize_text(segment.get("kind")) or "dialogue",
            "story_title": normalize_text(segment.get("story_title")),
            "characters": len(text),
            "audio": str(raw),
        })

    concat_list = run_dir / "audio" / "concat.txt"
    def concat_line(path: Path) -> str:
        escaped = str(path).replace("'", "'\\\\''")
        return "file '" + escaped + "'"
    concat_text = "\n".join(concat_line(p) for p in concat_items) + "\n"
    atomic_write(concat_list, concat_text)
    episode_tmp = run_dir / "episode.tmp.mp3"
    episode = run_dir / "episode.mp3"
    run([
        "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-b:a", cfg["bitrate"],
        "-metadata", f"title={normalize_text(script['title'])}",
        "-metadata", f"date={date_str}",
        str(episode_tmp),
    ])
    os.replace(episode_tmp, episode)
    notes_path = write_show_notes(script, run_dir)
    manifest = {
        "date": date_str,
        "title": normalize_text(script["title"]),
        "description": normalize_text(script.get("description")),
        "episode": str(episode),
        "show_notes": str(notes_path),
        "edition": str(content_path(date_str)),
        "edition_sha256": hashlib.sha256(content_path(date_str).read_bytes()).hexdigest(),
        "success_marker": str(marker) if marker.exists() else None,
        "tts_base_url": cfg["tts_base_url"],
        "tts_model": cfg["tts_model"],
        "voices": voices,
        "segments": manifest_segments,
        "rendered_at": iso_now(),
    }
    atomic_write(run_dir / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    try:
        probe = run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration,size,format_name",
            "-of", "json", str(episode),
        ], capture=True).stdout
        manifest["ffprobe"] = json.loads(probe)
        atomic_write(run_dir / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise RuntimeError("episode was written but ffprobe validation failed") from exc
    done = run_dir / "done.json"
    atomic_write(done, json.dumps({
        "date": date_str,
        "episode": str(episode),
        "manifest": str(run_dir / "manifest.json"),
        "completed_at": iso_now(),
    }, indent=2) + "\n")
    print(f"[podcast] rendered and validated: {episode}")
    return episode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=today_ist())
    parser.add_argument("--mark-ready", action="store_true")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--script", type=Path)
    parser.add_argument("--allow-unready", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        if args.mark_ready:
            mark_ready(args.date)
        if args.render:
            if not args.script:
                parser.error("--render requires --script")
            render(args.date, args.script, args.allow_unready, args.force)
        if not args.mark_ready and not args.render:
            parser.error("choose --mark-ready or --render")
        return 0
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[podcast] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
