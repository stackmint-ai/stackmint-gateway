# Stackmint Gateway

> Status: `v0.1.0-alpha`. This SDK is ready for early developer testing and
> feedback. It is not a standalone centralized authorization gateway; optional
> control-plane client hooks require compatible Stackmint backend endpoints.

> Package status: alpha. TestPyPI validation is recommended before publishing to
> PyPI.

Stackmint Gateway is an open-source Python SDK for AI agent governance. It helps
developers connect agent runtimes to a governance layer where agent identity,
configuration, tools, budgets, approvals, and execution state can be tracked.

## What It Is

Stackmint Gateway connects AI agents to runtime governance controls. The first
connector is LangChain, exposed through `GovernedAgent`, which wraps any
LangChain `Runnable`.

Today the SDK can:

- Sync agent configuration.
- Sync tool inventory.
- Pull remote policy from the Stackmint Gateway API.
- Apply local runtime guardrails such as budget checks and circuit breakers.
- Enforce local tool policy when tools are wrapped before agent construction.
- Add lightweight LangChain callback telemetry.
- Record completed, failed, and blocked executions.
- Redact and truncate telemetry payloads before recording.

Stackmint Gateway is not a replacement for observability platforms such as
LangSmith or Braintrust. It is also not a standalone centralized authorization
gateway. The alpha SDK includes optional client hooks for centralized
authorization, budget reservation/commit, approval requests, and tool events,
but those flows require compatible Stackmint backend endpoints.

## Why Governance, Not Just Observability

Observability tools are strong for traces, debugging, evaluation, and post-run
analysis. Stackmint Gateway focuses on runtime governance signals: what should
be allowed, blocked, approved, or recorded before and during execution.

Stackmint Gateway applies runtime governance signals before and during
execution: remote status checks, local budget guardrails, wrapped-tool
permissions, HITL gates, circuit breaker behavior, safe telemetry, and
execution state reporting.

| Capability | Standard Observability | Stackmint Gateway |
|---|---|---|
| Primary role | Reactive tracing, debugging, evaluation, and experiment analysis after an agent run. | Runtime governance signals for agent identity, tools, budgets, approvals, execution state, and local enforcement hooks. |
| Runtime control | Usually observes agent behavior without deciding whether the agent should continue, pause, or be blocked. | Checks remote agent status before execution and locally blocks when the returned status is `blocked` or `suspended`. |
| Human-in-the-loop gates | Can help review traces or annotate outputs after execution. | Represents approval policy through fields such as `hitl_conditions` and `require_approval_for`, and provides local wrapped-tool approval gates. |
| Budget enforcement | Typically reports cost, latency, token usage, or evaluation metrics after the fact. | Supports local/session budget guardrails from `budget_ceiling_cents`; strict centralized budget reservation requires backend authorization support. |
| Tool governance | Often shows which tools were called during a trace. | Syncs tool inventory and enforces permitted tools only when policy-wrapped tools are passed into the LangChain agent before construction. |
| Circuit breakers | Useful for detecting failures, regressions, or unsafe behavior once telemetry has been collected. | Blocks locally after repeated underlying agent failures and records blocked execution state. |
| Failure mode | Observability outages usually affect trace visibility, not the agent runtime itself. | `GovernedAgent` supports `fail_open=True` by default for gateway connectivity and telemetry failures. Deliberate policy blocks do not fail open. |

Stackmint Gateway can run alongside tracing and evaluation platforms. Use
LangSmith, Braintrust, or similar tools for trace inspection and quality
analysis; use Stackmint Gateway for runtime policy signals, local guardrails,
and execution-state reporting.

## 5-Minute Quickstart

The fastest local smoke test is the CLI demo. It does not need a model-provider
API key or a Stackmint workspace.

No OpenAI, Anthropic, Cerebras, or Stackmint API key is required for the local
governance demo.

```bash
uv sync --extra dev
uv run stackmint demo
uv run stackmint doctor --no-splash
```

After the package is published, the intended install path will be:

```bash
pip install stackmint-gateway
stackmint demo
```

To connect examples or governed agents to a Stackmint workspace, set a gateway
API key:

```bash
export STACKMINT_GATEWAY_API_KEY=""
uv run stackmint doctor --no-splash
```

Wrap an existing LangChain agent:

```python
from stackmint_gateway.langchain import GovernedAgent

agent = GovernedAgent(
    langchain_agent,
    name="support-agent",
    description="Customer support agent governed through Stackmint Gateway",
    framework="langchain",
    model="your-model-name",
    sync_on_init=True,
)

result = agent.invoke({"messages": [...]})
```

For actual tool enforcement, wrap tools before constructing the LangChain agent:

```python
from langchain.agents import create_agent
from stackmint_gateway.langchain import GovernedAgent, StackmintToolPolicy

raw_tools = [search_tool, ticket_tool]

tool_policy = StackmintToolPolicy(
    permitted_tool_slugs={"search_tool", "ticket_tool"},
    require_approval_for={"ticket_tool"},
)

langchain_agent = create_agent(
    model=model,
    tools=tool_policy.governed_tools(raw_tools),
)

agent = GovernedAgent(
    langchain_agent,
    name="support-agent",
    tools=raw_tools,
    permitted_tool_slugs=sorted(tool_policy.permitted_tool_slugs),
    require_approval_for=sorted(tool_policy.require_approval_for),
)
```

`STACKMINT_GATEWAY_API_KEY` is required only when you want to sync with a
Stackmint workspace. Provider-specific LLM API keys are required only by
provider-backed examples such as OpenAI, Anthropic, or Cerebras.

## CLI Commands

Stackmint Gateway ships with two equivalent console commands:

```bash
stackmint
stackmint-gateway
```

Use the CLI for the local demo, environment checks, MCP server startup, and
packaged examples:

```bash
stackmint
stackmint doctor
stackmint demo
stackmint demo --mode local
stackmint demo --mode mock-control-plane
stackmint demo --speed fast
stackmint demo --speed normal
stackmint demo --speed cinematic
stackmint demo --no-delay
stackmint demo --clear --speed cinematic
stackmint demo --json
stackmint demo --no-splash
stackmint init
stackmint example langchain
stackmint example langchain --json
stackmint example langchain --debug
stackmint mcp
stackmint mcp --preview
stackmint check --print
stackmint help
stackmint help demo
stackmint help mcp
stackmint version
```

`stackmint demo` is the recommended no-key local demo. It shows local policy,
allowed tools, HITL approval behavior, blocked tools, local budget blocking,
telemetry redaction, and a final governance summary without requiring OpenAI,
Anthropic, Cerebras, or Stackmint API keys. Local demo mode does not make
network calls.

`stackmint demo --mode mock-control-plane` uses a deterministic fake control
plane to show authorization, budget reservation rejection, approval-required
decisions, and tool-event recording. It does not make network calls or imply
that a live backend is connected.

For recording a launch video, use:

```bash
stackmint demo --clear --speed cinematic
```

For repeat local testing, use:

```bash
stackmint demo --no-delay
```

`stackmint doctor` checks Python, package import status, gateway configuration,
optional provider keys, MCP support, and example dependencies. It reports
whether keys are present, but never prints secret values.

`stackmint example langchain` is a provider-aware smoke test for the LangChain
`Runnable` wrapper path. The default fake provider requires no model-provider
API key. Use `stackmint demo` for the full governance walkthrough.

### Help

```bash
stackmint help
stackmint help demo
stackmint help doctor
stackmint help mcp
```

Use `stackmint help` for the curated command menu. Use
`stackmint <command> --help` for command-specific options.

### Environment and provider detection

`stackmint doctor` performs a lightweight local readiness check. It detects
installed agent frameworks, optional LangChain provider integrations, MCP
support, and relevant environment variables without printing secrets.

It is not a full SBOM scanner and does not scan your codebase.

```bash
stackmint doctor
stackmint doctor --json
```

Example output:

```text
[CHECK] LangChain Core: installed
[CHECK] MCP SDK: missing
[INFO] Run `uv sync --extra mcp` to enable the MCP governance server
[CHECK] OPENAI_API_KEY: missing
[RESULT] Doctor complete
```

`stackmint init` writes a safe `.env.example` scaffold. It writes `.env` only
when `--write-env` or `--env` is passed, and it does not overwrite existing
files unless `--force` is used.

`stackmint mcp` starts the MCP governance server over stdio. It does not print a
splash or decorative output to stdout because MCP stdio must remain
protocol-clean. Use `stackmint mcp --preview` for a human-facing list of MCP
governance tools, resources, and prompts.

`STACKMINT_NO_SPLASH=1` disables splash output globally. Command-level
suppression is also available through `--no-splash`, `--quiet`, and `--json`
where supported.

### Terminal output standard

Stackmint CLI commands use a fixed event vocabulary:

| Tag | Meaning |
|---|---|
| `[BOOT]` | Startup, version, selected mode, or selected provider. |
| `[POLICY]` | Policy loaded, refreshed, or summarized. |
| `[RUN]` | Demo or command action currently being exercised. |
| `[CHECK]` | Preflight checks such as budget, env, provider, and config. |
| `[ALLOW]` | Action allowed and executed. |
| `[APPROVAL]` | Human approval required or decided. |
| `[BLOCK]` | Governance policy blocked execution or a tool call. |
| `[SECURITY]` | Payload redaction, truncation, or telemetry safety. |
| `[RECORD]` | Execution or governance event recorded. |
| `[RESULT]` | Final user-facing result. |
| `[ERROR]` | Unexpected user-facing error. |
| `[INFO]` | Helpful context or next step. |

For automation, use `--json` to suppress splash, color, pacing delays, and
return structured events:

```bash
stackmint demo --json
stackmint example langchain --json
stackmint doctor --json
stackmint version --json
stackmint mcp --preview --json
STACKMINT_NO_SPLASH=1 stackmint demo
NO_COLOR=1 stackmint demo
STACKMINT_NO_DELAY=1 stackmint demo
STACKMINT_DEMO_SPEED=cinematic stackmint demo
```

`stackmint mcp` does not print a splash because stdio transport must remain
protocol-clean. `stackmint mcp --preview` is the human-facing inspection mode.

For polished terminal output, prefer `stackmint example langchain`. For raw
contributor debugging, you can run the Python file directly:

```bash
uv run python examples/langchain_example.py
```

The MCP module can also be run directly when debugging the stdio server:

```bash
uv run python -m stackmint_gateway.mcp_server
```

## Governance Demo

The local demo shows allowed tools, HITL approval, blocked tools, budget
guardrails, telemetry redaction, and execution recording without a provider key
or live backend. It does not make network calls.

```bash
stackmint demo
stackmint demo --mode local
stackmint demo --mode mock-control-plane
stackmint demo --speed cinematic
stackmint demo --no-delay
stackmint demo --clear --speed cinematic
stackmint demo --json
```

The demo uses deterministic fake/local behavior. Provider-backed model behavior
is demonstrated separately through `stackmint example langchain`.
Human terminal mode uses packet-based pacing by default. `--json`, non-TTY
output, CI, `--no-delay`, or `STACKMINT_NO_DELAY=1` disable pacing.
The human terminal demo ends with a governance recap table; `--json` returns the
same recap as structured data.

## Current Governance Capabilities

Today Stackmint Gateway supports:

- Agent configuration sync.
- Tool inventory sync.
- Remote agent status checks before execution.
- Local tool permission enforcement through wrapped tools.
- HITL approval wrappers for tools.
- Local session budget guardrails with preflight input estimates and post-run reconciliation.
- Local circuit breaker protection after repeated underlying agent failures.
- Execution recording for completed, failed, and blocked runs.
- Fail-open or fail-closed behavior for gateway connectivity issues.
- SDK-level telemetry redaction and truncation.
- Idempotency headers and retry/backoff for safe gateway writes.

These capabilities fall into three buckets:

- **Telemetry/reporting:** agent configuration sync, tool inventory sync, callback event capture, and execution recording.
- **Local enforcement:** local budget checks, circuit breaker state, policy-wrapped tool permissions, and terminal or callback-based HITL approvals.
- **Remote control-plane policy:** remote agent status, permitted tools, approval lists, and budget ceilings are pulled from Stackmint when available.

The SDK can block execution when it has a clear local or remote policy signal.
Budget checks are local/session guardrails unless the optional remote budget
flow is enabled against a compatible backend that supports reservation and
commit endpoints.

## Centralized Control Plane Support

The SDK also includes optional client and `GovernedAgent` hooks for compatible
Stackmint backend control-plane endpoints:

- Centralized execution authorization before a LangChain run.
- Budget reservation before execution and budget commit after execution.
- Approval request hooks when the backend returns `waiting_approval`.
- Backend-recorded tool-level events for policy-wrapped tools.

These flows are disabled by default for compatibility. Existing local
governance remains available without backend control-plane endpoints.

```python
from stackmint_gateway.langchain import GovernedAgent

agent = GovernedAgent(
    langchain_agent,
    api_key="...",
    remote_authorization=True,
    remote_budget=True,
    remote_approvals=True,
    record_tool_events=True,
)
```

Centralized authorization requires compatible Stackmint backend endpoints. If a
backend does not expose those endpoints and `fail_open=True`, the SDK stores the
control-plane error and falls back to existing local behavior for connectivity
or endpoint availability failures. Deliberate remote decisions such as `block`,
`budget_exceeded`, or `waiting_approval` do not fail open.

## MCP Governance Server

Stackmint Gateway includes an optional MCP governance server for MCP-compatible
clients. MCP lets agents discover and call tools; the Stackmint server exposes
governance actions for those agent executions and tool calls. It does not run
arbitrary customer tools.

The server is intended for:

- Policy lookup.
- Centralized execution authorization when compatible backend endpoints exist.
- Safe execution recording.
- Budget reservation and commit.
- Approval request hooks.
- Tool-level governance event recording.

Install the optional MCP extra:

```bash
uv sync --extra mcp
```

Run the server over stdio:

```bash
STACKMINT_GATEWAY_API_KEY=...
uv run python -m stackmint_gateway.mcp_server
```

Run in read-only mode:

```bash
STACKMINT_MCP_READ_ONLY=true uv run python -m stackmint_gateway.mcp_server
```

Mutating governance actions require confirmation by default. Callers must pass
`confirmed=true` when `STACKMINT_MCP_REQUIRE_CONFIRMATION=true`.

Configuration:

```bash
STACKMINT_GATEWAY_API_KEY=
STACKMINT_GATEWAY_BASE_URL=http://127.0.0.1:5173/api
STACKMINT_MCP_READ_ONLY=false
STACKMINT_MCP_REQUIRE_CONFIRMATION=true
STACKMINT_MCP_RECORD_PAYLOADS=true
```

Example MCP client configuration:

```json
{
  "mcpServers": {
    "stackmint-governance": {
      "command": "uv",
      "args": ["run", "python", "-m", "stackmint_gateway.mcp_server"],
      "env": {
        "STACKMINT_GATEWAY_API_KEY": ""
      }
    }
  }
}
```

Available governance tools:

- `stackmint_get_agent_policy`
- `stackmint_authorize_execution`
- `stackmint_record_execution`
- `stackmint_reserve_budget`
- `stackmint_commit_budget`
- `stackmint_create_approval_request`
- `stackmint_get_approval_decision`
- `stackmint_record_tool_event`

Available resources:

- `stackmint://agent/me`
- `stackmint://agent/policy`
- `stackmint://agent/executions/latest`
- `stackmint://approvals/{approval_request_id}`

Available prompts:

- `stackmint_governance_review`
- `stackmint_incident_summary`

The MCP server sanitizes payloads before sending them through SDK calls and
before returning responses. For stricter environments, set
`STACKMINT_MCP_RECORD_PAYLOADS=false`.

## Execution Lifecycle

```text
LangChain Runnable / Agent
        ↓
GovernedAgent
        ↓
Policy refresh + local checks
        ↓
Wrapped tools / HITL / budget / circuit breaker
        ↓
Underlying agent execution
        ↓
Safe telemetry + execution record
        ↓
Stackmint Gateway API
```

1. `GovernedAgent` refreshes local or remote policy.
2. It checks remote agent status.
3. It checks local/session budget guardrails.
4. It attaches Stackmint callback telemetry to the LangChain config.
5. It invokes the underlying LangChain `Runnable`.
6. It records completed, failed, or blocked execution state.
7. It tracks local token/cost usage when usage metadata is available.
8. It applies the local circuit breaker after repeated underlying failures.
9. It fails open only for gateway connectivity or telemetry failures when `fail_open=True`.
10. It does not fail open for deliberate policy blocks such as remote `blocked` status, local budget block, circuit-breaker block, or wrapped-tool denial.

Tool enforcement only applies to tools wrapped before LangChain agent
construction. Passing `tools=[...]` to `GovernedAgent` syncs inventory; it does
not mutate an already-created LangChain agent.

## Installation

The core package supports Python 3.10+ and uses `uv`.

```bash
uv sync
```

For development and the fake/local example:

```bash
uv sync --extra dev
```

After the package is published, the intended install command will be:

```bash
pip install stackmint-gateway
```

Until then, use the local `uv` workflow above.

For a LangChain-only provider-backed example setup:

```bash
uv sync --extra examples
```

Provider-specific extras are documented in [Model Provider Examples](#model-provider-examples).

## Configuration

Set the gateway API key when you want the SDK to sync with a Stackmint
workspace:

```bash
export STACKMINT_GATEWAY_API_KEY=""
```

Without `STACKMINT_GATEWAY_API_KEY`, `GovernedAgent` still wraps the LangChain
`Runnable` and can run local wrapper/tool-policy patterns, but it does not sync
or record execution state to a Stackmint workspace.

### Statefulness and Concurrency

`GovernedAgent` tracks local budget guardrails such as `current_session_cost` in
memory. It is designed to be instantiated per session or per request. Do not
share one `GovernedAgent` instance across concurrent requests in an async
framework such as FastAPI; local budget counters are not thread-locked and are
not a centralized budget authority. For concurrent budget enforcement across
multiple workers, enable the remote budget reservation hooks with a compatible
Stackmint managed backend.

By default, the client sends data to:

```text
http://127.0.0.1:5173/api
```

Override the API base URL with:

```bash
export STACKMINT_GATEWAY_BASE_URL="https://your-stackmint-api.example.com/api"
```

Example-specific model settings:

```bash
export STACKMINT_EXAMPLE_PROVIDER="fake"
export STACKMINT_EXAMPLE_MODEL="optional-model-name"
```

## LangChain Usage

Wrap any LangChain `Runnable` with `GovernedAgent` to sync metadata, pull
runtime policy, apply local budget/circuit-breaker checks, and record execution
state.

```python
from stackmint_gateway.langchain import GovernedAgent

agent = GovernedAgent(
    langchain_agent,
    name="support-agent",
    description="Customer support agent governed through Stackmint Gateway",
    framework="langchain",
    model="your-model-name",
    tools=[search_tool, ticket_tool],
    sync_on_init=True,
)

result = agent.invoke({"messages": [...]})
```

New code should prefer package imports such as
`stackmint_gateway.langchain`. The old top-level imports
(`core`, `security`, and `langchain_connector`) are kept as temporary
compatibility shims for the alpha.

When invoked, the wrapper can:

- Pull remote policy with `refresh_policy()`.
- Block when Stackmint reports the remote agent as `blocked` or `suspended`.
- Enforce local session budget preflight checks.
- Stop repeated failure loops with a local circuit breaker.
- Record completed, failed, and blocked executions.
- Fail open by default for gateway connectivity failures while still honoring deliberate policy blocks.

`GovernedAgent.ainvoke(...)` runs synchronous gateway policy refresh and
execution recording in worker threads so the async path avoids direct gateway
I/O on the event loop.

## Tool Enforcement

Tool sync and tool enforcement are different.

Passing `tools=[...]` to `GovernedAgent` keeps the Stackmint tool inventory in
sync. To actually prevent unauthorized tool execution, pass policy-wrapped tools
into the LangChain agent before creating it.

> Common mistake: `tools=[...]` on `GovernedAgent` does not mutate an
> already-created LangChain agent. To block unauthorized tools, wrap the tools
> before passing them to `create_agent(...)`.

```python
from langchain.agents import create_agent
from stackmint_gateway.langchain import GovernedAgent, StackmintToolPolicy

raw_tools = [search_tool, ticket_tool]

tool_policy = StackmintToolPolicy(
    permitted_tool_slugs={"search_tool", "ticket_tool"},
    require_approval_for={"ticket_tool"},
)

langchain_agent = create_agent(
    model=model,
    tools=tool_policy.governed_tools(raw_tools),
)

agent = GovernedAgent(
    langchain_agent,
    name="support-agent",
    tools=raw_tools,
    permitted_tool_slugs=sorted(tool_policy.permitted_tool_slugs),
    require_approval_for=sorted(tool_policy.require_approval_for),
)
```

If a wrapped tool is not permitted, it raises `StackmintToolNotAllowedError`
before the original tool runs. If a tool requires approval, the default
open-source implementation prompts in the terminal:

```text
Stackmint HITL: Tool [ticket_tool] requires approval to execute. Approve? (y/n):
```

Rejected approvals return `Action rejected by human supervisor.` to the agent
and are recorded in local telemetry.

For non-terminal environments, pass a custom approval callback:

```python
tool_policy = StackmintToolPolicy(
    permitted_tool_slugs={"search_tool", "ticket_tool"},
    require_approval_for={"ticket_tool"},
    approval_fn=lambda tool_name: approval_service_allows(tool_name),
)
```

The default open-source HITL implementation uses terminal input. Production
approval workflows should be implemented through a custom `approval_fn` or
future backend approval APIs.

If the LangChain agent has already been constructed, `GovernedAgent` cannot
safely mutate its internal tool registry. Create wrapped tools first with
`StackmintToolPolicy`, or use `GovernedAgent.governed_tools()` in integrations
where a wrapper instance exists before the final LangChain agent is built.

## Telemetry Security

Stackmint Gateway redacts and truncates telemetry payloads by default before
execution records are sent to the gateway API.

Inputs, outputs, errors, and caller-provided execution metadata are recorded by
default, but they are sanitized first. The SDK masks common secrets, API keys,
bearer tokens, private keys, cookies, sessions, credential-like values, and
email addresses. Large payloads and long strings are truncated. Circular or
non-serializable objects are replaced with safe serialization-error markers.

```python
from stackmint_gateway.langchain import GovernedAgent
from stackmint_gateway.security import StackmintTelemetrySecurityConfig

agent = GovernedAgent(
    langchain_agent,
    record_inputs=True,
    record_outputs=True,
    record_errors=True,
    telemetry_security=StackmintTelemetrySecurityConfig(
        redact_payloads=True,
        max_payload_bytes=64_000,
        max_string_length=8_000,
    ),
)
```

For stricter environments:

```python
agent = GovernedAgent(
    langchain_agent,
    record_inputs=False,
    record_outputs=False,
    record_errors=False,
)
```

If `record_errors=False`, the SDK records the safe exception type and a generic
message rather than the raw exception text. These controls are SDK-level
telemetry hygiene, not a replacement for enterprise DLP or backend-side policy
enforcement.

## Security Defaults

| Area | Default |
|---|---|
| Input recording | Enabled, redacted and truncated before sending. |
| Output recording | Enabled, redacted and truncated before sending. |
| Error recording | Enabled, redacted and truncated before sending. |
| Metadata recording | Enabled, sanitized before sending. |
| Secret handling | Common API keys, bearer tokens, private keys, cookies, sessions, credentials, tokens, auth fields, and emails are masked. |
| Payload size | Large payloads and long strings are truncated. |
| Gateway connectivity failure | `fail_open=True` by default. |
| Deliberate policy block | Does not fail open. |
| Budget enforcement | Local/session-level guardrail only. |
| Tool enforcement | Local enforcement only for policy-wrapped tools passed into the agent before construction. |
| Enterprise DLP | Not provided by the SDK. |

## Model Provider Examples

Stackmint Gateway is model-provider agnostic. The LangChain connector wraps any
LangChain `Runnable`, so the governance layer is not tied to Cerebras, OpenAI,
Anthropic, or any specific LLM provider.

The example supports multiple providers through environment variables:

| Provider | Install | Environment variables |
|---|---|---|
| Fake/local | `uv sync --extra examples` | `STACKMINT_EXAMPLE_PROVIDER=fake` |
| OpenAI | `uv sync --extra examples-openai` | `STACKMINT_EXAMPLE_PROVIDER=openai`, `OPENAI_API_KEY`, optional `STACKMINT_EXAMPLE_MODEL` |
| Anthropic | `uv sync --extra examples-anthropic` | `STACKMINT_EXAMPLE_PROVIDER=anthropic`, `ANTHROPIC_API_KEY`, optional `STACKMINT_EXAMPLE_MODEL` |
| Cerebras | `uv sync --extra examples-cerebras` | `STACKMINT_EXAMPLE_PROVIDER=cerebras`, `CEREBRAS_API_KEY`, optional `STACKMINT_EXAMPLE_MODEL` |

No-key local smoke test:

```bash
uv sync --extra examples
STACKMINT_EXAMPLE_PROVIDER=fake uv run stackmint example langchain
STACKMINT_EXAMPLE_PROVIDER=fake uv run stackmint example langchain --json
```

For raw LangChain message details while debugging:

```bash
STACKMINT_EXAMPLE_PROVIDER=fake uv run stackmint example langchain --debug
```

OpenAI:

```bash
uv sync --extra examples-openai
STACKMINT_EXAMPLE_PROVIDER=openai OPENAI_API_KEY=... uv run stackmint example langchain
```

Anthropic:

```bash
uv sync --extra examples-anthropic
STACKMINT_EXAMPLE_PROVIDER=anthropic ANTHROPIC_API_KEY=... uv run stackmint example langchain
```

Cerebras:

```bash
uv sync --extra examples-cerebras
STACKMINT_EXAMPLE_PROVIDER=cerebras CEREBRAS_API_KEY=... uv run stackmint example langchain
```

`STACKMINT_GATEWAY_API_KEY` connects the example to a Stackmint workspace.
Without it, developers can still inspect local policy wrapping and fail-open
behavior. Real tool-calling behavior may differ by provider and model. The fake
provider verifies wrapper setup locally; provider-backed examples exercise real
model tool-calling behavior.

The Cerebras optional dependency is guarded with a Python-version marker for
upstream compatibility (`>=3.11,<3.13`). On newer Python versions, the extra can
resolve, but the Cerebras provider branch requires a Python version supported by
`langchain-cerebras`.

## Core Client Reference

Most LangChain users should start with `GovernedAgent`. The lower-level
`CoreStackmintGateway` client is useful when building another framework
connector or custom runtime integration.

`CoreStackmintGateway` is a typed HTTP client. It serializes the Pydantic
payloads you pass to it, but it does not automatically redact or truncate direct
caller-provided payloads. Use `GovernedAgent`, the MCP helper handlers, or
`stackmint_gateway.security.sanitize_payload(...)` when recording untrusted
inputs, outputs, errors, or metadata directly.

| Method | Purpose |
|---|---|
| `get_me()` | Pull the current remote agent configuration and policy state. |
| `patch_config(...)` | Update remote agent configuration. |
| `sync_tools(...)` | Sync tool inventory for the current gateway agent. |
| `record_execution(...)` | Record completed, failed, or blocked execution state. |
| `authorize_execution(...)` | Call optional backend preflight authorization when available. |
| `reserve_budget(...)` | Call optional backend budget reservation when available. |
| `commit_budget(...)` | Commit or release an optional backend budget reservation. |
| `record_tool_event(...)` | Record an optional backend tool-level governance event. |
| `create_approval_request(...)` | Create an optional backend approval request. |
| `get_approval_decision(...)` | Fetch an optional backend approval decision. |
| `sync_agent(...)` | Sync agent config and tool inventory together. |

## Local Checks

Run the release verification checks:

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

Bandit scans runtime/example code using `.bandit` exclusions; tests are not
treated as production code for Bandit findings.
If `detect-secrets` changes only `.secrets.baseline` timestamp metadata, restore
that file before release. Review any new findings intentionally before updating
the baseline.

## Roadmap

Near-term:

- Additional model-provider-neutral examples.
- MCP governance server alpha hardening and MCP client configuration examples.
- Compatible backend deployment for the SDK's preflight authorization hooks.
- Compatible backend deployment for the SDK's budget reservation and commit hooks.
- Backend approval workflow implementation for SDK approval request hooks.
- Operational hardening for backend tool-level event history tied to execution IDs.
- OpenAI Agents SDK connector.
- Claude native connector.
- Additional MCP client configuration examples.
- CrewAI and AutoGen connectors.
- Custom runtime connector patterns.

Centralized budget authorization, backend approval workflows, and
workspace-level audit trails are planned as part of the managed Stackmint
control plane.

These are planned directions, not current v0.1.0-alpha capabilities.

## Enterprise and Managed Governance

Stackmint Gateway is the open-source SDK for adding governance primitives to
agent runtimes: policy refresh, wrapped-tool enforcement, HITL approval hooks,
local/session budget guardrails, circuit breaker behavior, and safe telemetry.

For teams that need a managed governance control plane, Stackmint also offers
an enterprise version with centralized policy management, workspace-level audit
trails, approval workflows, budget authorization, tool-level event history,
SSO/SAML, deployment support, and custom integration work.

If you are evaluating Stackmint Gateway for production agent governance,
contact us at [hello@stackmint.ai](mailto:hello@stackmint.ai) or visit
[stackmint.ai](https://stackmint.ai).

## License

Apache License 2.0. See [LICENSE](LICENSE).
