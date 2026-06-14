"""Minimal LangChain agent example with three simple tools."""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import tool

from stackmint_gateway.langchain import GovernedAgent, StackmintToolPolicy

DEFAULT_MODELS = {
    "fake": "stackmint-fake-chat",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-latest",
    "cerebras": "qwen-3-235b-a22b-instruct-2507",
}
PROVIDER_EXTRAS = {
    "openai": "examples-openai",
    "anthropic": "examples-anthropic",
    "cerebras": "examples-cerebras",
}
PROVIDER_DISPLAY_NAMES = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "cerebras": "Cerebras",
}
PROVIDER_API_KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
}
SUPPORTED_PROVIDERS = tuple(DEFAULT_MODELS)


class ExampleConfigurationError(RuntimeError):
    """Raised when the example is missing provider configuration."""


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


RAW_TOOLS = [add_numbers, multiply_numbers, get_current_time]


class FakeExampleAgent(Runnable[Any, dict[str, list[BaseMessage]]]):
    """Small no-key Runnable used by the fake/local example provider."""

    def __init__(self, model: Runnable[Any, BaseMessage]) -> None:
        self.model = model

    def invoke(
        self,
        input: Any,
        config: Any = None,
        **kwargs: Any,
    ) -> dict[str, list[BaseMessage]]:
        messages = _messages_from_input(input)
        response = self.model.invoke(messages, config=config)
        return {"messages": [*messages, response]}

    async def ainvoke(
        self,
        input: Any,
        config: Any = None,
        **kwargs: Any,
    ) -> dict[str, list[BaseMessage]]:
        messages = _messages_from_input(input)
        response = await self.model.ainvoke(messages, config=config)
        return {"messages": [*messages, response]}


def selected_provider() -> str:
    return os.getenv("STACKMINT_EXAMPLE_PROVIDER", "fake").strip().lower()


def _validate_provider(provider: str) -> None:
    if provider in DEFAULT_MODELS:
        return
    supported = ", ".join(SUPPORTED_PROVIDERS)
    raise ExampleConfigurationError(
        "Unsupported STACKMINT_EXAMPLE_PROVIDER "
        f"'{provider}'. Choose one of: {supported}."
    )


def selected_model_name(provider: str) -> str | None:
    _validate_provider(provider)
    return os.getenv("STACKMINT_EXAMPLE_MODEL")


def resolved_model_name(provider: str) -> str:
    return selected_model_name(provider) or DEFAULT_MODELS[provider]


def _messages_from_input(input: Any) -> list[BaseMessage]:
    if isinstance(input, dict) and isinstance(input.get("messages"), list):
        return list(input["messages"])
    if isinstance(input, list):
        return list(input)
    if isinstance(input, BaseMessage):
        return [input]
    return [HumanMessage(content=str(input))]


def _require_api_key(provider: str, env_var: str) -> None:
    if os.getenv(env_var):
        return
    raise ExampleConfigurationError(
        f"Set {env_var} before running the {provider} provider."
    )


def _provider_dependency_error(provider: str) -> ExampleConfigurationError:
    extra = PROVIDER_EXTRAS[provider]
    provider_name = PROVIDER_DISPLAY_NAMES[provider]
    api_key_name = PROVIDER_API_KEYS[provider]
    return ExampleConfigurationError(
        f"Install {provider_name} example dependencies with "
        f"`uv sync --extra {extra}`. "
        f"Set {api_key_name} before running this provider."
    )


def build_chat_model() -> Any:
    provider = selected_provider()
    _validate_provider(provider)
    model_name = resolved_model_name(provider)

    if provider == "fake":
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        return FakeListChatModel(
            name=model_name,
            responses=[
                "Stackmint fake provider response. The local wrapper, tool policy "
                "setup, telemetry redaction, and fail-open path are available "
                "without a paid model provider."
            ],
        )

    if provider == "openai":
        _require_api_key("OpenAI", "OPENAI_API_KEY")
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise _provider_dependency_error(provider) from exc
        return ChatOpenAI(model=model_name)

    if provider == "anthropic":
        _require_api_key("Anthropic", "ANTHROPIC_API_KEY")
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:
            raise _provider_dependency_error(provider) from exc
        return ChatAnthropic(model=model_name)

    if provider == "cerebras":
        _require_api_key("Cerebras", "CEREBRAS_API_KEY")
        try:
            from langchain_cerebras import ChatCerebras
        except ImportError as exc:
            raise _provider_dependency_error(provider) from exc
        return ChatCerebras(model=model_name)

    raise AssertionError(f"Unhandled provider: {provider}")


def build_agent(tools):
    """Create a small agent wired to three demo tools."""

    provider = selected_provider()
    model = build_chat_model()
    if provider == "fake":
        return FakeExampleAgent(model)

    try:
        from langchain.agents import create_agent
    except ImportError as exc:
        extra = PROVIDER_EXTRAS.get(provider, "examples")
        raise ExampleConfigurationError(
            "Install LangChain agent dependencies with "
            f"`uv sync --extra {extra}`."
        ) from exc

    return create_agent(
        model=model,
        tools=tools,
        system_prompt=(
            "You are a minimal Stackmint Gateway demo agent. "
            "Use the available tools when relevant, then answer clearly."
        ),
    )


def _print_error(title: str, exc: Exception, *, include_traceback: bool = True) -> None:
    print(f"{title}: {exc.__class__.__name__}: {exc}", file=sys.stderr)
    if include_traceback:
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


def main() -> int:
    try:
        provider = selected_provider()
        model_name = resolved_model_name(provider)
        print(f"Stackmint example provider: {provider}")
        print(f"Stackmint example model: {model_name}")
        if provider == "fake":
            print(
                "Using the fake local provider. This smoke test verifies the "
                "LangChain Runnable wrapper path; provider-backed runs exercise "
                "real model tool-calling behavior."
            )
        if not os.getenv("STACKMINT_GATEWAY_API_KEY"):
            print(
                "STACKMINT_GATEWAY_API_KEY is not set. The example will run "
                "local wrapper behavior without syncing to a Stackmint workspace."
            )

        local_tool_policy = StackmintToolPolicy(
            permitted_tool_slugs={tool.name for tool in RAW_TOOLS},
            require_approval_for={"multiply_numbers"},
        )
        governed_tools = local_tool_policy.governed_tools(RAW_TOOLS)

        agent = GovernedAgent(
            build_agent(governed_tools),
            api_key=os.getenv("STACKMINT_GATEWAY_API_KEY"),
            name="langchain_example",
            model=model_name,
            tools=RAW_TOOLS,
            permitted_tool_slugs=sorted(local_tool_policy.permitted_tool_slugs),
            require_approval_for=sorted(local_tool_policy.require_approval_for),
            sync_on_init=True,
        )
        result = agent.invoke(
            {
                "messages": [
                    HumanMessage(
                        content=(
                            "Run a short demonstration with the available tools."
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
    except ExampleConfigurationError as exc:
        _print_error("Example configuration error", exc, include_traceback=False)
        return 1
    except Exception as exc:
        _print_error("Execution failed", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
