# Stackmint Gateway

Stackmint Gateway is an open source gateway for agent governance and monitoring.
It helps teams connect their agents to a governance layer where agent identity,
configuration, tools, budgets, approvals, and executions can be tracked from one
place.

The project is backed by [Stackmint.ai](https://stackmint.ai), a company focused
on agentic governance. This module is the open source integration layer that
lets agent frameworks report their activity to a Stackmint governance workspace.

## Project Goal

The goal of Stackmint Gateway is to provide a framework-agnostic governance
adapter for AI agents.

Today, the repository ships with a first connector for LangChain. The long-term
direction is to support every major type of agent runtime, including OpenAI
Agents, Claude-based agents, CrewAI, AutoGen, custom internal agents, and other
frameworks.

Stackmint Gateway is designed to answer questions such as:

- Which agents are active in my workspace?
- Which tools can each agent use?
- What budget, approval, or HITL policies apply to an agent?
- What executions happened, when, and with which result?
- Which executions failed, were blocked, or require approval?

## Current Status

This repository currently contains:

- A typed Python client for the Stackmint external gateway API.
- A LangChain connector through `GovernedAgent`.
- Tool synchronization for connected agents.
- Execution reporting for successful and failed agent runs.
- A minimal LangChain example using three demo tools.

LangChain is the first supported connector, not the final scope of the project.
The core gateway models already include framework values for `langchain`,
`crewai`, `claude`, `openai`, `autogen`, and `custom`.

## Installation

This project uses Python 3.13 and `uv`.

```bash
uv sync
```

## Configuration

Set the gateway API key used by the agent connector:

```bash
export STACKMINT_GATEWAY_API_KEY="your_gateway_api_key"
```

By default, the client sends data to:

```text
http://127.0.0.1:5173/api
```

You can override this with:

```bash
export STACKMINT_GATEWAY_BASE_URL="https://your-stackmint-api.example.com/api"
```

The example agent also requires a Cerebras API key:

```bash
export CEREBRAS_API_KEY="your_cerebras_api_key"
```

## LangChain Usage

Wrap any LangChain `Runnable` with `GovernedAgent` to sync its metadata, tools,
and executions with Stackmint Gateway.

```python
from langchain_connector import GovernedAgent

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

When invoked, the wrapper can:

- Synchronize agent configuration.
- Register available tools.
- Record completed executions.
- Record failed executions with error metadata.
- Fail open by default so governance telemetry does not break the agent runtime.

## Example

Run the minimal LangChain example:

```bash
uv run python langchain_exemple.py
```

The example creates a LangChain agent with three tools:

- `add_numbers`
- `multiply_numbers`
- `get_current_time`

It then wraps the agent with `GovernedAgent`, syncs it to Stackmint Gateway, runs
a short demonstration, and records the execution.

## Core Client

The lower-level `CoreStackmintGateway` client exposes direct methods for the
gateway API:

- `get_me()`
- `patch_config(...)`
- `sync_tools(...)`
- `record_execution(...)`
- `sync_agent(...)`

These methods are useful when building new framework connectors or integrating a
custom agent runtime.

## Roadmap

The next major step is to make Stackmint Gateway a universal governance layer
for agentic systems.

Planned connector directions include:

- OpenAI Agents SDK
- Claude and MCP-based agents
- CrewAI
- AutoGen
- Custom Python agents
- Additional tool and execution policy hooks
- Richer approval and budget enforcement flows

## License

This project is intended to be open source. Add the repository license before
publishing or distributing it.
