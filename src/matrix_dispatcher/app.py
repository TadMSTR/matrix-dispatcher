"""Orchestration: event dispatch, the poll/cleanup loops, and ``main``.

This module is also the package's aggregation surface — it re-exports the public
names of every submodule so the test-suite (and any external caller) can reach
them through a single import. ``spawn``/``resume`` are invoked via
``runner.<name>`` attribute access so a single patch on :mod:`runner` reaches
both :func:`handle_event` here and :func:`matrix_dispatcher.hitl._resume_on_approval`.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nio import AsyncClient, RoomMessageText, SyncResponse

from . import runner
from .commands import (
    handle_help_command,
    handle_mirror_command,
    handle_recap_command,
    handle_sessions_command,
)
from .config import (
    PENDING_APPROVAL_EXPIRY_SECONDS,
    RATE_LIMIT_SECONDS,
    SUBPROCESS_TIMEOUT_SECONDS,
    load_config,
    log,
)
from .db import (
    delete_pending_approval,
    get_pending_approvals,
    get_session,
    get_session_by_event,
    get_since,
    init_db,
    insert_pending_approval,
    insert_session,
    migrate_v1_tokens,
    open_db,
    register_alias,
    run_cleanup,
    set_since,
    touch_session,
)
from .hitl import (
    _build_room_to_agent,
    _record_pending_if_gated,
    _register_session,
    _resume_on_approval,
    reconcile_loop,
    reconcile_once,
)
from .matrixio import (
    _get_event_sender,
    _post_response,
    extract_thread_root,
    get_dispatcher_credentials,
    post_message,
    split_on_paragraphs,
)
from .registry import get_registry
from .runner import (
    _active_processes,
    _last_spawn_at,
    _minimal_env,
    _room_lock,
    _room_locks,
    _run_claude,
    handle_cancel_command,
    resume_claude,
    spawn_claude,
)
from .transcripts import (
    _extract_text,
    find_unmirrored_session_id,
    project_jsonl_dir,
    read_first_user_message,
    read_last_n_turns,
)

if TYPE_CHECKING:
    from .registry import RegistryClient

# Outstanding handler tasks — kept as strong refs (set discards on done callback)
# so the GC doesn't collect mid-flight handlers, and so shutdown can await them.
_handlers: set[asyncio.Task] = set()


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------


async def handle_event(
    client: AsyncClient,
    room_id: str,
    event: RoomMessageText,
    trusted_sender: str,
    mention_user: str,
    max_message_length: int,
    agent_name: str,
    project_dir: str,
    db,
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
                    exit_code, output = await runner.resume_claude(
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
            exit_code, output = await runner.spawn_claude(
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
    db,
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
# Retention cleanup loop
# ---------------------------------------------------------------------------


async def cleanup_loop(db, retention_days: int) -> None:
    while True:
        try:
            run_cleanup(db, retention_days)
        except Exception:
            log.exception("action=cleanup_error")
        await asyncio.sleep(86400)


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


# ---------------------------------------------------------------------------
# Public API surface — re-exports so the whole dispatcher is reachable through
# ``matrix_dispatcher.app`` (and the flat-module test-suite that aliases it).
# ---------------------------------------------------------------------------

__all__ = [
    "PENDING_APPROVAL_EXPIRY_SECONDS",
    "RATE_LIMIT_SECONDS",
    "SUBPROCESS_TIMEOUT_SECONDS",
    "AsyncClient",
    "RoomMessageText",
    "SyncResponse",
    "_active_processes",
    "_build_room_to_agent",
    "_extract_text",
    "_get_event_sender",
    "_handlers",
    "_last_spawn_at",
    "_minimal_env",
    "_post_response",
    "_record_pending_if_gated",
    "_register_session",
    "_resume_on_approval",
    "_room_lock",
    "_room_locks",
    "_run_claude",
    "cleanup_loop",
    "cli_cleanup",
    "delete_pending_approval",
    "extract_thread_root",
    "find_unmirrored_session_id",
    "get_dispatcher_credentials",
    "get_pending_approvals",
    "get_registry",
    "get_session",
    "get_session_by_event",
    "get_since",
    "handle_cancel_command",
    "handle_event",
    "handle_help_command",
    "handle_mirror_command",
    "handle_recap_command",
    "handle_sessions_command",
    "init_db",
    "insert_pending_approval",
    "insert_session",
    "load_config",
    "log",
    "main",
    "migrate_v1_tokens",
    "open_db",
    "poll_loop",
    "post_message",
    "project_jsonl_dir",
    "read_first_user_message",
    "read_last_n_turns",
    "reconcile_loop",
    "reconcile_once",
    "register_alias",
    "resume_claude",
    "run_cleanup",
    "runner",
    "set_since",
    "spawn_claude",
    "split_on_paragraphs",
    "touch_session",
]
