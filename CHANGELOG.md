# Changelog

## [Unreleased]

## [0.4.0] - 2026-04-25

### Added
- `/cancel` command — sends SIGTERM to the active subprocess in the room and posts confirmation
- Per-room concurrency lock (`_room_locks`) — serializes spawn/resume per agent so two messages in the same room don't interleave
- Per-room rate limit on spawns (10s minimum gap, runaway-loop guard); resumes are unaffected
- Startup notification — posts to a configurable `startup_notification_agent` room (default `claudebox`) on dispatcher launch

### Changed
- Migrated `subprocess.run` → `asyncio.create_subprocess_exec` + `asyncio.wait_for` (resolves Phase 2 audit finding L2 — async loop no longer blocks for the duration of a session)
- `spawn_claude` and `resume_claude` are now async and accept `room_id` so the active process can be tracked for `/cancel`
- Active subprocesses tracked in `_active_processes[room_id]` for the duration of the run
- Help text updated to include `/cancel`

## [0.3.0] - 2026-04-25

### Added
- `/help` — list of dispatcher commands
- `/sessions` — show 10 most recent sessions in the room as numbered items; replying to one resumes it
- `/recap [N]` — read the most recent session's JSONL transcript and post the last N user+assistant turns (default 5, cap 20); read-only, no spawn
- `/mirror` — register the most recent untracked JSONL session in `project_dir` under a new thread root, allowing CloudCLI sessions to be resumed via Matrix replies
- Retention cleanup — `cleanup_loop()` runs at startup and every 24h, deleting sessions older than `session_retention_days` (default 30) plus orphaned event_aliases
- `python dispatcher.py --cleanup` — manual cleanup mode
- `session_retention_days` config option (default 30)

## [0.2.0] - 2026-04-25

### Added
- SQLite sessions database at `~/.claude/data/matrix-dispatcher/sessions.db` (WAL mode, parameterized queries throughout — security flag D)
- Thread-based resume routing: thread replies route to `claude -p --resume <session_id>`; room-root messages spawn fresh sessions
- `event_aliases` table maps ack and response chunk event IDs back to sessions, enabling resume when Element sends `m.in_reply_to` rather than `rel_type=m.thread`
- One-time migration from v1 `poll-tokens.json` to `poll_state` SQL table on startup

### Changed
- Spawn prompt now explicitly instructs agents not to call Matrix MCP tools — dispatcher owns all Matrix posting
- `get_session_by_event()` replaces direct session lookup — checks sessions table then falls back to event_aliases
- `_minimal_env()` extracted as shared helper between `spawn_claude()` and new `resume_claude()`
- `extract_thread_root()` replaces `is_room_root()` — returns thread root event ID or None instead of boolean

### Fixed
- Sessions now survive PM2 restarts (SQLite persistence vs. prior in-memory-only design)
- Thread replies no longer treated as orphaned when Element uses reply-chain rather than proper Matrix thread relation

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
