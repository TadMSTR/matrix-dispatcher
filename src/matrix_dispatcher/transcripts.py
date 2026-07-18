"""JSONL transcript helpers — read ``claude -p`` session transcripts for the
!sessions, !recap, and !mirror commands.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from .config import log


def project_jsonl_dir(project_dir: str) -> Path:
    """Convert a project directory path to its claude-code JSONL transcript dir."""
    encoded = project_dir.replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded


def _extract_text(content: object) -> str:
    """Best-effort text extraction from a claude transcript content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def read_first_user_message(session_id: str, project_dir: str, max_len: int = 80) -> str:
    """Return a one-line summary from the first user turn in the transcript."""
    jsonl = project_jsonl_dir(project_dir) / f"{session_id}.jsonl"
    if not jsonl.exists():
        return "(transcript missing)"
    try:
        with jsonl.open() as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                msg = obj.get("message", {})
                text = _extract_text(msg.get("content", "")).strip()
                # Strip dispatcher's injected prefix
                if text.startswith("[Invoked via Matrix"):
                    text = text.split("\n\n", 1)[-1]
                first_line = text.split("\n", 1)[0].strip()
                if not first_line:
                    continue
                return first_line[:max_len] + ("…" if len(first_line) > max_len else "")
    except OSError as e:
        log.warning("action=transcript_read_error session=%s err=%s", session_id, e)
        return "(transcript error)"
    return "(no user message)"


def read_last_n_turns(session_id: str, project_dir: str, n: int) -> str:
    """Return the last n user+assistant turns formatted as a Matrix-friendly recap."""
    jsonl = project_jsonl_dir(project_dir) / f"{session_id}.jsonl"
    if not jsonl.exists():
        return ""
    turns: list[tuple[str, str]] = []
    try:
        with jsonl.open() as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = obj.get("type")
                if role not in ("user", "assistant"):
                    continue
                msg = obj.get("message", {})
                text = _extract_text(msg.get("content", "")).strip()
                if not text:
                    continue
                if role == "user" and text.startswith("[Invoked via Matrix"):
                    text = text.split("\n\n", 1)[-1]
                turns.append((role, text))
    except OSError as e:
        log.warning("action=transcript_read_error session=%s err=%s", session_id, e)
        return ""
    pairs = turns[-(2 * n) :]
    return "\n\n".join(f"**{role}:**\n{text}" for role, text in pairs)


def find_unmirrored_session_id(
    db: sqlite3.Connection, project_dir: str, room_id: str
) -> str | None:
    """Find the most recent JSONL session in project_dir not yet tracked for this room.

    Only files whose stem parses as a UUID are considered — the value flows into
    `claude -p --resume <session_id>` as argv, so anything starting with `-` or
    otherwise unstructured must be rejected before reaching the subprocess.
    """
    jsonl_dir = project_jsonl_dir(project_dir)
    if not jsonl_dir.exists():
        return None
    rows = db.execute("SELECT session_id FROM sessions WHERE room_id = ?", (room_id,)).fetchall()
    known = {r["session_id"] for r in rows}
    candidates: list[Path] = []
    for path in jsonl_dir.glob("*.jsonl"):
        if path.stem in known:
            continue
        try:
            uuid.UUID(path.stem)
        except ValueError:
            continue
        candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime).stem
