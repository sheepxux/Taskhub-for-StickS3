"""
StickS3 voice-recorder skill server (Step 1: skeleton with in-memory fake data).

Per voice-recorder-design.md §7, exposes 5 endpoints. No TLS, no auth in this step.
Run: python3 server.py  (listens on 127.0.0.1:5577)
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

app = FastAPI(title="StickS3 voice-recorder", version="0.1.0-step1")

CST = timezone(timedelta(hours=8))


def _new_entry_id(when: datetime) -> str:
    return f"ent_{when:%Y_%m_%d}_{secrets.token_hex(3)}_{secrets.token_hex(3)}"


def _new_notification_id() -> str:
    return f"ntf_{secrets.token_hex(6)}"


# In-memory state. Replaced by SQLite in Step 2.
_entries: dict[str, dict] = {}
_client_seq_to_entry: dict[int, str] = {}  # idempotency lookup, O(1)
_notifications: dict[str, dict] = {}


def _seed_fake_data() -> None:
    """Seed two entries and one unread notification so GET endpoints return realistic shapes."""
    today = datetime.now(CST).replace(hour=14, minute=32, second=11, microsecond=0)
    e1_id = _new_entry_id(today)
    _entries[e1_id] = {
        "id": e1_id,
        "recorded_at": today.isoformat(),
        "duration_ms": 12340,
        "title": "咖啡馆灵感",
        "preview": "想到一个产品点子——把本地 LLM 跑在路由器上...",
    }
    later = today.replace(hour=16, minute=5, second=2)
    e2_id = _new_entry_id(later)
    _entries[e2_id] = {
        "id": e2_id,
        "recorded_at": later.isoformat(),
        "duration_ms": 8200,
        "title": "读 Bret Victor",
        "preview": "Inventing on Principle 那段关于即时反馈的论述...",
    }

    n_id = _new_notification_id()
    _notifications[n_id] = {
        "id": n_id,
        "kind": "daily_summary",
        "title": "今天记了 7 条",
        "body": "产品点子(3) · 阅读笔记(2) · 待办(2)。详情已推送到 Telegram。",
        "read": False,
    }


_seed_fake_data()


@app.post("/api/v1/entries")
async def upload_entry(
    audio: UploadFile = File(...),
    recorded_at: str = Form(...),
    device_id: str = Form(...),
    duration_ms: int = Form(...),
    client_seq: int = Form(...),
):
    # Idempotency: same client_seq replays return the existing entry.
    prior = _client_seq_to_entry.get(client_seq)
    if prior is not None:
        return {"entry_id": prior, "status": "queued"}

    try:
        when = datetime.fromisoformat(recorded_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="recorded_at must be ISO 8601")

    blob = await audio.read()  # discarded in Step 1; Step 2 writes to audio_uploads/
    entry_id = _new_entry_id(when)
    _entries[entry_id] = {
        "id": entry_id,
        "recorded_at": when.isoformat(),
        "duration_ms": duration_ms,
        "device_id": device_id,
        "client_seq": client_seq,
        "audio_bytes": len(blob),
        "title": "(转录中)",
        "preview": "",
    }
    _client_seq_to_entry[client_seq] = entry_id
    return {"entry_id": entry_id, "status": "queued"}


@app.get("/api/v1/entries/today")
async def list_today():
    # Compare CST calendar dates — device may send recorded_at with any timezone offset.
    today_cst = datetime.now(CST).date()
    items = []
    for e in _entries.values():
        when = datetime.fromisoformat(e["recorded_at"]).astimezone(CST).date()
        if when != today_cst:
            continue
        items.append({
            "id": e["id"],
            "recorded_at": e["recorded_at"],
            "duration_ms": e["duration_ms"],
            "title": e.get("title", ""),
            "preview": e.get("preview", ""),
        })
    items.sort(key=lambda x: x["recorded_at"])
    return items


@app.delete("/api/v1/entries/{entry_id}")
async def delete_entry(entry_id: str):
    entry = _entries.pop(entry_id, None)
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    seq = entry.get("client_seq")
    if seq is not None:
        _client_seq_to_entry.pop(seq, None)
    return {"deleted": entry_id}


@app.get("/api/v1/notifications/unread")
async def list_unread():
    return [
        {"id": n["id"], "kind": n["kind"], "title": n["title"], "body": n["body"]}
        for n in _notifications.values()
        if not n["read"]
    ]


@app.post("/api/v1/notifications/{notification_id}/ack")
async def ack_notification(notification_id: str):
    n = _notifications.get(notification_id)
    if n is None:
        raise HTTPException(status_code=404, detail="notification not found")
    n["read"] = True
    return {"acked": notification_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=5577, log_level="info")
