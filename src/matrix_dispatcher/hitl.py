"""HITL resume-on-approval (SMCP-38).

Session-registry writes, post-turn pending detection, and the reconcile loop
that resumes a session once its operator approval lands. Every registry touch is
fail-open: a down/absent agent-postgres degrades to manual-retry, never an outage
and never a gate bypass.

``spawn``/``resume`` are invoked via ``runner.<name>`` attribute access so a
single test patch on the :mod:`runner` module reaches both this module and
:func:`matrix_dispatcher.app.handle_event`.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from . import runner
from .config import (
    PENDING_APPROVAL_EXPIRY_SECONDS,
    RECONCILE_INTERVAL_SECONDS,
    SUBPROCESS_TIMEOUT_SECONDS,
    log,
)
from .db import (
    delete_pending_approval,
    get_pending_approvals,
    insert_pending_approval,
    register_alias,
    touch_session,
)
from .matrixio import _post_response, post_message

if TYPE_CHECKING:
    from nio import AsyncClient

    from .registry import RegistryClient


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
    async with runner._room_lock(room_id):
        turn_start = datetime.now(UTC)
        try:
            exit_code, output = await runner.resume_claude(
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
