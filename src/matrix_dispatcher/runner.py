"""Subprocess spawn/resume runner, shared runtime state, and !cancel handling.

Owns the process-local runtime state (per-room locks, active processes,
per-room rate-limit timestamps). These objects are imported *by reference* by
:mod:`app` and :mod:`hitl`, so all mutators share the same dict/set/lock.

Security flag C: subprocesses receive a minimal env allowlist, never os.environ.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from typing import TYPE_CHECKING

from . import config
from .config import SUBPROCESS_TIMEOUT_SECONDS, log
from .matrixio import post_message

if TYPE_CHECKING:
    from nio import AsyncClient, RoomMessageText

# ---------------------------------------------------------------------------
# Runtime state — per-room locks, active processes, rate-limit timestamps.
# Process-local; lost on restart (acceptable — orphaned procs run to completion).
# ---------------------------------------------------------------------------

_room_locks: dict[str, asyncio.Lock] = {}
_active_processes: dict[str, asyncio.subprocess.Process] = {}
_last_spawn_at: dict[str, float] = {}


def _room_lock(room_id: str) -> asyncio.Lock:
    if room_id not in _room_locks:
        _room_locks[room_id] = asyncio.Lock()
    return _room_locks[room_id]


# ---------------------------------------------------------------------------
# Spawn / Resume — security flag C: minimal env allowlist
# ---------------------------------------------------------------------------


def _minimal_env(agent_name: str) -> dict[str, str]:
    env = {
        "HOME": os.environ["HOME"],
        "PATH": os.environ["PATH"],
        "AGENT_ID": agent_name,
        "AGENT_TYPE": agent_name,
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TERM": os.environ.get("TERM", "xterm"),
        "USER": os.environ.get("USER", "ted"),
    }
    # Explicit allowlist — never glob CLAUDE_*, which would leak any
    # CLAUDE_API_KEY / CLAUDE_CODE_OAUTH_TOKEN into agent subprocesses.
    for key in ("CLAUDE_CONFIG_DIR",):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


async def _run_claude(
    args: list[str],
    project_dir: str,
    agent_name: str,
    room_id: str,
    timeout: int = SUBPROCESS_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    """Run claude with the given args, registering the process for /cancel.

    Returns (exit_code, output). Raises asyncio.TimeoutError on timeout.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=project_dir,
        env=_minimal_env(agent_name),
    )
    _active_processes[room_id] = proc
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        # Drain pipe transports — wait_for cancelled communicate() mid-read,
        # leaving stdout/stderr file descriptors open. communicate() is
        # idempotent post-exit and closes the transports.
        with contextlib.suppress(Exception):
            await proc.communicate()
        raise
    finally:
        _active_processes.pop(room_id, None)

    rc = proc.returncode if proc.returncode is not None else -1
    stdout = stdout_b.decode(errors="replace").strip()
    stderr = stderr_b.decode(errors="replace").strip()
    # SECURITY[accepted]: on nonzero exit the (bounded, 500-char) stderr is surfaced to
    # the room. The destination room is trusted-sender-gated in handle_event and is
    # operator-owned, so the operator only ever sees their own agent's stderr — no
    # cross-trust-boundary disclosure. Accepted in the 2026-07-18 Showcase audit (Info
    # finding); see host-forge/build-reports accepted-risks.md.
    output = stdout if rc == 0 else stderr[:500]
    return rc, output


async def spawn_claude(
    session_id: str,
    prompt: str,
    project_dir: str,
    agent_name: str,
    room_id: str,
    timeout: int = SUBPROCESS_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    return await _run_claude(
        ["claude", "-p", "--session-id", session_id, prompt],
        project_dir,
        agent_name,
        room_id,
        timeout=timeout,
    )


async def resume_claude(
    session_id: str,
    message: str,
    project_dir: str,
    agent_name: str,
    room_id: str,
    timeout: int = SUBPROCESS_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    return await _run_claude(
        ["claude", "-p", "--resume", session_id, message],
        project_dir,
        agent_name,
        room_id,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# !cancel — SIGTERM the active subprocess in a room
# ---------------------------------------------------------------------------


async def handle_cancel_command(
    client: AsyncClient,
    room_id: str,
    event: RoomMessageText,
    mention_user: str,
) -> None:
    proc = _active_processes.get(room_id)
    # If the room lock is held but no proc is registered yet, the spawn is
    # mid-`create_subprocess_exec`. Wait briefly for it to land.
    if proc is None:
        lock = _room_locks.get(room_id)
        if lock is not None and lock.locked():
            elapsed = 0.0
            while elapsed < config.CANCEL_REGISTRATION_WAIT_SECONDS:
                await asyncio.sleep(config.CANCEL_POLL_INTERVAL_SECONDS)
                elapsed += config.CANCEL_POLL_INTERVAL_SECONDS
                proc = _active_processes.get(room_id)
                if proc is not None:
                    break
    if proc is None:
        log.info("action=cmd_cancel_noop room=%s event_id=%s", room_id, event.event_id)
        await post_message(
            client,
            room_id,
            f"{mention_user} No active session in this room.",
            reply_to=event.event_id,
        )
        return
    pid = proc.pid
    log.info("action=cmd_cancel room=%s event_id=%s pid=%s", room_id, event.event_id, pid)
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(signal.SIGTERM)
    await post_message(
        client,
        room_id,
        f"{mention_user} Sent SIGTERM to active session (pid {pid}).",
        reply_to=event.event_id,
    )
