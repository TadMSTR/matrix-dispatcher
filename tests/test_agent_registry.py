"""Tests for SMCP-42: Vault-backed AGENT_REGISTRY_DSN resolution.

``get_registry()`` resolves the DSN from Vault first (if the AppRole env vars
are set), falling back to the plaintext ``AGENT_REGISTRY_DSN`` env var on any
failure — this must never raise or block the Matrix ``/sync`` loop. Pool
construction (asyncpg) is stubbed out here; these tests only assert on DSN
*selection*, not real DB connectivity.
"""

from __future__ import annotations

import sys
import types

import pytest

import matrix_dispatcher.registry as agent_registry


@pytest.fixture(autouse=True)
def _reset_registry_singleton(monkeypatch):
    """Every test gets a clean module-level singleton and a clean env."""
    monkeypatch.setattr(agent_registry, "_registry", None)
    monkeypatch.setattr(agent_registry, "_init_lock", None)
    for var in (
        "AGENT_REGISTRY_DSN",
        "VAULT_ADDR",
        agent_registry._VAULT_ROLE_ID_ENV,
        agent_registry._VAULT_SECRET_ID_ENV,
    ):
        monkeypatch.delenv(var, raising=False)


# ── get_registry(): DSN source selection ───────────────────────────────


async def test_get_registry_uses_vault_when_configured(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example:8200")
    monkeypatch.setenv(agent_registry._VAULT_ROLE_ID_ENV, "role-id")
    monkeypatch.setenv(agent_registry._VAULT_SECRET_ID_ENV, "secret-id")
    monkeypatch.setenv("AGENT_REGISTRY_DSN", "postgresql://env-fallback/should-not-be-used")

    monkeypatch.setattr(
        agent_registry,
        "_read_dsn_from_vault_sync",
        lambda: "postgresql://from-vault/agent_platform",
    )

    seen = {}

    async def fake_build_pool(dsn):
        seen["dsn"] = dsn
        return object()

    monkeypatch.setattr(agent_registry, "_build_pool", fake_build_pool)

    client = await agent_registry.get_registry()
    assert client.enabled
    assert seen["dsn"] == "postgresql://from-vault/agent_platform"


async def test_get_registry_falls_back_to_env_when_vault_read_fails(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example:8200")
    monkeypatch.setenv(agent_registry._VAULT_ROLE_ID_ENV, "role-id")
    monkeypatch.setenv(agent_registry._VAULT_SECRET_ID_ENV, "secret-id")
    monkeypatch.setenv("AGENT_REGISTRY_DSN", "postgresql://env-fallback/agent_platform")

    # Vault reachable but the read failed for any reason — fail-open contract.
    monkeypatch.setattr(agent_registry, "_read_dsn_from_vault_sync", lambda: None)

    seen = {}

    async def fake_build_pool(dsn):
        seen["dsn"] = dsn
        return object()

    monkeypatch.setattr(agent_registry, "_build_pool", fake_build_pool)

    client = await agent_registry.get_registry()
    assert client.enabled
    assert seen["dsn"] == "postgresql://env-fallback/agent_platform"


async def test_get_registry_env_only_when_vault_unconfigured(monkeypatch):
    monkeypatch.setenv("AGENT_REGISTRY_DSN", "postgresql://env-only/agent_platform")
    # No VAULT_ADDR / role / secret set — _vault_configured() must be False.

    called = {"vault": False}

    def fake_vault_read():
        called["vault"] = True
        return "should-not-be-called"

    monkeypatch.setattr(agent_registry, "_read_dsn_from_vault_sync", fake_vault_read)

    seen = {}

    async def fake_build_pool(dsn):
        seen["dsn"] = dsn
        return object()

    monkeypatch.setattr(agent_registry, "_build_pool", fake_build_pool)

    client = await agent_registry.get_registry()
    assert client.enabled
    assert seen["dsn"] == "postgresql://env-only/agent_platform"
    assert called["vault"] is False


async def test_get_registry_disabled_when_nothing_configured():
    # Neither Vault nor plaintext env configured => disabled client, no crash.
    client = await agent_registry.get_registry()
    assert client.enabled is False


# ── _vault_configured() ─────────────────────────────────────────────────


def test_vault_configured_requires_all_three_vars(monkeypatch):
    assert agent_registry._vault_configured() is False

    monkeypatch.setenv("VAULT_ADDR", "https://vault.example:8200")
    assert agent_registry._vault_configured() is False

    monkeypatch.setenv(agent_registry._VAULT_ROLE_ID_ENV, "role-id")
    assert agent_registry._vault_configured() is False

    monkeypatch.setenv(agent_registry._VAULT_SECRET_ID_ENV, "secret-id")
    assert agent_registry._vault_configured() is True


# ── _read_dsn_from_vault_sync(): AppRole login + KV-v2 read ─────────────


def _fake_hvac_client(*, dsn_value=None, login_error=None, revoke_error=None, revoked=None):
    """``revoked`` (if given) is a list this appends to when revoke_self() is called."""

    class FakeKvV2:
        def read_secret_version(self, path):
            assert path == agent_registry._VAULT_KV_PATH
            data = {"AGENT_REGISTRY_DSN": dsn_value} if dsn_value else {}
            return {"data": {"data": data}}

    def _revoke_self():
        if revoked is not None:
            revoked.append(True)
        if revoke_error:
            raise revoke_error

    class FakeAuth:
        approle = types.SimpleNamespace(
            login=lambda role_id, secret_id: (
                (_ for _ in ()).throw(login_error) if login_error else None
            )
        )
        token = types.SimpleNamespace(revoke_self=_revoke_self)

    class FakeClient:
        def __init__(self, url):
            self.url = url
            self.auth = FakeAuth()
            self.secrets = types.SimpleNamespace(kv=types.SimpleNamespace(v2=FakeKvV2()))

    return types.SimpleNamespace(Client=FakeClient)


def test_read_dsn_from_vault_sync_missing_hvac(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example:8200")
    monkeypatch.setenv(agent_registry._VAULT_ROLE_ID_ENV, "role-id")
    monkeypatch.setenv(agent_registry._VAULT_SECRET_ID_ENV, "secret-id")
    # sys.modules[name] = None forces `import hvac` to raise ModuleNotFoundError
    # regardless of whether hvac is actually installed in this environment.
    monkeypatch.setitem(sys.modules, "hvac", None)
    assert agent_registry._read_dsn_from_vault_sync() is None


def test_read_dsn_from_vault_sync_success(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example:8200")
    monkeypatch.setenv(agent_registry._VAULT_ROLE_ID_ENV, "role-id")
    monkeypatch.setenv(agent_registry._VAULT_SECRET_ID_ENV, "secret-id")
    revoked = []
    monkeypatch.setitem(
        sys.modules,
        "hvac",
        _fake_hvac_client(dsn_value="postgresql://from-vault/agent_platform", revoked=revoked),
    )

    assert agent_registry._read_dsn_from_vault_sync() == "postgresql://from-vault/agent_platform"
    # SMCP-42 audit INFO finding: the one-shot reader token must be revoked
    # server-side after a successful read, not left valid for its full TTL.
    assert revoked == [True]


def test_read_dsn_from_vault_sync_revoke_failure_does_not_fail_the_read(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example:8200")
    monkeypatch.setenv(agent_registry._VAULT_ROLE_ID_ENV, "role-id")
    monkeypatch.setenv(agent_registry._VAULT_SECRET_ID_ENV, "secret-id")
    monkeypatch.setitem(
        sys.modules,
        "hvac",
        _fake_hvac_client(
            dsn_value="postgresql://from-vault/agent_platform",
            revoke_error=RuntimeError("revoke endpoint unreachable"),
        ),
    )

    # A revoke failure must not turn a successful DSN read into a failure.
    assert agent_registry._read_dsn_from_vault_sync() == "postgresql://from-vault/agent_platform"


def test_read_dsn_from_vault_sync_missing_key_returns_none(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example:8200")
    monkeypatch.setenv(agent_registry._VAULT_ROLE_ID_ENV, "role-id")
    monkeypatch.setenv(agent_registry._VAULT_SECRET_ID_ENV, "secret-id")
    monkeypatch.setitem(sys.modules, "hvac", _fake_hvac_client(dsn_value=None))

    assert agent_registry._read_dsn_from_vault_sync() is None


def test_read_dsn_from_vault_sync_login_failure_returns_none(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example:8200")
    monkeypatch.setenv(agent_registry._VAULT_ROLE_ID_ENV, "role-id")
    monkeypatch.setenv(agent_registry._VAULT_SECRET_ID_ENV, "secret-id")
    monkeypatch.setitem(
        sys.modules, "hvac", _fake_hvac_client(login_error=RuntimeError("connection refused"))
    )

    assert agent_registry._read_dsn_from_vault_sync() is None
