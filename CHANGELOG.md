# Changelog

## [Unreleased]

## [0.6.0] - 2026-07-18

Showcase-tier promotion + public-history security remediation.

### Added
- Showcase tooling: `pyproject.toml` (hatchling) consolidating pinned runtime + dev
  deps and ruff / mypy / pytest / coverage config; GitHub Actions CI (ruff, mypy,
  `pytest --cov` ≥ 80% on Python 3.11 / 3.12 / 3.13, `pip-audit --strict`); a
  source-only release workflow; `.pre-commit-config.yaml`.
- Test suite lifted from ~43% to ~98% coverage (new unit / command / loop / registry
  suites). The subprocess + env-allowlist + UUID-argv security path is covered
  end-to-end against real short-lived processes.
- Docs: `ARCHITECTURE.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, PR + issue
  templates; README badges and Mermaid data-flow / component diagrams;
  `config.example.yml` now documents `bot_user_id` and `startup_notification_agent`.

### Changed
- Deploy templates (`start.sh`, `ecosystem.config.js`) are now portable — no hardcoded
  home paths (resolve via `$SCRIPT_DIR` / `__dirname` / `$HOME`). Install is now
  `pip install .` / `pip install -e '.[dev]'`; `requirements*.txt` removed (deps live
  in `pyproject.toml`).

### Fixed
- Genericized a test fixture that embedded a real host path (operator username + agent
  name) introduced during the coverage lift — resolves the Showcase-audit LOW finding.

### Security
- **Purged leaked forge topology from all public git history** (`git filter-repo` +
  force-push, rewritten tags, stale merged-PR branches deleted). The Matrix homeserver,
  bot/admin user IDs, five real room IDs, and host paths had been committed via
  `config.forge.yml` / `start-forge.sh` / `ecosystem.forge.config.js` and test
  fixtures; all are removed from every clone-reachable commit and the deployment files
  now live only in a private repo (gitignored here via `*.forge.*` / `*-forge.*` /
  `config.*.yml`). No credentials were ever committed (env-var names only), so no
  secret rotation was required. Note: GitHub `refs/pull/*` still pin the old commits by
  SHA until GitHub Support expunges them.

## [0.5.1] - 2026-07-18

### Added
- `AGENT_REGISTRY_DSN` is now read from Vault first (SMCP-42), via a one-shot
  AppRole login-read-discard against the `dispatcher-registry-reader` role
  (new `hvac` dependency, optional-import — falls back cleanly if unset or
  unavailable). The existing plaintext `AGENT_REGISTRY_DSN` env var remains
  the fail-open fallback, used unconditionally if Vault is unconfigured or
  the read fails for any reason (network, auth, missing key). Does not port
  scoped-mcp's renewal-loop machinery — this DSN is read once at first use,
  not held as a long-lived credential.

### Changed
- `requirements.txt` adds `hvac==2.4.0`.

### Security
- Pre-merge audit: 1 Info finding (Vault AppRole reader token not explicitly
  revoked after the one-shot read) — fixed by calling
  `client.auth.token.revoke_self()` immediately after a successful read,
  guarded so a revoke failure can never turn a successful DSN read into a
  failure. No security bypass; audit clean otherwise.

## [0.5.0] - 2026-07-17

### Added
- HITL resume-on-approval (SMCP-38). The dispatcher now watches the agent-postgres
  session registry and `claude -p --resume`s the originating session when an operator
  approval lands, closing the last gap in the HITL flow (SMCP-14) — previously the
  approval wrote a pre-approval token that the already-exited turn never consumed.
  - New fail-open `agent_registry.py` asyncpg client (`upsert_session`,
    `find_pending_approval`, `link_session`, `get_approval_states`). Disabled and
    fully no-op unless `AGENT_REGISTRY_DSN` is set and the pool builds.
  - New local SQLite `pending_approvals` table correlating approval → session.
  - `sessions` registry rows written at spawn/resume/`!mirror` (finally wiring the
    writer SMCP-14 left dangling) and `hitl_approvals.session_id` populated.
  - Reconcile loop (10s) resumes exactly once on `approved` (claim-then-act), posts a
    note on `denied`, and expires stale local rows after 2× the HITL timeout (600s).
  - Duplicate-execution guard: only approvals still `pending` at post-turn detection
    are tracked, so an approval the agent self-resolved in-turn is never re-executed.
  - Resume is dispatcher-initiated off agent-postgres state, not a reply to a
    foreign-bot prompt — MDISP-6 stays intact. The resume nudge carries no secret.

### Changed
- `requirements.txt` adds `asyncpg==0.31.0` (runtime dep for the registry client;
  optional import — feature stays off when `AGENT_REGISTRY_DSN` is unset).

### Fixed
- Reconcile resume now notifies the operator on any resume failure, not just
  timeout (audit LOW-1). Because the local `pending_approvals` row is claimed
  before the resume for the exactly-once guarantee, a non-timeout failure (e.g.
  a transiently missing `claude` binary) previously dropped the approval
  silently; it now posts a "retry manually" notice symmetric with the timeout path.

### Security
- Pre-audit baseline: `.gitignore` extended to cover `.env`/`*.env` and
  `core.*`/`*.core` (SC-02) so the new `AGENT_REGISTRY_DSN` secret can't land as a
  stray dotenv. Audit outcome: 2 Low findings (1 fixed, 1 deferred to scoped-mcp);
  no security bypass; all 7 scrutiny points held; 18/18 tests; pip-audit clean.

## [0.4.1] - 2026-07-17

### Fixed
- Orphaned / foreign-bot replies no longer spawn a new session (MDISP-6). A reply
  whose thread root has no tracked session is never treated as a spawn trigger:
  replies to our own expired threads get an actionable hint, replies to foreign-bot
  posts (scoped-mcp HITL, matrix-hitl-bot, etc.) are ignored silently. Fail-closed —
  any error fetching the parent event is treated as foreign (no spawn).

### Added
- First automated test suite (`tests/test_handle_event.py`, `pytest` + `pytest-asyncio`)
  covering the `handle_event` dispatch outcomes; `requirements-dev.txt` for dev deps.

### Security
- Dev dependency `pytest` pinned `>=9.0.3` to clear PYSEC-2026-1845 (predictable
  `/tmp/pytest-of-*` dir; dev-only, non-runtime). Audit LOW finding closed by adding a
  regression test for empty/misconfigured `bot_user_id` degrading to no-spawn.

## [0.4.0] - 2026-04-25

### Added
- `!cancel` command — sends SIGTERM to the active subprocess in the room and posts confirmation
- Per-room concurrency lock (`_room_locks`) — serializes spawn/resume per agent so two messages in the same room don't interleave
- Per-room rate limit on spawns (10s minimum gap, runaway-loop guard); resumes are unaffected
- Startup notification — posts to a configurable `startup_notification_agent` room (default `claudebox`) on dispatcher launch

### Changed
- Migrated `subprocess.run` → `asyncio.create_subprocess_exec` + `asyncio.wait_for` (resolves Phase 2 audit finding L2 — async loop no longer blocks for the duration of a session)
- `spawn_claude` and `resume_claude` are now async and accept `room_id` so the active process can be tracked for `!cancel`
- Active subprocesses tracked in `_active_processes[room_id]` for the duration of the run
- Help text updated to include `!cancel`

## [0.3.0] - 2026-04-25

### Added
- `!help` — list of dispatcher commands
- `!sessions` — show 10 most recent sessions in the room as numbered items; replying to one resumes it
- `!recap [N]` — read the most recent session's JSONL transcript and post the last N user+assistant turns (default 5, cap 20); read-only, no spawn
- `!mirror` — register the most recent untracked JSONL session in `project_dir` under a new thread root, allowing CloudCLI sessions to be resumed via Matrix replies
- Retention cleanup — `cleanup_loop()` runs at startup and every 24h, deleting sessions older than `session_retention_days` (default 30) plus orphaned event_aliases
- `python dispatcher.py --cleanup` — manual cleanup mode
- `session_retention_days` config option (default 30)

### Note
- Dispatcher commands use `!` prefix because Element intercepts `/`-prefixed messages client-side (IRC-style commands like `/me`, `/join`, `/help`) and never sends them to Matrix.

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
