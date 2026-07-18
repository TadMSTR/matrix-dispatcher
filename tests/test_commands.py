"""Tests for bang-command handlers, _post_response, and handle_event routing.

Assert on posted-message outcomes and DB side effects with a fake Matrix client
and monkeypatched spawn/resume/transcript readers — no real subprocess or I/O.
"""

from __future__ import annotations

import signal
import sqlite3
from types import SimpleNamespace

import pytest

import matrix_dispatcher.app as dispatcher
import matrix_dispatcher.runner as runner
import matrix_dispatcher.transcripts as transcripts

TRUSTED = "@admin:example.org"
BOT = "@dispatcher-bot:example.org"
MENTION = "@ted"
ROOM = "!room:example.org"
AGENT = "sysadmin"
PROJECT = "/tmp/proj"


class _SendResponse:
    def __init__(self, event_id: str) -> None:
        self.event_id = event_id


class FakeClient:
    def __init__(self, parent_sender: str | None = None) -> None:
        self.sent: list[dict] = []
        self._parent_sender = parent_sender
        self._counter = 0

    async def room_send(self, room_id, message_type, content):
        self._counter += 1
        relates = content.get("m.relates_to", {})
        reply_to = relates.get("m.in_reply_to", {}).get("event_id")
        self.sent.append({"room_id": room_id, "body": content.get("body"), "reply_to": reply_to})
        return _SendResponse(f"$sent{self._counter}")

    async def room_get_event(self, room_id, event_id):
        return SimpleNamespace(event=SimpleNamespace(sender=self._parent_sender))


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


class FakeProc:
    def __init__(self, pid=4242, raise_lookup=False) -> None:
        self.pid = pid
        self.signals: list[int] = []
        self._raise = raise_lookup

    def send_signal(self, sig) -> None:
        if self._raise:
            raise ProcessLookupError()
        self.signals.append(sig)


def make_event(sender: str, body: str, event_id: str, reply_to: str | None = None):
    content: dict = {"body": body}
    if reply_to is not None:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    return SimpleNamespace(sender=sender, body=body, event_id=event_id, source={"content": content})


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dispatcher.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _clean_runtime_state():
    dispatcher._active_processes.clear()
    dispatcher._room_locks.clear()
    dispatcher._last_spawn_at.clear()
    dispatcher._handlers.clear()
    yield
    dispatcher._active_processes.clear()
    dispatcher._room_locks.clear()
    dispatcher._last_spawn_at.clear()


@pytest.fixture
def spies(monkeypatch):
    spawn = Spy()
    resume = Spy()
    monkeypatch.setattr(runner, "spawn_claude", spawn)
    monkeypatch.setattr(runner, "resume_claude", resume)
    return SimpleNamespace(spawn=spawn, resume=resume)


async def _dispatch(client, event, db, **kw):
    await dispatcher.handle_event(
        client=client,
        room_id=ROOM,
        event=event,
        trusted_sender=TRUSTED,
        mention_user=MENTION,
        max_message_length=4000,
        agent_name=AGENT,
        project_dir=PROJECT,
        db=db,
        bot_user_id=BOT,
        **kw,
    )


# --------------------------------------------------------------------------- #
# _post_response
# --------------------------------------------------------------------------- #


async def test_post_response_error_path(db):
    client = FakeClient()
    dispatcher.insert_session(db, "$root", ROOM, AGENT, "sess-abcdefgh")
    await dispatcher._post_response(
        client,
        ROOM,
        "boom detail",
        1,
        "sess-abcdefgh",
        MENTION,
        4000,
        reply_target="$root",
        action="spawn",
        db=db,
        thread_root_id="$root",
    )
    assert len(client.sent) == 1
    assert "spawn error" in client.sent[0]["body"]
    assert "boom detail" in client.sent[0]["body"]
    # alias registered for the error post
    assert db.execute("SELECT COUNT(*) FROM event_aliases").fetchone()[0] == 1


async def test_post_response_success_chunks_and_aliases(db):
    client = FakeClient()
    dispatcher.insert_session(db, "$root", ROOM, AGENT, "sess-abcdefgh")
    output = "a" * 25
    await dispatcher._post_response(
        client,
        ROOM,
        output,
        0,
        "sess-abcdefgh",
        MENTION,
        10,
        reply_target="$root",
        action="spawn",
        db=db,
        thread_root_id="$root",
    )
    assert len(client.sent) >= 3  # chunked at max_len=10
    assert client.sent[0]["body"].startswith(f"{MENTION} ")
    # every chunk registered an alias
    n = db.execute("SELECT COUNT(*) FROM event_aliases").fetchone()[0]
    assert n == len(client.sent)


async def test_post_response_empty_output(db):
    client = FakeClient()
    dispatcher.insert_session(db, "$root", ROOM, AGENT, "sess-abcdefgh")
    await dispatcher._post_response(
        client,
        ROOM,
        "",
        0,
        "sess-abcdefgh",
        MENTION,
        4000,
        reply_target="$root",
        action="resume",
        db=db,
        thread_root_id="$root",
    )
    assert "(no output)" in client.sent[0]["body"]


# --------------------------------------------------------------------------- #
# Individual command handlers
# --------------------------------------------------------------------------- #


async def test_help_command(db):
    client = FakeClient()
    ev = make_event(TRUSTED, "!help", "$e")
    await dispatcher.handle_help_command(client, ROOM, ev, MENTION)
    assert "!sessions" in client.sent[0]["body"]


async def test_sessions_command_empty(db):
    client = FakeClient()
    ev = make_event(TRUSTED, "!sessions", "$e")
    await dispatcher.handle_sessions_command(client, ROOM, ev, MENTION, AGENT, PROJECT, db)
    assert "No sessions yet" in client.sent[0]["body"]


async def test_sessions_command_lists_and_aliases(db, monkeypatch):
    monkeypatch.setattr(transcripts, "read_first_user_message", lambda *a, **k: "summary")
    dispatcher.insert_session(db, "$r1", ROOM, AGENT, "sess-1111aaaa")
    dispatcher.insert_session(db, "$r2", ROOM, AGENT, "sess-2222bbbb")
    client = FakeClient()
    ev = make_event(TRUSTED, "!sessions", "$e")
    await dispatcher.handle_sessions_command(client, ROOM, ev, MENTION, AGENT, PROJECT, db)
    # header + 2 items
    assert len(client.sent) == 3
    assert "Recent sessions" in client.sent[0]["body"]
    # each list item registered an alias so replies resolve to that session
    assert db.execute("SELECT COUNT(*) FROM event_aliases").fetchone()[0] == 2


async def test_recap_command_no_session(db):
    client = FakeClient()
    ev = make_event(TRUSTED, "!recap", "$e")
    await dispatcher.handle_recap_command(client, ROOM, ev, MENTION, PROJECT, db, 4000, "")
    assert "No prior sessions" in client.sent[0]["body"]


async def test_recap_command_no_turns(db, monkeypatch):
    monkeypatch.setattr(transcripts, "read_last_n_turns", lambda *a, **k: "")
    dispatcher.insert_session(db, "$r1", ROOM, AGENT, "sess-1111aaaa")
    client = FakeClient()
    ev = make_event(TRUSTED, "!recap 3", "$e")
    await dispatcher.handle_recap_command(client, ROOM, ev, MENTION, PROJECT, db, 4000, "3")
    assert "no readable turns" in client.sent[0]["body"]


async def test_recap_command_with_turns(db, monkeypatch):
    monkeypatch.setattr(transcripts, "read_last_n_turns", lambda *a, **k: "**user:**\nhi")
    dispatcher.insert_session(db, "$r1", ROOM, AGENT, "sess-1111aaaa")
    client = FakeClient()
    ev = make_event(TRUSTED, "!recap", "$e")
    await dispatcher.handle_recap_command(client, ROOM, ev, MENTION, PROJECT, db, 4000, "")
    assert any("Recap of session" in s["body"] for s in client.sent)


async def test_mirror_command_not_found(db, monkeypatch):
    monkeypatch.setattr(transcripts, "find_unmirrored_session_id", lambda *a, **k: None)
    client = FakeClient()
    ev = make_event(TRUSTED, "!mirror", "$e")
    await dispatcher.handle_mirror_command(client, ROOM, ev, MENTION, AGENT, PROJECT, db)
    assert "No unmirrored" in client.sent[0]["body"]


async def test_mirror_command_links_session(db, monkeypatch):
    sid = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(transcripts, "find_unmirrored_session_id", lambda *a, **k: sid)
    client = FakeClient()
    ev = make_event(TRUSTED, "!mirror", "$mir")
    await dispatcher.handle_mirror_command(client, ROOM, ev, MENTION, AGENT, PROJECT, db)
    assert dispatcher.get_session(db, "$mir")["session_id"] == sid
    assert any("Linked session" in s["body"] for s in client.sent)


async def test_cancel_command_no_proc(db):
    client = FakeClient()
    ev = make_event(TRUSTED, "!cancel", "$e")
    await dispatcher.handle_cancel_command(client, ROOM, ev, MENTION)
    assert "No active session" in client.sent[0]["body"]


async def test_cancel_command_sigterms_active(db):
    proc = FakeProc(pid=999)
    dispatcher._active_processes[ROOM] = proc
    client = FakeClient()
    ev = make_event(TRUSTED, "!cancel", "$e")
    await dispatcher.handle_cancel_command(client, ROOM, ev, MENTION)
    assert signal.SIGTERM in proc.signals
    assert "Sent SIGTERM" in client.sent[0]["body"]


async def test_cancel_command_process_already_gone(db):
    dispatcher._active_processes[ROOM] = FakeProc(raise_lookup=True)
    client = FakeClient()
    ev = make_event(TRUSTED, "!cancel", "$e")
    # ProcessLookupError is suppressed; still posts the SIGTERM notice.
    await dispatcher.handle_cancel_command(client, ROOM, ev, MENTION)
    assert "Sent SIGTERM" in client.sent[0]["body"]


# --------------------------------------------------------------------------- #
# handle_event command routing
# --------------------------------------------------------------------------- #


async def test_route_help(db, spies):
    client = FakeClient()
    await _dispatch(client, make_event(TRUSTED, "!help", "$e"), db)
    assert "!sessions" in client.sent[0]["body"]


async def test_route_unknown_command(db, spies):
    client = FakeClient()
    await _dispatch(client, make_event(TRUSTED, "!frobnicate", "$e"), db)
    assert "Unknown command" in client.sent[0]["body"]
    assert not spies.spawn.called


async def test_route_sessions_empty(db, spies):
    client = FakeClient()
    await _dispatch(client, make_event(TRUSTED, "!sessions", "$e"), db)
    assert "No sessions yet" in client.sent[0]["body"]


async def test_route_cancel(db, spies):
    client = FakeClient()
    await _dispatch(client, make_event(TRUSTED, "!cancel", "$e"), db)
    assert "No active session" in client.sent[0]["body"]


async def test_route_recap(db, spies):
    client = FakeClient()
    await _dispatch(client, make_event(TRUSTED, "!recap", "$e"), db)
    assert "No prior sessions" in client.sent[0]["body"]


async def test_route_mirror(db, spies, monkeypatch):
    monkeypatch.setattr(transcripts, "find_unmirrored_session_id", lambda *a, **k: None)
    client = FakeClient()
    await _dispatch(client, make_event(TRUSTED, "!mirror", "$e"), db)
    assert "No unmirrored" in client.sent[0]["body"]


# --------------------------------------------------------------------------- #
# timeout + rate-limit paths
# --------------------------------------------------------------------------- #


async def test_resume_timeout_posts_notice(db, monkeypatch):
    dispatcher.insert_session(db, "$root", ROOM, AGENT, "sess-abcdefgh")

    async def timeout(*a, **k):
        raise TimeoutError()

    monkeypatch.setattr(runner, "resume_claude", timeout)
    client = FakeClient()
    ev = make_event(TRUSTED, "continue", "$reply", reply_to="$root")
    await _dispatch(client, ev, db)
    assert any("timed out" in s["body"] for s in client.sent)


async def test_spawn_timeout_posts_notice(db, monkeypatch):
    async def timeout(*a, **k):
        raise TimeoutError()

    monkeypatch.setattr(runner, "spawn_claude", timeout)
    client = FakeClient()
    ev = make_event(TRUSTED, "do a thing", "$root")
    await _dispatch(client, ev, db)
    assert any("timed out" in s["body"] for s in client.sent)


async def test_rate_limited(db, spies):
    import time

    dispatcher._last_spawn_at[ROOM] = time.time()  # just spawned
    client = FakeClient()
    ev = make_event(TRUSTED, "another", "$root2")
    await _dispatch(client, ev, db)
    assert any("Rate-limited" in s["body"] for s in client.sent)
    assert not spies.spawn.called
