"""Tests for the async loops, entrypoint orchestration, and the real subprocess
runner (`_run_claude`) — the remaining untested surface after the unit + command
suites.

`_run_claude` is exercised against real short-lived processes (/bin/echo, /bin/sh)
so the security-relevant path — minimal-env subprocess exec, active-process
registration, timeout kill — runs end-to-end.
"""

from __future__ import annotations

import asyncio
import signal
import sqlite3
from types import SimpleNamespace

import pytest

import dispatcher

ROOM = "!room:example.org"
AGENT = "sysadmin"
MENTION = "@ted"

CONFIG = {
    "trusted_sender": "@admin:example.org",
    "mention_user": MENTION,
    "bot_user_id": "@dispatcher-bot:example.org",
    "poll_interval_seconds": 0,
    "max_message_length": 4000,
    "agents": {AGENT: {"room_id": ROOM, "project_dir": "/tmp/does-not-matter"}},
}


class _SendResponse:
    def __init__(self, event_id: str) -> None:
        self.event_id = event_id


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self._counter = 0

    async def room_send(self, room_id, message_type, content):
        self._counter += 1
        relates = content.get("m.relates_to", {})
        reply_to = relates.get("m.in_reply_to", {}).get("event_id")
        self.sent.append({"room_id": room_id, "body": content.get("body"), "reply_to": reply_to})
        return _SendResponse(f"$sent{self._counter}")

    async def close(self):
        self.closed = True


class FakeRegistry:
    def __init__(self, enabled=False) -> None:
        self._enabled = enabled
        self.closed = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def close(self):
        self.closed = True


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dispatcher.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _clean_state():
    dispatcher._active_processes.clear()
    dispatcher._room_locks.clear()
    dispatcher._last_spawn_at.clear()
    dispatcher._handlers.clear()
    yield
    dispatcher._active_processes.clear()
    dispatcher._room_locks.clear()
    dispatcher._last_spawn_at.clear()
    dispatcher._handlers.clear()


# --------------------------------------------------------------------------- #
# _run_claude — real subprocess (security path end-to-end)
# --------------------------------------------------------------------------- #


async def test_run_claude_success(tmp_path):
    rc, out = await dispatcher._run_claude(["/bin/echo", "hello world"], str(tmp_path), AGENT, ROOM)
    assert rc == 0
    assert out == "hello world"
    assert ROOM not in dispatcher._active_processes  # deregistered in finally


async def test_run_claude_nonzero_returns_stderr(tmp_path):
    rc, out = await dispatcher._run_claude(
        ["/bin/sh", "-c", "echo boom >&2; exit 3"], str(tmp_path), AGENT, ROOM
    )
    assert rc == 3
    assert "boom" in out


async def test_run_claude_timeout_kills(tmp_path):
    with pytest.raises(TimeoutError):
        await dispatcher._run_claude(["/bin/sleep", "5"], str(tmp_path), AGENT, ROOM, timeout=0.2)
    assert ROOM not in dispatcher._active_processes


async def test_spawn_and_resume_compose_argv(monkeypatch):
    seen = {}

    async def fake_run(args, project_dir, agent_name, room_id, timeout=0):
        seen["args"] = args
        return (0, "ok")

    monkeypatch.setattr(dispatcher, "_run_claude", fake_run)
    await dispatcher.spawn_claude("sid", "prompt", "/tmp", AGENT, ROOM)
    assert seen["args"] == ["claude", "-p", "--session-id", "sid", "prompt"]
    await dispatcher.resume_claude("sid", "msg", "/tmp", AGENT, ROOM)
    assert seen["args"] == ["claude", "-p", "--resume", "sid", "msg"]


# --------------------------------------------------------------------------- #
# transcript error branches
# --------------------------------------------------------------------------- #


def test_read_first_user_message_skips_bad_json_line(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    pd = "/tmp/proj"
    d = dispatcher.project_jsonl_dir(pd)
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.jsonl").write_text('{bad json\n{"type": "user", "message": {"content": "good one"}}')
    assert dispatcher.read_first_user_message("s", pd) == "good one"


def test_read_first_user_message_oserror(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    pd = "/tmp/proj"
    d = dispatcher.project_jsonl_dir(pd)
    d.mkdir(parents=True, exist_ok=True)
    # A directory at the transcript path: exists() true, open() raises IsADirectoryError.
    (d / "s.jsonl").mkdir()
    assert dispatcher.read_first_user_message("s", pd) == "(transcript error)"


def test_read_last_n_turns_skips_bad_json_and_handles_oserror(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    pd = "/tmp/proj"
    d = dispatcher.project_jsonl_dir(pd)
    d.mkdir(parents=True, exist_ok=True)
    (d / "ok.jsonl").write_text('not-json\n{"type": "assistant", "message": {"content": "answer"}}')
    assert "answer" in dispatcher.read_last_n_turns("ok", pd, 5)
    # OSError branch
    (d / "dir.jsonl").mkdir()
    assert dispatcher.read_last_n_turns("dir", pd, 5) == ""


# --------------------------------------------------------------------------- #
# cancel registration-wait loop
# --------------------------------------------------------------------------- #


async def test_cancel_waits_for_registration_then_noops(monkeypatch):
    monkeypatch.setattr(dispatcher, "CANCEL_REGISTRATION_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(dispatcher, "CANCEL_POLL_INTERVAL_SECONDS", 0.01)
    lock = dispatcher._room_lock(ROOM)
    await lock.acquire()  # simulate spawn in-flight, proc not yet registered
    try:
        client = FakeClient()
        ev = SimpleNamespace(event_id="$e")
        await dispatcher.handle_cancel_command(client, ROOM, ev, MENTION)
        assert "No active session" in client.sent[0]["body"]
    finally:
        lock.release()


# --------------------------------------------------------------------------- #
# _resume_on_approval edge branches (via reconcile_once)
# --------------------------------------------------------------------------- #


async def test_resume_on_approval_unconfigured_room_drops(db, monkeypatch):
    resume = _Spy()
    monkeypatch.setattr(dispatcher, "resume_claude", resume)
    other_room = "!gone:example.org"
    dispatcher.insert_session(db, "$r", other_room, AGENT, "sess-x")
    dispatcher.insert_pending_approval(db, "sysadmin.a", "$r", "sess-x", other_room, "tool")

    reg = _EnabledStates({"sysadmin.a": "approved"})
    room_to_agent = dispatcher._build_room_to_agent(CONFIG)  # doesn't include other_room
    await dispatcher.reconcile_once(FakeClient(), db, CONFIG, room_to_agent, reg)

    assert not resume.called
    assert dispatcher.get_pending_approvals(db) == []  # dropped as un-routable


async def test_resume_on_approval_timeout_notifies(db, monkeypatch):
    async def timeout(*a, **k):
        raise TimeoutError()

    monkeypatch.setattr(dispatcher, "resume_claude", timeout)
    dispatcher.insert_session(db, "$r", ROOM, AGENT, "sess-y")
    dispatcher.insert_pending_approval(db, "sysadmin.b", "$r", "sess-y", ROOM, "tool")
    reg = _EnabledStates({"sysadmin.b": "approved"})
    client = FakeClient()
    room_to_agent = dispatcher._build_room_to_agent(CONFIG)
    await dispatcher.reconcile_once(client, db, CONFIG, room_to_agent, reg)
    assert any("timed out resuming" in s["body"] for s in client.sent)
    assert dispatcher.get_pending_approvals(db) == []


class _Spy:
    def __init__(self):
        self.calls = []

    async def __call__(self, *a, **k):
        self.calls.append((a, k))
        return (0, "ok")

    @property
    def called(self):
        return bool(self.calls)


class _EnabledStates:
    def __init__(self, states):
        self._states = states

    @property
    def enabled(self):
        return True

    async def get_approval_states(self, ids):
        return {k: v for k, v in self._states.items() if k in ids}

    async def upsert_session(self, **k):
        pass

    async def find_pending_approval(self, agent_id, since_ts):
        return None

    async def link_session(self, a, s):
        pass


# --------------------------------------------------------------------------- #
# cleanup_loop / reconcile_loop (single iteration via sleep-cancel)
# --------------------------------------------------------------------------- #


async def test_cleanup_loop_runs_then_cancelled(db, monkeypatch):
    ran = {"n": 0}

    def fake_cleanup(_db, _days):
        ran["n"] += 1
        return (0, 0)

    async def cancel_sleep(_):
        raise asyncio.CancelledError()

    monkeypatch.setattr(dispatcher, "run_cleanup", fake_cleanup)
    monkeypatch.setattr(dispatcher.asyncio, "sleep", cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.cleanup_loop(db, 30)
    assert ran["n"] == 1


async def test_cleanup_loop_swallows_cleanup_error(db, monkeypatch):
    def boom(_db, _days):
        raise RuntimeError("db locked")

    async def cancel_sleep(_):
        raise asyncio.CancelledError()

    monkeypatch.setattr(dispatcher, "run_cleanup", boom)
    monkeypatch.setattr(dispatcher.asyncio, "sleep", cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.cleanup_loop(db, 30)  # error logged, not raised


async def test_reconcile_loop_enabled_iterates_then_cancelled(db, monkeypatch):
    calls = {"n": 0}

    async def fake_once(*a, **k):
        calls["n"] += 1

    async def cancel_sleep(_):
        raise asyncio.CancelledError()

    monkeypatch.setattr(dispatcher, "reconcile_once", fake_once)
    monkeypatch.setattr(dispatcher.asyncio, "sleep", cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.reconcile_loop(FakeClient(), CONFIG, db, FakeRegistry(enabled=True))
    assert calls["n"] == 1


async def test_reconcile_loop_swallows_iteration_error(db, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("pg blip")

    async def cancel_sleep(_):
        raise asyncio.CancelledError()

    monkeypatch.setattr(dispatcher, "reconcile_once", boom)
    monkeypatch.setattr(dispatcher.asyncio, "sleep", cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.reconcile_loop(FakeClient(), CONFIG, db, FakeRegistry(enabled=True))


# --------------------------------------------------------------------------- #
# poll_loop
# --------------------------------------------------------------------------- #


class FakeSync:
    def __init__(self, next_batch, join=None):
        self.next_batch = next_batch
        self.rooms = SimpleNamespace(join=join or {})


class FakeMsg:
    def __init__(self, sender="@admin:example.org", body="hi", event_id="$e"):
        self.sender = sender
        self.body = body
        self.event_id = event_id
        self.source = {"content": {"body": body}}


async def test_poll_loop_seeds_and_dispatches(db, monkeypatch):
    monkeypatch.setattr(dispatcher, "SyncResponse", FakeSync)
    monkeypatch.setattr(dispatcher, "RoomMessageText", FakeMsg)

    dispatched = {"n": 0}

    async def fake_handle(**kwargs):
        dispatched["n"] += 1

    monkeypatch.setattr(dispatcher, "handle_event", fake_handle)

    timeline = SimpleNamespace(events=[FakeMsg()])
    room_info = SimpleNamespace(timeline=timeline)

    responses = [
        FakeSync("seed-batch"),  # cold-start seed (since is None)
        FakeSync("batch-1", join={ROOM: room_info}),  # one real iteration
    ]

    class SyncClient:
        def __init__(self):
            self.n = 0

        async def sync(self, timeout=0, since=None, full_state=False):
            if self.n < len(responses):
                r = responses[self.n]
                self.n += 1
                return r
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await dispatcher.poll_loop(SyncClient(), CONFIG, db, FakeRegistry())

    assert dispatcher.get_since(db) == "batch-1"  # main loop advanced the token
    assert dispatched["n"] == 1  # the RoomMessageText was dispatched


async def test_poll_loop_seed_error_and_sync_error(db, monkeypatch):
    monkeypatch.setattr(dispatcher, "SyncResponse", FakeSync)
    monkeypatch.setattr(dispatcher, "RoomMessageText", FakeMsg)

    class Wrong:  # not a FakeSync
        pass

    class SyncClient:
        def __init__(self):
            self.n = 0

        async def sync(self, timeout=0, since=None, full_state=False):
            self.n += 1
            if self.n == 1:
                return Wrong()  # seed error branch
            if self.n == 2:
                return Wrong()  # sync_error branch (isinstance false -> sleep+continue)
            raise asyncio.CancelledError()

    async def instant_sleep(_):
        return None

    monkeypatch.setattr(dispatcher.asyncio, "sleep", instant_sleep)
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.poll_loop(SyncClient(), CONFIG, db, FakeRegistry())


async def test_poll_loop_swallows_unexpected_error(db, monkeypatch):
    monkeypatch.setattr(dispatcher, "SyncResponse", FakeSync)

    class SyncClient:
        def __init__(self):
            self.n = 0

        async def sync(self, timeout=0, since=None, full_state=False):
            self.n += 1
            if self.n == 1:
                return FakeSync("seed")
            if self.n == 2:
                raise RuntimeError("transient sync failure")  # caught, logged
            raise asyncio.CancelledError()

    async def instant_sleep(_):
        return None

    monkeypatch.setattr(dispatcher.asyncio, "sleep", instant_sleep)
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.poll_loop(SyncClient(), CONFIG, db, FakeRegistry())


# --------------------------------------------------------------------------- #
# main() orchestration + cli_cleanup
# --------------------------------------------------------------------------- #


async def test_main_orchestration(monkeypatch, tmp_path):
    monkeypatch.setattr(
        dispatcher, "get_dispatcher_credentials", lambda: ("https://h", "@bot:h", "tok")
    )
    monkeypatch.setattr(
        dispatcher, "load_config", lambda: dict(CONFIG, startup_notification_agent=AGENT)
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(dispatcher, "open_db", lambda: conn)
    monkeypatch.setattr(dispatcher, "migrate_v1_tokens", lambda _db: None)

    fake_client = FakeClient()
    monkeypatch.setattr(dispatcher, "AsyncClient", lambda hs, uid: fake_client)
    reg = FakeRegistry(enabled=False)

    async def fake_get_registry():
        return reg

    monkeypatch.setattr(dispatcher, "get_registry", fake_get_registry)

    async def quick_loop(*a, **k):
        return None

    monkeypatch.setattr(dispatcher, "cleanup_loop", quick_loop)
    monkeypatch.setattr(dispatcher, "reconcile_loop", quick_loop)
    monkeypatch.setattr(dispatcher, "poll_loop", quick_loop)

    # a live subprocess to SIGTERM during shutdown
    class P:
        pid = 7

        def __init__(self):
            self.sig = None

        def send_signal(self, s):
            self.sig = s

    proc = P()
    dispatcher._active_processes[ROOM] = proc

    await dispatcher.main()

    assert any("matrix-dispatcher started" in s["body"] for s in fake_client.sent)
    assert reg.closed
    assert fake_client.closed
    assert proc.sig == signal.SIGTERM


def test_cli_cleanup(monkeypatch, capsys):
    monkeypatch.setattr(dispatcher, "load_config", lambda: {"session_retention_days": 30})
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dispatcher.init_db(conn)
    monkeypatch.setattr(dispatcher, "open_db", lambda: conn)
    rc = dispatcher.cli_cleanup()
    assert rc == 0
    assert "sessions_deleted=" in capsys.readouterr().out
