# Changelog

## v0.1.0-alpha

Initial alpha release of Stackmint Gateway.

Python package version: `0.1.0a1`
Release label/tag: `v0.1.0-alpha`

### Added

- LangChain `GovernedAgent` wrapper for runtime governance.
- Remote policy refresh and remote `blocked` / `suspended` status checks.
- Local/session budget guardrails with preflight estimates and post-run reconciliation.
- Local circuit breaker for repeated underlying agent failures.
- Policy-wrapped tool enforcement with `StackmintToolPolicy`.
- Human-in-the-loop approval wrapper with terminal approval and injectable `approval_fn`.
- Safe execution telemetry recording for completed, failed, and blocked runs.
- Telemetry redaction, truncation, circular-reference handling, and safe serialization.
- Sanitized caller metadata and reserved SDK/security metadata protection.
- Idempotency-aware gateway request retries.
- SDK-specific gateway exceptions.
- Optional SDK hooks and typed client methods for centralized authorization,
  budget reservation/commit, approval requests, and tool-level governance
  events when compatible backend endpoints are available.
- Optional alpha MCP governance server for policy lookup, execution authorization
  hooks, safe execution recording, budget reserve/commit hooks, approval hooks,
  and tool-event recording. The server does not execute arbitrary customer
  tools.
- `stackmint` and `stackmint-gateway` console commands with curated
  `stackmint help` output.
- No-key `stackmint demo` governance walkthrough with branded event tags,
  telemetry redaction output, final recap table, JSON output, pacing controls,
  and `--clear --speed cinematic` recording mode.
- `stackmint doctor` detector for framework, provider, MCP, and environment
  readiness.
- `stackmint example langchain` provider-aware smoke test with polished terminal
  output and debug mode for raw LangChain messages.
- Importable `stackmint_gateway` package layout with temporary flat-module compatibility shims.
- Local package build metadata for wheel and sdist validation.
- Model-provider-neutral LangChain example with fake/local, OpenAI, Anthropic, and Cerebras provider options.
- CI/security checks for tests, linting, Bandit, pip-audit, and detect-secrets.

### Alpha Limitations

- Budget enforcement is local/session-level only.
- Centralized authorization, budget reservation/commit, approval workflows, and
  tool-level event history require compatible backend endpoints and managed
  control-plane deployment.
- Tool enforcement is real only when developers pass policy-wrapped tools into the LangChain agent before construction.
- Default HITL approval is local/terminal-based unless a custom `approval_fn` is provided.
- The SDK provides telemetry hygiene, not enterprise DLP.
- Native OpenAI Agents SDK, Claude native API, CrewAI, and AutoGen connectors
  are not included in this alpha.
- Native Claude/MCP agent-runtime connector is not included. MCP support is
  limited to the optional Stackmint governance server.
