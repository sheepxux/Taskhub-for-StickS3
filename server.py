"""
StickS3 voice-recorder skill server (Step 2: SQLite-backed, real audio storage).

Per voice-recorder-design.md §7, exposes 5 endpoints. Uploads are written to
audio_uploads/ and enqueued in state.db; transcribe.py picks them up
asynchronously. No TLS, no auth in this step.
Run: python3 server.py  (listens on 127.0.0.1:5577)
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

import db

app = FastAPI(title="StickS3 voice-recorder", version="0.2.0-step2")

CST = timezone(timedelta(hours=8))
AUDIO_DIR = Path(__file__).resolve().parent / "audio_uploads"


def _new_entry_id(when: datetime) -> str:
    return f"ent_{when:%Y_%m_%d}_{secrets.token_hex(3)}_{secrets.token_hex(3)}"


def _new_notification_id() -> str:
    return f"ntf_{secrets.token_hex(6)}"


def _now_iso() -> str:
    return datetime.now(CST).isoformat()


@app.on_event("startup")
def _startup() -> None:
    AUDIO_DIR.mkdir(exist_ok=True)
    db.init_db()
    _seed_notification()


def _seed_notification() -> None:
    """Seed one unread notification so notification endpoints are testable before notify.py exists."""
    with db.connect() as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()
        if count:
            return
        conn.execute(
            "INSERT INTO notifications (id, kind, title, body, read, created_at) VALUES (?,?,?,?,0,?)",
            (
                _new_notification_id(),
                "daily_summary",
                "今天记了 7 条",
                "产品点子(3) · 阅读笔记(2) · 待办(2)。详情已推送到 Telegram。",
                _now_iso(),
            ),
        )


@app.post("/api/v1/entries")
async def upload_entry(
    audio: UploadFile = File(...),
    recorded_at: str = Form(...),
    device_id: str = Form(...),
    duration_ms: int = Form(...),
    client_seq: int = Form(...),
):
    # Idempotency: same client_seq replays return the existing entry.
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM entries WHERE client_seq = ?", (client_seq,)
        ).fetchone()
        if row is not None:
            return {"entry_id": row["id"], "status": "queued"}

    try:
        when = datetime.fromisoformat(recorded_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="recorded_at must be ISO 8601")

    entry_id = _new_entry_id(when)
    audio_path = AUDIO_DIR / f"{entry_id}.opus"
    blob = await audio.read()
    audio_path.write_bytes(blob)

    with db.connect() as conn:
        conn.execute(
            """INSERT INTO entries
               (id, recorded_at, duration_ms, device_id, client_seq, audio_path,
                title, preview, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                entry_id,
                when.isoformat(),
                duration_ms,
                device_id,
                client_seq,
                str(audio_path),
                "(转录中)",
                "",
                "queued",
                _now_iso(),
            ),
        )
    return {"entry_id": entry_id, "status": "queued"}


@app.get("/api/v1/entries/today")
async def list_today():
    # Compare CST calendar dates — device may send recorded_at with any timezone offset.
    today_cst = datetime.now(CST).date()
    items = []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, recorded_at, duration_ms, title, preview, tag FROM entries"
        ).fetchall()
    for r in rows:
        when = datetime.fromisoformat(r["recorded_at"]).astimezone(CST).date()
        if when != today_cst:
            continue
        items.append(
            {
                "id": r["id"],
                "recorded_at": r["recorded_at"],
                "duration_ms": r["duration_ms"],
                "title": r["title"],
                "preview": r["preview"],
                "tag": r["tag"],
            }
        )
    items.sort(key=lambda x: x["recorded_at"])
    return items


@app.delete("/api/v1/entries/{entry_id}")
async def delete_entry(entry_id: str):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT audio_path FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="entry not found")
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    if row["audio_path"]:
        Path(row["audio_path"]).unlink(missing_ok=True)
    return {"deleted": entry_id}


@app.get("/api/v1/notifications/unread")
async def list_unread():
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, kind, title, body FROM notifications WHERE read = 0"
        ).fetchall()
    return [
        {"id": r["id"], "kind": r["kind"], "title": r["title"], "body": r["body"]}
        for r in rows
    ]


@app.post("/api/v1/notifications/{notification_id}/ack")
async def ack_notification(notification_id: str):
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE notifications SET read = 1 WHERE id = ?", (notification_id,)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="notification not found")
    return {"acked": notification_id}


if __name__ == "__main__":
    import os

    import uvicorn

    # Step 6: the StickS3 reaches this over the home LAN, so bind 0.0.0.0 by
    # default. No auth/TLS yet — fine for a trusted home network. Override with
    # VR_HOST/VR_PORT if you want to restrict it.
    host = os.environ.get("VR_HOST", "0.0.0.0")
    port = int(os.environ.get("VR_PORT", "5577"))
    uvicorn.run(app, host=host, port=port, log_level="info")
