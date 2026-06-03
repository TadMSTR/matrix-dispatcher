# matrix-dispatcher

Python asyncio Matrix bot that spawns or resumes `claude -p` sessions from Matrix room messages. Not an MCP server.

## What it does

Listens to Matrix rooms via matrix-nio, spawns a `claude -p` subprocess per room on incoming messages from authorized users, and streams responses back into the room. Tracks sessions in SQLite across restarts.

## Structure

```
dispatcher.py                  Main bot — matrix-nio client, per-room concurrency lock,
                               rate limiter, SQLite session tracking, subprocess management
config.yml                     Active config (gitignored — see config.example.yml)
config.example.yml             Reference configuration
ecosystem.forge.config.js      PM2 config for forge deployment
requirements.txt               Pinned exact versions
```

## Architecture decisions

- **Per-room concurrency lock** — prevents two simultaneous Claude sessions in the same room. A second message while a session is active is queued, not dropped.
- **Rate limit** (10 s between spawns per room) — prevents rapid-fire session creation from bursting the subprocess pool.
- **Minimal subprocess env** — subprocesses receive only a curated allowlist of env vars, not the full `os.environ`. This prevents credential leakage into Claude sessions.
- **No message body in logs** — only event IDs, room IDs, session IDs, and exit codes are written to the log. Message content is never persisted by the dispatcher.
- **SQLite session tracking** — `DB_PATH = ~/.claude/data/matrix-dispatcher/sessions.db`. Enables `!sessions` and `!recap` commands. Orphaned processes run to completion after a restart; the dispatcher does not SIGKILL them.
- **`/cancel`** — sends SIGTERM to the active subprocess for the room and waits up to `CANCEL_REGISTRATION_WAIT_SECONDS` for a registration acknowledgement before giving up.

## Configuration

Config lives in `config.yml` (gitignored). `config.example.yml` is the reference. Credentials are loaded from `DISPATCHER_*` env vars asserted at startup — the bot will not start if any required credential is missing.

## Common tasks

**Add a new Matrix room** — add it to `config.yml` and ensure the bot account has been invited to the room.

**Change rate limit or concurrency** — edit the relevant constants in `dispatcher.py`; they are not yet externalized to config.

**Debug a stuck session** — check `sessions.db` for orphaned rows with no exit code, then locate the subprocess PID and inspect or kill it manually.

## Testing

No automated tests. Manual testing requires a Matrix homeserver and a bot account. Use a test room to validate spawn/cancel/session tracking before deploying changes.

## Git workflow

Branch before editing — do not commit directly to `main`.
