"""Bang-prefix command handlers (!sessions, !recap, !mirror, !help).

!cancel lives in :mod:`runner` (it manipulates runtime process state); !help,
!sessions, !recap, !mirror are here. Transcript readers are invoked via
``transcripts.<name>`` attribute access so tests can patch them on that module.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from . import hitl, transcripts
from .config import log
from .db import insert_session, register_alias
from .matrixio import post_message, split_on_paragraphs

if TYPE_CHECKING:
    from nio import AsyncClient, RoomMessageText

    from .registry import RegistryClient


HELP_TEXT = (
    "Dispatcher commands (! prefix — Element intercepts /-commands client-side):\n"
    "  !sessions       — list recent sessions (reply to one to resume)\n"
    "  !recap [N]      — show last N turns of most recent session (default: 5)\n"
    "  !mirror         — link most recent CloudCLI session to this room for resume\n"
    "  !cancel         — SIGTERM the active session in this room\n"
    "  !help           — this message"
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
        summary = transcripts.read_first_user_message(row["session_id"], project_dir)
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
    body = transcripts.read_last_n_turns(row["session_id"], project_dir, n)
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
    session_id = transcripts.find_unmirrored_session_id(db, project_dir, room_id)
    if session_id is None:
        await post_message(
            client,
            room_id,
            f"{mention_user} No unmirrored CloudCLI sessions found for #{agent_name}.",
            reply_to=event.event_id,
        )
        return
    insert_session(db, event.event_id, room_id, agent_name, session_id)
    await hitl._register_session(
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
