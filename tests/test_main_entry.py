"""Tests for the console-script entry point (matrix_dispatcher.__main__:main).

The entry wraps the async app.main in a sync callable and routes the one-shot
``--cleanup`` invocation to cli_cleanup. Both branches are covered here with the
async loop and cleanup stubbed out — no Matrix client, no DB.
"""

from __future__ import annotations

import contextlib

import pytest

import matrix_dispatcher.__main__ as entry


def test_main_runs_async_loop(monkeypatch):
    ran = {"async": False, "cleanup": False}

    async def fake_async_main():
        ran["async"] = True

    def fake_run(coro):
        # Drive the coroutine to completion without a real event loop.
        with contextlib.suppress(StopIteration):
            coro.send(None)

    monkeypatch.setattr(entry, "_async_main", fake_async_main)
    monkeypatch.setattr(entry, "cli_cleanup", lambda: ran.__setitem__("cleanup", True))
    monkeypatch.setattr(entry.asyncio, "run", fake_run)
    monkeypatch.setattr(entry.sys, "argv", ["matrix-dispatcher"])

    entry.main()

    assert ran["async"] is True
    assert ran["cleanup"] is False


def test_main_cleanup_branch_exits(monkeypatch):
    monkeypatch.setattr(entry, "cli_cleanup", lambda: 0)
    monkeypatch.setattr(entry.sys, "argv", ["matrix-dispatcher", "--cleanup"])

    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 0
