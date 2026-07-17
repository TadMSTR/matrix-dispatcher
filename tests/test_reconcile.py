"""Tests for HITL resume-on-approval (SMCP-38).

Covers the dispatcher-side feature: post-turn pending detection (with the
duplicate-execution guard), and the reconcile loop that resumes on 'approved',
posts a note on 'denied', expires stale locals, and — critically — resumes
exactly once and no-ops entirely when the registry is disabled (fail-open).

Assert on outcomes (resume called, local pending rows, messages posted) with a
fake registry and monkeypatched spawn/resume — no real DB or subprocess.
"""

import sqlite3
import time
import uuid
from types import SimpleNamespace

import pytest

import dispatcher


TRUSTED = "@admin:example.org"
MENTION = "@ted"
ROOM = "!room:example.org"
AGENT = "sysadmin"

CONFIG = {
    "mention_user": MENTION,
    "max_message_length": 4000,
    "agents": {AGENT: {"room_id": ROOM, "project_dir": "/tmp/does-not-matter"}},
}


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _SendResponse:
    def __init__(self, event_id: str) -> None:
        self.event_id = event_id


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._counter = 0

    async def room_send(self, room_id, message_type, content):
        self._counter += 1
        relates = content.get("m.relates_to", {})
        reply_to = relates.get("m.in_reply_to", {}).get("event_id")
        self.sent.append(
            {"room_id": room_id, "body": content.get("body"), "reply_to": reply_to}
        )
        return _SendResponse(f"$sent{self._counter}")


class Spy:
    def __init__(self, ret=(0, "ok")) -> None:
        self.calls: list[tuple] = []
        self.ret = ret

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.ret

    @property
    def called(self) -> bool:
        return bool(self.calls)


class FakeRegistry:
    """Stand-in for agent_registry.RegistryClient."""

    def __init__(self, enabled=True, pending=None, states=None) -> None:
        self._enabled = enabled
        self._pending = pending
        self._states = states or {}
        self.linked: list[tuple[str, str]] = []
        self.upserts: list[dict] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def upsert_session(self, **kwargs):
        self.upserts.append(kwargs)

    async def find_pending_approval(self, agent_id, since_ts):
        return self._pending

    async def link_session(self, approval_id, session_id):
        self.linked.append((approval_id, session_id))

    async def get_approval_states(self, approval_ids):
        return {k: v for k, v in self._states.items() if k in approval_ids}


def make_event(sender: str, body: str, event_id: str, reply_to: str | None = None):
    content: dict = {"body": body}
    if reply_to is not None:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    return SimpleNamespace(
        sender=sender, body=body, event_id=event_id, source={"content": content}
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dispatcher.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def spies(monkeypatch):
    spawn = Spy()
    resume = Spy()
    monkeypatch.setattr(dispatcher, "spawn_claude", spawn)
    monkeypatch.setattr(dispatcher, "resume_claude", resume)
    dispatcher._last_spawn_at.clear()
    return SimpleNamespace(spawn=spawn, resume=resume)


async def _spawn_via_handle_event(client, event, db, registry):
    await dispatcher.handle_event(
        client=client,
        room_id=ROOM,
        event=event,
        trusted_sender=TRUSTED,
        mention_user=MENTION,
        max_message_length=4000,
        agent_name=AGENT,
        project_dir="/tmp/does-not-matter",
        db=db,
        bot_user_id="@bot:example.org",
        registry=registry,
    )


# --------------------------------------------------------------------------- #
# Post-turn pending detection
# --------------------------------------------------------------------------- #

async def test_spawn_detects_pending_and_links(db, spies):
    """A gated call during a spawn turn is recorded locally and linked in PG."""
    reg = FakeRegistry(pending={
        "approval_id": f"{AGENT}.abc123", "tool_name": "gitea_pr_merge", "created_at": None,
    })
    client = FakeClient()
    event = make_event(TRUSTED, "merge that PR", "$root", reply_to=None)

    await _spawn_via_handle_event(client, event, db, reg)

    rows = dispatcher.get_pending_approvals(db)
    assert len(rows) == 1
    assert rows[0]["approval_id"] == f"{AGENT}.abc123"
    assert rows[0]["tool_name"] == "gitea_pr_merge"
    assert rows[0]["thread_root_id"] == "$root"
    assert rows[0]["room_id"] == ROOM
    # session_id was minted and linked in agent-postgres for the audit trail.
    session_id = rows[0]["session_id"]
    uuid.UUID(session_id)  # is a valid UUID
    assert reg.linked == [(f"{AGENT}.abc123", session_id)]
    # session registry was written before the turn (FK-safe link).
    assert reg.upserts and reg.upserts[0]["session_id"] == session_id


async def test_no_pending_records_nothing(db, spies):
    """No gated call (or already self-resolved) → no local pending row."""
    reg = FakeRegistry(pending=None)
    client = FakeClient()
    event = make_event(TRUSTED, "just chat", "$root2", reply_to=None)

    await _spawn_via_handle_event(client, event, db, reg)

    assert dispatcher.get_pending_approvals(db) == []
    assert reg.linked == []


async def test_disabled_registry_no_detection(db, spies):
    """Fail-open: a disabled registry records nothing and still spawns."""
    reg = FakeRegistry(enabled=False, pending={
        "approval_id": f"{AGENT}.x", "tool_name": "t", "created_at": None,
    })
    client = FakeClient()
    event = make_event(TRUSTED, "hello", "$root3", reply_to=None)

    await _spawn_via_handle_event(client, event, db, reg)

    assert spies.spawn.called
    assert dispatcher.get_pending_approvals(db) == []
    assert reg.linked == []
    assert reg.upserts == []


# --------------------------------------------------------------------------- #
# Reconcile loop
# --------------------------------------------------------------------------- #

def _seed_pending(db, approval_id, session_id="sess-1", tool="gitea_pr_merge"):
    dispatcher.insert_session(db, "$root", ROOM, AGENT, session_id)
    dispatcher.insert_pending_approval(db, approval_id, "$root", session_id, ROOM, tool)


async def test_reconcile_resumes_on_approved(db, spies):
    _seed_pending(db, f"{AGENT}.a1", session_id="sess-approved")
    reg = FakeRegistry(states={f"{AGENT}.a1": "approved"})
    client = FakeClient()
    room_to_agent = dispatcher._build_room_to_agent(CONFIG)

    await dispatcher.reconcile_once(client, db, CONFIG, room_to_agent, reg)

    assert spies.resume.called
    # resume_claude(session_id, nudge, ...) — session first, nudge second positional
    assert spies.resume.calls[0][0][0] == "sess-approved"
    assert "same arguments" in spies.resume.calls[0][0][1]
    assert "otp" not in spies.resume.calls[0][0][1].lower()  # no secret on the nudge
    assert dispatcher.get_pending_approvals(db) == []  # claimed + cleared


async def test_reconcile_resumes_exactly_once(db, spies):
    _seed_pending(db, f"{AGENT}.a2", session_id="sess-once")
    reg = FakeRegistry(states={f"{AGENT}.a2": "approved"})
    client = FakeClient()
    room_to_agent = dispatcher._build_room_to_agent(CONFIG)

    # Two passes: the row is claimed (deleted) on the first, so the second no-ops.
    await dispatcher.reconcile_once(client, db, CONFIG, room_to_agent, reg)
    await dispatcher.reconcile_once(client, db, CONFIG, room_to_agent, reg)

    assert len(spies.resume.calls) == 1


async def test_reconcile_denied_posts_note_no_resume(db, spies):
    _seed_pending(db, f"{AGENT}.a3", tool="gitea_pr_merge")
    reg = FakeRegistry(states={f"{AGENT}.a3": "denied"})
    client = FakeClient()
    room_to_agent = dispatcher._build_room_to_agent(CONFIG)

    await dispatcher.reconcile_once(client, db, CONFIG, room_to_agent, reg)

    assert not spies.resume.called
    assert dispatcher.get_pending_approvals(db) == []
    assert any("denied" in s["body"].lower() for s in client.sent)


async def test_reconcile_still_pending_leaves_row(db, spies):
    _seed_pending(db, f"{AGENT}.a4")
    reg = FakeRegistry(states={f"{AGENT}.a4": "pending"})
    client = FakeClient()
    room_to_agent = dispatcher._build_room_to_agent(CONFIG)

    await dispatcher.reconcile_once(client, db, CONFIG, room_to_agent, reg)

    assert not spies.resume.called
    assert len(dispatcher.get_pending_approvals(db)) == 1  # left for a later pass


async def test_reconcile_expires_stale_local(db, spies):
    _seed_pending(db, f"{AGENT}.a5")
    # Backdate the local row past the expiry window.
    db.execute(
        "UPDATE pending_approvals SET created_at = ? WHERE approval_id = ?",
        (int(time.time()) - dispatcher.PENDING_APPROVAL_EXPIRY_SECONDS - 10, f"{AGENT}.a5"),
    )
    db.commit()
    reg = FakeRegistry(states={f"{AGENT}.a5": "pending"})
    client = FakeClient()
    room_to_agent = dispatcher._build_room_to_agent(CONFIG)

    await dispatcher.reconcile_once(client, db, CONFIG, room_to_agent, reg)

    assert not spies.resume.called
    assert dispatcher.get_pending_approvals(db) == []  # dropped by expiry


async def test_reconcile_once_disabled_registry_noop(db, spies):
    _seed_pending(db, f"{AGENT}.a6")
    reg = FakeRegistry(enabled=False, states={f"{AGENT}.a6": "approved"})
    client = FakeClient()
    room_to_agent = dispatcher._build_room_to_agent(CONFIG)

    await dispatcher.reconcile_once(client, db, CONFIG, room_to_agent, reg)

    assert not spies.resume.called
    assert len(dispatcher.get_pending_approvals(db)) == 1  # untouched


async def test_reconcile_loop_disabled_returns(db, spies):
    """A disabled registry makes the loop return immediately (no busy-loop)."""
    client = FakeClient()
    await dispatcher.reconcile_loop(client, CONFIG, db, FakeRegistry(enabled=False))
    # If it returned, we're here — assert nothing fired.
    assert not spies.resume.called


async def test_reconcile_resume_failure_notifies(db, spies, monkeypatch):
    """Audit LOW-1: a non-timeout resume failure notifies the operator instead of
    silently dropping the (already-claimed) approval."""
    _seed_pending(db, f"{AGENT}.a7", session_id="sess-fail")
    reg = FakeRegistry(states={f"{AGENT}.a7": "approved"})
    client = FakeClient()
    room_to_agent = dispatcher._build_room_to_agent(CONFIG)

    async def boom(*args, **kwargs):
        raise OSError("claude binary transiently missing")

    monkeypatch.setattr(dispatcher, "resume_claude", boom)

    await dispatcher.reconcile_once(client, db, CONFIG, room_to_agent, reg)

    # Row was claimed (deleted) AND an operator notice was posted — not silent.
    assert dispatcher.get_pending_approvals(db) == []
    assert any(
        "retry the request manually" in s["body"].lower() for s in client.sent
    )
