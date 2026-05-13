"""Minimal LangChain agent example with three simple tools."""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_cerebras import ChatCerebras
from langchain_connector import GovernedAgent

@tool
def add_numbers(a: float, b: float) -> float:
    """Add two numbers."""

    return a + b


@tool
def multiply_numbers(a: float, b: float) -> float:
    """Multiply two numbers."""

    return a * b


@tool
def get_current_time(timezone: str = "Europe/Paris") -> str:
    """Return the current time in the requested timezone."""

    current_time = datetime.now(ZoneInfo(timezone))
    return current_time.isoformat(timespec="seconds")


def build_agent():
    """Create a small agent wired to three demo tools."""

    if not os.getenv("CEREBRAS_API_KEY"):
        raise RuntimeError(
            "Set CEREBRAS_API_KEY before running this example with Cerebras."
        )

    model = ChatCerebras(model="qwen-3-235b-a22b-instruct-2507")
    return create_agent(
        model=model,
        tools=[add_numbers, multiply_numbers, get_current_time],
        system_prompt=(
            "Tu es un agent basique de démonstration. "
            "Utilise les tools quand c'est pertinent, puis réponds clairement."
        ),
    )

def _print_error(title: str, exc: Exception) -> None:
    print(f"{title}: {exc.__class__.__name__}: {exc}", file=sys.stderr)
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


def main() -> int:
    try:
        agent = GovernedAgent(
            build_agent(),
            api_key=os.getenv("STACKMINT_GATEWAY_API_KEY"),
            name="langchain_exemple",
            model="qwen-3-235b-a22b-instruct-2507",
            tools=[add_numbers, multiply_numbers, get_current_time],
            sync_on_init=True,
        )
        result = agent.invoke(
            {
                "messages": [
                    HumanMessage(
                        content=(
                            "Fais une démonstration rapide avec les trois tools "
                            "disponibles."
                        )
                    )
                ]
            }
        )

        if agent.state.last_sync_error is not None:
            _print_error("Gateway sync error", agent.state.last_sync_error)
        if agent.state.last_execution_error is not None:
            _print_error("Execution sync error", agent.state.last_execution_error)

        for message in result["messages"]:
            role = message.__class__.__name__
            content = getattr(message, "content", "")
            if getattr(message, "tool_calls", None):
                print(f"{role}: tool_calls={message.tool_calls}")
            else:
                print(f"{role}: {content}")
        return 0
    except Exception as exc:
        _print_error("Execution failed", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
