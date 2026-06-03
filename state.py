"""
SQLite WAL state store for chunked-progressive audits.

Persisted across PM2 restarts. One DB file shared by the FastMCP server and
the background runner thread — WAL mode handles the concurrent reader/writer.

Schema:
  sessions     — one row per chunked audit (status, settings, progress)
  chunks       — per-polling-interval metrics (p95, err_rate, delay used)
  artifacts    — paths to MD / CSV / PDF files produced at finalize
  events       — append-only log of state transitions (for debugging + UI)

NOT stored here: the crawl frontier (URLs to fetch). LibreCrawl owns that —
it's a single crawl per session at the upstream level. Our "chunks" are
30-60 second polling windows where the runner tunes crawlDelay based on
the target server's response signals.
"""

import os
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DB_PATH = Path(os.getenv("LIBRECRAWL_STATE_DB", Path.home() / "librecrawl-state.db"))
_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    url                 TEXT NOT NULL,
    status              TEXT NOT NULL,
    upstream_crawl_id   INTEGER,
    total_max_pages     INTEGER NOT NULL,
    chunk_target_pages  INTEGER NOT NULL,
    politeness          TEXT NOT NULL,
    current_delay_ms    INTEGER NOT NULL,
    pages_done          INTEGER NOT NULL DEFAULT 0,
    started_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    finished_at         REAL,
    last_error          TEXT,
    incomplete_reasons  TEXT,
    audit_complete      INTEGER NOT NULL DEFAULT 0,
    settings_json       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS sessions_status_idx ON sessions(status);

CREATE TABLE IF NOT EXISTS chunks (
    session_id          TEXT NOT NULL,
    chunk_no            INTEGER NOT NULL,
    started_at          REAL NOT NULL,
    ended_at            REAL NOT NULL,
    pages_in_chunk      INTEGER NOT NULL,
    p95_ms              INTEGER,
    err_rate            REAL,
    delay_used_ms       INTEGER NOT NULL,
    upstream_speed      REAL,
    note                TEXT,
    PRIMARY KEY (session_id, chunk_no),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    session_id          TEXT NOT NULL,
    kind                TEXT NOT NULL,
    path                TEXT NOT NULL,
    size_bytes          INTEGER,
    created_at          REAL NOT NULL,
    PRIMARY KEY (session_id, kind),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS events (
    session_id          TEXT NOT NULL,
    at                  REAL NOT NULL,
    kind                TEXT NOT NULL,
    detail              TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS events_session_idx ON events(session_id, at);
"""


def _connect():
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db():
    """Create schema if missing. Safe to call repeatedly."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = _connect()
        conn.executescript(SCHEMA)
        conn.close()


# ── Sessions ──────────────────────────────────────────────────────────────────

def create_session(url, total_max_pages, chunk_target_pages, politeness, settings):
    """Insert a new session in 'queued' state. Returns session_id."""
    sid = uuid.uuid4().hex[:16]
    now = time.time()
    initial_delay = 500  # ms — tuned by runner after first chunk
    with _LOCK:
        conn = _connect()
        conn.execute(
            """INSERT INTO sessions (
                id, url, status, total_max_pages, chunk_target_pages,
                politeness, current_delay_ms, started_at, updated_at,
                settings_json
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)""",
            (sid, url, total_max_pages, chunk_target_pages, politeness,
             initial_delay, now, now, json.dumps(settings)),
        )
        conn.execute("INSERT INTO events VALUES (?, ?, ?, ?)",
                     (sid, now, "created", json.dumps({"url": url})))
        conn.close()
    return sid


def get_session(session_id):
    """Return session row as dict, or None."""
    conn = _connect()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["settings"] = json.loads(d.pop("settings_json", "{}") or "{}")
    return d


def update_session(session_id, **fields):
    """Patch arbitrary session columns. updated_at always refreshed."""
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [session_id]
    with _LOCK:
        conn = _connect()
        conn.execute(f"UPDATE sessions SET {cols} WHERE id = ?", vals)
        conn.close()


def set_status(session_id, status, detail=None):
    """Transition + log event. Use this not update_session for status changes."""
    now = time.time()
    with _LOCK:
        conn = _connect()
        if status in ("done", "failed", "cancelled"):
            conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                (status, now, now, session_id),
            )
        else:
            conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, session_id),
            )
        conn.execute("INSERT INTO events VALUES (?, ?, ?, ?)",
                     (session_id, now, f"status_{status}", detail))
        conn.close()


def find_active_sessions():
    """Sessions that should be running but aren't terminal. For boot recovery."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM sessions WHERE status IN ('queued', 'crawling', 'throttled', 'paused') ORDER BY started_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_queued_sessions():
    """Sessions waiting for the runner to pick up."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM sessions WHERE status = 'queued' ORDER BY started_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Chunks ────────────────────────────────────────────────────────────────────

def record_chunk(session_id, chunk_no, started_at, pages_in_chunk,
                 p95_ms=None, err_rate=None, delay_used_ms=0,
                 upstream_speed=None, note=None):
    """Persist one polling-window's worth of metrics."""
    with _LOCK:
        conn = _connect()
        conn.execute(
            """INSERT OR REPLACE INTO chunks (
                session_id, chunk_no, started_at, ended_at, pages_in_chunk,
                p95_ms, err_rate, delay_used_ms, upstream_speed, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, chunk_no, started_at, time.time(), pages_in_chunk,
             p95_ms, err_rate, delay_used_ms, upstream_speed, note),
        )
        conn.close()


def last_chunks(session_id, n=5):
    """Most recent N chunks for trend / EWMA in the controller."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM chunks WHERE session_id = ? ORDER BY chunk_no DESC LIMIT ?",
        (session_id, n),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def chunk_count(session_id):
    conn = _connect()
    n = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    conn.close()
    return n


# ── Artifacts ─────────────────────────────────────────────────────────────────

def add_artifact(session_id, kind, path):
    """Register a produced file. Re-registering same kind updates the path."""
    p = Path(path)
    size = p.stat().st_size if p.exists() else None
    with _LOCK:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?, ?, ?)",
            (session_id, kind, str(p), size, time.time()),
        )
        conn.close()


def list_artifacts(session_id):
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM artifacts WHERE session_id = ? ORDER BY kind",
        (session_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Events (for UI / debugging) ───────────────────────────────────────────────

def log_event(session_id, kind, detail=None):
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail)
    with _LOCK:
        conn = _connect()
        conn.execute("INSERT INTO events VALUES (?, ?, ?, ?)",
                     (session_id, time.time(), kind, detail))
        conn.close()


def recent_events(session_id, n=20):
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM events WHERE session_id = ? ORDER BY at DESC LIMIT ?",
        (session_id, n),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
