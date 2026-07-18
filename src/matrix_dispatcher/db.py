"""SQLite persistence layer — security flag D: parameterized queries throughout.

Path constants are read via the :mod:`config` module (attribute access) so tests
can monkeypatch ``config.DB_PATH`` / ``config.POLL_TOKEN_PATH``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from . import config
from .config import log


def open_db() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    # Tighten permissions on the DB and its WAL/SHM siblings — defense-in-depth
    # against accidental world-readability (session_id values are usable with --resume).
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(config.DB_PATH) + suffix)
        if path.exists():
            os.chmod(path, 0o600)
    return db


def init_db(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
          thread_root_id TEXT PRIMARY KEY,
          room_id        TEXT NOT NULL,
          agent          TEXT NOT NULL,
          session_id     TEXT NOT NULL,
          created_at     INTEGER NOT NULL,
          last_used_at   INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_last_used ON sessions(last_used_at);

        -- Maps any dispatcher-posted event_id (ack, response chunks) back to a session,
        -- so that replies to those events resolve to the right session even when Element
        -- sends m.in_reply_to rather than rel_type=m.thread.
        CREATE TABLE IF NOT EXISTS event_aliases (
          event_id       TEXT PRIMARY KEY,
          thread_root_id TEXT NOT NULL REFERENCES sessions(thread_root_id)
        );

        CREATE TABLE IF NOT EXISTS poll_state (
          room_id    TEXT PRIMARY KEY,
          since      TEXT NOT NULL,
          updated_at INTEGER NOT NULL
        );

        -- HITL resume-on-approval (SMCP-38): a gated tool call rejected during a
        -- spawned/resumed turn is recorded here after the turn exits, correlated
        -- to its originating session. The reconcile loop resumes that session
        -- once agent-postgres shows the approval as 'approved'. thread_root_id
        -- stays in the dispatcher's own SQLite by design (no agent-postgres
        -- schema change). tool_name is carried for an informative resume nudge.
        CREATE TABLE IF NOT EXISTS pending_approvals (
          approval_id    TEXT PRIMARY KEY,
          thread_root_id TEXT NOT NULL,
          session_id     TEXT NOT NULL,
          room_id        TEXT NOT NULL,
          tool_name      TEXT NOT NULL DEFAULT '',
          created_at     INTEGER NOT NULL
        );
    """)
    db.commit()


def migrate_v1_tokens(db: sqlite3.Connection) -> None:
    """One-time migration: move the v1 poll-tokens.json into poll_state."""
    if not config.POLL_TOKEN_PATH.exists():
        return
    try:
        tokens = json.loads(config.POLL_TOKEN_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("action=migrate_skip err=%s", e)
        return
    since = tokens.get("global_since")
    if since:
        db.execute(
            "INSERT OR IGNORE INTO poll_state (room_id, since, updated_at) VALUES (?, ?, ?)",
            ("global", since, int(time.time())),
        )
        db.commit()
    config.POLL_TOKEN_PATH.unlink(missing_ok=True)
    log.info("action=migrate_poll_tokens since=%s", since)


def get_since(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT since FROM poll_state WHERE room_id = ?", ("global",)).fetchone()
    return row["since"] if row else None


def set_since(db: sqlite3.Connection, since: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO poll_state (room_id, since, updated_at) VALUES (?, ?, ?)",
        ("global", since, int(time.time())),
    )
    db.commit()


def get_session(db: sqlite3.Connection, thread_root_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM sessions WHERE thread_root_id = ?", (thread_root_id,)
    ).fetchone()


def get_session_by_event(db: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
    """Look up a session by any related event ID (thread root, ack, or response chunk)."""
    row = db.execute("SELECT * FROM sessions WHERE thread_root_id = ?", (event_id,)).fetchone()
    if row:
        return row
    alias = db.execute(
        "SELECT thread_root_id FROM event_aliases WHERE event_id = ?", (event_id,)
    ).fetchone()
    if alias:
        return db.execute(
            "SELECT * FROM sessions WHERE thread_root_id = ?", (alias["thread_root_id"],)
        ).fetchone()
    return None


def register_alias(db: sqlite3.Connection, event_id: str, thread_root_id: str) -> None:
    """Register a dispatcher-posted event_id so replies to it resolve to the right session."""
    if not event_id:
        return
    db.execute(
        "INSERT OR IGNORE INTO event_aliases (event_id, thread_root_id) VALUES (?, ?)",
        (event_id, thread_root_id),
    )
    db.commit()


def insert_session(
    db: sqlite3.Connection,
    thread_root_id: str,
    room_id: str,
    agent: str,
    session_id: str,
) -> None:
    now = int(time.time())
    db.execute(
        """INSERT INTO sessions
           (thread_root_id, room_id, agent, session_id, created_at, last_used_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (thread_root_id, room_id, agent, session_id, now, now),
    )
    db.commit()


def touch_session(db: sqlite3.Connection, thread_root_id: str) -> None:
    db.execute(
        "UPDATE sessions SET last_used_at = ? WHERE thread_root_id = ?",
        (int(time.time()), thread_root_id),
    )
    db.commit()


# ---------------------------------------------------------------------------
# HITL pending-approval store (SMCP-38) — local mapping approval → session.
# ---------------------------------------------------------------------------


def insert_pending_approval(
    db: sqlite3.Connection,
    approval_id: str,
    thread_root_id: str,
    session_id: str,
    room_id: str,
    tool_name: str,
) -> None:
    """Record a pending approval to reconcile. Idempotent on approval_id."""
    db.execute(
        """INSERT OR REPLACE INTO pending_approvals
           (approval_id, thread_root_id, session_id, room_id, tool_name, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (approval_id, thread_root_id, session_id, room_id, tool_name, int(time.time())),
    )
    db.commit()


def get_pending_approvals(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM pending_approvals").fetchall()


def delete_pending_approval(db: sqlite3.Connection, approval_id: str) -> None:
    db.execute("DELETE FROM pending_approvals WHERE approval_id = ?", (approval_id,))
    db.commit()


# ---------------------------------------------------------------------------
# Retention cleanup
# ---------------------------------------------------------------------------


def run_cleanup(db: sqlite3.Connection, retention_days: int) -> tuple[int, int]:
    cutoff = int(time.time()) - retention_days * 86400
    # event_aliases.thread_root_id REFERENCES sessions(thread_root_id) with FK
    # enforcement on and no ON DELETE CASCADE, so the referencing alias rows must be
    # removed BEFORE the sessions they point at — otherwise the session DELETE raises
    # sqlite3.IntegrityError (FOREIGN KEY constraint failed) and retention never runs.
    cur = db.execute(
        "DELETE FROM event_aliases WHERE thread_root_id IN "
        "(SELECT thread_root_id FROM sessions WHERE last_used_at < ?)",
        (cutoff,),
    )
    aliases_deleted = cur.rowcount
    cur = db.execute("DELETE FROM sessions WHERE last_used_at < ?", (cutoff,))
    sessions_deleted = cur.rowcount
    # Defensive sweep of any orphaned aliases (e.g. rows left by an older code path).
    cur = db.execute(
        "DELETE FROM event_aliases "
        "WHERE thread_root_id NOT IN (SELECT thread_root_id FROM sessions)"
    )
    aliases_deleted += cur.rowcount
    db.commit()
    log.info(
        "action=cleanup sessions_deleted=%d aliases_deleted=%d retention_days=%d",
        sessions_deleted,
        aliases_deleted,
        retention_days,
    )
    return sessions_deleted, aliases_deleted
