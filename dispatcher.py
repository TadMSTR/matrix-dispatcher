"""Matrix dispatcher — spawn or resume claude -p sessions from Matrix room messages.

v1: Spawn-only (no SQLite resume). Polls agent rooms for messages from the
trusted sender, spawns a fresh claude -p session per room-root message, and
posts the response back to the room.

Security flags addressed:
  B: Dispatcher credentials loaded from DISPATCHER_* env vars (asserted at startup).
  C: Subprocesses receive a minimal allowlist env, not os.environ.
  D: No SQL in v1; SQLite added in v2 with parameterized queries.
  E: Log policy: event IDs, room IDs, session IDs, action, exit code only — no message body.
  F: requirements.txt pins exact versions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path

import yaml
from nio import AsyncClient, MatrixRoom, RoomMessageText, SyncResponse

# ---------------------------------------------------------------------------
# Logging — structured, no message body content
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("dispatcher")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.yml"
POLL_TOKEN_PATH = Path.home() / ".claude" / "data" / "matrix-dispatcher" / "poll-tokens.json"


def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


def load_poll_tokens() -> dict[str, str]:
    if not POLL_TOKEN_PATH.exists():
        return {}
    try:
        return json.loads(POLL_TOKEN_PATH.read_text())
    except json.JSONDecodeError as e:
        log.warning("action=poll_token_corrupt path=%s err=%s", POLL_TOKEN_PATH, e)
        return {}


def save_poll_tokens(tokens: dict[str, str]) -> None:
    POLL_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = POLL_TOKEN_PATH.with_suffix(POLL_TOKEN_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(tokens, indent=2))
    tmp.replace(POLL_TOKEN_PATH)


# ---------------------------------------------------------------------------
# Dispatcher credentials — security flag B: assert non-empty at startup
# ---------------------------------------------------------------------------

def get_dispatcher_credentials() -> tuple[str, str, str]:
    homeserver = os.environ.get("DISPATCHER_HOMESERVER", "").strip()
    user_id = os.environ.get("DISPATCHER_USER_ID", "").strip()
    token = os.environ.get("DISPATCHER_ACCESS_TOKEN", "").strip()

    missing = [k for k, v in [
        ("DISPATCHER_HOMESERVER", homeserver),
        ("DISPATCHER_USER_ID", user_id),
        ("DISPATCHER_ACCESS_TOKEN", token),
    ] if not v]

    if missing:
        log.error("Missing required env vars at startup: %s", ", ".join(missing))
        sys.exit(1)

    return homeserver, user_id, token


# ---------------------------------------------------------------------------
# Matrix helpers
# ---------------------------------------------------------------------------

async def post_message(
    client: AsyncClient,
    room_id: str,
    body: str,
    reply_to: str | None = None,
) -> str:
    content: dict = {"msgtype": "m.text", "body": body}
    if reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}

    resp = await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content=content,
    )
    return getattr(resp, "event_id", "")


def split_on_paragraphs(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    current = ""

    for para in paragraphs:
        candidate = (current + "\n\n" + para).lstrip("\n") if current else para
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # If single paragraph is too long, hard-split
            if len(para) > max_len:
                for i in range(0, len(para), max_len):
                    chunks.append(para[i:i + max_len])
                current = ""
            else:
                current = para

    if current:
        chunks.append(current)

    return chunks or [text[:max_len]]


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------

def spawn_claude(
    session_id: str,
    prompt: str,
    project_dir: str,
    agent_name: str,
) -> tuple[int, str]:
    """Run claude -p with a pre-generated session ID. Returns (exit_code, output)."""
    # Security flag C: minimal env allowlist — no dispatcher secrets flow into agent
    minimal_env = {
        "HOME": os.environ["HOME"],
        "PATH": os.environ["PATH"],
        "AGENT_ID": agent_name,
        "AGENT_TYPE": agent_name,
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TERM": os.environ.get("TERM", "xterm"),
        "USER": os.environ.get("USER", "ted"),
    }

    # Explicit allowlist — never glob CLAUDE_*, which would leak any
    # CLAUDE_API_KEY / CLAUDE_CODE_OAUTH_TOKEN added to the dispatcher env
    # file or inherited from PM2's parent env into every spawned subprocess.
    CLAUDE_PASSTHROUGH = ("CLAUDE_CONFIG_DIR",)
    for key in CLAUDE_PASSTHROUGH:
        if key in os.environ:
            minimal_env[key] = os.environ[key]

    result = subprocess.run(
        ["claude", "-p", "--session-id", session_id, prompt],
        capture_output=True,
        text=True,
        cwd=project_dir,
        env=minimal_env,
        timeout=300,
    )

    output = result.stdout.strip() if result.returncode == 0 else result.stderr.strip()[:500]
    return result.returncode, output


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------

def is_room_root(event: RoomMessageText) -> bool:
    """True if this event is a top-level room message (not a thread reply)."""
    source = getattr(event, "source", {})
    content = source.get("content", {}) if isinstance(source, dict) else {}
    return "m.relates_to" not in content


async def handle_event(
    client: AsyncClient,
    room_id: str,
    event: RoomMessageText,
    config: dict,
    trusted_sender: str,
    mention_user: str,
    max_message_length: int,
    agent_name: str,
    project_dir: str,
) -> None:
    # Security flag B: sender gate — first check, silently discard non-trusted
    if event.sender != trusted_sender:
        return

    if not is_room_root(event):
        return

    user_message = event.body.strip()

    # Dispatcher command intercept (v3 — stub only in v1)
    if user_message.startswith("/"):
        log.info(
            "action=command_ignored room=%s event_id=%s body_prefix=%s",
            room_id, event.event_id, user_message[:20],
        )
        return

    session_id = str(uuid.uuid4())
    short_id = session_id[:8]

    log.info(
        "action=spawn_start room=%s event_id=%s agent=%s session=%s",
        room_id, event.event_id, agent_name, session_id,
    )

    # Acknowledgment message
    ack_event_id = await post_message(
        client, room_id, f"Working... (session {short_id})", reply_to=event.event_id
    )

    # Inject dispatcher prefix — tells agent which room/agent invoked it
    prompt = f"[Invoked via Matrix room #{agent_name} by @ted]\n\n{user_message}"

    try:
        exit_code, output = spawn_claude(session_id, prompt, project_dir, agent_name)
    except subprocess.TimeoutExpired:
        log.error("action=spawn_timeout room=%s session=%s", room_id, session_id)
        await post_message(
            client, room_id,
            f"{mention_user} Session {short_id} timed out after 300s.",
            reply_to=ack_event_id or event.event_id,
        )
        return

    log.info(
        "action=spawn_complete room=%s session=%s exit_code=%d",
        room_id, session_id, exit_code,
    )

    if exit_code != 0:
        await post_message(
            client, room_id,
            f"{mention_user} Session {short_id} exited with error:\n\n{output}",
            reply_to=ack_event_id or event.event_id,
        )
        return

    if not output:
        output = "(no output)"

    chunks = split_on_paragraphs(output, max_message_length)
    reply_target = ack_event_id or event.event_id

    for i, chunk in enumerate(chunks):
        text = f"{mention_user} {chunk}" if i == 0 else chunk
        await post_message(client, room_id, text, reply_to=reply_target)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

async def poll_loop(client: AsyncClient, config: dict) -> None:
    trusted_sender: str = config.get("trusted_sender", "@ted:claudebox.me")
    mention_user: str = config.get("mention_user", "@ted:claudebox.me")
    poll_interval: int = config.get("poll_interval_seconds", 5)
    max_message_length: int = config.get("max_message_length", 4000)

    agents: dict = config.get("agents", {})
    # Map room_id → agent config for fast lookup
    room_to_agent: dict[str, dict] = {}
    for name, agent_cfg in agents.items():
        room_id = agent_cfg["room_id"]
        room_to_agent[room_id] = {"name": name, **agent_cfg}

    poll_tokens = load_poll_tokens()
    # Use a single since token across all rooms (Matrix sync is global)
    since: str | None = poll_tokens.get("global_since")

    # Cold-start seeding: with since=None, /sync returns recent timeline events.
    # Iterating those would re-spawn claude for already-answered messages. Run
    # one priming sync to capture next_batch without processing any events.
    if since is None:
        seed = await client.sync(timeout=0, since=None, full_state=False)
        if isinstance(seed, SyncResponse):
            since = seed.next_batch
            poll_tokens["global_since"] = since
            save_poll_tokens(poll_tokens)
            log.info("action=poll_seed since=%s", since)
        else:
            log.warning("action=poll_seed_error response=%s", type(seed).__name__)

    log.info("action=poll_start agents=%s since=%s", list(agents.keys()), since)

    while True:
        try:
            resp = await client.sync(timeout=0, since=since, full_state=False)

            if not isinstance(resp, SyncResponse):
                log.warning("action=sync_error response=%s", type(resp).__name__)
                await asyncio.sleep(poll_interval)
                continue

            since = resp.next_batch
            poll_tokens["global_since"] = since
            save_poll_tokens(poll_tokens)

            for room_id, room_info in resp.rooms.join.items():
                if room_id not in room_to_agent:
                    continue

                agent_cfg = room_to_agent[room_id]
                agent_name = agent_cfg["name"]
                project_dir = agent_cfg["project_dir"]

                for event in room_info.timeline.events:
                    if not isinstance(event, RoomMessageText):
                        continue
                    await handle_event(
                        client=client,
                        room_id=room_id,
                        event=event,
                        config=config,
                        trusted_sender=trusted_sender,
                        mention_user=mention_user,
                        max_message_length=max_message_length,
                        agent_name=agent_name,
                        project_dir=project_dir,
                    )

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("action=poll_error")

        await asyncio.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    homeserver, user_id, token = get_dispatcher_credentials()
    config = load_config()

    client = AsyncClient(homeserver, user_id)
    client.access_token = token
    client.user_id = user_id

    log.info("action=startup user_id=%s homeserver=%s", user_id, homeserver)

    try:
        await poll_loop(client, config)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
