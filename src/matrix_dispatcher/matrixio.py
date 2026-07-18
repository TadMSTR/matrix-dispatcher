"""Matrix I/O helpers — posting, sender lookup, chunking, thread-root extraction,
dispatcher credentials, and the shared response-posting helper.

``_post_response`` lives here (rather than in :mod:`commands`) because it is a
Matrix-posting concern used by both :func:`matrix_dispatcher.app.handle_event`
and :func:`matrix_dispatcher.hitl._resume_on_approval`; keeping it here avoids a
``commands`` ↔ ``hitl`` import cycle.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from typing import TYPE_CHECKING

from .config import log
from .db import register_alias

if TYPE_CHECKING:
    from nio import AsyncClient, RoomMessageText


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
