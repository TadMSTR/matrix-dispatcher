"""Tests for agent_registry.RegistryClient query bodies + pool/get_registry paths.

The existing test_agent_registry.py only exercises DSN *selection* and the
``_pool is None`` early-returns. Here we drive the real parameterized statements
against a fake asyncpg pool and assert the fail-open contract holds (every DB
error is swallowed, never raised into the Matrix loop).
"""

from __future__ import annotations

import sys
from datetime import UTC

import pytest

import agent_registry

# --------------------------------------------------------------------------- #
# Fake asyncpg pool / connection
# --------------------------------------------------------------------------- #


class FakeConn:
    def __init__(self, *, fetchrow_ret=None, fetch_ret=None, exc: Exception | None = None) -> None:
        self.fetchrow_ret = fetchrow_ret
        self.fetch_ret = fetch_ret
        self.exc = exc
        self.calls: list[tuple] = []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        if self.exc:
            raise self.exc

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if self.exc:
            raise self.exc
        return self.fetchrow_ret

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if self.exc:
            raise self.exc
        return self.fetch_ret


class _Acquire:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


class FakePool:
    def __init__(self, conn: FakeConn, *, close_exc: Exception | None = None) -> None:
        self._conn = conn
        self.closed = False
        self._close_exc = close_exc

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)

    async def close(self) -> None:
        if self._close_exc:
            raise self._close_exc
        self.closed = True


# --------------------------------------------------------------------------- #
# upsert_session
# --------------------------------------------------------------------------- #


async def test_upsert_session_executes_insert():
    conn = FakeConn()
    client = agent_registry.RegistryClient(FakePool(conn))
    await client.upsert_session(
        session_id="s1",
        agent_id="sysadmin",
        transport="matrix",
        room_id="!r:example.org",
        project_dir="/tmp/x",
        scoped_mcp_url="https://mcp",
    )
    assert conn.calls and conn.calls[0][0] == "execute"
    assert "INSERT INTO sessions" in conn.calls[0][1]
    assert conn.calls[0][2][0] == "s1"  # session_id bound as $1


async def test_upsert_session_disabled_noop():
    client = agent_registry.RegistryClient(None)
    await client.upsert_session(session_id="s", agent_id="a", transport="matrix")
    assert client.enabled is False


async def test_upsert_session_swallows_db_error():
    conn = FakeConn(exc=RuntimeError("pg down"))
    client = agent_registry.RegistryClient(FakePool(conn))
    # Must NOT raise — fail-open.
    await client.upsert_session(session_id="s", agent_id="a", transport="matrix")


# --------------------------------------------------------------------------- #
# find_pending_approval
# --------------------------------------------------------------------------- #


async def test_find_pending_approval_returns_row(monkeypatch):
    from datetime import datetime

    row = {"approval_id": "sysadmin.abc", "tool_name": "gitea_pr_merge", "created_at": "ts"}
    conn = FakeConn(fetchrow_ret=row)
    client = agent_registry.RegistryClient(FakePool(conn))
    got = await client.find_pending_approval("sysadmin", datetime.now(UTC))
    assert got == row
    assert conn.calls[0][0] == "fetchrow"


async def test_find_pending_approval_none_row():
    from datetime import datetime

    conn = FakeConn(fetchrow_ret=None)
    client = agent_registry.RegistryClient(FakePool(conn))
    assert await client.find_pending_approval("a", datetime.now(UTC)) is None


async def test_find_pending_approval_disabled():
    from datetime import datetime

    client = agent_registry.RegistryClient(None)
    assert await client.find_pending_approval("a", datetime.now(UTC)) is None


async def test_find_pending_approval_swallows_error():
    from datetime import datetime

    conn = FakeConn(exc=RuntimeError("boom"))
    client = agent_registry.RegistryClient(FakePool(conn))
    assert await client.find_pending_approval("a", datetime.now(UTC)) is None


# --------------------------------------------------------------------------- #
# link_session
# --------------------------------------------------------------------------- #


async def test_link_session_executes_update():
    conn = FakeConn()
    client = agent_registry.RegistryClient(FakePool(conn))
    await client.link_session("appr-1", "sess-1")
    assert "UPDATE hitl_approvals" in conn.calls[0][1]
    assert conn.calls[0][2] == ("appr-1", "sess-1")


async def test_link_session_disabled_noop():
    client = agent_registry.RegistryClient(None)
    await client.link_session("a", "s")


async def test_link_session_swallows_error():
    conn = FakeConn(exc=RuntimeError("boom"))
    client = agent_registry.RegistryClient(FakePool(conn))
    await client.link_session("a", "s")


# --------------------------------------------------------------------------- #
# get_approval_states
# --------------------------------------------------------------------------- #


async def test_get_approval_states_maps_rows():
    rows = [
        {"approval_id": "a1", "state": "approved"},
        {"approval_id": "a2", "state": "denied"},
    ]
    conn = FakeConn(fetch_ret=rows)
    client = agent_registry.RegistryClient(FakePool(conn))
    got = await client.get_approval_states(["a1", "a2"])
    assert got == {"a1": "approved", "a2": "denied"}


async def test_get_approval_states_empty_ids_short_circuits():
    conn = FakeConn(fetch_ret=[])
    client = agent_registry.RegistryClient(FakePool(conn))
    assert await client.get_approval_states([]) == {}
    assert conn.calls == []  # never hit the DB


async def test_get_approval_states_disabled():
    client = agent_registry.RegistryClient(None)
    assert await client.get_approval_states(["a1"]) == {}


async def test_get_approval_states_swallows_error():
    conn = FakeConn(exc=RuntimeError("boom"))
    client = agent_registry.RegistryClient(FakePool(conn))
    assert await client.get_approval_states(["a1"]) == {}


# --------------------------------------------------------------------------- #
# close
# --------------------------------------------------------------------------- #


async def test_close_closes_pool_and_disables():
    pool = FakePool(FakeConn())
    client = agent_registry.RegistryClient(pool)
    assert client.enabled
    await client.close()
    assert pool.closed
    assert client.enabled is False


async def test_close_disabled_noop():
    client = agent_registry.RegistryClient(None)
    await client.close()


async def test_close_swallows_error_but_disables():
    pool = FakePool(FakeConn(), close_exc=RuntimeError("close failed"))
    client = agent_registry.RegistryClient(pool)
    await client.close()  # error swallowed
    assert client.enabled is False  # finally still ran


# --------------------------------------------------------------------------- #
# _build_pool
# --------------------------------------------------------------------------- #


async def test_build_pool_missing_asyncpg(monkeypatch):
    monkeypatch.setitem(sys.modules, "asyncpg", None)
    assert await agent_registry._build_pool("postgresql://x") is None


async def test_build_pool_create_failure(monkeypatch):
    import types

    fake_asyncpg = types.SimpleNamespace()

    async def _raise(*a, **k):
        raise RuntimeError("connection refused")

    fake_asyncpg.create_pool = _raise
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)
    assert await agent_registry._build_pool("postgresql://x") is None


async def test_build_pool_success(monkeypatch):
    import types

    sentinel = object()
    fake_asyncpg = types.SimpleNamespace()

    async def _ok(dsn, **k):
        return sentinel

    fake_asyncpg.create_pool = _ok
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)
    assert await agent_registry._build_pool("postgresql://x") is sentinel


# --------------------------------------------------------------------------- #
# get_registry branches
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch):
    monkeypatch.setattr(agent_registry, "_registry", None)
    monkeypatch.setattr(agent_registry, "_init_lock", None)
    for var in (
        "AGENT_REGISTRY_DSN",
        "VAULT_ADDR",
        agent_registry._VAULT_ROLE_ID_ENV,
        agent_registry._VAULT_SECRET_ID_ENV,
    ):
        monkeypatch.delenv(var, raising=False)


async def test_get_registry_returns_cached_singleton(monkeypatch):
    cached = agent_registry.RegistryClient(None)
    monkeypatch.setattr(agent_registry, "_registry", cached)
    assert await agent_registry.get_registry() is cached


async def test_get_registry_enabled_but_pool_unavailable(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_REGISTRY_DSN", "postgresql://host/db")

    async def _no_pool(dsn):
        return None

    monkeypatch.setattr(agent_registry, "_build_pool", _no_pool)
    client = await agent_registry.get_registry()
    assert client.enabled is False  # DSN present but pool failed -> disabled client
