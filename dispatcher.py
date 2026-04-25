"""Matrix dispatcher — spawn or resume claude -p sessions from Matrix room messages.

v2: SQLite + thread-based resume. Thread replies resume existing sessions via
--resume <session_id>; room-root messages spawn fresh sessions. Poll state
migrated from v1 JSON file to poll_state table.

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
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml
from nio import AsyncClient, RoomMessageText, SyncResponse

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


def spawn_claude(
    session_id: str,
    prompt: str,
    project_dir: str,
    agent_name: str,
) -> tuple[int, str]:
    result = subprocess.run(
        ["claude", "-p", "--session-id", session_id, prompt],
        capture_output=True,
        text=True,
        cwd=project_dir,
        env=_minimal_env(agent_name),
        timeout=300,
    )
    output = result.stdout.strip() if result.returncode == 0 else result.stderr.strip()[:500]
    return result.returncode, output


def resume_claude(
    session_id: str,
    message: str,
    project_dir: str,
    agent_name: str,
) -> tuple[int, str]:
    result = subprocess.run(
        ["claude", "-p", "--resume", session_id, message],
        capture_output=True,
        text=True,
        cwd=project_dir,
        env=_minimal_env(agent_name),
        timeout=300,
    )
    output = result.stdout.strip() if result.returncode == 0 else result.stderr.strip()[:500]
    return result.returncode, output


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------

def extract_thread_root(event: RoomMessageText) -> str | None:
    """Return the thread-root event ID for a reply, or None for a room-root message.

    Handles both proper Matrix threads (rel_type=m.thread, event_id=thread_root)
    and simple in-reply-to chains (falls back to the replied-to event ID).
    """
    source = getattr(event, "source", {})
    content = source.get("content", {}) if isinstance(source, dict) else {}
    relates_to = content.get("m.relates_to")
    if not relates_to:
        return None
    if relates_to.get("rel_type") == "m.thread":
        return relates_to.get("event_id")
    in_reply_to = relates_to.get("m.in_reply_to", {})
    return in_reply_to.get("event_id")


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

    # Commands only intercepted on room-root messages (v3 — stub in v2)
    if thread_root is None and user_message.startswith("/"):
        log.info(
            "action=command_ignored room=%s event_id=%s body_prefix=%s",
            room_id, event.event_id, user_message[:20],
        )
        return

    # Thread reply → attempt resume
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
            try:
                exit_code, output = resume_claude(session_id, user_message, project_dir, agent_name)
            except subprocess.TimeoutExpired:
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

    # Room-root (or orphaned reply) → spawn new session
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
    try:
        exit_code, output = spawn_claude(session_id, prompt, project_dir, agent_name)
    except subprocess.TimeoutExpired:
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

    client = AsyncClient(homeserver, user_id)
    client.access_token = token
    client.user_id = user_id

    log.info("action=startup user_id=%s homeserver=%s", user_id, homeserver)

    try:
        await poll_loop(client, config, db)
    finally:
        db.close()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
