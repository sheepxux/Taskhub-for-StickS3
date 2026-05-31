"""
Shared SQLite access for the voice-recorder skill (Step 2).

server.py, transcribe.py, summarize.py and notify.py all open the same
state.db. WAL mode lets the FastAPI server and the background worker read/write
concurrently without blocking each other.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id          TEXT PRIMARY KEY,
    recorded_at TEXT    NOT NULL,
    duration_ms INTEGER NOT NULL,
    device_id   TEXT,
    client_seq  INTEGER UNIQUE,
    audio_path  TEXT,
    title       TEXT    NOT NULL DEFAULT '(转录中)',
    preview     TEXT    NOT NULL DEFAULT '',
    tag         TEXT    NOT NULL DEFAULT '其他',  -- sticky-note category
    transcript  TEXT,
    status      TEXT    NOT NULL DEFAULT 'queued',  -- queued|transcribing|done|error
    error       TEXT,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id         TEXT PRIMARY KEY,
    kind       TEXT    NOT NULL,
    title      TEXT    NOT NULL,
    body       TEXT    NOT NULL,
    read       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(_SCHEMA)
        # Migrate older DBs that predate the tag column.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(entries)")}
        if "tag" not in cols:
            conn.execute("ALTER TABLE entries ADD COLUMN tag TEXT NOT NULL DEFAULT '其他'")
