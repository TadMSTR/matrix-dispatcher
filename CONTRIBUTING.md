# Contributing

Thanks for your interest in matrix-dispatcher. This is a small, single-maintainer
project; contributions are welcome but please open an issue to discuss non-trivial
changes before sending a large PR.

## Development setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e '.[dev]'
pre-commit install   # optional but recommended
```

## Checks (must pass before a PR merges — CI enforces all of these)

```bash
ruff check .                 # lint
ruff format --check .        # formatting
mypy dispatcher.py agent_registry.py
pytest --cov --cov-report=term-missing   # coverage gate: fail_under = 80
pip-audit --strict .         # dependency audit
```

`ruff format .` and `ruff check --fix .` apply the fixes locally. The pre-commit
hooks run ruff + whitespace/EOF/YAML/TOML checks automatically on commit.

## Tests

Tests live in `tests/` and use `pytest-asyncio` (asyncio auto-mode). They rely on a
fake Matrix client, an in-memory SQLite DB, and monkeypatched spawn/resume — no real
homeserver, network, or `claude` binary. New behavior should ship with a test; keep
total coverage at or above the 80% gate (currently ~98%), and cover the security path
(subprocess env, UUID/argv validation, credential handling) fully.

## Branch & commit rules

- Branch off `main`; never commit directly to `main`.
- Keep commits focused; write a clear imperative subject line.
- Reference the issue or ticket ID where applicable.

## Security

Do not open public issues for vulnerabilities — see [SECURITY.md](SECURITY.md) for
private disclosure channels.
