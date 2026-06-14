from __future__ import annotations

import builtins
import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

PROVIDER_MODULES = {
    "openai": ("langchain_openai", "OPENAI_API_KEY", "examples-openai"),
    "anthropic": ("langchain_anthropic", "ANTHROPIC_API_KEY", "examples-anthropic"),
    "cerebras": ("langchain_cerebras", "CEREBRAS_API_KEY", "examples-cerebras"),
}
EXAMPLE_MODULE = "examples.langchain_example"


def import_example() -> Any:
    sys.modules.pop(EXAMPLE_MODULE, None)
    return importlib.import_module(EXAMPLE_MODULE)


def test_package_imports_work() -> None:
    import stackmint_gateway
    from stackmint_gateway import GovernedAgent
    from stackmint_gateway.core import CoreStackmintGateway
    from stackmint_gateway.langchain import StackmintToolPolicy
    from stackmint_gateway.security import StackmintTelemetrySecurityConfig

    assert stackmint_gateway.CoreStackmintGateway is CoreStackmintGateway
    assert stackmint_gateway.GovernedAgent is GovernedAgent
    assert stackmint_gateway.StackmintToolPolicy is StackmintToolPolicy
    assert (
        stackmint_gateway.StackmintTelemetrySecurityConfig
        is StackmintTelemetrySecurityConfig
    )


def test_legacy_import_shim_still_works() -> None:
    from core import CoreStackmintGateway as LegacyCoreStackmintGateway
    from langchain_connector import GovernedAgent as LegacyGovernedAgent
    from security import StackmintTelemetrySecurityConfig as LegacySecurityConfig
    from stackmint_gateway.core import CoreStackmintGateway
    from stackmint_gateway.langchain import GovernedAgent
    from stackmint_gateway.security import StackmintTelemetrySecurityConfig

    assert LegacyCoreStackmintGateway is CoreStackmintGateway
    assert LegacyGovernedAgent is GovernedAgent
    assert LegacySecurityConfig is StackmintTelemetrySecurityConfig


def test_import_does_not_require_provider_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in {details[0] for details in PROVIDER_MODULES.values()}:
            raise AssertionError(f"provider package imported at module load: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    module = import_example()

    assert module.selected_provider() == "fake"


def test_default_provider_is_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STACKMINT_EXAMPLE_PROVIDER", raising=False)
    monkeypatch.delenv("STACKMINT_EXAMPLE_MODEL", raising=False)
    module = import_example()

    assert module.selected_provider() == "fake"
    assert module.selected_model_name("fake") is None
    assert module.resolved_model_name("fake") == "stackmint-fake-chat"


def test_selected_model_name_uses_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STACKMINT_EXAMPLE_MODEL", "custom-model")
    module = import_example()

    assert module.selected_model_name("openai") == "custom-model"
    assert module.resolved_model_name("openai") == "custom-model"


def test_fake_provider_builds_model_without_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STACKMINT_EXAMPLE_PROVIDER", "fake")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    module = import_example()

    model = module.build_chat_model()
    message = model.invoke("hello")

    assert "Stackmint fake provider response" in message.content


def test_fake_provider_builds_runnable_agent_without_langchain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STACKMINT_EXAMPLE_PROVIDER", "fake")
    module = import_example()

    agent = module.build_agent([])
    result = agent.invoke({"messages": [module.HumanMessage(content="hello")]})

    assert "messages" in result
    assert "Stackmint fake provider response" in result["messages"][-1].content


def test_unsupported_provider_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STACKMINT_EXAMPLE_PROVIDER", "unknown")
    module = import_example()

    with pytest.raises(RuntimeError, match="Unsupported STACKMINT_EXAMPLE_PROVIDER"):
        module.build_chat_model()


@pytest.mark.parametrize(
    ("provider", "module_name", "api_key_name", "extra_name"),
    [
        (provider, *details)
        for provider, details in PROVIDER_MODULES.items()
    ],
)
def test_real_provider_without_api_key_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    module_name: str,
    api_key_name: str,
    extra_name: str,
) -> None:
    monkeypatch.setenv("STACKMINT_EXAMPLE_PROVIDER", provider)
    monkeypatch.delenv(api_key_name, raising=False)
    module = import_example()

    with pytest.raises(RuntimeError, match=f"Set {api_key_name}"):
        module.build_chat_model()


@pytest.mark.parametrize(
    ("provider", "module_name", "api_key_name", "extra_name"),
    [
        (provider, *details)
        for provider, details in PROVIDER_MODULES.items()
    ],
)
def test_missing_optional_dependency_message_includes_provider_extra(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    module_name: str,
    api_key_name: str,
    extra_name: str,
) -> None:
    monkeypatch.setenv("STACKMINT_EXAMPLE_PROVIDER", provider)
    monkeypatch.setenv(api_key_name, "test-key")
    sys.modules.pop(module_name, None)
    real_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == module_name:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    module = import_example()

    with pytest.raises(RuntimeError, match=extra_name):
        module.build_chat_model()


def test_readme_documents_current_example_and_governance_sections() -> None:
    readme = Path("README.md").read_text()
    old_example_name = "langchain_" + "exemple.py"
    expected_sections = [
        "## What It Is",
        "## Why Governance, Not Just Observability",
        "## 5-Minute Quickstart",
        "## CLI Commands",
        "## Current Governance Capabilities",
        "## Centralized Control Plane Support",
        "## MCP Governance Server",
        "## Execution Lifecycle",
        "## Installation",
        "## Configuration",
        "## LangChain Usage",
        "## Tool Enforcement",
        "## Telemetry Security",
        "## Security Defaults",
        "## Model Provider Examples",
        "## Core Client Reference",
        "## Local Checks",
        "## Roadmap",
        "## Enterprise and Managed Governance",
        "## License",
    ]

    assert "examples/langchain_example.py" in readme
    assert old_example_name not in readme
    section_positions = [readme.index(section) for section in expected_sections]
    assert section_positions == sorted(section_positions)
    assert "Tool sync and tool enforcement are different" in readme
    normalized_readme = " ".join(readme.split())
    assert "does not mutate an already-created LangChain agent" in normalized_readme
    assert "## Model Provider Examples" in readme
    assert "uv sync --extra examples` | `STACKMINT_EXAMPLE_PROVIDER=fake" in readme
    stale_cerebras_phrase = "requires a " + "Cerebras API key"
    assert stale_cerebras_phrase not in readme
    assert "v0.1.0-alpha" in readme
    assert "not a standalone centralized authorization gateway" in readme
    assert "local/session budget guardrails" in readme
    assert "policy-wrapped" in readme
    assert "## Centralized Control Plane Support" in readme
    assert "remote_authorization=True" in readme
    assert (
        "Centralized authorization requires compatible Stackmint backend endpoints"
        in readme
    )
    assert "## Enterprise and Managed Governance" in readme
    assert "hello@stackmint.ai" in readme
    assert (
        "Centralized budget authorization, backend approval workflows, and "
        "workspace-level audit trails are planned as part of the managed "
        "Stackmint control plane."
    ) in normalized_readme
    assert "centralized budget authorization is available" not in normalized_readme
    assert "These flows are disabled by default for compatibility" in normalized_readme
    assert (
        "`CoreStackmintGateway` is a typed HTTP client. It serializes the "
        "Pydantic payloads you pass to it, but it does not automatically "
        "redact or truncate direct caller-provided payloads."
    ) in normalized_readme


def test_release_documents_and_alpha_version_exist() -> None:
    pyproject = Path("pyproject.toml").read_text()
    changelog = Path("CHANGELOG.md").read_text()
    release_notes = Path("RELEASE.md").read_text()

    assert 'version = "0.1.0a1"' in pyproject
    assert Path("RELEASE_CHECKLIST.md").is_file()
    assert "## v0.1.0-alpha" in changelog
    assert "Python package version: `0.1.0a1`" in changelog
    assert "# Stackmint Gateway v0.1.0-alpha" in release_notes
    assert "Optional SDK hooks and typed client methods" in release_notes
    assert "Optional alpha MCP governance server" in release_notes
    assert "A deployed managed backend is required" in release_notes
    assert "Optional alpha MCP governance server" in changelog
