# Stackmint Gateway v0.1.0-alpha

Python package version: `0.1.0a1`
Release label/tag: `v0.1.0-alpha`

## What This Release Is

A first alpha of the open-source Stackmint Gateway Python SDK for connecting
LangChain agents to Stackmint-style governance workflows.

## What Is Real Today

- LangChain `Runnable` wrapping through `GovernedAgent`.
- Remote agent status checks.
- Local/session budget guardrails.
- Local circuit breaker.
- Policy-wrapped tool permission enforcement.
- HITL approval wrapper with default terminal prompt and injectable approval callback.
- Safe telemetry recording with redaction and truncation.
- Execution records for completed, failed, and blocked runs.
- Idempotency-aware execution recording.
- Optional SDK hooks and typed client methods for centralized authorization,
  budget reservation/commit, approval requests, and tool-level governance
  events when compatible backend endpoints are available.
- Optional alpha MCP governance server for policy lookup, execution
  authorization calls, safe execution recording, budget calls, approval hooks,
  and tool-event reporting.
- `stackmint` / `stackmint-gateway` CLI commands with curated help, doctor
  readiness checks, branded event output, and JSON output for automation.
- No-key `stackmint demo` governance walkthrough with local and mock
  control-plane modes, `--json`, and `--clear --speed cinematic` for recording.
- Provider-aware `stackmint example langchain` smoke test with fake/local,
  OpenAI, Anthropic, and Cerebras provider paths.
- SDK-specific gateway exceptions for HTTP, connection, timeout, and response errors.
- Importable `stackmint_gateway` package layout with temporary flat-module compatibility shims.
- Model-provider-neutral LangChain examples for fake/local, OpenAI, Anthropic, and Cerebras paths.

## Control-Plane Boundaries and Limitations

- SDK client hooks and optional `GovernedAgent` integration for centralized
  authorization, budget reservation/commit, approval hooks, and tool events are
  included. A compatible managed/backend control plane is required for full
  centralized enforcement. A deployed managed backend is required for
  end-to-end centralized governance.
- Budget enforcement is local/session-level unless compatible remote budget
  endpoints are enabled.
- Production-hardened backend tool-level event history tied to execution IDs.
- Native non-LangChain framework connectors.
- Production enterprise DLP.
- Automatic mutation of already-created LangChain agents.
- Python 3.13 passed local verification, but official package classifiers stay
  at Python 3.10-3.12 until CI covers 3.13.

## Verification

Release preparation verification commands:

```bash
uv sync --extra dev
uv run ruff check .
uv run python -m compileall stackmint_gateway examples tests
uv run pytest
uv run bandit --ini .bandit -r stackmint_gateway examples tests
uv run pip-audit
uv run detect-secrets scan --baseline .secrets.baseline
# Do not commit timestamp-only .secrets.baseline churn.
uv run stackmint demo --no-splash --no-delay
uv run stackmint demo --json
uv run stackmint doctor --no-splash
uv run stackmint doctor --json
uv run stackmint help
uv run stackmint mcp --preview --no-splash
uv run python -m build
uv run twine check dist/*
```

Result for this `v0.1.0-alpha` preparation pass:

- `uv sync --extra dev`: passed.
- `uv run ruff check .`: passed.
- `uv run python -m compileall stackmint_gateway examples tests`: passed.
- `uv run pytest`: passed, `183 passed`.
- `uv run bandit --ini .bandit -r stackmint_gateway examples tests`: passed, no issues identified. Bandit scans runtime/example code using `.bandit` exclusions.
- `uv run pip-audit`: passed, no known vulnerabilities found.
- `uv run detect-secrets scan --baseline .secrets.baseline`: passed. Timestamp-only `.secrets.baseline` churn should be restored before release; new findings should be reviewed intentionally.
- `uv run stackmint demo --no-splash --no-delay`: passed.
- `uv run stackmint demo --json`: passed.
- `uv run stackmint doctor --no-splash`: passed.
- `uv run stackmint doctor --json`: passed.
- `uv run stackmint help`: passed.
- `uv run stackmint mcp --preview --no-splash`: passed.
- `uv run python -m build`: passed, built `stackmint_gateway-0.1.0a1.tar.gz` and `stackmint_gateway-0.1.0a1-py3-none-any.whl`.
- `uv run twine check dist/*`: passed for both artifacts.
- Clean wheel install smoke test: passed after installing dependencies into a temporary virtual environment; package imports and legacy shims resolved.

Optional example extra check:

```bash
uv sync --extra examples
```

Result: passed.

No OpenAI, Anthropic, or Cerebras API keys are required for release
verification.
