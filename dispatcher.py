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
import contextlib
import json
import logging
import os
import signal
import sqlite3
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml
from nio import AsyncClient, RoomMessageText, SyncResponse

from agent_registry import RegistryClient, get_registry

# ---------------------------------------------------------------------------
# Runtime state — per-room locks, active processes, rate-limit timestamps.
# Process-local; lost on restart (acceptable — orphaned procs run to completion).
# ---------------------------------------------------------------------------

_room_locks: dict[str, asyncio.Lock] = {}
_active_processes: dict[str, asyncio.subprocess.Process] = {}
_last_spawn_at: dict[str, float] = {}
# Outstanding handler tasks — kept as strong refs (set discards on done callback)
# so the GC doesn't collect mid-flight handlers, and so shutdown can await them.
_handlers: set[asyncio.Task] = set()

SUBPROCESS_TIMEOUT_SECONDS = 300
RATE_LIMIT_SECONDS = 10
# Brief retry window for !cancel when the spawn is mid-`create_subprocess_exec`
# (proc not yet registered in _active_processes).
CANCEL_REGISTRATION_WAIT_SECONDS = 1.0
CANCEL_POLL_INTERVAL_SECONDS = 0.05

# HITL resume-on-approval (SMCP-38). The reconcile loop polls agent-postgres for
# approval-state transitions on locally-tracked pending approvals and resumes the
# originating session once an operator approval lands. Interval must be « the
# scoped-mcp pre-approval token TTL (300s) so the resumed retry lands well inside
# the token's life. Local pending rows are expired after 2x the HITL timeout
# default (300s) so an approval that is never actioned can't accumulate.
RECONCILE_INTERVAL_SECONDS = 10
PENDING_APPROVAL_EXPIRY_SECONDS = 600


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
# Dispatcher credentials — security flag B: assert non-empty at startup
# ---------------------------------------------------------------------------


def get_dispatcher_credentials() -> tuple[str, str, str]:
    homeserver = os.environ.get("DISPATCHER_HOMESERVER", "").strip()
    user_id = os.environ.get("DISPATCHER_USER_ID", "").strip()
    token = os.environ.get("DISPATCHER_ACCESS_TOKEN", "").strip()

    missing = [
        k
        for k, v in [
            ("DISPATCHER_HOMESERVER", homeserver),
            ("DISPATCHER_USER_ID", user_id),
            ("DISPATCHER_ACCESS_TOKEN", token),
        ]
        if not v
    ]

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


async def _get_event_sender(client: AsyncClient, room_id: str, event_id: str) -> str | None:
    """Return the sender of an event, or None if it can't be fetched.

    Used only on the orphaned-reply path to distinguish our own expired
    threads from foreign-bot messages. Fail-closed: any error -> None ->
    treated as foreign (silent ignore), never a spawn.

    Note: nio's RoomGetEventResponse has a .event attribute; RoomGetEventError
    does not -- the getattr guard handles the error-response case without an
    isinstance import.
    """
    try:
        resp = await client.room_get_event(room_id, event_id)
    except Exception:
        log.exception("action=get_event_error room=%s event_id=%s", room_id, event_id)
        return None
    fetched = getattr(resp, "event", None)
    return getattr(fetched, "sender", None)


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
                    chunks.append(para[i : i + max_len])
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
    timeout: int = SUBPROCESS_TIMEOUT_SECONDS,
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
            proc.communicate(),
            timeout=timeout,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        # Drain pipe transports — wait_for cancelled communicate() mid-read,
        # leaving stdout/stderr file descriptors open. communicate() is
        # idempotent post-exit and closes the transports.
        with contextlib.suppress(Exception):
            await proc.communicate()
        raise
    finally:
        _active_processes.pop(room_id, None)

    rc = proc.returncode if proc.returncode is not None else -1
    stdout = stdout_b.decode(errors="replace").strip()
    stderr = stderr_b.decode(errors="replace").strip()
    # SECURITY[accepted]: on nonzero exit the (bounded, 500-char) stderr is surfaced to
    # the room. The destination room is trusted-sender-gated (dispatcher.py:1203) and
    # operator-owned, so the operator only ever sees their own agent's stderr — no
    # cross-trust-boundary disclosure. Accepted in the 2026-07-18 Showcase audit (Info
    # finding); see host-forge/build-reports accepted-risks.md.
    output = stdout if rc == 0 else stderr[:500]
    return rc, output


async def spawn_claude(
    session_id: str,
    prompt: str,
    project_dir: str,
    agent_name: str,
    room_id: str,
    timeout: int = SUBPROCESS_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    return await _run_claude(
        ["claude", "-p", "--session-id", session_id, prompt],
        project_dir,
        agent_name,
        room_id,
        timeout=timeout,
    )


async def resume_claude(
    session_id: str,
    message: str,
    project_dir: str,
    agent_name: str,
    room_id: str,
    timeout: int = SUBPROCESS_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    return await _run_claude(
        ["claude", "-p", "--resume", session_id, message],
        project_dir,
        agent_name,
        room_id,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# HITL resume-on-approval (SMCP-38) — session registry write + pending detect.
# Both helpers are no-ops when the registry is disabled/absent (fail-open): a
# down agent-postgres must never affect the spawn/resume path.
# ---------------------------------------------------------------------------


async def _register_session(
    registry: RegistryClient | None,
    session_id: str,
    agent_name: str,
    room_id: str,
    project_dir: str,
    scoped_mcp_url: str,
) -> None:
    """Best-effort upsert of the agent-postgres ``sessions`` row for this turn.

    Writing this before the turn makes ``hitl_approvals.session_id`` FK-safe at
    :meth:`RegistryClient.link_session` time. ``agent_name`` is the ``agent_id``
    in agent-postgres — the dispatcher passes it to the subprocess as ``AGENT_ID``,
    so it equals the ``{agent_id}`` prefix of any ``approval_id`` scoped-mcp mints.
    """
    if registry is None or not registry.enabled:
        return
    await registry.upsert_session(
        session_id=session_id,
        agent_id=agent_name,
        transport="matrix",
        room_id=room_id,
        project_dir=project_dir,
        scoped_mcp_url=scoped_mcp_url or None,
    )


async def _record_pending_if_gated(
    db: sqlite3.Connection,
    registry: RegistryClient | None,
    agent_name: str,
    room_id: str,
    session_id: str,
    thread_root_id: str,
    turn_start: datetime,
) -> None:
    """After a turn exits, correlate any still-pending HITL approval to it.

    Only approvals still ``pending`` at this point are tracked — an approval the
    agent self-resolved in-turn is excluded by :meth:`find_pending_approval`, so
    a later reconcile can never re-execute a tool call that already ran (the
    mandatory duplicate-execution guard).
    """
    if registry is None or not registry.enabled:
        return
    pending = await registry.find_pending_approval(agent_name, turn_start)
    if pending is None:
        return
    approval_id = pending["approval_id"]
    tool_name = pending.get("tool_name") or ""
    insert_pending_approval(db, approval_id, thread_root_id, session_id, room_id, tool_name)
    await registry.link_session(approval_id, session_id)
    log.info(
        "action=hitl_pending_detected room=%s session=%s approval=%s tool=%s",
        room_id,
        session_id,
        approval_id,
        tool_name,
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
            getattr(event, "event_id", "?"),
            type(candidate).__name__,
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
    pairs = turns[-(2 * n) :]
    return "\n\n".join(f"**{role}:**\n{text}" for role, text in pairs)


def find_unmirrored_session_id(
    db: sqlite3.Connection, project_dir: str, room_id: str
) -> str | None:
    """Find the most recent JSONL session in project_dir not yet tracked for this room.

    Only files whose stem parses as a UUID are considered — the value flows into
    `claude -p --resume <session_id>` as argv, so anything starting with `-` or
    otherwise unstructured must be rejected before reaching the subprocess.
    """
    jsonl_dir = project_jsonl_dir(project_dir)
    if not jsonl_dir.exists():
        return None
    rows = db.execute("SELECT session_id FROM sessions WHERE room_id = ?", (room_id,)).fetchall()
    known = {r["session_id"] for r in rows}
    candidates: list[Path] = []
    for path in jsonl_dir.glob("*.jsonl"):
        if path.stem in known:
            continue
        try:
            uuid.UUID(path.stem)
        except ValueError:
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
            client,
            room_id,
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
    client: AsyncClient,
    room_id: str,
    event: RoomMessageText,
    mention_user: str,
) -> None:
    proc = _active_processes.get(room_id)
    # If the room lock is held but no proc is registered yet, the spawn is
    # mid-`create_subprocess_exec`. Wait briefly for it to land.
    if proc is None:
        lock = _room_locks.get(room_id)
        if lock is not None and lock.locked():
            elapsed = 0.0
            while elapsed < CANCEL_REGISTRATION_WAIT_SECONDS:
                await asyncio.sleep(CANCEL_POLL_INTERVAL_SECONDS)
                elapsed += CANCEL_POLL_INTERVAL_SECONDS
                proc = _active_processes.get(room_id)
                if proc is not None:
                    break
    if proc is None:
        log.info("action=cmd_cancel_noop room=%s event_id=%s", room_id, event.event_id)
        await post_message(
            client,
            room_id,
            f"{mention_user} No active session in this room.",
            reply_to=event.event_id,
        )
        return
    pid = proc.pid
    log.info("action=cmd_cancel room=%s event_id=%s pid=%s", room_id, event.event_id, pid)
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(signal.SIGTERM)
    await post_message(
        client,
        room_id,
        f"{mention_user} Sent SIGTERM to active session (pid {pid}).",
        reply_to=event.event_id,
    )


async def handle_help_command(
    client: AsyncClient,
    room_id: str,
    event: RoomMessageText,
    mention_user: str,
) -> None:
    log.info("action=cmd_help room=%s event_id=%s", room_id, event.event_id)
    await post_message(
        client,
        room_id,
        f"{mention_user}\n\n{HELP_TEXT}",
        reply_to=event.event_id,
    )


async def handle_sessions_command(
    client: AsyncClient,
    room_id: str,
    event: RoomMessageText,
    mention_user: str,
    agent_name: str,
    project_dir: str,
    db: sqlite3.Connection,
) -> None:
    log.info("action=cmd_sessions room=%s event_id=%s", room_id, event.event_id)
    rows = db.execute(
        "SELECT * FROM sessions WHERE room_id = ? ORDER BY last_used_at DESC LIMIT 10",
        (room_id,),
    ).fetchall()
    if not rows:
        await post_message(
            client,
            room_id,
            f"{mention_user} No sessions yet in this room.",
            reply_to=event.event_id,
        )
        return
    header_event_id = await post_message(
        client,
        room_id,
        f"{mention_user} Recent sessions in #{agent_name} (reply to one to resume):",
        reply_to=event.event_id,
    )
    for i, row in enumerate(rows, 1):
        summary = read_first_user_message(row["session_id"], project_dir)
        text = f"{i}. ({row['session_id'][:8]}) {summary}"
        list_event_id = await post_message(
            client,
            room_id,
            text,
            reply_to=header_event_id,
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
    client: AsyncClient,
    room_id: str,
    event: RoomMessageText,
    mention_user: str,
    project_dir: str,
    db: sqlite3.Connection,
    max_message_length: int,
    arg: str,
) -> None:
    n = _parse_recap_n(arg)
    log.info("action=cmd_recap room=%s event_id=%s n=%d", room_id, event.event_id, n)
    row = db.execute(
        "SELECT * FROM sessions WHERE room_id = ? ORDER BY last_used_at DESC LIMIT 1",
        (room_id,),
    ).fetchone()
    if row is None:
        await post_message(
            client,
            room_id,
            f"{mention_user} No prior sessions to recap.",
            reply_to=event.event_id,
        )
        return
    body = read_last_n_turns(row["session_id"], project_dir, n)
    if not body:
        await post_message(
            client,
            room_id,
            f"{mention_user} Session {row['session_id'][:8]} has no readable turns.",
            reply_to=event.event_id,
        )
        return
    header = f"{mention_user} Recap of session {row['session_id'][:8]} (last {n} turns):\n\n"
    chunks = split_on_paragraphs(header + body, max_message_length)
    for chunk in chunks:
        await post_message(client, room_id, chunk, reply_to=event.event_id)


async def handle_mirror_command(
    client: AsyncClient,
    room_id: str,
    event: RoomMessageText,
    mention_user: str,
    agent_name: str,
    project_dir: str,
    db: sqlite3.Connection,
    registry: RegistryClient | None = None,
    scoped_mcp_url: str = "",
) -> None:
    log.info("action=cmd_mirror room=%s event_id=%s", room_id, event.event_id)
    session_id = find_unmirrored_session_id(db, project_dir, room_id)
    if session_id is None:
        await post_message(
            client,
            room_id,
            f"{mention_user} No unmirrored CloudCLI sessions found for #{agent_name}.",
            reply_to=event.event_id,
        )
        return
    insert_session(db, event.event_id, room_id, agent_name, session_id)
    await _register_session(
        registry,
        session_id,
        agent_name,
        room_id,
        project_dir,
        scoped_mcp_url,
    )
    response_event_id = await post_message(
        client,
        room_id,
        f"{mention_user} Linked session {session_id[:8]} to this thread. Reply here to resume.",
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
        sessions_deleted,
        aliases_deleted,
        retention_days,
    )
    return sessions_deleted, aliases_deleted


async def cleanup_loop(db: sqlite3.Connection, retention_days: int) -> None:
    while True:
        try:
            run_cleanup(db, retention_days)
        except Exception:
            log.exception("action=cleanup_error")
        await asyncio.sleep(86400)


# ---------------------------------------------------------------------------
# HITL resume-on-approval reconcile loop (SMCP-38)
# ---------------------------------------------------------------------------


def _build_room_to_agent(config: dict) -> dict[str, dict]:
    """Map room_id → agent config (name + agent fields), as poll_loop does."""
    room_to_agent: dict[str, dict] = {}
    for name, agent_cfg in config.get("agents", {}).items():
        room_to_agent[agent_cfg["room_id"]] = {"name": name, **agent_cfg}
    return room_to_agent


async def _resume_on_approval(
    client: AsyncClient,
    db: sqlite3.Connection,
    config: dict,
    room_to_agent: dict[str, dict],
    registry: RegistryClient | None,
    row: sqlite3.Row,
) -> None:
    """Resume the originating session for one approved approval — exactly once.

    The local pending row is deleted *before* the resume (claim-then-act) so an
    overlapping reconcile pass or a restart mid-resume can never fire it twice.
    A failed resume degrades to the operator's manual retry (fail-open), which is
    the safe direction: a missed auto-resume beats a duplicate tool execution.
    Runs under the per-room lock so it never interleaves with a live turn.
    """
    approval_id = row["approval_id"]
    room_id = row["room_id"]
    session_id = row["session_id"]
    thread_root_id = row["thread_root_id"]
    tool_name = row["tool_name"] or ""
    mention_user = config.get("mention_user", "")
    max_message_length = config.get("max_message_length", 4000)

    agent_cfg = room_to_agent.get(room_id)
    if agent_cfg is None:
        # Room no longer configured — nothing to resume into. Drop the record.
        delete_pending_approval(db, approval_id)
        log.warning(
            "action=hitl_resume_no_agent room=%s approval=%s",
            room_id,
            approval_id,
        )
        return
    agent_name = agent_cfg["name"]
    project_dir = agent_cfg["project_dir"]
    timeout = agent_cfg.get("subprocess_timeout_seconds", SUBPROCESS_TIMEOUT_SECONDS)

    # Claim first — exactly-once guarantee.
    delete_pending_approval(db, approval_id)

    # The nudge carries NO secret (no OTP, no token). The agent must retry the
    # identical call — the pre-approval token is bound to (tool, args_hash).
    nudge = (
        f"Operator approved your pending request for `{tool_name}` "
        f"(approval {approval_id}). Retry the exact same tool call now — "
        f"same arguments — to consume the approval."
    )
    log.info(
        "action=hitl_resume_start room=%s session=%s approval=%s",
        room_id,
        session_id,
        approval_id,
    )
    async with _room_lock(room_id):
        turn_start = datetime.now(UTC)
        try:
            exit_code, output = await resume_claude(
                session_id,
                nudge,
                project_dir,
                agent_name,
                room_id,
                timeout=timeout,
            )
        except TimeoutError:
            log.error(
                "action=hitl_resume_timeout room=%s session=%s approval=%s",
                room_id,
                session_id,
                approval_id,
            )
            evt = await post_message(
                client,
                room_id,
                f"{mention_user} Session {session_id[:8]} timed out resuming after approval.",
                reply_to=thread_root_id,
            )
            register_alias(db, evt, thread_root_id)
            return
        except Exception:
            # The row was already claimed (deleted) for the exactly-once guarantee,
            # so a non-timeout failure here would otherwise drop the approval
            # silently. Notify the operator so they can retry manually — symmetric
            # with the timeout path (audit LOW-1).
            log.exception(
                "action=hitl_resume_failed room=%s session=%s approval=%s",
                room_id,
                session_id,
                approval_id,
            )
            evt = await post_message(
                client,
                room_id,
                f"{mention_user} Session {session_id[:8]} failed to resume after approval "
                f"(approval {approval_id}); please retry the request manually.",
                reply_to=thread_root_id,
            )
            register_alias(db, evt, thread_root_id)
            return
        touch_session(db, thread_root_id)
        log.info(
            "action=hitl_resume_complete room=%s session=%s approval=%s exit_code=%d",
            room_id,
            session_id,
            approval_id,
            exit_code,
        )
        await _post_response(
            client,
            room_id,
            output,
            exit_code,
            session_id,
            mention_user,
            max_message_length,
            reply_target=thread_root_id,
            action="resume",
            db=db,
            thread_root_id=thread_root_id,
        )
        # A resumed-after-approval turn may itself hit another gate — track it too.
        await _record_pending_if_gated(
            db,
            registry,
            agent_name,
            room_id,
            session_id,
            thread_root_id,
            turn_start,
        )


async def reconcile_once(
    client: AsyncClient,
    db: sqlite3.Connection,
    config: dict,
    room_to_agent: dict[str, dict],
    registry: RegistryClient | None,
) -> None:
    """One reconcile pass: expire stale locals, then resume/deny by DB state."""
    if registry is None or not registry.enabled:
        return
    pending = get_pending_approvals(db)
    if not pending:
        return
    mention_user = config.get("mention_user", "")
    now = int(time.time())

    # Expire local rows for approvals never actioned within the window.
    live: list[sqlite3.Row] = []
    for row in pending:
        if now - row["created_at"] > PENDING_APPROVAL_EXPIRY_SECONDS:
            delete_pending_approval(db, row["approval_id"])
            log.info(
                "action=hitl_pending_expired room=%s approval=%s",
                row["room_id"],
                row["approval_id"],
            )
        else:
            live.append(row)
    if not live:
        return

    states = await registry.get_approval_states([r["approval_id"] for r in live])
    for row in live:
        state = states.get(row["approval_id"])
        if state == "approved":
            await _resume_on_approval(client, db, config, room_to_agent, registry, row)
        elif state == "denied":
            delete_pending_approval(db, row["approval_id"])
            log.info(
                "action=hitl_denied_noresume room=%s approval=%s",
                row["room_id"],
                row["approval_id"],
            )
            evt = await post_message(
                client,
                row["room_id"],
                f"{mention_user} Operator denied the pending request for "
                f"`{row['tool_name'] or 'the tool'}`; not retrying.",
                reply_to=row["thread_root_id"],
            )
            register_alias(db, evt, row["thread_root_id"])
        # else: still 'pending' (or absent) — leave for a later pass.


async def reconcile_loop(
    client: AsyncClient,
    config: dict,
    db: sqlite3.Connection,
    registry: RegistryClient | None,
) -> None:
    """Periodically resume sessions whose HITL approvals have landed (SMCP-38).

    No-ops entirely when the registry is disabled (``AGENT_REGISTRY_DSN`` unset),
    so with the feature off the dispatcher behaves exactly as before. Never lets a
    registry error escape into the process — a failed pass is logged and retried.
    """
    if registry is None or not registry.enabled:
        log.info("action=reconcile_disabled reason=registry-off")
        return
    room_to_agent = _build_room_to_agent(config)
    log.info("action=reconcile_start interval=%d", RECONCILE_INTERVAL_SECONDS)
    while True:
        try:
            await reconcile_once(client, db, config, room_to_agent, registry)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("action=reconcile_error")
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)


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
    bot_user_id: str = "",
    subprocess_timeout_seconds: int = SUBPROCESS_TIMEOUT_SECONDS,
    registry: RegistryClient | None = None,
    scoped_mcp_url: str = "",
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
                client,
                room_id,
                event,
                mention_user,
                agent_name,
                project_dir,
                db,
            )
        elif cmd == "!recap":
            await handle_recap_command(
                client,
                room_id,
                event,
                mention_user,
                project_dir,
                db,
                max_message_length,
                arg,
            )
        elif cmd == "!mirror":
            await handle_mirror_command(
                client,
                room_id,
                event,
                mention_user,
                agent_name,
                project_dir,
                db,
                registry=registry,
                scoped_mcp_url=scoped_mcp_url,
            )
        elif cmd == "!cancel":
            await handle_cancel_command(client, room_id, event, mention_user)
        else:
            log.info(
                "action=command_unknown room=%s event_id=%s cmd=%s",
                room_id,
                event.event_id,
                cmd[:20],
            )
            await post_message(
                client,
                room_id,
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
                room_id,
                event.event_id,
                agent_name,
                session_id,
            )
            ack_event_id = await post_message(
                client,
                room_id,
                f"Resuming... (session {short_id})",
                reply_to=event.event_id,
            )
            register_alias(db, ack_event_id, thread_root_id)
            await _register_session(
                registry,
                session_id,
                agent_name,
                room_id,
                project_dir,
                scoped_mcp_url,
            )
            async with _room_lock(room_id):
                turn_start = datetime.now(UTC)
                try:
                    exit_code, output = await resume_claude(
                        session_id,
                        user_message,
                        project_dir,
                        agent_name,
                        room_id,
                        timeout=subprocess_timeout_seconds,
                    )
                except TimeoutError:
                    log.error("action=resume_timeout room=%s session=%s", room_id, session_id)
                    timeout_event_id = await post_message(
                        client,
                        room_id,
                        f"{mention_user} Session {short_id} timed out after "
                        f"{subprocess_timeout_seconds}s.",
                        reply_to=ack_event_id or event.event_id,
                    )
                    register_alias(db, timeout_event_id, thread_root_id)
                    return
                touch_session(db, thread_root_id)
                log.info(
                    "action=resume_complete room=%s session=%s exit_code=%d",
                    room_id,
                    session_id,
                    exit_code,
                )
                await _post_response(
                    client,
                    room_id,
                    output,
                    exit_code,
                    session_id,
                    mention_user,
                    max_message_length,
                    reply_target=ack_event_id or event.event_id,
                    action="resume",
                    db=db,
                    thread_root_id=thread_root_id,
                )
                await _record_pending_if_gated(
                    db,
                    registry,
                    agent_name,
                    room_id,
                    session_id,
                    thread_root_id,
                    turn_start,
                )
            return
        # Orphaned reply: thread root has no tracked session. Never spawn a new
        # session from a reply — that only ever produces a context-free duplicate
        # (the supported way to adopt an untracked session is !mirror). Determine
        # whether the replied-to event is one of our own posts vs. a foreign
        # message, to pick hint vs. silence. Fail-closed: unknown -> foreign.
        parent_sender = await _get_event_sender(client, room_id, thread_root)
        if bot_user_id and parent_sender == bot_user_id:
            # Our own thread whose session was cleaned up (retention) — give Ted
            # a usable next step instead of silent nothing.
            log.info(
                "action=expired_thread_reply room=%s event_id=%s thread_root=%s",
                room_id,
                event.event_id,
                thread_root,
            )
            await post_message(
                client,
                room_id,
                f"{mention_user} No active session for that thread (it may have "
                f"expired). Send a new message (not a reply) to start one, or "
                f"`!sessions` to list recent ones.",
                reply_to=event.event_id,
            )
        else:
            # Foreign-bot / third-party message (scoped-mcp HITL, matrix-hitl-bot,
            # matrix-admin-bot, matrix-task-queue-bot, etc.) — the reply was meant
            # for that bot, not the dispatcher. Ignore silently; no room noise.
            log.info(
                "action=foreign_reply_ignored room=%s event_id=%s thread_root=%s sender=%s",
                room_id,
                event.event_id,
                thread_root,
                parent_sender or "unknown",
            )
        return

    # Room-root (thread_root is None) → spawn new session.
    # Acquire lock first so rate-limit + spawn are atomic per room — with
    # concurrent handlers, two messages racing the rate-limit check would
    # otherwise both pass.
    async with _room_lock(room_id):
        now = time.time()
        last = _last_spawn_at.get(room_id, 0.0)
        if now - last < RATE_LIMIT_SECONDS:
            remaining = int(RATE_LIMIT_SECONDS - (now - last))
            log.info(
                "action=rate_limited room=%s event_id=%s remaining=%d",
                room_id,
                event.event_id,
                remaining,
            )
            await post_message(
                client,
                room_id,
                f"{mention_user} Rate-limited; try again in {remaining}s.",
                reply_to=event.event_id,
            )
            return
        _last_spawn_at[room_id] = now

        session_id = str(uuid.uuid4())
        short_id = session_id[:8]
        log.info(
            "action=spawn_start room=%s event_id=%s agent=%s session=%s",
            room_id,
            event.event_id,
            agent_name,
            session_id,
        )
        ack_event_id = await post_message(
            client,
            room_id,
            f"Working... (session {short_id})",
            reply_to=event.event_id,
        )
        # Store session before spawning so thread replies can resume it even if spawn errors
        insert_session(db, event.event_id, room_id, agent_name, session_id)
        register_alias(db, ack_event_id, event.event_id)
        await _register_session(
            registry,
            session_id,
            agent_name,
            room_id,
            project_dir,
            scoped_mcp_url,
        )

        # The dispatcher owns all Matrix posting. Agents must NOT call any Matrix
        # or mcp__matrix__ tools — doing so causes double-posts and permission blocks.
        prompt = (
            f"[Invoked via Matrix room #{agent_name} by @ted. "
            f"Output your response as plain text only. "
            f"Do NOT use mcp__matrix__ tools or any Matrix MCP — "
            f"the dispatcher will post your stdout to the room automatically.]\n\n"
            f"{user_message}"
        )
        turn_start = datetime.now(UTC)
        try:
            exit_code, output = await spawn_claude(
                session_id,
                prompt,
                project_dir,
                agent_name,
                room_id,
                timeout=subprocess_timeout_seconds,
            )
        except TimeoutError:
            log.error("action=spawn_timeout room=%s session=%s", room_id, session_id)
            timeout_event_id = await post_message(
                client,
                room_id,
                f"{mention_user} Session {short_id} timed out after {subprocess_timeout_seconds}s.",
                reply_to=ack_event_id or event.event_id,
            )
            register_alias(db, timeout_event_id, event.event_id)
            return
        log.info(
            "action=spawn_complete room=%s session=%s exit_code=%d",
            room_id,
            session_id,
            exit_code,
        )
        await _post_response(
            client,
            room_id,
            output,
            exit_code,
            session_id,
            mention_user,
            max_message_length,
            reply_target=ack_event_id or event.event_id,
            action="spawn",
            db=db,
            thread_root_id=event.event_id,
        )
        await _record_pending_if_gated(
            db,
            registry,
            agent_name,
            room_id,
            session_id,
            event.event_id,
            turn_start,
        )


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------


async def poll_loop(
    client: AsyncClient,
    config: dict,
    db: sqlite3.Connection,
    registry: RegistryClient | None = None,
) -> None:
    trusted_sender: str = config.get("trusted_sender", "")
    mention_user: str = config.get("mention_user", "")
    bot_user_id: str = config.get("bot_user_id", "")
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
                    # Dispatch as a task so the poll loop keeps reading sync
                    # responses while subprocesses run. Per-room serialization
                    # is provided by _room_lock inside handle_event. This is
                    # what allows !cancel to actually fire mid-spawn.
                    task = asyncio.create_task(
                        handle_event(
                            client=client,
                            room_id=room_id,
                            event=event,
                            trusted_sender=trusted_sender,
                            mention_user=mention_user,
                            max_message_length=max_message_length,
                            agent_name=agent_name,
                            project_dir=project_dir,
                            db=db,
                            bot_user_id=bot_user_id,
                            subprocess_timeout_seconds=agent_cfg.get(
                                "subprocess_timeout_seconds", SUBPROCESS_TIMEOUT_SECONDS
                            ),
                            registry=registry,
                            scoped_mcp_url=agent_cfg.get("scoped_mcp_url", ""),
                        )
                    )
                    _handlers.add(task)
                    task.add_done_callback(_handlers.discard)

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
                client,
                notify_room,
                f"matrix-dispatcher started — agents: {list(agents_cfg.keys())}",
            )
        except Exception:
            log.exception("action=startup_notify_error")

    # HITL resume-on-approval (SMCP-38). Disabled unless AGENT_REGISTRY_DSN is set
    # and the pool builds — fail-open, so a down/absent agent-postgres degrades to
    # the prior manual-retry behavior, never an outage.
    registry = await get_registry()
    log.info("action=registry_status enabled=%s", registry.enabled)

    cleanup_task = asyncio.create_task(cleanup_loop(db, retention_days))
    reconcile_task = asyncio.create_task(reconcile_loop(client, config, db, registry))
    try:
        await poll_loop(client, config, db, registry)
    finally:
        for task in (cleanup_task, reconcile_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await registry.close()
        # Send SIGTERM to any active subprocesses so handlers can complete
        # naturally instead of leaving orphans.
        for room_id, proc in list(_active_processes.items()):
            try:
                proc.send_signal(signal.SIGTERM)
                log.info("action=shutdown_sigterm room=%s pid=%s", room_id, proc.pid)
            except ProcessLookupError:
                pass
        # Brief grace period for in-flight handlers to finish posting their
        # responses. After 10s, abandon — PM2 will force-kill us soon anyway.
        if _handlers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*_handlers, return_exceptions=True),
                    timeout=10,
                )
            except TimeoutError:
                log.warning("action=shutdown_handlers_timeout outstanding=%d", len(_handlers))
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
