# Release Checklist

## Before Tagging

- [ ] Confirm version in `pyproject.toml`.
- [ ] Run `uv sync --extra dev`.
- [ ] Run `find . -name ".DS_Store" -delete`.
- [ ] Run `find . -type d -name "__pycache__" -prune -exec rm -rf {} +`.
- [ ] Run `rm -rf .pytest_cache .ruff_cache dist build`.
- [ ] Run `find . -maxdepth 1 -name "*.egg-info" -type d -prune -exec rm -rf {} +`.
- [ ] Run `uv run ruff check .`.
- [ ] Run `uv run python -m compileall stackmint_gateway examples tests`.
- [ ] Run `uv run pytest`.
- [ ] Run `uv run bandit --ini .bandit -r stackmint_gateway examples tests`.
  - Bandit scans runtime/example code using `.bandit` exclusions; tests are not treated as production code for Bandit findings.
- [ ] Run `uv run pip-audit`.
- [ ] Run `uv run detect-secrets scan --baseline .secrets.baseline`.
- [ ] If only timestamp metadata changed in `.secrets.baseline`, restore it before release.
- [ ] If new findings appear, review them intentionally before updating the baseline.
- [ ] Run `uv run stackmint version`.
- [ ] Run `uv run stackmint doctor --no-splash`.
- [ ] Run `uv run stackmint doctor --json`.
- [ ] Run `uv run stackmint demo --no-splash`.
- [ ] Run `uv run stackmint demo --no-splash --no-delay`.
- [ ] Run `uv run stackmint demo --json`.
- [ ] Run `uv run stackmint help`.
- [ ] Run `uv run stackmint mcp --preview --no-splash`.
- [ ] Run `uv run stackmint check --print`.
- [ ] Run `rm -rf dist build`.
- [ ] Run `find . -maxdepth 1 -name "*.egg-info" -type d -prune -exec rm -rf {} +`.
- [ ] Run `uv run python -m build`.
- [ ] Run `uv run twine check dist/*`.
- [ ] Confirm built wheel includes `stackmint_gateway/assets/`.
- [ ] Install the built wheel in a clean temporary environment and confirm package imports.
- [ ] Confirm no `.env`, `.DS_Store`, `__pycache__/`, or `*.pyc` files are tracked/staged.
- [ ] Review README alpha limitations.
- [ ] Review CHANGELOG entry.

## Tagging

Suggested Git tag:

```bash
git tag -a v0.1.0-alpha -m "Stackmint Gateway v0.1.0-alpha"
```

Suggested push command, only when ready:

```bash
git push origin v0.1.0-alpha
```

## After Tagging

- [ ] Create GitHub release from `v0.1.0-alpha`.
- [ ] Paste release notes from `RELEASE.md`.
- [ ] Do not publish to PyPI until package layout and TestPyPI validation are complete.

## TestPyPI Dry Run

Upload to TestPyPI only after credentials are configured and the release owner
has explicitly approved the upload:

```bash
uv run twine upload --repository testpypi dist/*
```

Install from TestPyPI in a clean environment:

```bash
uv run python -m venv /tmp/stackmint-gateway-testpypi
source /tmp/stackmint-gateway-testpypi/bin/activate
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple stackmint-gateway==0.1.0a1
/tmp/stackmint-gateway-testpypi/bin/python - <<'PY'
import stackmint_gateway
from stackmint_gateway.langchain import GovernedAgent
print("ok")
PY
deactivate
```
