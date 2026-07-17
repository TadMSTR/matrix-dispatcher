"""agent-postgres session registry client for matrix-dispatcher (fail-open).

The dispatcher is the resume authority for HITL approvals (SMCP-38). It writes
the ``sessions`` registry and reconciles ``hitl_approvals`` state from
agent-postgres (``127.0.0.1:5433``) so that when an operator approval lands, the
originating ``claude -p`` session is resumed and the gated call proceeds.

**Fail-open contract.** Every method here is best-effort. A missing ``asyncpg``,
an unset ``AGENT_REGISTRY_DSN``, or any database error is caught, logged at
warning, and swallowed — it MUST NOT raise into the caller and MUST NOT block or
crash the Matrix ``/sync`` loop. Resume-on-approval is an *availability* feature
layered on top of a fail-*closed* gate (the Dragonfly OTP in scoped-mcp): if this
client can't reach the DB, the feature simply doesn't fire and the operator falls
back to the current manual-retry behavior — never a bypass.

This intentionally duplicates the small fail-open shape of scoped-mcp's
``registry_db.py`` rather than sharing an installable: the dispatcher is a
single-file app with a flat ``requirements.txt`` and a different runtime, and the
two consumers touch disjoint subsets of the schema (scoped-mcp writes the audit
row; the dispatcher writes ``sessions`` and reads/links the approval state). The
duplication is a handful of parameterized statements against the frozen
migration-0001 schema.

Configuration:
- ``AGENT_REGISTRY_DSN`` — Postgres DSN, e.g.
  ``postgresql://dispatcher_registry:***@127.0.0.1:5433/agent_registry``. Unset ⇒
  the registry is disabled and every method is a no-op (behavior identical to
  before this feature — off by default).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

log = logging.getLogger("dispatcher.registry")

# Module-level singleton. ``_init_lock`` guards concurrent first-use so the pool
# is built exactly once even if startup and the first reconcile pass race.
_registry: RegistryClient | None = None
_init_lock: asyncio.Lock | None = None


class RegistryClient:
    """asyncpg-backed registry client. All calls are fail-open.

    Construct via :func:`get_registry`. A client whose ``pool`` is ``None`` is a
    valid *disabled* instance — every method no-ops — so callers can invoke
    methods unconditionally and only branch on :attr:`enabled` when they want to
    skip work entirely.
    """

    def __init__(self, pool: Any | None) -> None:
        self._pool = pool

    @property
    def enabled(self) -> bool:
        return self._pool is not None

    async def upsert_session(
        self,
        session_id: str,
        agent_id: str,
        transport: str,
        room_id: str | None = None,
        project_dir: str | None = None,
        scoped_mcp_url: str | None = None,
        status: str = "active",
    ) -> None:
        """Insert or refresh a ``sessions`` row for this dispatcher session.

        Writing this at spawn/resume is what makes ``hitl_approvals.session_id``
        FK-satisfiable at :meth:`link_session` time and finally wires the
        ``sessions`` registry SMCP-14 left without a writer.
        """
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, agent_id, transport, room_id, project_dir,
                        scoped_mcp_url, status, last_seen_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, now())
                    ON CONFLICT (session_id) DO UPDATE SET
                        agent_id       = EXCLUDED.agent_id,
                        transport      = EXCLUDED.transport,
                        room_id        = COALESCE(EXCLUDED.room_id, sessions.room_id),
                        project_dir    = COALESCE(EXCLUDED.project_dir, sessions.project_dir),
                        scoped_mcp_url = COALESCE(EXCLUDED.scoped_mcp_url, sessions.scoped_mcp_url),
                        status         = EXCLUDED.status,
                        last_seen_at   = now()
                    """,
                    session_id,
                    agent_id,
                    transport,
                    room_id,
                    project_dir,
                    scoped_mcp_url,
                    status,
                )
        except Exception as e:  # fail-open
            log.warning("action=registry_upsert_session_failed err=%s", type(e).__name__)

    async def find_pending_approval(
        self, agent_id: str, since_ts: datetime
    ) -> dict[str, Any] | None:
        """Return the most recent still-``pending`` approval for ``agent_id``
        created at/after ``since_ts`` (the turn start), or ``None``.

        Filtering on ``state='pending'`` is the duplicate-execution guard: an
        approval the agent already self-resolved in-turn is ``approved`` /
        ``denied`` / ``consumed`` and is deliberately excluded, so the dispatcher
        never resumes a session whose tool call already ran.

        ``since_ts`` must be timezone-aware (UTC); it is compared against the
        server-side ``created_at`` (``TIMESTAMPTZ``). A strict lower bound of the
        turn start naturally excludes any pending row from a prior turn.
        """
        if self._pool is None:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT approval_id, tool_name, created_at
                      FROM hitl_approvals
                     WHERE agent_id = $1
                       AND state = 'pending'
                       AND created_at >= $2
                     ORDER BY created_at DESC
                     LIMIT 1
                    """,
                    agent_id,
                    since_ts,
                )
            if row is None:
                return None
            return {
                "approval_id": row["approval_id"],
                "tool_name": row["tool_name"],
                "created_at": row["created_at"],
            }
        except Exception as e:  # fail-open
            log.warning("action=registry_find_pending_failed err=%s", type(e).__name__)
            return None

    async def link_session(self, approval_id: str, session_id: str) -> None:
        """Attach an approval's originating ``session_id`` for the audit trail."""
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE hitl_approvals SET session_id = $2 WHERE approval_id = $1",
                    approval_id,
                    session_id,
                )
        except Exception as e:  # fail-open
            log.warning("action=registry_link_session_failed err=%s", type(e).__name__)

    async def get_approval_states(self, approval_ids: list[str]) -> dict[str, str]:
        """Return ``{approval_id: state}`` for the given ids (missing ⇒ absent)."""
        if self._pool is None or not approval_ids:
            return {}
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT approval_id, state FROM hitl_approvals "
                    "WHERE approval_id = ANY($1::text[])",
                    list(approval_ids),
                )
            return {r["approval_id"]: r["state"] for r in rows}
        except Exception as e:  # fail-open
            log.warning("action=registry_get_states_failed err=%s", type(e).__name__)
            return {}

    async def close(self) -> None:
        if self._pool is None:
            return
        try:
            await self._pool.close()
        except Exception as e:  # fail-open
            log.warning("action=registry_close_failed err=%s", type(e).__name__)
        finally:
            self._pool = None


async def _build_pool(dsn: str) -> Any | None:
    """Create an asyncpg pool, or return None on any failure (fail-open)."""
    try:
        import asyncpg  # local import — optional runtime dep
    except ImportError:
        log.warning("action=registry_disabled_missing_dep detail=asyncpg-not-installed")
        return None
    try:
        return await asyncpg.create_pool(dsn, min_size=1, max_size=3, command_timeout=5.0)
    except Exception as e:  # fail-open — a down DB must never stop the Matrix loop
        log.warning("action=registry_pool_init_failed err=%s", type(e).__name__)
        return None


async def get_registry() -> RegistryClient:
    """Return the process-wide registry client, building the pool on first use.

    Always returns a :class:`RegistryClient`: a disabled one (``pool=None``) when
    ``AGENT_REGISTRY_DSN`` is unset or the pool cannot be built, so callers can
    invoke methods unconditionally.
    """
    global _registry, _init_lock
    if _registry is not None:
        return _registry
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    async with _init_lock:
        if _registry is not None:  # lost the race
            return _registry
        dsn = os.environ.get("AGENT_REGISTRY_DSN", "").strip()
        pool = await _build_pool(dsn) if dsn else None
        if dsn and pool is None:
            log.warning("action=registry_enabled_but_unavailable")
        _registry = RegistryClient(pool)
        return _registry


__all__ = ["RegistryClient", "get_registry"]
