## Summary

<!-- What does this change and why? -->

## Related

<!-- Issue / ticket IDs, if any -->

## Checklist

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `mypy dispatcher.py agent_registry.py` passes
- [ ] `pytest --cov` passes (coverage stays ≥ 80%)
- [ ] New behavior is covered by tests
- [ ] No forge/private topology or secrets added (see `.gitignore`; secrets are
      referenced by env-var name only)
- [ ] CHANGELOG.md updated under `[Unreleased]` if user-facing
