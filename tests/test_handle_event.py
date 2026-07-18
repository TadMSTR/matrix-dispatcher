"""Tests for dispatcher.handle_event dispatch outcomes (MDISP-6).

Focus: an orphaned reply (a reply whose thread root has no tracked session)
must never spawn a new claude -p session. Foreign-bot replies are ignored
silently; replies to our own expired threads get a hint; genuine room-root
messages still spawn; tracked-thread replies still resume.

Assert on the dispatch *outcome* (spawn/resume called, messages posted, log
action=) rather than launching real subprocesses.
"""

import logging
import sqlite3
from types import SimpleNamespace

import pytest

import dispatcher

TRUSTED = "@admin:example.org"
BOT_USER_ID = "@dispatcher-bot:example.org"
FOREIGN = "@other-bot:example.org"
MENTION = "@ted"
ROOM = "!room:example.org"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _SendResponse:
    def __init__(self, event_id: str) -> None:
        self.event_id = event_id


class _EventObj:
    def __init__(self, sender: str) -> None:
        self.sender = sender


class _GetEventResponse:
    """Mimics nio RoomGetEventResponse (has .event)."""

    def __init__(self, sender: str) -> None:
        self.event = _EventObj(sender)


class FakeClient:
    """Minimal AsyncClient stand-in.

    Records room_send calls; room_get_event returns a configurable parent
    sender or raises (to exercise the fail-closed path).
    """

    def __init__(self, parent_sender: str | None = None, get_event_raises: bool = False) -> None:
        self.sent: list[dict] = []
        self._parent_sender = parent_sender
        self._get_event_raises = get_event_raises
        self._counter = 0

    async def room_send(self, room_id, message_type, content):
        self._counter += 1
        relates = content.get("m.relates_to", {})
        reply_to = relates.get("m.in_reply_to", {}).get("event_id")
        self.sent.append({"room_id": room_id, "body": content.get("body"), "reply_to": reply_to})
        return _SendResponse(f"$sent{self._counter}")

    async def room_get_event(self, room_id, event_id):
        if self._get_event_raises:
            raise RuntimeError("simulated room_get_event failure")
        return _GetEventResponse(self._parent_sender)


class Spy:
    """Async callable that records calls and returns a fixed (exit_code, output)."""

    def __init__(self, ret=(0, "ok")) -> None:
        self.calls: list[tuple] = []
        self.ret = ret

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.ret

    @property
    def called(self) -> bool:
        return bool(self.calls)


def make_event(sender: str, body: str, event_id: str, reply_to: str | None = None):
    """Build a RoomMessageText-shaped object handle_event understands.

    handle_event only reads .sender/.body/.event_id and, via
    extract_thread_root, .source["content"]["m.relates_to"].
    """
    content: dict = {"body": body}
    if reply_to is not None:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    return SimpleNamespace(sender=sender, body=body, event_id=event_id, source={"content": content})


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
    # Clear per-room rate-limit state so spawn tests aren't throttled.
    dispatcher._last_spawn_at.clear()
    return SimpleNamespace(spawn=spawn, resume=resume)


async def _dispatch(client, event, db, bot_user_id=BOT_USER_ID):
    await dispatcher.handle_event(
        client=client,
        room_id=ROOM,
        event=event,
        trusted_sender=TRUSTED,
        mention_user=MENTION,
        max_message_length=4000,
        agent_name="sysadmin",
        project_dir="/tmp/does-not-matter",
        db=db,
        bot_user_id=bot_user_id,
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


async def test_foreign_bot_reply_never_spawns(db, spies, caplog):
    """The reported bug: replying to a foreign-bot post must not spawn."""
    client = FakeClient(parent_sender=FOREIGN)
    event = make_event(TRUSTED, "approve", "$reply1", reply_to="$foreign_event")

    with caplog.at_level(logging.INFO, logger="dispatcher"):
        await _dispatch(client, event, db)

    assert not spies.spawn.called
    assert not spies.resume.called
    assert client.sent == []  # no room noise
    assert "action=foreign_reply_ignored" in caplog.text


async def test_expired_own_thread_reply_hints_no_spawn(db, spies, caplog):
    """Reply to our own (expired) thread posts a hint, never spawns."""
    client = FakeClient(parent_sender=BOT_USER_ID)
    event = make_event(TRUSTED, "continue please", "$reply2", reply_to="$our_old_event")

    with caplog.at_level(logging.INFO, logger="dispatcher"):
        await _dispatch(client, event, db)

    assert not spies.spawn.called
    assert not spies.resume.called
    assert len(client.sent) == 1
    assert "No active session for that thread" in client.sent[0]["body"]
    assert client.sent[0]["reply_to"] == "$reply2"
    assert "action=expired_thread_reply" in caplog.text


async def test_tracked_thread_reply_resumes(db, spies):
    """A reply to a dispatcher-tracked thread still resumes (regression guard)."""
    session_id = "sess-1234-5678-abcd"
    dispatcher.insert_session(db, "$root1", ROOM, "sysadmin", session_id)
    client = FakeClient()
    event = make_event(TRUSTED, "next step", "$reply3", reply_to="$root1")

    await _dispatch(client, event, db)

    assert spies.resume.called
    assert not spies.spawn.called
    # resume_claude(session_id, user_message, ...) — first positional arg
    assert spies.resume.calls[0][0][0] == session_id


async def test_room_root_message_spawns(db, spies, caplog):
    """A genuine top-level message still spawns (regression guard)."""
    client = FakeClient()
    event = make_event(TRUSTED, "hello agent", "$root2", reply_to=None)

    with caplog.at_level(logging.INFO, logger="dispatcher"):
        await _dispatch(client, event, db)

    assert spies.spawn.called
    assert not spies.resume.called
    assert "action=spawn_start" in caplog.text


async def test_room_get_event_failure_treated_as_foreign(db, spies, caplog):
    """Fail-closed: if the parent event can't be fetched, ignore, never spawn."""
    client = FakeClient(get_event_raises=True)
    event = make_event(TRUSTED, "approve", "$reply4", reply_to="$unknown_event")

    with caplog.at_level(logging.INFO, logger="dispatcher"):
        await _dispatch(client, event, db)

    assert not spies.spawn.called
    assert not spies.resume.called
    assert client.sent == []
    assert "action=foreign_reply_ignored" in caplog.text
    assert "sender=unknown" in caplog.text


async def test_empty_bot_user_id_degrades_to_foreign(db, spies, caplog):
    """Fail-closed: an unset/empty bot_user_id never spawns and never mislabels
    a foreign post as 'ours' — the `if bot_user_id and ...` guard falls through
    to the foreign/silent-ignore branch. Regression guard for that short-circuit.
    """
    client = FakeClient(parent_sender=FOREIGN)
    event = make_event(TRUSTED, "approve", "$reply6", reply_to="$foreign_event")

    with caplog.at_level(logging.INFO, logger="dispatcher"):
        await _dispatch(client, event, db, bot_user_id="")

    assert not spies.spawn.called
    assert not spies.resume.called
    assert client.sent == []  # no hint, no spawn
    assert "action=foreign_reply_ignored" in caplog.text
    assert "action=expired_thread_reply" not in caplog.text


async def test_non_trusted_sender_ignored(db, spies):
    """Sender gate still discards non-trusted senders before any dispatch."""
    client = FakeClient(parent_sender=FOREIGN)
    event = make_event("@stranger:example.org", "approve", "$reply5", reply_to="$x")

    await _dispatch(client, event, db)

    assert not spies.spawn.called
    assert not spies.resume.called
    assert client.sent == []
