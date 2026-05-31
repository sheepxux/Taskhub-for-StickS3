"""
StickS3 voice-recorder transcription worker (Step 2).

Polls state.db for queued entries and, for each:
  1. ffmpeg: Opus -> 16 kHz mono WAV
  2. whisper.cpp (whisper-cli): WAV -> text
  3. derive a short title
  4. append to memory/voice/YYYY-MM-DD.md
  5. delete the staged audio file
  6. mark the entry done

Run continuously:  python3 transcribe.py
Drain once & exit: python3 transcribe.py --once

Env overrides:
  WHISPER_BIN    path to whisper-cli            (default: found on PATH)
  WHISPER_MODEL  path to ggml model             (default: ./models/ggml-large-v3.bin)
  FFMPEG_BIN     path to ffmpeg                  (default: found on PATH)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import db
from summarize import classify_text

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
# Where transcripts land. Point this at OpenClaw's indexed memory dir
# (~/.openclaw/workspace/memory/voice) so `openclaw memory search` can find them.
VOICE_DIR = Path(os.environ.get("VOICE_DIR", ROOT / "memory" / "voice"))
POLL_INTERVAL_S = 0.5

WHISPER_BIN = os.environ.get("WHISPER_BIN") or shutil.which("whisper-cli")
WHISPER_MODEL = Path(os.environ.get("WHISPER_MODEL", ROOT / "models" / "ggml-large-v3.bin"))
FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
# If set (e.g. http://127.0.0.1:8080), POST to a resident whisper-server so the
# model stays in memory instead of reloading per file (matters a lot for large-v3).
WHISPER_SERVER_URL = os.environ.get("WHISPER_SERVER_URL", "").rstrip("/")
# OpenClaw CLI; after each transcript we ask it to reindex so the note is
# immediately searchable via `openclaw memory search`. Best-effort.
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN") or shutil.which("openclaw") \
    or str(Path.home() / ".local" / "npm" / "bin" / "openclaw")


def _check_tools() -> None:
    missing = []
    if not FFMPEG_BIN:
        missing.append("ffmpeg (set FFMPEG_BIN or `brew install ffmpeg`)")
    if WHISPER_SERVER_URL:
        return _finish_check(missing)  # server mode: no local binary/model needed here
    if not WHISPER_BIN:
        missing.append("whisper-cli (set WHISPER_BIN or `brew install whisper-cpp`)")
    if not WHISPER_MODEL.exists():
        missing.append(f"model file {WHISPER_MODEL}")
    _finish_check(missing)


def _finish_check(missing: list[str]) -> None:
    if missing:
        sys.exit("transcribe.py: missing dependencies:\n  - " + "\n  - ".join(missing))


def _decode_to_wav(src: Path, dst: Path) -> None:
    subprocess.run(
        [FFMPEG_BIN, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", "-f", "wav", str(dst)],
        check=True,
        capture_output=True,
    )


def _whisper(wav: Path) -> str:
    if WHISPER_SERVER_URL:
        return _whisper_via_server(wav)
    return _whisper_via_cli(wav)


def _whisper_via_server(wav: Path) -> str:
    """POST the WAV to a resident whisper-server /inference endpoint."""
    boundary = uuid.uuid4().hex
    fields = {"temperature": "0.0", "response_format": "json", "language": "auto"}
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
    )
    parts.append(wav.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        f"{WHISPER_SERVER_URL}/inference",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        payload = resp.read().decode("utf-8")
    try:
        return json.loads(payload).get("text", "").strip()
    except json.JSONDecodeError:
        return payload.strip()  # response_format may have returned plain text


def _whisper_via_cli(wav: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        out_prefix = Path(tmp) / "out"
        subprocess.run(
            [
                WHISPER_BIN, "-m", str(WHISPER_MODEL), "-f", str(wav),
                "-l", "auto", "-nt", "-otxt", "-of", str(out_prefix),
            ],
            check=True,
            capture_output=True,
        )
        text = (out_prefix.with_suffix(".txt")).read_text(encoding="utf-8")
    return text.strip()


def reindex_memory() -> None:
    """Best-effort `openclaw memory index` so a new transcript is searchable now.
    Must never break transcription, so all failures are swallowed."""
    if not Path(OPENCLAW_BIN).exists():
        return
    try:
        subprocess.run(
            [OPENCLAW_BIN, "memory", "index"],
            check=False, capture_output=True, timeout=180,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[index] skipped: {e!r}", file=sys.stderr)


def _make_title(text: str) -> str:
    """Short 5-8 char-ish title. Step 3 replaces this with an LLM call."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    for sep in ("。", "，", ".", ",", "!", "?", "！", "？"):
        if sep in first:
            first = first.split(sep, 1)[0]
            break
    return (first[:12] or "无标题").strip()


def _preview(text: str, limit: int = 40) -> str:
    flat = " ".join(text.split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _append_markdown(when: datetime, title: str, text: str, tag: str) -> None:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    day_file = VOICE_DIR / f"{when.astimezone(CST):%Y-%m-%d}.md"
    # Trailing #tag keeps the heading parseable AND makes the category greppable
    # / searchable inside OpenClaw memory.
    block = f"## {when.astimezone(CST):%H:%M} · {title} #{tag}\n\n{text}\n\n"
    with day_file.open("a", encoding="utf-8") as f:
        f.write(block)


def _claim_one() -> dict | None:
    """Atomically grab one queued entry and mark it transcribing."""
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM entries WHERE status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute("UPDATE entries SET status = 'transcribing' WHERE id = ?", (row["id"],))
        conn.execute("COMMIT")
        return dict(row)


def _finish(entry_id: str, title: str, preview: str, transcript: str, tag: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE entries SET status='done', title=?, preview=?, transcript=?, tag=? WHERE id=?",
            (title, preview, transcript, tag, entry_id),
        )


def _fail(entry_id: str, err: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE entries SET status='error', error=? WHERE id=?", (err[:500], entry_id)
        )


def _process(entry: dict) -> None:
    entry_id = entry["id"]
    audio_path = Path(entry["audio_path"]) if entry["audio_path"] else None
    if not audio_path or not audio_path.exists():
        _fail(entry_id, f"audio file missing: {audio_path}")
        return
    try:
        when = datetime.fromisoformat(entry["recorded_at"])
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "audio.wav"
            _decode_to_wav(audio_path, wav)
            text = _whisper(wav)
        title = _make_title(text)
        tag = classify_text(text)
        _append_markdown(when, title, text, tag)
        _finish(entry_id, title, _preview(text), text, tag)
        audio_path.unlink(missing_ok=True)
        print(f"[done] {entry_id}  «{title}» #{tag}  ({len(text)} chars)")
        reindex_memory()  # make it searchable in OpenClaw right away
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", "replace") if e.stderr else str(e)
        _fail(entry_id, f"{e} :: {stderr}")
        print(f"[error] {entry_id}: {stderr}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - worker must survive any single bad entry
        _fail(entry_id, repr(e))
        print(f"[error] {entry_id}: {e!r}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="drain the queue then exit")
    args = ap.parse_args()

    _check_tools()
    db.init_db()
    backend = f"server={WHISPER_SERVER_URL}" if WHISPER_SERVER_URL else f"cli model={WHISPER_MODEL.name}"
    print(f"transcribe worker up. backend={backend}")

    while True:
        entry = _claim_one()
        if entry is None:
            if args.once:
                return
            time.sleep(POLL_INTERVAL_S)
            continue
        _process(entry)


if __name__ == "__main__":
    main()
