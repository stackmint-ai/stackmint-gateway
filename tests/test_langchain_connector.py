from __future__ import annotations

import asyncio
import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tools import tool

from stackmint_gateway.core import GatewayExternalMeResponse
from stackmint_gateway.langchain import (
    HITL_REJECTION_FALLBACK_MESSAGE,
    GovernedAgent,
    StackmintAgentBlockedError,
    StackmintApprovalRequiredError,
    StackmintAuthorizationBlockedError,
    StackmintBudgetExceededError,
    StackmintCallbackHandler,
    StackmintCircuitBreakerError,
    StackmintToolNotAllowedError,
    get_local_hitl_telemetry,
    get_local_tool_policy_telemetry,
    wrap_tool_with_stackmint_policy,
)
from stackmint_gateway.security import StackmintTelemetrySecurityConfig


def make_me(
    *,
    status: str = "active",
    permitted_tool_slugs: list[str] | None = None,
    require_approval_for: list[str] | None = None,
    budget_ceiling_cents: int | None = None,
) -> GatewayExternalMeResponse:
    return GatewayExternalMeResponse(
        gateway_agent_id="gateway-agent",
        workspace_id="workspace",
        name="agent",
        framework="langchain",
        status=status,
        permitted_tool_slugs=permitted_tool_slugs or [],
        require_approval_for=require_approval_for or [],
        budget_ceiling_cents=budget_ceiling_cents,
    )


class FakeClient:
    def __init__(
        self,
        me_response: GatewayExternalMeResponse | None = None,
        *,
        get_me_error: Exception | None = None,
        sync_error: Exception | None = None,
        record_error: Exception | None = None,
        authorize_response: Any = None,
        reserve_response: Any = None,
        commit_response: Any = None,
        approval_response: Any = None,
        control_plane_error: Exception | None = None,
    ) -> None:
        self.me_response = me_response or make_me()
        self.get_me_error = get_me_error
        self.sync_error = sync_error
        self.record_error = record_error
        self.authorize_response = authorize_response
        self.reserve_response = reserve_response
        self.commit_response = commit_response
        self.approval_response = approval_response
        self.control_plane_error = control_plane_error
        self.get_me_calls = 0
        self.sync_payloads: list[Any] = []
        self.execution_payloads: list[Any] = []
        self.idempotency_keys: list[str | None] = []
        self.authorize_payloads: list[Any] = []
        self.authorize_idempotency_keys: list[str | None] = []
        self.reserve_payloads: list[Any] = []
        self.reserve_idempotency_keys: list[str | None] = []
        self.commit_payloads: list[Any] = []
        self.commit_idempotency_keys: list[str | None] = []
        self.tool_event_payloads: list[Any] = []
        self.tool_event_idempotency_keys: list[str | None] = []
        self.approval_payloads: list[Any] = []
        self.approval_idempotency_keys: list[str | None] = []
        self.approval_decision_ids: list[str] = []
        self.current_session_cost = 0.0
        self.consecutive_failures = 0
        self.breaker_open = False
        self.breaker_cooldown_until = None

    def get_me(self) -> GatewayExternalMeResponse:
        self.get_me_calls += 1
        if self.get_me_error is not None:
            raise self.get_me_error
        return self.me_response

    def sync_agent(self, payload: Any) -> Any:
        self.sync_payloads.append(payload)
        if self.sync_error is not None:
            raise self.sync_error
        return SimpleNamespace(agent=self.me_response)

    def record_execution(
        self,
        payload: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        if self.record_error is not None:
            raise self.record_error
        self.execution_payloads.append(payload)
        self.idempotency_keys.append(idempotency_key)
        return SimpleNamespace(execution_id="execution", status=payload.status)

    def authorize_execution(
        self,
        payload: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        if self.control_plane_error is not None:
            raise self.control_plane_error
        self.authorize_payloads.append(payload)
        self.authorize_idempotency_keys.append(idempotency_key)
        return self.authorize_response or SimpleNamespace(
            decision="allow",
            gateway_agent_id="gateway-agent",
            workspace_id="workspace",
            reason=None,
            message=None,
            approval_request_id=None,
            budget_reservation_id=None,
            budget_ceiling_cents=None,
            remaining_budget_cents=None,
            policy_version=None,
            metadata={},
        )

    def reserve_budget(
        self,
        payload: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        if self.control_plane_error is not None:
            raise self.control_plane_error
        self.reserve_payloads.append(payload)
        self.reserve_idempotency_keys.append(idempotency_key)
        return self.reserve_response or SimpleNamespace(
            status="reserved",
            budget_reservation_id="reservation",
            reason=None,
            approved_cost_cents=payload.estimated_cost_cents,
            remaining_budget_cents=10,
            metadata={},
        )

    def commit_budget(
        self,
        payload: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        if self.control_plane_error is not None:
            raise self.control_plane_error
        self.commit_payloads.append(payload)
        self.commit_idempotency_keys.append(idempotency_key)
        return self.commit_response or SimpleNamespace(
            status="committed",
            budget_reservation_id=payload.budget_reservation_id,
            committed_cost_cents=payload.actual_cost_cents,
            remaining_budget_cents=9,
            metadata={},
        )

    def record_tool_event(
        self,
        payload: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        if self.control_plane_error is not None:
            raise self.control_plane_error
        self.tool_event_payloads.append(payload)
        self.tool_event_idempotency_keys.append(idempotency_key)
        return SimpleNamespace(
            tool_event_id="tool-event",
            status=payload.status,
            external_tool_ref=payload.external_tool_ref,
        )

    def create_approval_request(
        self,
        payload: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        if self.control_plane_error is not None:
            raise self.control_plane_error
        self.approval_payloads.append(payload)
        self.approval_idempotency_keys.append(idempotency_key)
        return self.approval_response or SimpleNamespace(
            approval_request_id="approval-created",
            status="pending",
            message=None,
            metadata={},
        )

    def get_approval_decision(self, approval_request_id: str) -> Any:
        self.approval_decision_ids.append(approval_request_id)
        return SimpleNamespace(
            approval_request_id=approval_request_id,
            status="pending",
        )


class FakeAgent:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = (
            result if result is not None else {"usage_metadata": {"total_tokens": 1}}
        )
        self.error = error
        self.calls = 0
        self.configs: list[Any] = []

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self.calls += 1
        self.configs.append(config)
        if self.error is not None:
            raise self.error
        return self.result

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self.calls += 1
        self.configs.append(config)
        if self.error is not None:
            raise self.error
        return self.result


def governed(
    agent: FakeAgent,
    client: FakeClient | None = None,
    **kwargs: Any,
) -> GovernedAgent:
    wrapper = GovernedAgent(agent, api_key="test", sync_on_invoke=False, **kwargs)
    wrapper.client = client or FakeClient()
    return wrapper


def test_remote_agent_status_blocks_execution_and_records_blocked() -> None:
    agent = FakeAgent()
    client = FakeClient(make_me(status="blocked"))
    wrapper = governed(agent, client)

    with pytest.raises(StackmintAgentBlockedError):
        wrapper.invoke({"messages": ["run"]})

    assert agent.calls == 0
    assert client.execution_payloads[-1].status == "blocked"
    assert client.execution_payloads[-1].metadata["reason"] == "agent_status"


def test_gateway_error_fail_open_allows_execution() -> None:
    agent = FakeAgent(result={"usage_metadata": {"total_tokens": 1}})
    client = FakeClient(get_me_error=RuntimeError("offline"))
    wrapper = governed(agent, client, fail_open=True)

    assert wrapper.invoke("hello") == {"usage_metadata": {"total_tokens": 1}}
    assert agent.calls == 1


def test_gateway_error_fail_closed_blocks_execution() -> None:
    agent = FakeAgent()
    client = FakeClient(get_me_error=RuntimeError("offline"))
    wrapper = governed(agent, client, fail_open=False)

    with pytest.raises(RuntimeError, match="offline"):
        wrapper.invoke("hello")

    assert agent.calls == 0


def test_refresh_policy_remote_values_win() -> None:
    wrapper = governed(
        FakeAgent(),
        FakeClient(
            make_me(
                permitted_tool_slugs=["remote-tool"],
                require_approval_for=["remote-approval"],
                budget_ceiling_cents=42,
            )
        ),
        permitted_tool_slugs=["local-tool"],
        require_approval_for=["local-approval"],
        budget_ceiling_cents=1,
    )

    policy = wrapper.refresh_policy()

    assert policy is not None
    assert policy.permitted_tool_slugs == {"remote-tool"}
    assert policy.require_approval_for == {"remote-approval"}
    assert policy.budget_ceiling_cents == 42


def test_refresh_policy_gateway_unavailable_fail_open_uses_local_policy() -> None:
    wrapper = governed(
        FakeAgent(),
        FakeClient(get_me_error=RuntimeError("offline")),
        fail_open=True,
        permitted_tool_slugs=["local-tool"],
        require_approval_for=["local-approval"],
        budget_ceiling_cents=12,
    )

    policy = wrapper.refresh_policy()

    assert policy is not None
    assert policy.permitted_tool_slugs == {"local-tool"}
    assert policy.require_approval_for == {"local-approval"}
    assert policy.budget_ceiling_cents == 12


def test_refresh_policy_gateway_unavailable_fail_closed_raises() -> None:
    wrapper = governed(
        FakeAgent(),
        FakeClient(get_me_error=RuntimeError("offline")),
        fail_open=False,
    )

    with pytest.raises(RuntimeError, match="offline"):
        wrapper.refresh_policy()


def test_remote_control_plane_defaults_do_not_call_new_endpoints() -> None:
    agent = FakeAgent()
    client = FakeClient()
    wrapper = governed(agent, client)

    wrapper.invoke("hello")

    assert agent.calls == 1
    assert client.authorize_payloads == []
    assert client.reserve_payloads == []
    assert client.commit_payloads == []
    assert client.tool_event_payloads == []


def test_remote_authorization_allow_invokes_underlying_agent() -> None:
    agent = FakeAgent()
    client = FakeClient()
    wrapper = governed(agent, client, remote_authorization=True)

    wrapper.invoke({"prompt": "hello"})

    assert agent.calls == 1
    assert len(client.authorize_payloads) == 1
    assert client.authorize_idempotency_keys[-1] == (
        client.authorize_payloads[-1].external_execution_ref
    )


def test_authorization_budget_reservation_id_skips_separate_reserve_call() -> None:
    agent = FakeAgent(result={"usage_metadata": {"total_tokens": 100}})
    client = FakeClient(
        authorize_response=SimpleNamespace(
            decision="allow",
            gateway_agent_id="gateway-agent",
            workspace_id="workspace",
            reason=None,
            message=None,
            approval_request_id=None,
            budget_reservation_id="authorization-reservation",
            budget_ceiling_cents=10,
            remaining_budget_cents=7,
            policy_version="policy-v1",
            metadata={},
        )
    )
    wrapper = governed(
        agent,
        client,
        remote_authorization=True,
        remote_budget=True,
    )

    wrapper.invoke("hello")

    assert agent.calls == 1
    assert len(client.authorize_payloads) == 1
    assert client.reserve_payloads == []
    assert len(client.commit_payloads) == 1
    assert client.commit_payloads[-1].budget_reservation_id == (
        "authorization-reservation"
    )
    assert wrapper.state.last_budget_reservation_response.budget_reservation_id == (
        "authorization-reservation"
    )


def test_remote_authorization_block_prevents_agent_call_and_records() -> None:
    agent = FakeAgent()
    client = FakeClient(
        authorize_response=SimpleNamespace(
            decision="block",
            gateway_agent_id="gateway-agent",
            workspace_id="workspace",
            reason="policy",
            message="blocked remotely",
            approval_request_id=None,
            budget_reservation_id=None,
            budget_ceiling_cents=None,
            remaining_budget_cents=None,
            policy_version="v1",
            metadata={},
        )
    )
    wrapper = governed(agent, client, remote_authorization=True)

    with pytest.raises(StackmintAuthorizationBlockedError):
        wrapper.invoke("hello")

    assert agent.calls == 0
    assert client.execution_payloads[-1].status == "blocked"
    assert (
        client.execution_payloads[-1].metadata["reason"]
        == "remote_authorization_blocked"
    )


def test_remote_authorization_budget_exceeded_blocks_agent_call() -> None:
    agent = FakeAgent()
    client = FakeClient(
        authorize_response=SimpleNamespace(
            decision="budget_exceeded",
            gateway_agent_id="gateway-agent",
            workspace_id="workspace",
            reason="budget",
            message=None,
            approval_request_id=None,
            budget_reservation_id=None,
            budget_ceiling_cents=1,
            remaining_budget_cents=0,
            policy_version=None,
            metadata={},
        )
    )
    wrapper = governed(agent, client, remote_authorization=True)

    with pytest.raises(StackmintBudgetExceededError):
        wrapper.invoke("hello")

    assert agent.calls == 0
    assert client.execution_payloads[-1].status == "blocked"
    assert client.execution_payloads[-1].metadata["reason"] == "budget_exceeded"


def test_remote_authorization_waiting_approval_records_waiting_state() -> None:
    agent = FakeAgent()
    client = FakeClient(
        authorize_response=SimpleNamespace(
            decision="waiting_approval",
            gateway_agent_id="gateway-agent",
            workspace_id="workspace",
            reason="needs approval",
            message=None,
            approval_request_id="approval-1",
            budget_reservation_id=None,
            budget_ceiling_cents=None,
            remaining_budget_cents=None,
            policy_version=None,
            metadata={},
        )
    )
    wrapper = governed(agent, client, remote_authorization=True)

    with pytest.raises(StackmintApprovalRequiredError) as exc_info:
        wrapper.invoke("hello")

    assert exc_info.value.approval_request_id == "approval-1"
    assert agent.calls == 0
    assert client.execution_payloads[-1].status == "waiting_approval"
    assert client.execution_payloads[-1].metadata["reason"] == "waiting_approval"


def test_remote_approval_request_created_when_authorization_has_no_id() -> None:
    agent = FakeAgent()
    client = FakeClient(
        authorize_response=SimpleNamespace(
            decision="waiting_approval",
            gateway_agent_id="gateway-agent",
            workspace_id="workspace",
            reason="needs approval",
            message=None,
            approval_request_id=None,
            budget_reservation_id=None,
            budget_ceiling_cents=None,
            remaining_budget_cents=None,
            policy_version=None,
            metadata={},
        )
    )
    wrapper = governed(
        agent,
        client,
        remote_authorization=True,
        remote_approvals=True,
    )

    with pytest.raises(StackmintApprovalRequiredError) as exc_info:
        wrapper.invoke("hello")

    assert exc_info.value.approval_request_id == "approval-created"
    assert len(client.approval_payloads) == 1
    assert client.approval_idempotency_keys[-1].endswith(":approval")


def test_remote_budget_reservation_rejected_blocks_execution() -> None:
    agent = FakeAgent()
    client = FakeClient(
        reserve_response=SimpleNamespace(
            status="rejected",
            budget_reservation_id="reservation",
            reason="no budget",
            approved_cost_cents=None,
            remaining_budget_cents=0,
            metadata={},
        )
    )
    wrapper = governed(agent, client, remote_budget=True)

    with pytest.raises(StackmintBudgetExceededError):
        wrapper.invoke("hello")

    assert agent.calls == 0
    assert len(client.reserve_payloads) == 1
    assert client.execution_payloads[-1].status == "blocked"


def test_remote_budget_commit_runs_after_success() -> None:
    agent = FakeAgent(result={"usage_metadata": {"total_tokens": 100}})
    client = FakeClient()
    wrapper = governed(agent, client, remote_budget=True)

    wrapper.invoke("hello")

    assert agent.calls == 1
    assert len(client.reserve_payloads) == 1
    assert len(client.commit_payloads) == 1
    assert client.commit_payloads[-1].status == "completed"
    assert client.commit_idempotency_keys[-1].endswith(":budget_commit")


def test_remote_budget_commit_runs_after_failure_when_reserved() -> None:
    agent = FakeAgent(error=RuntimeError("boom"))
    client = FakeClient()
    wrapper = governed(agent, client, remote_budget=True)

    with pytest.raises(RuntimeError, match="boom"):
        wrapper.invoke("hello")

    assert len(client.reserve_payloads) == 1
    assert len(client.commit_payloads) == 1
    assert client.commit_payloads[-1].status == "failed"


def test_remote_control_plane_fail_open_falls_back_to_local_behavior() -> None:
    agent = FakeAgent()
    client = FakeClient(control_plane_error=RuntimeError("not deployed"))
    wrapper = governed(agent, client, remote_authorization=True, fail_open=True)

    wrapper.invoke("hello")

    assert agent.calls == 1
    assert isinstance(wrapper.state.last_control_plane_error, RuntimeError)


def test_run_execution_ref_is_shared_across_remote_calls() -> None:
    agent = FakeAgent()
    client = FakeClient()
    wrapper = governed(
        agent,
        client,
        remote_authorization=True,
        remote_budget=True,
    )

    wrapper.invoke("hello")

    execution_ref = client.execution_payloads[-1].external_execution_ref
    assert client.authorize_payloads[-1].external_execution_ref == execution_ref
    assert client.reserve_payloads[-1].external_execution_ref == execution_ref
    assert client.commit_payloads[-1].external_execution_ref == execution_ref
    assert client.authorize_idempotency_keys[-1] == execution_ref
    assert client.reserve_idempotency_keys[-1] == execution_ref


def test_budget_preflight_block_records_blocked_without_agent_call() -> None:
    agent = FakeAgent()
    client = FakeClient(make_me(budget_ceiling_cents=0))
    wrapper = governed(agent, client)

    with pytest.raises(StackmintBudgetExceededError):
        wrapper.invoke("hello")

    assert agent.calls == 0
    assert client.execution_payloads[-1].status == "blocked"
    assert client.execution_payloads[-1].metadata["reason"] == "budget_exceeded"
    assert "estimated_input_cost_cents" in client.execution_payloads[-1].metadata


def test_budget_post_run_reconciliation_blocks_future_calls() -> None:
    agent = FakeAgent(result={"usage_metadata": {"total_tokens": 2000}})
    client = FakeClient(make_me(budget_ceiling_cents=1))
    wrapper = governed(agent, client)

    assert wrapper.invoke("small") == {"usage_metadata": {"total_tokens": 2000}}
    assert agent.calls == 1
    assert wrapper.current_session_cost == 2.0

    with pytest.raises(StackmintBudgetExceededError):
        wrapper.invoke("small")

    assert agent.calls == 1
    assert client.execution_payloads[-1].status == "blocked"


def test_circuit_breaker_opens_after_threshold_failures() -> None:
    agent = FakeAgent(error=RuntimeError("boom"))
    wrapper = GovernedAgent(agent, sync_on_invoke=False, fail_open=True)

    for _ in range(3):
        with pytest.raises(RuntimeError, match="boom"):
            wrapper.invoke("x")

    assert wrapper.consecutive_failures == 3
    assert wrapper.breaker_open is True


def test_circuit_breaker_blocks_execution_during_cooldown() -> None:
    agent = FakeAgent()
    client = FakeClient()
    wrapper = governed(agent, client)
    wrapper.consecutive_failures = 3
    wrapper.breaker_open = True
    wrapper.breaker_cooldown_until = datetime.now(UTC) + timedelta(seconds=60)

    with pytest.raises(StackmintCircuitBreakerError):
        wrapper.invoke("x")

    assert agent.calls == 0
    assert client.execution_payloads[-1].status == "blocked"
    assert client.execution_payloads[-1].metadata["reason"] == "circuit_breaker_open"


def test_circuit_breaker_resets_after_cooldown_expires() -> None:
    agent = FakeAgent()
    wrapper = GovernedAgent(agent, sync_on_invoke=False)
    wrapper.consecutive_failures = 3
    wrapper.breaker_open = True
    wrapper.breaker_cooldown_until = datetime.now(UTC) - timedelta(minutes=5)

    assert wrapper.invoke("x") == {"usage_metadata": {"total_tokens": 1}}

    assert agent.calls == 1
    assert wrapper.breaker_open is False
    assert wrapper.consecutive_failures == 0
    assert wrapper.breaker_cooldown_until is None


def test_circuit_breaker_recording_failure_does_not_mask_block() -> None:
    agent = FakeAgent()
    client = FakeClient(record_error=RuntimeError("telemetry down"))
    wrapper = governed(agent, client, fail_open=False)
    wrapper.consecutive_failures = 3
    wrapper.breaker_open = True
    wrapper.breaker_cooldown_until = datetime.now(UTC) + timedelta(seconds=60)

    with pytest.raises(StackmintCircuitBreakerError):
        wrapper.invoke("x")

    assert agent.calls == 0
    assert wrapper.consecutive_failures == 3


def test_async_circuit_breaker_block_is_recorded() -> None:
    agent = FakeAgent()
    client = FakeClient()
    wrapper = governed(agent, client)
    wrapper.consecutive_failures = 3
    wrapper.breaker_open = True
    wrapper.breaker_cooldown_until = datetime.now(UTC) + timedelta(seconds=60)

    async def run() -> None:
        with pytest.raises(StackmintCircuitBreakerError):
            await wrapper.ainvoke("x")

    asyncio.run(run())

    assert agent.calls == 0
    assert client.execution_payloads[-1].status == "blocked"
    assert client.execution_payloads[-1].metadata["reason"] == "circuit_breaker_open"


def test_tool_policy_denial_does_not_increment_circuit_breaker() -> None:
    agent = FakeAgent(error=StackmintToolNotAllowedError("blocked tool"))
    client = FakeClient()
    wrapper = governed(agent, client)

    with pytest.raises(StackmintToolNotAllowedError):
        wrapper.invoke("x")

    assert wrapper.consecutive_failures == 0
    assert wrapper.breaker_open is False
    assert client.execution_payloads[-1].status == "blocked"
    assert client.execution_payloads[-1].metadata["reason"] == "tool_not_permitted"


def test_policy_denial_recording_failure_does_not_mask_policy_error() -> None:
    agent = FakeAgent(error=StackmintToolNotAllowedError("blocked tool"))
    client = FakeClient(record_error=RuntimeError("telemetry down"))
    wrapper = governed(agent, client, fail_open=False)

    with pytest.raises(StackmintToolNotAllowedError):
        wrapper.invoke("x")

    assert wrapper.consecutive_failures == 0
    assert wrapper.breaker_open is False


def test_async_tool_policy_denial_does_not_increment_circuit_breaker() -> None:
    agent = FakeAgent(error=StackmintToolNotAllowedError("blocked tool"))
    client = FakeClient()
    wrapper = governed(agent, client)

    async def run() -> None:
        with pytest.raises(StackmintToolNotAllowedError):
            await wrapper.ainvoke("x")

    asyncio.run(run())

    assert wrapper.consecutive_failures == 0
    assert wrapper.breaker_open is False
    assert client.execution_payloads[-1].status == "blocked"
    assert client.execution_payloads[-1].metadata["reason"] == "tool_not_permitted"


def test_actual_agent_failure_still_increments_circuit_breaker() -> None:
    agent = FakeAgent(error=RuntimeError("boom"))
    wrapper = GovernedAgent(agent, sync_on_invoke=False, fail_open=True)

    with pytest.raises(RuntimeError, match="boom"):
        wrapper.invoke("x")

    assert wrapper.consecutive_failures == 1
    assert wrapper.breaker_open is False


def test_async_record_execution_runs_off_event_loop_thread() -> None:
    agent = FakeAgent()
    wrapper = GovernedAgent(agent, sync_on_invoke=False)
    record_thread_ids: list[int] = []

    def record_execution(*args: Any, **kwargs: Any) -> None:
        record_thread_ids.append(threading.get_ident())

    wrapper.record_execution = record_execution  # type: ignore[method-assign]

    async def run() -> int:
        event_loop_thread_id = threading.get_ident()
        await wrapper.ainvoke("x")
        return event_loop_thread_id

    event_loop_thread_id = asyncio.run(run())

    assert record_thread_ids
    assert event_loop_thread_id not in record_thread_ids


def test_permitted_tool_executes() -> None:
    calls = {"count": 0}

    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        calls["count"] += 1
        return a + b

    governed_tool = wrap_tool_with_stackmint_policy(
        add,
        permitted_tool_slugs={"add"},
    )

    assert governed_tool.invoke({"a": 2, "b": 3}) == 5
    assert calls["count"] == 1


def test_unauthorized_tool_is_blocked() -> None:
    calls = {"count": 0}

    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        calls["count"] += 1
        return a + b

    governed_tool = wrap_tool_with_stackmint_policy(
        add,
        permitted_tool_slugs={"other-tool"},
    )

    with pytest.raises(StackmintToolNotAllowedError):
        governed_tool.invoke({"a": 2, "b": 3})

    assert calls["count"] == 0
    assert get_local_tool_policy_telemetry()[-1]["tool_name"] == "add"


def test_record_tool_events_records_blocked_tool_attempt() -> None:
    calls = {"count": 0}

    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        calls["count"] += 1
        return a + b

    client = FakeClient(make_me(permitted_tool_slugs=["other-tool"]))
    wrapper = GovernedAgent(
        FakeAgent(),
        "test",
        sync_on_invoke=False,
        tools=[add],
        record_tool_events=True,
    )
    wrapper.client = client
    governed_tool = wrapper.governed_tools()[0]

    with pytest.raises(StackmintToolNotAllowedError):
        governed_tool.invoke({"a": 2, "b": 3})

    assert calls["count"] == 0
    assert client.tool_event_payloads[-1].status == "blocked"
    assert client.tool_event_payloads[-1].metadata["reason"] == "tool_not_permitted"


def test_record_tool_events_redacts_tool_payloads() -> None:
    sensitive_value = "plain-test-value"

    @tool
    def echo(value: str) -> dict[str, str]:
        """Echo a value."""
        return {"value": value}

    client = FakeClient(make_me(permitted_tool_slugs=["echo"]))
    wrapper = GovernedAgent(
        FakeAgent(),
        "test",
        sync_on_invoke=False,
        tools=[echo],
        record_tool_events=True,
        telemetry_security=StackmintTelemetrySecurityConfig(redact_keys={"value"}),
    )
    wrapper.client = client
    governed_tool = wrapper.governed_tools()[0]

    assert governed_tool.invoke({"value": sensitive_value}) == {
        "value": sensitive_value
    }

    serialized = json.dumps(client.tool_event_payloads[-1].model_dump(mode="json"))
    assert sensitive_value not in serialized
    assert "[REDACTED]" in serialized


def test_record_tool_events_records_hitl_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    client = FakeClient(
        make_me(
            permitted_tool_slugs=["add"],
            require_approval_for=["add"],
        )
    )
    wrapper = GovernedAgent(
        FakeAgent(),
        "test",
        sync_on_invoke=False,
        tools=[add],
        record_tool_events=True,
    )
    wrapper.client = client
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    governed_tool = wrapper.governed_tools()[0]

    assert (
        governed_tool.invoke({"a": 2, "b": 3})
        == HITL_REJECTION_FALLBACK_MESSAGE
    )
    assert client.tool_event_payloads[-1].status == "rejected"
    assert client.tool_event_payloads[-1].metadata["reason"] == "hitl_rejected"


def test_record_tool_events_records_tool_failure() -> None:
    @tool
    def explode() -> str:
        """Raise an error."""
        raise RuntimeError("boom")

    client = FakeClient(make_me(permitted_tool_slugs=["explode"]))
    wrapper = GovernedAgent(
        FakeAgent(),
        "test",
        sync_on_invoke=False,
        tools=[explode],
        record_tool_events=True,
    )
    wrapper.client = client
    governed_tool = wrapper.governed_tools()[0]

    with pytest.raises(RuntimeError, match="boom"):
        governed_tool.invoke({})

    assert client.tool_event_payloads[-1].status == "failed"


def test_hitl_approved_tool_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        calls["count"] += 1
        return a + b

    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    governed_tool = wrap_tool_with_stackmint_policy(
        add,
        require_approval_for={"add"},
    )

    assert governed_tool.invoke({"a": 2, "b": 3}) == 5
    assert calls["count"] == 1


def test_hitl_rejected_tool_does_not_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        calls["count"] += 1
        return a + b

    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    governed_tool = wrap_tool_with_stackmint_policy(
        add,
        require_approval_for={"add"},
    )

    assert governed_tool.invoke({"a": 2, "b": 3}) == HITL_REJECTION_FALLBACK_MESSAGE
    assert calls["count"] == 0
    assert get_local_hitl_telemetry()[-1]["tool_name"] == "add"


def test_custom_hitl_approval_callback_controls_execution() -> None:
    calls = {"count": 0}

    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        calls["count"] += 1
        return a + b

    approved = wrap_tool_with_stackmint_policy(
        add,
        require_approval_for={"add"},
        approval_fn=lambda tool_name: tool_name == "add",
    )
    rejected = wrap_tool_with_stackmint_policy(
        add,
        require_approval_for={"add"},
        approval_fn=lambda tool_name: False,
    )

    assert approved.invoke({"a": 1, "b": 2}) == 3
    assert rejected.invoke({"a": 1, "b": 2}) == HITL_REJECTION_FALLBACK_MESSAGE
    assert calls["count"] == 1


def test_tool_policy_async_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        calls["count"] += 1
        return a + b

    async def run() -> None:
        permitted = wrap_tool_with_stackmint_policy(add, permitted_tool_slugs={"add"})
        assert await permitted.ainvoke({"a": 1, "b": 2}) == 3

        blocked = wrap_tool_with_stackmint_policy(add, permitted_tool_slugs={"other"})
        with pytest.raises(StackmintToolNotAllowedError):
            await blocked.ainvoke({"a": 1, "b": 2})

        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        rejected = wrap_tool_with_stackmint_policy(add, require_approval_for={"add"})
        assert (
            await rejected.ainvoke({"a": 1, "b": 2})
            == HITL_REJECTION_FALLBACK_MESSAGE
        )

    asyncio.run(run())
    assert calls["count"] == 1


def test_record_execution_supports_explicit_status_and_metadata() -> None:
    client = FakeClient()
    wrapper = governed(FakeAgent(), client)

    wrapper.record_execution(
        "input",
        status="blocked",
        metadata={"reason": "test_block"},
        external_tool_ref="tool",
    )

    payload = client.execution_payloads[-1]
    assert payload.status == "blocked"
    assert payload.external_tool_ref == "tool"
    assert payload.metadata["source"] == "langchain"
    assert payload.metadata["reason"] == "test_block"
    assert client.idempotency_keys[-1] == payload.external_execution_ref


def test_callback_handler_appends_to_existing_callbacks() -> None:
    class ExistingCallback(BaseCallbackHandler):
        pass

    class CallbackAgent(FakeAgent):
        def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
            self.calls += 1
            self.configs.append(config)
            callbacks = config["callbacks"]
            stackmint_callback = next(
                callback
                for callback in callbacks
                if isinstance(callback, StackmintCallbackHandler)
            )
            run_id = uuid.uuid4()
            stackmint_callback.on_chain_start({}, {"input": input}, run_id=run_id)
            stackmint_callback.on_chain_end({"output": "ok"}, run_id=run_id)
            return self.result

    existing = ExistingCallback()
    agent = CallbackAgent(result={"usage_metadata": {"total_tokens": 1}})
    wrapper = GovernedAgent(agent, sync_on_invoke=False)

    wrapper.invoke("hello", config={"callbacks": [existing]})

    callbacks = agent.configs[-1]["callbacks"]
    assert existing in callbacks
    assert any(isinstance(callback, StackmintCallbackHandler) for callback in callbacks)
    assert [event["event"] for event in wrapper.callback_handler.events] == [
        "chain_start",
        "chain_end",
    ]
