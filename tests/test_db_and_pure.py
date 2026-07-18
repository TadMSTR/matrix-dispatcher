"""Unit tests for dispatcher DB helpers and pure functions.

Covers the untested-by-feature-work surface: SQLite helpers, the security-path
UUID filter + env allowlist + credential fail-fast, transcript readers, text
chunking, and retention cleanup.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import matrix_dispatcher.app as dispatcher
import matrix_dispatcher.config as config


@pytest.fixture
def db():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Match production (open_db enables this): without FK enforcement the retention
    # cleanup ordering bug is invisible to tests — this parity is what makes
    # test_run_cleanup_* a real regression guard for the event_aliases FK.
    conn.execute("PRAGMA foreign_keys=ON")
    dispatcher.init_db(conn)
    yield conn
    conn.close()


# --------------------------------------------------------------------------- #
# config + db bootstrap
# --------------------------------------------------------------------------- #


def test_load_config_reads_yaml(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yml"
    cfg.write_text("homeserver: https://h\nagents: {}\n")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    assert dispatcher.load_config()["homeserver"] == "https://h"


def test_open_db_creates_file_and_pragmas(tmp_path, monkeypatch):
    db_path = tmp_path / "data" / "sessions.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    conn = dispatcher.open_db()
    try:
        assert db_path.exists()
        # WAL mode set
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        # 0o600 tightening applied
        assert oct(os.stat(db_path).st_mode)[-3:] == "600"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# poll-state + v1 migration
# --------------------------------------------------------------------------- #


def test_get_set_since_roundtrip(db):
    assert dispatcher.get_since(db) is None
    dispatcher.set_since(db, "s_batch_1")
    assert dispatcher.get_since(db) == "s_batch_1"
    dispatcher.set_since(db, "s_batch_2")  # REPLACE path
    assert dispatcher.get_since(db) == "s_batch_2"


def test_migrate_v1_tokens_no_file_noop(tmp_path, monkeypatch, db):
    monkeypatch.setattr(config, "POLL_TOKEN_PATH", tmp_path / "absent.json")
    dispatcher.migrate_v1_tokens(db)
    assert dispatcher.get_since(db) is None


def test_migrate_v1_tokens_imports_and_unlinks(tmp_path, monkeypatch, db):
    tok = tmp_path / "poll-tokens.json"
    tok.write_text(json.dumps({"global_since": "batch-xyz"}))
    monkeypatch.setattr(config, "POLL_TOKEN_PATH", tok)
    dispatcher.migrate_v1_tokens(db)
    assert dispatcher.get_since(db) == "batch-xyz"
    assert not tok.exists()  # consumed


def test_migrate_v1_tokens_bad_json_is_skipped(tmp_path, monkeypatch, db):
    tok = tmp_path / "poll-tokens.json"
    tok.write_text("{not json")
    monkeypatch.setattr(config, "POLL_TOKEN_PATH", tok)
    dispatcher.migrate_v1_tokens(db)  # must not raise
    assert dispatcher.get_since(db) is None


# --------------------------------------------------------------------------- #
# sessions + aliases
# --------------------------------------------------------------------------- #


def test_get_session_by_event_direct_alias_and_miss(db):
    dispatcher.insert_session(db, "$root", "!r:example.org", "sysadmin", "sess-1")
    # direct hit on thread_root_id
    assert dispatcher.get_session_by_event(db, "$root")["session_id"] == "sess-1"
    # alias hit
    dispatcher.register_alias(db, "$ack", "$root")
    assert dispatcher.get_session_by_event(db, "$ack")["session_id"] == "sess-1"
    # miss
    assert dispatcher.get_session_by_event(db, "$nope") is None


def test_register_alias_empty_event_id_noop(db):
    dispatcher.insert_session(db, "$root", "!r:example.org", "sysadmin", "sess-1")
    dispatcher.register_alias(db, "", "$root")
    # no alias row created
    rows = db.execute("SELECT * FROM event_aliases").fetchall()
    assert rows == []


def test_touch_session_updates_last_used(db):
    dispatcher.insert_session(db, "$root", "!r:example.org", "sysadmin", "sess-1")
    db.execute("UPDATE sessions SET last_used_at = 0 WHERE thread_root_id = '$root'")
    db.commit()
    dispatcher.touch_session(db, "$root")
    assert dispatcher.get_session(db, "$root")["last_used_at"] > 0


def test_pending_approval_store_roundtrip(db):
    dispatcher.insert_pending_approval(db, "a1", "$root", "sess", "!r:example.org", "tool")
    rows = dispatcher.get_pending_approvals(db)
    assert len(rows) == 1 and rows[0]["approval_id"] == "a1"
    dispatcher.delete_pending_approval(db, "a1")
    assert dispatcher.get_pending_approvals(db) == []


# --------------------------------------------------------------------------- #
# credentials — fail-fast
# --------------------------------------------------------------------------- #


def test_get_dispatcher_credentials_success(monkeypatch):
    monkeypatch.setenv("DISPATCHER_HOMESERVER", "https://h")
    monkeypatch.setenv("DISPATCHER_USER_ID", "@bot:h")
    monkeypatch.setenv("DISPATCHER_ACCESS_TOKEN", "tok")
    assert dispatcher.get_dispatcher_credentials() == ("https://h", "@bot:h", "tok")


def test_get_dispatcher_credentials_missing_exits(monkeypatch):
    for v in ("DISPATCHER_HOMESERVER", "DISPATCHER_USER_ID", "DISPATCHER_ACCESS_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(SystemExit) as exc:
        dispatcher.get_dispatcher_credentials()
    assert exc.value.code == 1


# --------------------------------------------------------------------------- #
# _minimal_env — security allowlist (no CLAUDE_* secret leak)
# --------------------------------------------------------------------------- #


def test_minimal_env_allowlist_excludes_secrets(monkeypatch):
    monkeypatch.setenv("HOME", "/home/agent")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CLAUDE_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-should-not-leak")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    env = dispatcher._minimal_env("sysadmin")

    assert env["AGENT_ID"] == "sysadmin"
    assert env["AGENT_TYPE"] == "sysadmin"
    assert env["HOME"] == "/home/agent"
    # The whole point: no CLAUDE_* secret is forwarded.
    assert "CLAUDE_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert not any(k.startswith("CLAUDE_") and k != "CLAUDE_CONFIG_DIR" for k in env)


def test_minimal_env_forwards_config_dir_when_present(monkeypatch):
    monkeypatch.setenv("HOME", "/home/agent")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/agent/.claude")
    env = dispatcher._minimal_env("research")
    assert env["CLAUDE_CONFIG_DIR"] == "/home/agent/.claude"


# --------------------------------------------------------------------------- #
# split_on_paragraphs
# --------------------------------------------------------------------------- #


def test_split_short_text_single_chunk():
    assert dispatcher.split_on_paragraphs("hi", 100) == ["hi"]


def test_split_groups_paragraphs_under_limit():
    text = "aaaaa\n\nbbbbb\n\nccccc"  # 19 chars
    chunks = dispatcher.split_on_paragraphs(text, 12)
    assert chunks == ["aaaaa\n\nbbbbb", "ccccc"]
    assert all(len(c) <= 12 for c in chunks)


def test_split_hard_splits_oversized_paragraph():
    chunks = dispatcher.split_on_paragraphs("x" * 25, 10)
    assert chunks == ["x" * 10, "x" * 10, "x" * 5]


# --------------------------------------------------------------------------- #
# extract_thread_root
# --------------------------------------------------------------------------- #


def _evt(source):
    return SimpleNamespace(source=source, event_id="$e")


def test_extract_thread_root_room_root_is_none():
    assert dispatcher.extract_thread_root(_evt({"content": {}})) is None


def test_extract_thread_root_m_thread():
    src = {"content": {"m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}}}
    assert dispatcher.extract_thread_root(_evt(src)) == "$root"


def test_extract_thread_root_in_reply_to():
    src = {"content": {"m.relates_to": {"m.in_reply_to": {"event_id": "$parent"}}}}
    assert dispatcher.extract_thread_root(_evt(src)) == "$parent"


def test_extract_thread_root_relates_not_dict():
    src = {"content": {"m.relates_to": "garbage"}}
    assert dispatcher.extract_thread_root(_evt(src)) is None


def test_extract_thread_root_malformed_non_string(caplog):
    src = {"content": {"m.relates_to": {"rel_type": "m.thread", "event_id": 12345}}}
    assert dispatcher.extract_thread_root(_evt(src)) is None


# --------------------------------------------------------------------------- #
# transcript helpers
# --------------------------------------------------------------------------- #


def test_project_jsonl_dir_encoding(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    got = dispatcher.project_jsonl_dir("/home/user/.claude/projects/dev")
    assert got == tmp_path / ".claude" / "projects" / "-home-user-.claude-projects-dev"


def test_extract_text_variants():
    assert dispatcher._extract_text("plain") == "plain"
    blocks = [{"type": "text", "text": "a"}, {"type": "tool_use"}, {"type": "text", "text": "b"}]
    assert dispatcher._extract_text(blocks) == "a\nb"
    assert dispatcher._extract_text(42) == ""


def _write_transcript(home: Path, project_dir: str, session_id: str, rows: list[dict]) -> None:
    d = dispatcher.project_jsonl_dir(project_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.jsonl").write_text("\n".join(json.dumps(r) for r in rows))


def test_read_first_user_message_strips_prefix_and_truncates(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    pd = "/tmp/proj"
    long = "z" * 200
    _write_transcript(
        tmp_path,
        pd,
        "sess",
        [
            {"type": "user", "message": {"content": f"[Invoked via Matrix ...]\n\n{long}"}},
        ],
    )
    out = dispatcher.read_first_user_message("sess", pd, max_len=80)
    assert out.startswith("z" * 80)
    assert out.endswith("…")


def test_read_first_user_message_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert dispatcher.read_first_user_message("nope", "/tmp/proj") == "(transcript missing)"


def test_read_first_user_message_no_user_turn(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    pd = "/tmp/proj"
    _write_transcript(tmp_path, pd, "s2", [{"type": "assistant", "message": {"content": "hi"}}])
    assert dispatcher.read_first_user_message("s2", pd) == "(no user message)"


def test_read_last_n_turns_formats(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    pd = "/tmp/proj"
    _write_transcript(
        tmp_path,
        pd,
        "s3",
        [
            {"type": "user", "message": {"content": "[Invoked via Matrix x]\n\nhello"}},
            {"type": "assistant", "message": {"content": "world"}},
        ],
    )
    out = dispatcher.read_last_n_turns("s3", pd, 5)
    assert "**user:**\nhello" in out
    assert "**assistant:**\nworld" in out


def test_read_last_n_turns_missing_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert dispatcher.read_last_n_turns("nope", "/tmp/proj", 3) == ""


# --------------------------------------------------------------------------- #
# find_unmirrored_session_id — security: UUID-only argv filter
# --------------------------------------------------------------------------- #


def test_find_unmirrored_dir_missing(monkeypatch, tmp_path, db):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert dispatcher.find_unmirrored_session_id(db, "/tmp/absent", "!r:example.org") is None


def test_find_unmirrored_rejects_non_uuid_and_picks_newest(monkeypatch, tmp_path, db):
    import uuid as uuidmod

    monkeypatch.setenv("HOME", str(tmp_path))
    pd = "/tmp/proj"
    d = dispatcher.project_jsonl_dir(pd)
    d.mkdir(parents=True, exist_ok=True)
    older = str(uuidmod.uuid4())
    newer = str(uuidmod.uuid4())
    known = str(uuidmod.uuid4())
    for stem in (older, newer, known, "-rf", "notauuid"):
        (d / f"{stem}.jsonl").write_text("{}")
    # make `newer` the most recent
    os.utime(d / f"{older}.jsonl", (1000, 1000))
    os.utime(d / f"{newer}.jsonl", (2000, 2000))
    # `known` is already tracked for this room -> excluded
    dispatcher.insert_session(db, "$r", "!r:example.org", "sysadmin", known)

    got = dispatcher.find_unmirrored_session_id(db, pd, "!r:example.org")
    assert got == newer  # newest UUID that isn't already known; non-UUIDs rejected


def test_find_unmirrored_all_known_returns_none(monkeypatch, tmp_path, db):
    import uuid as uuidmod

    monkeypatch.setenv("HOME", str(tmp_path))
    pd = "/tmp/proj"
    d = dispatcher.project_jsonl_dir(pd)
    d.mkdir(parents=True, exist_ok=True)
    sid = str(uuidmod.uuid4())
    (d / f"{sid}.jsonl").write_text("{}")
    dispatcher.insert_session(db, "$r", "!r:example.org", "sysadmin", sid)
    assert dispatcher.find_unmirrored_session_id(db, pd, "!r:example.org") is None


# --------------------------------------------------------------------------- #
# retention cleanup
# --------------------------------------------------------------------------- #


def test_run_cleanup_deletes_old_sessions_and_orphan_aliases(db):
    now = int(time.time())
    # fresh session (kept) + old session (purged)
    dispatcher.insert_session(db, "$fresh", "!r:example.org", "sysadmin", "s-fresh")
    dispatcher.insert_session(db, "$old", "!r:example.org", "sysadmin", "s-old")
    dispatcher.register_alias(db, "$a_fresh", "$fresh")
    dispatcher.register_alias(db, "$a_old", "$old")
    db.execute(
        "UPDATE sessions SET last_used_at = ? WHERE thread_root_id = '$old'", (now - 40 * 86400,)
    )
    db.commit()

    sessions_deleted, aliases_deleted = dispatcher.run_cleanup(db, retention_days=30)

    assert sessions_deleted == 1
    assert aliases_deleted == 1  # the orphaned alias for the purged session
    assert dispatcher.get_session(db, "$old") is None
    assert dispatcher.get_session(db, "$fresh") is not None


def test_run_cleanup_deletes_referenced_aliases_before_sessions(db):
    """Regression: run_cleanup must not raise FOREIGN KEY constraint failed.

    event_aliases.thread_root_id REFERENCES sessions(thread_root_id) with no cascade;
    an expiring session with one or more registered aliases previously aborted the
    whole DELETE (IntegrityError) so retention never ran. With FK enforcement on
    (see the db fixture) this test fails against the pre-fix ordering.
    """
    now = int(time.time())
    dispatcher.insert_session(db, "$old", "!r:example.org", "sysadmin", "s-old")
    # An expiring session typically has several aliases (ack + each response chunk).
    dispatcher.register_alias(db, "$ack", "$old")
    dispatcher.register_alias(db, "$chunk1", "$old")
    dispatcher.register_alias(db, "$chunk2", "$old")
    db.execute(
        "UPDATE sessions SET last_used_at = ? WHERE thread_root_id = '$old'",
        (now - 40 * 86400,),
    )
    db.commit()

    # Must not raise sqlite3.IntegrityError.
    sessions_deleted, aliases_deleted = dispatcher.run_cleanup(db, retention_days=30)

    assert sessions_deleted == 1
    assert aliases_deleted == 3
    assert dispatcher.get_session(db, "$old") is None
    assert db.execute("SELECT COUNT(*) FROM event_aliases").fetchone()[0] == 0
