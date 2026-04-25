# matrix-dispatcher

A PM2 daemon that watches each agent's Matrix room for messages from a trusted sender, spawns fresh `claude -p` sessions for room-root messages, and posts the response back. Reply inside a thread to resume the same session (v2+).

## How it works

```
@ted sends message to #research
        ↓
Dispatcher (PM2, polls every 5s)
        ↓ room-root message → spawn
claude -p --session-id <uuid> "<prompt>"
        ↓ stdout
Dispatcher posts response to Matrix room
```

**Routing:** Room-root messages spawn a new session. Thread replies resume (v2+, requires SQLite).

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

agents:
  research:
    room_id: "!roomid:your-homeserver.com"
    project_dir: "/home/user/.claude/projects/research"
```

`project_dir` is the working directory passed to `claude -p` — the project's CLAUDE.md must be present there.

### 4. Register with PM2

```bash
pm2 start ecosystem.config.js
pm2 save
```

## Security notes

- Only messages from `trusted_sender` are processed — all others are silently discarded.
- Subprocess env is a minimal allowlist; dispatcher credentials do not flow into agent processes.
- Log files contain event IDs, session IDs, and exit codes only — no message body content.
- `requirements.txt` pins exact versions.

## Phases

| Phase | Status | Description |
|-------|--------|-------------|
| v1 | ✓ deployed | Spawn-only loop, acknowledgment message, @mention, response chunking |
| v2 | planned | SQLite + thread-based resume, restart-safe |
| v3 | planned | `/sessions`, `/recap`, `/mirror`, `/help` commands, nightly cleanup |
| Phase 4 | planned | Concurrency lock, timeout, rate limiting, `/cancel`, error surfacing |

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
└── sessions.db         # SQLite (v2+, created at first run)
~/.claude/data/matrix-dispatcher/poll-tokens.json  # per-run sync token (v1)
```
