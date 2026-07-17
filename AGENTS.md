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
- **HITL resume-on-approval reconcile loop** (SMCP-38, v0.5.0) — a background task
  (`RECONCILE_INTERVAL_SECONDS = 10`) polls the local `pending_approvals` SQLite table
  (rows written when a turn ends with an approval still pending) and cross-checks each
  against `hitl_approvals` state on agent-postgres via `agent_registry.py`. On `approved`
  it `claude -p --resume`s the originating session; on `denied` it posts a note; rows
  older than 2× the HITL timeout (600s) are expired. **Claim-then-act**: the local row is
  deleted *before* the resume fires, so an overlapping reconcile pass or a restart
  mid-resume can never fire the same resume twice. The loop is a no-op (`reconcile_disabled
  reason=registry-off`) when the registry client is disabled. This is layered on top of a
  fail-*closed* gate (scoped-mcp's Dragonfly OTP) — if agent-postgres is unreachable, the
  feature simply doesn't fire and the operator falls back to manual retry; it is never a
  bypass.

## Configuration

Config lives in `config.yml` (gitignored). `config.example.yml` is the reference. Credentials are loaded from `DISPATCHER_*` env vars asserted at startup — the bot will not start if any required credential is missing.

**`AGENT_REGISTRY_DSN`** (env, SMCP-38) — Postgres DSN for the agent-postgres session
registry, e.g. `postgresql://dispatcher_registry:***@127.0.0.1:5433/agent_registry`.
Provisioned in the PM2 env from Vault, not `config.yml`. Unset ⇒ HITL resume-on-approval is
off and the dispatcher behaves exactly as before this feature (fail-open by design — see
`agent_registry.py`). Each agent entry in `config.yml` also accepts an optional
`scoped_mcp_url` (e.g. `http://127.0.0.1:8471`), a best-effort value recorded in the
`sessions` registry row for that agent; it is not on the resume hot path and can be omitted.

## Common tasks

**Add a new Matrix room** — add it to `config.yml` and ensure the bot account has been invited to the room.

**Change rate limit or concurrency** — edit the relevant constants in `dispatcher.py`; they are not yet externalized to config.

**Debug a stuck session** — check `sessions.db` for orphaned rows with no exit code, then locate the subprocess PID and inspect or kill it manually.

## Testing

No automated tests. Manual testing requires a Matrix homeserver and a bot account. Use a test room to validate spawn/cancel/session tracking before deploying changes.

## Git workflow

Branch before editing — do not commit directly to `main`.
