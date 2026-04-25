"""Matrix dispatcher — spawn or resume claude -p sessions from Matrix room messages.

v4: Hardening — async subprocess (asyncio.create_subprocess_exec, no event-loop
blocking), per-room concurrency lock, per-room rate limit on spawns, /cancel
command (SIGTERM the active subprocess), startup notification.

v3: Bang-prefix commands (!sessions, !recap, !mirror, !help, !cancel) + nightly cleanup.
v2: SQLite + thread-based resume.
v1: Spawn-only loop.

Security flags addressed:
  B: Dispatcher credentials loaded from DISPATCHER_* env vars (asserted at startup).
  C: Subprocesses receive a minimal allowlist env, not os.environ.
  D: SQLite queries use parameterized statements throughout (no f-string SQL).
  E: Log policy: event IDs, room IDs, session IDs, action, exit code only — no message body.
  F: requirements.txt pins exact versions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import yaml
from nio import AsyncClient, RoomMessageText, SyncResponse

# ---------------------------------------------------------------------------
# Runtime state — per-room locks, active processes, rate-limit timestamps.
# Process-local; lost on restart (acceptable — orphaned procs run to completion).
# ---------------------------------------------------------------------------

_room_locks: dict[str, asyncio.Lock] = {}
_active_processes: dict[str, asyncio.subprocess.Process] = {}
_last_spawn_at: dict[str, float] = {}

SUBPROCESS_TIMEOUT_SECONDS = 300
RATE_LIMIT_SECONDS = 10


def _room_lock(room_id: str) -> asyncio.Lock:
    if room_id not in _room_locks:
        _room_locks[room_id] = asyncio.Lock()
    return _room_locks[room_id]

# ---------------------------------------------------------------------------
# Logging — structured, no message body content
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("dispatcher")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.yml"
# v1 poll-token JSON — kept only for one-time migration to SQLite
POLL_TOKEN_PATH = Path.home() / ".claude" / "data" / "matrix-dispatcher" / "poll-tokens.json"
DB_PATH = Path.home() / ".claude" / "data" / "matrix-dispatcher" / "sessions.db"


def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# SQLite DB layer — security flag D: parameterized queries throughout
# ---------------------------------------------------------------------------

def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    # Tighten permissions on the DB and its WAL/SHM siblings — defense-in-depth
    # against accidental world-readability (session_id values are usable with --resume).
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(DB_PATH) + suffix)
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
    """)
    db.commit()


def migrate_v1_tokens(db: sqlite3.Connection) -> None:
    """One-time migration: move the v1 poll-tokens.json into poll_state."""
    if not POLL_TOKEN_PATH.exists():
        return
    try:
        tokens = json.loads(POLL_TOKEN_PATH.read_text())
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
    POLL_TOKEN_PATH.unlink(missing_ok=True)
    log.info("action=migrate_poll_tokens since=%s", since)


def get_since(db: sqlite3.Connection) -> str | None:
    row = db.execute(
        "SELECT since FROM poll_state WHERE room_id = ?", ("global",)
    ).fetchone()
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
    row = db.execute(
        "SELECT * FROM sessions WHERE thread_root_id = ?", (event_id,)
    ).fetchone()
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
# Dispatcher credentials — security flag B: assert non-empty at startup
# ---------------------------------------------------------------------------

def get_dispatcher_credentials() -> tuple[str, str, str]:
    homeserver = os.environ.get("DISPATCHER_HOMESERVER", "").strip()
    user_id = os.environ.get("DISPATCHER_USER_ID", "").strip()
    token = os.environ.get("DISPATCHER_ACCESS_TOKEN", "").strip()

    missing = [k for k, v in [
        ("DISPATCHER_HOMESERVER", homeserver),
        ("DISPATCHER_USER_ID", user_id),
        ("DISPATCHER_ACCESS_TOKEN", token),
    ] if not v]

    if missing:
        log.error("Missing required env vars at startup: %s", ", ".join(missing))
        sys.exit(1)

    return homeserver, user_id, token


# ---------------------------------------------------------------------------
# Matrix helpers
# ---------------------------------------------------------------------------

async def post_message(
    client: AsyncClient,
    room_id: str,
    body: str,
    reply_to: str | None = None,
) -> str:
    content: dict = {"msgtype": "m.text", "body": body}
    if reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}

    resp = await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content=content,
    )
    return getattr(resp, "event_id", "")


def split_on_paragraphs(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    current = ""

    for para in paragraphs:
        candidate = (current + "\n\n" + para).lstrip("\n") if current else para
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(para) > max_len:
                for i in range(0, len(para), max_len):
                    chunks.append(para[i:i + max_len])
                current = ""
            else:
                current = para

    if current:
        chunks.append(current)

    return chunks or [text[:max_len]]


# ---------------------------------------------------------------------------
# Spawn / Resume — security flag C: minimal env allowlist
# ---------------------------------------------------------------------------

def _minimal_env(agent_name: str) -> dict[str, str]:
    env = {
        "HOME": os.environ["HOME"],
        "PATH": os.environ["PATH"],
        "AGENT_ID": agent_name,
        "AGENT_TYPE": agent_name,
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TERM": os.environ.get("TERM", "xterm"),
        "USER": os.environ.get("USER", "ted"),
    }
    # Explicit allowlist — never glob CLAUDE_*, which would leak any
    # CLAUDE_API_KEY / CLAUDE_CODE_OAUTH_TOKEN into agent subprocesses.
    for key in ("CLAUDE_CONFIG_DIR",):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


async def _run_claude(
    args: list[str],
    project_dir: str,
    agent_name: str,
    room_id: str,
) -> tuple[int, str]:
    """Run claude with the given args, registering the process for /cancel.

    Returns (exit_code, output). Raises asyncio.TimeoutError on timeout.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=project_dir,
        env=_minimal_env(agent_name),
    )
    _active_processes[room_id] = proc
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    finally:
        _active_processes.pop(room_id, None)

    rc = proc.returncode if proc.returncode is not None else -1
    stdout = stdout_b.decode(errors="replace").strip()
    stderr = stderr_b.decode(errors="replace").strip()
    output = stdout if rc == 0 else stderr[:500]
    return rc, output


async def spawn_claude(
    session_id: str,
    prompt: str,
    project_dir: str,
    agent_name: str,
    room_id: str,
) -> tuple[int, str]:
    return await _run_claude(
        ["claude", "-p", "--session-id", session_id, prompt],
        project_dir, agent_name, room_id,
    )


async def resume_claude(
    session_id: str,
    message: str,
    project_dir: str,
    agent_name: str,
    room_id: str,
) -> tuple[int, str]:
    return await _run_claude(
        ["claude", "-p", "--resume", session_id, message],
        project_dir, agent_name, room_id,
    )


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------

def extract_thread_root(event: RoomMessageText) -> str | None:
    """Return the thread-root event ID for a reply, or None for a room-root message.

    Handles both proper Matrix threads (rel_type=m.thread, event_id=thread_root)
    and simple in-reply-to chains (falls back to the replied-to event ID).
    Rejects malformed event metadata (non-string event_id) rather than letting
    bad values flow into SQLite parameter binding.
    """
    source = getattr(event, "source", {})
    content = source.get("content", {}) if isinstance(source, dict) else {}
    relates_to = content.get("m.relates_to")
    if not isinstance(relates_to, dict):
        return None
    candidate: object | None = None
    if relates_to.get("rel_type") == "m.thread":
        candidate = relates_to.get("event_id")
    else:
        in_reply_to = relates_to.get("m.in_reply_to")
        if isinstance(in_reply_to, dict):
            candidate = in_reply_to.get("event_id")
    if candidate is None:
        return None
    if not isinstance(candidate, str):
        log.warning(
            "action=malformed_relates_to event_id=%s type=%s",
            getattr(event, "event_id", "?"), type(candidate).__name__,
        )
        return None
    return candidate


# ---------------------------------------------------------------------------
# JSONL transcript helpers — read claude -p session transcripts for
# /sessions, /recap, /mirror commands.
# ---------------------------------------------------------------------------

def project_jsonl_dir(project_dir: str) -> Path:
    """Convert a project directory path to its claude-code JSONL transcript dir."""
    encoded = project_dir.replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded


def _extract_text(content: object) -> str:
    """Best-effort text extraction from a claude transcript content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def read_first_user_message(session_id: str, project_dir: str, max_len: int = 80) -> str:
    """Return a one-line summary from the first user turn in the transcript."""
    jsonl = project_jsonl_dir(project_dir) / f"{session_id}.jsonl"
    if not jsonl.exists():
        return "(transcript missing)"
    try:
        with jsonl.open() as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                msg = obj.get("message", {})
                text = _extract_text(msg.get("content", "")).strip()
                # Strip dispatcher's injected prefix
                if text.startswith("[Invoked via Matrix"):
                    text = text.split("\n\n", 1)[-1]
                first_line = text.split("\n", 1)[0].strip()
                if not first_line:
                    continue
                return first_line[:max_len] + ("…" if len(first_line) > max_len else "")
    except OSError as e:
        log.warning("action=transcript_read_error session=%s err=%s", session_id, e)
        return "(transcript error)"
    return "(no user message)"


def read_last_n_turns(session_id: str, project_dir: str, n: int) -> str:
    """Return the last n user+assistant turns formatted as a Matrix-friendly recap."""
    jsonl = project_jsonl_dir(project_dir) / f"{session_id}.jsonl"
    if not jsonl.exists():
        return ""
    turns: list[tuple[str, str]] = []
    try:
        with jsonl.open() as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = obj.get("type")
                if role not in ("user", "assistant"):
                    continue
                msg = obj.get("message", {})
                text = _extract_text(msg.get("content", "")).strip()
                if not text:
                    continue
                if role == "user" and text.startswith("[Invoked via Matrix"):
                    text = text.split("\n\n", 1)[-1]
                turns.append((role, text))
    except OSError as e:
        log.warning("action=transcript_read_error session=%s err=%s", session_id, e)
        return ""
    pairs = turns[-(2 * n):]
    return "\n\n".join(f"**{role}:**\n{text}" for role, text in pairs)


def find_unmirrored_session_id(db: sqlite3.Connection, project_dir: str, room_id: str) -> str | None:
    """Find the most recent JSONL session in project_dir not yet tracked for this room."""
    jsonl_dir = project_jsonl_dir(project_dir)
    if not jsonl_dir.exists():
        return None
    rows = db.execute(
        "SELECT session_id FROM sessions WHERE room_id = ?", (room_id,)
    ).fetchall()
    known = {r["session_id"] for r in rows}
    candidates: list[Path] = []
    for path in jsonl_dir.glob("*.jsonl"):
        if path.stem in known:
            continue
        candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime).stem


async def _post_response(
    client: AsyncClient,
    room_id: str,
    output: str,
    exit_code: int,
    session_id: str,
    mention_user: str,
    max_message_length: int,
    reply_target: str,
    action: str,
    db: sqlite3.Connection,
    thread_root_id: str,
) -> None:
    short_id = session_id[:8]
    if exit_code != 0:
        event_id = await post_message(
            client, room_id,
            f"{mention_user} Session {short_id} {action} error:\n\n{output}",
            reply_to=reply_target,
        )
        register_alias(db, event_id, thread_root_id)
        return
    if not output:
        output = "(no output)"
    chunks = split_on_paragraphs(output, max_message_length)
    for i, chunk in enumerate(chunks):
        text = f"{mention_user} {chunk}" if i == 0 else chunk
        event_id = await post_message(client, room_id, text, reply_to=reply_target)
        register_alias(db, event_id, thread_root_id)


HELP_TEXT = (
    "Dispatcher commands (! prefix — Element intercepts /-commands client-side):\n"
    "  !sessions       — list recent sessions (reply to one to resume)\n"
    "  !recap [N]      — show last N turns of most recent session (default: 5)\n"
    "  !mirror         — link most recent CloudCLI session to this room for resume\n"
    "  !cancel         — SIGTERM the active session in this room\n"
    "  !help           — this message"
)


async def handle_cancel_command(
    client: AsyncClient, room_id: str, event: RoomMessageText, mention_user: str,
) -> None:
    proc = _active_processes.get(room_id)
    if proc is None:
        log.info("action=cmd_cancel_noop room=%s event_id=%s", room_id, event.event_id)
        await post_message(
            client, room_id,
            f"{mention_user} No active session in this room.",
            reply_to=event.event_id,
        )
        return
    pid = proc.pid
    log.info("action=cmd_cancel room=%s event_id=%s pid=%s", room_id, event.event_id, pid)
    try:
        proc.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        pass
    await post_message(
        client, room_id,
        f"{mention_user} Sent SIGTERM to active session (pid {pid}).",
        reply_to=event.event_id,
    )


async def handle_help_command(
    client: AsyncClient, room_id: str, event: RoomMessageText, mention_user: str,
) -> None:
    log.info("action=cmd_help room=%s event_id=%s", room_id, event.event_id)
    await post_message(
        client, room_id, f"{mention_user}\n\n{HELP_TEXT}", reply_to=event.event_id,
    )


async def handle_sessions_command(
    client: AsyncClient, room_id: str, event: RoomMessageText, mention_user: str,
    agent_name: str, project_dir: str, db: sqlite3.Connection,
) -> None:
    log.info("action=cmd_sessions room=%s event_id=%s", room_id, event.event_id)
    rows = db.execute(
        "SELECT * FROM sessions WHERE room_id = ? ORDER BY last_used_at DESC LIMIT 10",
        (room_id,),
    ).fetchall()
    if not rows:
        await post_message(
            client, room_id, f"{mention_user} No sessions yet in this room.",
            reply_to=event.event_id,
        )
        return
    header_event_id = await post_message(
        client, room_id,
        f"{mention_user} Recent sessions in #{agent_name} (reply to one to resume):",
        reply_to=event.event_id,
    )
    for i, row in enumerate(rows, 1):
        summary = read_first_user_message(row["session_id"], project_dir)
        text = f"{i}. ({row['session_id'][:8]}) {summary}"
        list_event_id = await post_message(
            client, room_id, text, reply_to=header_event_id,
        )
        # Reply to this list-item event resolves to the session via event_aliases
        register_alias(db, list_event_id, row["thread_root_id"])


def _parse_recap_n(arg: str, default: int = 5) -> int:
    try:
        n = int(arg.strip())
    except ValueError:
        return default
    return max(1, min(n, 20))


async def handle_recap_command(
    client: AsyncClient, room_id: str, event: RoomMessageText, mention_user: str,
    project_dir: str, db: sqlite3.Connection, max_message_length: int, arg: str,
) -> None:
    n = _parse_recap_n(arg)
    log.info("action=cmd_recap room=%s event_id=%s n=%d", room_id, event.event_id, n)
    row = db.execute(
        "SELECT * FROM sessions WHERE room_id = ? ORDER BY last_used_at DESC LIMIT 1",
        (room_id,),
    ).fetchone()
    if row is None:
        await post_message(
            client, room_id, f"{mention_user} No prior sessions to recap.",
            reply_to=event.event_id,
        )
        return
    body = read_last_n_turns(row["session_id"], project_dir, n)
    if not body:
        await post_message(
            client, room_id,
            f"{mention_user} Session {row['session_id'][:8]} has no readable turns.",
            reply_to=event.event_id,
        )
        return
    header = f"{mention_user} Recap of session {row['session_id'][:8]} (last {n} turns):\n\n"
    chunks = split_on_paragraphs(header + body, max_message_length)
    for chunk in chunks:
        await post_message(client, room_id, chunk, reply_to=event.event_id)


async def handle_mirror_command(
    client: AsyncClient, room_id: str, event: RoomMessageText, mention_user: str,
    agent_name: str, project_dir: str, db: sqlite3.Connection,
) -> None:
    log.info("action=cmd_mirror room=%s event_id=%s", room_id, event.event_id)
    session_id = find_unmirrored_session_id(db, project_dir, room_id)
    if session_id is None:
        await post_message(
            client, room_id,
            f"{mention_user} No unmirrored CloudCLI sessions found for #{agent_name}.",
            reply_to=event.event_id,
        )
        return
    insert_session(db, event.event_id, room_id, agent_name, session_id)
    response_event_id = await post_message(
        client, room_id,
        f"{mention_user} Linked session {session_id[:8]} to this thread. "
        f"Reply here to resume.",
        reply_to=event.event_id,
    )
    register_alias(db, response_event_id, event.event_id)


# ---------------------------------------------------------------------------
# Retention cleanup
# ---------------------------------------------------------------------------

def run_cleanup(db: sqlite3.Connection, retention_days: int) -> tuple[int, int]:
    cutoff = int(time.time()) - retention_days * 86400
    cur = db.execute("DELETE FROM sessions WHERE last_used_at < ?", (cutoff,))
    sessions_deleted = cur.rowcount
    cur = db.execute(
        "DELETE FROM event_aliases "
        "WHERE thread_root_id NOT IN (SELECT thread_root_id FROM sessions)"
    )
    aliases_deleted = cur.rowcount
    db.commit()
    log.info(
        "action=cleanup sessions_deleted=%d aliases_deleted=%d retention_days=%d",
        sessions_deleted, aliases_deleted, retention_days,
    )
    return sessions_deleted, aliases_deleted


async def cleanup_loop(db: sqlite3.Connection, retention_days: int) -> None:
    while True:
        try:
            run_cleanup(db, retention_days)
        except Exception:
            log.exception("action=cleanup_error")
        await asyncio.sleep(86400)


async def handle_event(
    client: AsyncClient,
    room_id: str,
    event: RoomMessageText,
    trusted_sender: str,
    mention_user: str,
    max_message_length: int,
    agent_name: str,
    project_dir: str,
    db: sqlite3.Connection,
) -> None:
    # Security flag B: sender gate — first check, silently discard non-trusted
    if event.sender != trusted_sender:
        return

    user_message = event.body.strip()
    thread_root = extract_thread_root(event)

    # Commands intercepted on room-root messages only — thread replies starting
    # with "!" pass through to the resumed session as ordinary text. The "!"
    # prefix is used because Element intercepts "/" client-side as IRC-style
    # commands (e.g. /me, /join, /help) and never sends them to Matrix.
    if thread_root is None and user_message.startswith("!"):
        parts = user_message.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if cmd == "!help":
            await handle_help_command(client, room_id, event, mention_user)
        elif cmd == "!sessions":
            await handle_sessions_command(
                client, room_id, event, mention_user, agent_name, project_dir, db,
            )
        elif cmd == "!recap":
            await handle_recap_command(
                client, room_id, event, mention_user, project_dir, db,
                max_message_length, arg,
            )
        elif cmd == "!mirror":
            await handle_mirror_command(
                client, room_id, event, mention_user, agent_name, project_dir, db,
            )
        elif cmd == "!cancel":
            await handle_cancel_command(client, room_id, event, mention_user)
        else:
            log.info(
                "action=command_unknown room=%s event_id=%s cmd=%s",
                room_id, event.event_id, cmd[:20],
            )
            await post_message(
                client, room_id,
                f"{mention_user} Unknown command `{cmd}`. Send `!help` for the list.",
                reply_to=event.event_id,
            )
        return

    # Thread reply → attempt resume (per-room lock serializes with other work)
    if thread_root is not None:
        row = get_session_by_event(db, thread_root)
        if row is not None:
            session_id = row["session_id"]
            thread_root_id = row["thread_root_id"]
            short_id = session_id[:8]
            log.info(
                "action=resume_start room=%s event_id=%s agent=%s session=%s",
                room_id, event.event_id, agent_name, session_id,
            )
            ack_event_id = await post_message(
                client, room_id, f"Resuming... (session {short_id})",
                reply_to=event.event_id,
            )
            register_alias(db, ack_event_id, thread_root_id)
            async with _room_lock(room_id):
                try:
                    exit_code, output = await resume_claude(
                        session_id, user_message, project_dir, agent_name, room_id,
                    )
                except asyncio.TimeoutError:
                    log.error("action=resume_timeout room=%s session=%s", room_id, session_id)
                    await post_message(
                        client, room_id,
                        f"{mention_user} Session {short_id} timed out after 300s.",
                        reply_to=ack_event_id or event.event_id,
                    )
                    return
            touch_session(db, thread_root_id)
            log.info(
                "action=resume_complete room=%s session=%s exit_code=%d",
                room_id, session_id, exit_code,
            )
            await _post_response(
                client, room_id, output, exit_code, session_id,
                mention_user, max_message_length,
                reply_target=ack_event_id or event.event_id,
                action="resume",
                db=db,
                thread_root_id=thread_root_id,
            )
            return
        # Orphaned reply (session unknown — maybe started before dispatcher)
        log.info(
            "action=orphaned_reply room=%s event_id=%s thread_root=%s — spawning new",
            room_id, event.event_id, thread_root,
        )

    # Room-root (or orphaned reply) → spawn new session.
    # Rate-limit applies to spawns only — runaway-loop guard, not user friction.
    now = time.time()
    last = _last_spawn_at.get(room_id, 0.0)
    if now - last < RATE_LIMIT_SECONDS:
        remaining = int(RATE_LIMIT_SECONDS - (now - last))
        log.info(
            "action=rate_limited room=%s event_id=%s remaining=%d",
            room_id, event.event_id, remaining,
        )
        await post_message(
            client, room_id,
            f"{mention_user} Rate-limited; try again in {remaining}s.",
            reply_to=event.event_id,
        )
        return
    _last_spawn_at[room_id] = now

    session_id = str(uuid.uuid4())
    short_id = session_id[:8]
    log.info(
        "action=spawn_start room=%s event_id=%s agent=%s session=%s",
        room_id, event.event_id, agent_name, session_id,
    )
    ack_event_id = await post_message(
        client, room_id, f"Working... (session {short_id})", reply_to=event.event_id,
    )
    # Store session before spawning so thread replies can resume it even if spawn errors
    insert_session(db, event.event_id, room_id, agent_name, session_id)
    register_alias(db, ack_event_id, event.event_id)

    # The dispatcher owns all Matrix posting. Agents must NOT call any Matrix
    # or mcp__matrix__ tools — doing so causes double-posts and permission blocks.
    prompt = (
        f"[Invoked via Matrix room #{agent_name} by @ted. "
        f"Output your response as plain text only. "
        f"Do NOT use mcp__matrix__ tools or any Matrix MCP — "
        f"the dispatcher will post your stdout to the room automatically.]\n\n"
        f"{user_message}"
    )
    async with _room_lock(room_id):
        try:
            exit_code, output = await spawn_claude(
                session_id, prompt, project_dir, agent_name, room_id,
            )
        except asyncio.TimeoutError:
            log.error("action=spawn_timeout room=%s session=%s", room_id, session_id)
            await post_message(
                client, room_id,
                f"{mention_user} Session {short_id} timed out after 300s.",
                reply_to=ack_event_id or event.event_id,
            )
            return
    log.info(
        "action=spawn_complete room=%s session=%s exit_code=%d",
        room_id, session_id, exit_code,
    )
    await _post_response(
        client, room_id, output, exit_code, session_id,
        mention_user, max_message_length,
        reply_target=ack_event_id or event.event_id,
        action="spawn",
        db=db,
        thread_root_id=event.event_id,
    )


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

async def poll_loop(client: AsyncClient, config: dict, db: sqlite3.Connection) -> None:
    trusted_sender: str = config.get("trusted_sender", "@ted:claudebox.me")
    mention_user: str = config.get("mention_user", "@ted:claudebox.me")
    poll_interval: int = config.get("poll_interval_seconds", 5)
    max_message_length: int = config.get("max_message_length", 4000)

    agents: dict = config.get("agents", {})
    room_to_agent: dict[str, dict] = {}
    for name, agent_cfg in agents.items():
        room_id = agent_cfg["room_id"]
        room_to_agent[room_id] = {"name": name, **agent_cfg}

    since: str | None = get_since(db)

    # Cold-start seeding: with since=None, /sync returns recent timeline events.
    # Run one priming sync to capture next_batch without processing any events,
    # preventing re-spawns for messages already answered before this run.
    if since is None:
        seed = await client.sync(timeout=0, since=None, full_state=False)
        if isinstance(seed, SyncResponse):
            since = seed.next_batch
            set_since(db, since)
            log.info("action=poll_seed since=%s", since)
        else:
            log.warning("action=poll_seed_error response=%s", type(seed).__name__)

    log.info("action=poll_start agents=%s since=%s", list(agents.keys()), since)

    while True:
        try:
            resp = await client.sync(timeout=0, since=since, full_state=False)

            if not isinstance(resp, SyncResponse):
                log.warning("action=sync_error response=%s", type(resp).__name__)
                await asyncio.sleep(poll_interval)
                continue

            since = resp.next_batch
            set_since(db, since)

            for room_id, room_info in resp.rooms.join.items():
                if room_id not in room_to_agent:
                    continue

                agent_cfg = room_to_agent[room_id]
                agent_name = agent_cfg["name"]
                project_dir = agent_cfg["project_dir"]

                for event in room_info.timeline.events:
                    if not isinstance(event, RoomMessageText):
                        continue
                    await handle_event(
                        client=client,
                        room_id=room_id,
                        event=event,
                        trusted_sender=trusted_sender,
                        mention_user=mention_user,
                        max_message_length=max_message_length,
                        agent_name=agent_name,
                        project_dir=project_dir,
                        db=db,
                    )

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("action=poll_error")

        await asyncio.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    homeserver, user_id, token = get_dispatcher_credentials()
    config = load_config()

    db = open_db()
    init_db(db)
    migrate_v1_tokens(db)

    retention_days = int(config.get("session_retention_days", 30))

    client = AsyncClient(homeserver, user_id)
    client.access_token = token
    client.user_id = user_id

    log.info("action=startup user_id=%s homeserver=%s", user_id, homeserver)

    # Startup notification — post to one of the configured agent rooms
    notify_agent = config.get("startup_notification_agent", "claudebox")
    agents_cfg = config.get("agents", {})
    notify_room = (agents_cfg.get(notify_agent) or {}).get("room_id")
    if notify_room:
        try:
            await post_message(
                client, notify_room,
                f"matrix-dispatcher started — agents: {list(agents_cfg.keys())}",
            )
        except Exception:
            log.exception("action=startup_notify_error")

    cleanup_task = asyncio.create_task(cleanup_loop(db, retention_days))
    try:
        await poll_loop(client, config, db)
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except (asyncio.CancelledError, Exception):
            pass
        db.close()
        await client.close()


def cli_cleanup() -> int:
    """Run retention cleanup once and exit (for manual / cron invocation)."""
    config = load_config()
    db = open_db()
    init_db(db)
    retention_days = int(config.get("session_retention_days", 30))
    sessions_deleted, aliases_deleted = run_cleanup(db, retention_days)
    db.close()
    print(f"sessions_deleted={sessions_deleted} aliases_deleted={aliases_deleted}")
    return 0


if __name__ == "__main__":
    if "--cleanup" in sys.argv:
        sys.exit(cli_cleanup())
    asyncio.run(main())
