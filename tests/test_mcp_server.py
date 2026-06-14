from __future__ import annotations

import builtins
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from stackmint_gateway import mcp_server
from stackmint_gateway.core import GatewayExternalMeResponse
from stackmint_gateway.mcp_server import (
    StackmintMCPConfig,
    handle_stackmint_authorize_execution,
    handle_stackmint_commit_budget,
    handle_stackmint_create_approval_request,
    handle_stackmint_get_agent_me,
    handle_stackmint_get_agent_policy,
    handle_stackmint_get_approval_decision,
    handle_stackmint_record_execution,
    handle_stackmint_record_tool_event,
    handle_stackmint_reserve_budget,
    stackmint_governance_review_prompt,
    stackmint_incident_summary_prompt,
)


class FakeMCPClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.execution_payloads: list[Any] = []
        self.execution_idempotency_keys: list[str | None] = []
        self.authorize_payloads: list[Any] = []
        self.authorize_idempotency_keys: list[str | None] = []
        self.reserve_payloads: list[Any] = []
        self.reserve_idempotency_keys: list[str | None] = []
        self.commit_payloads: list[Any] = []
        self.commit_idempotency_keys: list[str | None] = []
        self.approval_payloads: list[Any] = []
        self.approval_idempotency_keys: list[str | None] = []
        self.approval_decision_ids: list[str] = []
        self.tool_event_payloads: list[Any] = []
        self.tool_event_idempotency_keys: list[str | None] = []

    def _raise_if_needed(self) -> None:
        if self.error is not None:
            raise self.error

    def get_me(self) -> GatewayExternalMeResponse:
        self._raise_if_needed()
        return GatewayExternalMeResponse(
            gateway_agent_id="agent",
            workspace_id="workspace",
            name="demo",
            framework="langchain",
            status="active",
            permitted_tool_slugs=["search"],
            budget_ceiling_cents=10,
            require_approval_for=["ticket"],
            latest_execution_status="completed",
            latest_execution_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def authorize_execution(
        self,
        payload: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        self._raise_if_needed()
        self.authorize_payloads.append(payload)
        self.authorize_idempotency_keys.append(idempotency_key)
        return SimpleNamespace(
            decision="allow",
            reason="ok",
            message=None,
            approval_request_id=None,
            budget_reservation_id="reservation",
            policy_version="v1",
            metadata={"checked": True},
        )

    def record_execution(
        self,
        payload: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        self._raise_if_needed()
        self.execution_payloads.append(payload)
        self.execution_idempotency_keys.append(idempotency_key)
        return SimpleNamespace(
            execution_id="execution",
            external_execution_ref=payload.external_execution_ref,
            status=payload.status,
        )

    def reserve_budget(
        self,
        payload: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        self._raise_if_needed()
        self.reserve_payloads.append(payload)
        self.reserve_idempotency_keys.append(idempotency_key)
        return SimpleNamespace(
            status="reserved",
            budget_reservation_id="reservation",
            approved_cost_cents=payload.estimated_cost_cents,
            remaining_budget_cents=9,
            reason=None,
        )

    def commit_budget(
        self,
        payload: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        self._raise_if_needed()
        self.commit_payloads.append(payload)
        self.commit_idempotency_keys.append(idempotency_key)
        return SimpleNamespace(
            status="committed",
            budget_reservation_id=payload.budget_reservation_id,
            committed_cost_cents=payload.actual_cost_cents,
            remaining_budget_cents=8,
        )

    def create_approval_request(
        self,
        payload: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        self._raise_if_needed()
        self.approval_payloads.append(payload)
        self.approval_idempotency_keys.append(idempotency_key)
        return SimpleNamespace(
            approval_request_id="approval",
            status="pending",
            message=None,
            metadata={},
        )

    def get_approval_decision(self, approval_request_id: str) -> Any:
        self._raise_if_needed()
        self.approval_decision_ids.append(approval_request_id)
        return SimpleNamespace(
            approval_request_id=approval_request_id,
            status="approved",
            approved_by="reviewer",
            decided_at=datetime(2026, 1, 1, tzinfo=UTC),
            message="approved",
            metadata={},
        )

    def record_tool_event(
        self,
        payload: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        self._raise_if_needed()
        self.tool_event_payloads.append(payload)
        self.tool_event_idempotency_keys.append(idempotency_key)
        return SimpleNamespace(
            tool_event_id="tool-event",
            external_execution_ref=payload.external_execution_ref,
            external_tool_ref=payload.external_tool_ref,
            status=payload.status,
        )


def config(**kwargs: Any) -> StackmintMCPConfig:
    defaults = {
        "read_only": False,
        "require_confirmation": True,
        "record_payloads": True,
    }
    defaults.update(kwargs)
    return StackmintMCPConfig("token-value", "http://stackmint.test/api", **defaults)


def test_mcp_config_loads_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("STACKMINT_GATEWAY_" + "API_KEY", "token-value")
    monkeypatch.setenv("STACKMINT_GATEWAY_BASE_URL", "http://example.test/api")
    monkeypatch.setenv("STACKMINT_MCP_READ_ONLY", "true")
    monkeypatch.setenv("STACKMINT_MCP_REQUIRE_CONFIRMATION", "false")
    monkeypatch.setenv("STACKMINT_MCP_RECORD_PAYLOADS", "false")

    loaded = StackmintMCPConfig.from_env()

    assert loaded.api_key == "token-value"
    assert loaded.base_url == "http://example.test/api"
    assert loaded.read_only is True
    assert loaded.require_confirmation is False
    assert loaded.record_payloads is False


def test_missing_api_key_returns_structured_error() -> None:
    result = handle_stackmint_get_agent_policy(
        config=StackmintMCPConfig(None, "http://example.test/api"),
    )

    assert result["ok"] is False
    assert result["error"] == "missing_api_key"
    assert "STACKMINT_GATEWAY_API_KEY" in result["message"]


def test_read_only_mode_blocks_mutating_tools() -> None:
    client = FakeMCPClient()
    result = handle_stackmint_record_execution(
        status="completed",
        confirmed=True,
        config=config(read_only=True),
        client=client,
    )

    assert result["ok"] is False
    assert result["error"] == "read_only_mode"
    assert client.execution_payloads == []


def test_confirmation_required_blocks_mutation_without_confirmed() -> None:
    client = FakeMCPClient()
    result = handle_stackmint_record_execution(
        status="completed",
        config=config(),
        client=client,
    )

    assert result["ok"] is False
    assert result["requires_confirmation"] is True
    assert client.execution_payloads == []


def test_confirmed_record_execution_calls_client_and_sanitizes_payload() -> None:
    client = FakeMCPClient()
    result = handle_stackmint_record_execution(
        external_execution_ref="execution-ref",
        status="completed",
        input_payload={"authorization": "Bearer abcdefghijklmnopqrstuvwxyz"},
        result_payload={"ok": True},
        metadata={"custom": "safe"},
        confirmed=True,
        config=config(),
        client=client,
    )

    assert result["ok"] is True
    assert client.execution_idempotency_keys[-1] == "execution-ref"
    recorded = client.execution_payloads[-1]
    serialized = json.dumps(recorded.model_dump(mode="json"))
    assert "abcdefghijklmnopqrstuvwxyz" not in serialized
    assert "[REDACTED]" in serialized


def test_get_agent_policy_calls_client() -> None:
    result = handle_stackmint_get_agent_policy(config=config(), client=FakeMCPClient())

    assert result["ok"] is True
    assert result["gateway_agent_id"] == "agent"
    assert result["permitted_tool_slugs"] == ["search"]


def test_get_agent_me_returns_full_safe_agent_payload() -> None:
    result = handle_stackmint_get_agent_me(config=config(), client=FakeMCPClient())

    assert result["ok"] is True
    assert result["agent"]["gateway_agent_id"] == "agent"
    assert result["agent"]["workspace_id"] == "workspace"


def test_authorize_execution_calls_client() -> None:
    client = FakeMCPClient()
    result = handle_stackmint_authorize_execution(
        external_execution_ref="execution-ref",
        input_payload={"prompt": "hello"},
        confirmed=True,
        config=config(),
        client=client,
    )

    assert result["ok"] is True
    assert result["decision"] == "allow"
    assert client.authorize_idempotency_keys[-1] == "execution-ref"
    assert client.authorize_payloads[-1].input_payload == {"prompt": "hello"}


def test_reserve_budget_calls_client() -> None:
    client = FakeMCPClient()
    result = handle_stackmint_reserve_budget(
        external_execution_ref="execution-ref",
        estimated_tokens=100,
        estimated_cost_cents=0.1,
        confirmed=True,
        config=config(),
        client=client,
    )

    assert result["ok"] is True
    assert result["status"] == "reserved"
    assert client.reserve_idempotency_keys[-1] == "execution-ref"


def test_commit_budget_calls_client() -> None:
    client = FakeMCPClient()
    result = handle_stackmint_commit_budget(
        external_execution_ref="execution-ref",
        budget_reservation_id="reservation",
        actual_tokens=200,
        actual_cost_cents=0.2,
        status="completed",
        confirmed=True,
        config=config(),
        client=client,
    )

    assert result["ok"] is True
    assert result["status"] == "committed"
    assert client.commit_idempotency_keys[-1] == "execution-ref:budget_commit"


def test_create_approval_request_calls_client() -> None:
    client = FakeMCPClient()
    result = handle_stackmint_create_approval_request(
        external_execution_ref="execution-ref",
        external_tool_ref="tool",
        reason="needs review",
        input_payload={"prompt": "hello"},
        confirmed=True,
        config=config(),
        client=client,
    )

    assert result["ok"] is True
    assert result["approval_request_id"] == "approval"
    assert client.approval_idempotency_keys[-1] == "execution-ref:approval"


def test_get_approval_decision_calls_client() -> None:
    client = FakeMCPClient()
    result = handle_stackmint_get_approval_decision(
        approval_request_id="approval",
        config=config(),
        client=client,
    )

    assert result["ok"] is True
    assert result["status"] == "approved"
    assert client.approval_decision_ids == ["approval"]


def test_record_tool_event_sanitizes_payloads() -> None:
    client = FakeMCPClient()
    result = handle_stackmint_record_tool_event(
        external_execution_ref="execution-ref",
        external_tool_ref="tool",
        status="failed",
        input_payload={"token": "plain-token"},
        error_payload={"message": "Bearer abcdefghijklmnopqrstuvwxyz"},
        confirmed=True,
        config=config(),
        client=client,
    )

    assert result["ok"] is True
    payload = client.tool_event_payloads[-1]
    serialized = json.dumps(payload.model_dump(mode="json"))
    assert "plain-token" not in serialized
    assert "abcdefghijklmnopqrstuvwxyz" not in serialized
    assert "[REDACTED]" in serialized


def test_api_key_never_appears_in_error_response() -> None:
    sensitive = "DO_NOT_LEAK_VALUE"
    client = FakeMCPClient(error=RuntimeError(f"backend failed with {sensitive}"))

    result = handle_stackmint_get_agent_policy(
        config=StackmintMCPConfig(sensitive, "http://example.test/api"),
        client=client,
    )

    assert result["ok"] is False
    assert sensitive not in json.dumps(result)


def test_mcp_prompt_helpers_are_governance_focused() -> None:
    review = stackmint_governance_review_prompt(
        "Create a ticket",
        tool_name="ticket_tool",
        risk_level="medium",
        business_context="support workflow",
    )
    incident = stackmint_incident_summary_prompt(
        execution_metadata="{status: blocked}",
        policy_decision="block",
    )

    assert "human approval" in review
    assert "allow, block, or approve-with-conditions" in review
    assert "governance reason" in incident


def test_readme_documents_mcp_governance_server() -> None:
    readme = open("README.md").read()
    normalized_readme = " ".join(readme.split())

    assert "## MCP Governance Server" in readme
    assert "uv sync --extra mcp" in readme
    assert "does not run arbitrary customer tools" in normalized_readme
    assert "STACKMINT_MCP_REQUIRE_CONFIRMATION=true" in readme
    assert "stackmint_record_tool_event" in readme


def test_missing_mcp_dependency_error_is_actionable(monkeypatch) -> None:
    real_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "mcp.server.fastmcp":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    with pytest.raises(RuntimeError, match="uv sync --extra mcp"):
        mcp_server._load_fastmcp()
