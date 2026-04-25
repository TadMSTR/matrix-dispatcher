# matrix-dispatcher

A PM2 daemon that watches each agent's Matrix room for messages from a trusted sender, spawns or resumes `claude -p` sessions, and posts the response back. Send a message to the room → the agent replies. Reply inside a thread → the same session resumes. Bang-prefix commands provide session management without spawning anything.

## How it works

```
@you sends message to #research
        ↓
matrix-dispatcher (PM2, polls every 5s)
        ↓  room-root message  → spawn new session
        ↓  thread reply       → resume prior session via --resume <session_id>
        ↓  !<command>         → intercepted (no spawn)
asyncio.create_subprocess_exec("claude", "-p", "--session-id", uuid, prompt, ...)
  cwd: <project_dir>
        ↓  stdout
Dispatcher posts response back to the Matrix room
  (with @you mention so Element fires a push notification)
```

**Routing discriminator:** Matrix thread structure only — room-root spawns, thread reply resumes. No timers, no AI judgment about intent.

**Element reply quirk:** Element sometimes uses `m.in_reply_to` instead of the spec-correct `rel_type=m.thread`. The `event_aliases` table maps acknowledgment and response chunk event IDs back to their parent session, so a reply to any of those events still resolves to the right session.

## Commands

Commands use the `!` prefix because Element intercepts `/`-prefixed messages client-side as IRC commands (`/me`, `/join`, `/help`) and never sends them to Matrix.

| Command | What it does |
|---------|-------------|
| `!help` | List of dispatcher commands |
| `!sessions` | 10 most recent sessions in the room as numbered items; reply-in-thread to resume |
| `!recap [N]` | Last N user+assistant turns from the most recent session (default 5, cap 20). Read-only — no spawn, no resume registered |
| `!mirror` | Register the most recent untracked JSONL session in `project_dir` under a new thread root, so a CloudCLI-started session can be resumed via Matrix replies |
| `!cancel` | Send SIGTERM to the active subprocess in this room and confirm |

## Requirements

- Python 3.11+
- `claude` CLI in PATH (authenticated)
- Matrix homeserver with client API access
- One Matrix account for the dispatcher to poll and post with

## Setup

### 1. Create a venv and install deps

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 2. Write credentials file

Create `~/.claude-secrets/matrix-dispatcher.env` (chmod 600):

```bash
DISPATCHER_HOMESERVER=https://matrix.your-homeserver.com
DISPATCHER_USER_ID=@dispatcher-bot:your-homeserver.com
DISPATCHER_ACCESS_TOKEN=<access-token>
```

The dispatcher account needs to be a member of every agent room it watches.

### 3. Configure agents

Copy `config.example.yml` to `config.yml` and edit:

```yaml
trusted_sender: "@you:your-homeserver.com"
mention_user: "@you:your-homeserver.com"
poll_interval_seconds: 5
max_message_length: 4000
session_retention_days: 30
startup_notification_agent: research    # room to post on dispatcher launch

agents:
  research:
    room_id: "!roomid:your-homeserver.com"
    project_dir: "/home/user/.claude/projects/research"
  dev:
    room_id: "!roomid:your-homeserver.com"
    project_dir: "/home/user/.claude/projects/dev"
```

`project_dir` is the working directory passed to `claude -p` — the project's `CLAUDE.md` must be present there.

### 4. Register with PM2

```bash
pm2 start ecosystem.config.js
pm2 save
```

Logs land at `~/.pm2/logs/matrix-dispatcher-out.log` and `~/.pm2/logs/matrix-dispatcher-error.log`.

## SQLite session store

State lives at `~/.claude/data/matrix-dispatcher/sessions.db` (WAL mode). Three tables:

| Table | Purpose |
|-------|---------|
| `sessions` | One row per spawned session — `thread_root_id`, `room_id`, `agent`, `session_id`, `created_at`, `last_used_at` |
| `event_aliases` | Maps ack and response chunk event IDs back to their parent session (handles Element's `m.in_reply_to` reply format) |
| `poll_state` | Per-room Matrix sync tokens — survives restarts, no message replay |

Sessions older than `session_retention_days` (default 30) and orphaned `event_aliases` rows are deleted by `cleanup_loop()` at startup and every 24 hours. Manual cleanup: `python dispatcher.py --cleanup`.

## Security

- Only messages from `trusted_sender` are processed — all others are silently discarded. Applies to spawns, resumes, and all dispatcher commands.
- Subprocess env is a minimal allowlist (`HOME`, `PATH`, `AGENT_ID`, `AGENT_TYPE`, plus required `CLAUDE_*` vars). Dispatcher credentials do not flow into agent processes.
- Log files contain timestamps, event IDs, session IDs, room IDs, actions, and exit codes only — no message body content.
- All SQLite queries are parameterized — no f-string SQL construction anywhere in the dispatcher.
- `sessions.db` is created with mode 600.
- JSONL stems are validated with `uuid.UUID()` before being passed to `--resume` argv.
- `requirements.txt` pins exact versions.

## Phases

| Phase | Status | Description |
|-------|--------|-------------|
| v0.1 | shipped | Spawn-only loop, acknowledgment message, @mention, response chunking |
| v0.2 | shipped | SQLite session store, thread-based resume, `event_aliases` for Element reply quirk |
| v0.3 | shipped | `!sessions`, `!recap`, `!mirror`, `!help`; 30-day retention cleanup |
| v0.4 | shipped | Async subprocess, per-room concurrency lock, per-room rate limit, `!cancel`, startup notification |

See [CHANGELOG.md](CHANGELOG.md) for per-phase detail.

## Files

```
matrix-dispatcher/
├── dispatcher.py       # main daemon
├── config.yml          # live config (not committed — contains room IDs)
├── config.example.yml  # committed template
├── requirements.txt    # pinned deps
├── ecosystem.config.js # PM2 definition (uses start.sh)
├── start.sh            # sources credentials env file, execs dispatcher
└── venv/               # local venv
~/.claude/data/matrix-dispatcher/
└── sessions.db         # SQLite (sessions, event_aliases, poll_state)
~/.claude-secrets/
└── matrix-dispatcher.env   # bot credentials (chmod 600)
```

## License

MIT — see [LICENSE](LICENSE).
