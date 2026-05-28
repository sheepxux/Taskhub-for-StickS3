"""
StickS3 voice-recorder notifier (Step 3).

Runs right after summarize.py. For a given date it:
  1. reads memory/voice/YYYY-MM-DD-summary.md
  2. inserts an unread notification into state.db (always; this is what the
     device pulls via GET /api/v1/notifications/unread)
  3. pushes the summary to Telegram if TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
     are set; otherwise prints a dry-run line

Pushing a copy to the StickS3 inbound endpoint is deferred until the firmware
exists (roadmap Step 7).

  python3 notify.py                 # today (CST)
  python3 notify.py --date 2026-05-28
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import db

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
VOICE_DIR = ROOT / "memory" / "voice"


def _today_str() -> str:
    return f"{datetime.now(CST):%Y-%m-%d}"


def _now_iso() -> str:
    return datetime.now(CST).isoformat()


def _new_notification_id() -> str:
    return f"ntf_{secrets.token_hex(6)}"


def _parse_summary(text: str) -> tuple[str, str]:
    """Return (title, body) for the notification card from the summary markdown.

    Title = the "共 N 条 · ..." counts line if present, else the H1.
    Body  = the category headings joined, as a compact teaser.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    h1 = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), "今日摘要")
    counts = next((ln.strip() for ln in lines if ln.startswith("共 ")), h1)
    cats = [ln[3:].strip() for ln in lines if ln.startswith("## ")]
    body = counts if not cats else counts + "\n" + " · ".join(cats)
    return counts, body


def _insert_notification(title: str, body: str) -> str:
    nid = _new_notification_id()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO notifications (id, kind, title, body, read, created_at) VALUES (?,?,?,?,0,?)",
            (nid, "daily_summary", title, body, _now_iso()),
        )
    return nid


def _push_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("notify: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — skipping push (dry-run)")
        return False
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ok = json.loads(resp.read().decode("utf-8")).get("ok", False)
        print(f"notify: telegram push ok={ok}")
        return bool(ok)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"notify: telegram push failed: {e}")
        return False


def run(date: str) -> str | None:
    summary_file = VOICE_DIR / f"{date}-summary.md"
    if not summary_file.exists():
        print(f"notify: no summary file {summary_file} — run summarize.py first")
        return None
    text = summary_file.read_text(encoding="utf-8")
    db.init_db()

    title, body = _parse_summary(text)
    nid = _insert_notification(title, body)
    print(f"notify: inserted notification {nid}  «{title}»")

    _push_telegram(text)
    return nid


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=_today_str(), help="YYYY-MM-DD (CST), default today")
    args = ap.parse_args()
    run(args.date)


if __name__ == "__main__":
    main()
