"""Configuration, logging, paths, and runtime tunables.

Single source of truth for the dispatcher's constants. Path and tunable
constants are read at call time (attribute access on this module) by the
functions that consume them, so tests can monkeypatch e.g. ``config.DB_PATH``
or ``config.CANCEL_REGISTRATION_WAIT_SECONDS`` and have the change take effect.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Runtime tunables
# ---------------------------------------------------------------------------

SUBPROCESS_TIMEOUT_SECONDS = 300
RATE_LIMIT_SECONDS = 10
# Brief retry window for !cancel when the spawn is mid-`create_subprocess_exec`
# (proc not yet registered in _active_processes).
CANCEL_REGISTRATION_WAIT_SECONDS = 1.0
CANCEL_POLL_INTERVAL_SECONDS = 0.05

# HITL resume-on-approval (SMCP-38). The reconcile loop polls agent-postgres for
# approval-state transitions on locally-tracked pending approvals and resumes the
# originating session once an operator approval lands. Interval must be « the
# scoped-mcp pre-approval token TTL (300s) so the resumed retry lands well inside
# the token's life. Local pending rows are expired after 2x the HITL timeout
# default (300s) so an approval that is never actioned can't accumulate.
RECONCILE_INTERVAL_SECONDS = 10
PENDING_APPROVAL_EXPIRY_SECONDS = 600

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
# Paths
# ---------------------------------------------------------------------------

# Repo root is three levels up from this file (src/matrix_dispatcher/config.py),
# so config.yml resolves next to the entry shim regardless of install layout.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = _REPO_ROOT / "config.yml"
# v1 poll-token JSON — kept only for one-time migration to SQLite
POLL_TOKEN_PATH = Path.home() / ".claude" / "data" / "matrix-dispatcher" / "poll-tokens.json"
DB_PATH = Path.home() / ".claude" / "data" / "matrix-dispatcher" / "sessions.db"


def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)
