# Changelog

## [Unreleased]

## [0.1.0] - 2026-04-25

### Added
- v1 spawn-only dispatcher loop — polls all configured agent rooms, spawns `claude -p` per room-root message from trusted sender
- Immediate acknowledgment message ("Working... (session <short-uuid>)") posted before spawn
- `@ted` mention prepended to all responses for Element push notification
- Long response chunking on paragraph boundaries at configurable `max_message_length` (default 4000 chars)
- Per-run Matrix sync token persisted to `~/.claude/data/matrix-dispatcher/poll-tokens.json` — daemon restarts do not reprocess old messages
- Minimal env allowlist for subprocesses — dispatcher credentials do not flow into agent processes
- Structured log output with event IDs, session IDs, room IDs, exit codes — no message body content logged
- PM2 ecosystem config with `start.sh` wrapper to source credentials from `~/.claude-secrets/matrix-dispatcher.env`
- Startup assertion: exits immediately if DISPATCHER_* env vars are missing
