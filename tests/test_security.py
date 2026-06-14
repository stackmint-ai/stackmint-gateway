from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from stackmint_gateway.core import (
    CoreStackmintGateway,
    GatewayExternalApprovalCreateRequest,
    GatewayExternalAuthorizeRequest,
    GatewayExternalBudgetCommitRequest,
    GatewayExternalBudgetReserveRequest,
    GatewayExternalExecutionCreateRequest,
    GatewayExternalSyncRequest,
    GatewayExternalToolEventCreateRequest,
    StackmintGatewayAuthError,
    StackmintGatewayResponseError,
    StackmintGatewayServerError,
    StackmintGatewayTimeoutError,
    StackmintRetryConfig,
)
from stackmint_gateway.langchain import GovernedAgent
from stackmint_gateway.security import (
    StackmintTelemetrySecurityConfig,
    sanitize_payload,
)


class FakeTelemetryClient:
    def __init__(self) -> None:
        self.execution_payloads: list[Any] = []
        self.idempotency_keys: list[str | None] = []

    def record_execution(
        self,
        payload: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        self.execution_payloads.append(payload)
        self.idempotency_keys.append(idempotency_key)
        return SimpleNamespace(execution_id="execution", status=payload.status)


class FakeAgent:
    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        return {"ok": True}


def response(
    status_code: int,
    payload: dict[str, Any] | None = None,
) -> requests.Response:
    http_response = requests.Response()
    http_response.status_code = status_code
    http_response._content = json.dumps(payload or {}).encode("utf-8")
    http_response.headers["X-Request-Id"] = "request-123"
    return http_response


def raw_response(status_code: int, content: bytes) -> requests.Response:
    http_response = requests.Response()
    http_response.status_code = status_code
    http_response._content = content
    http_response.headers["X-Request-Id"] = "request-123"
    return http_response


def test_recursive_sensitive_key_redaction() -> None:
    secret = "super-secret-value"
    sanitized = sanitize_payload({"nested": {"api_key": secret}})

    assert sanitized.value["nested"]["api_key"] == "[REDACTED]"
    assert sanitized.redacted is True
    assert secret not in json.dumps(sanitized.value)


@pytest.mark.parametrize("key", ["token", "auth", "credential", "credentials"])
def test_generic_credential_key_redaction(key: str) -> None:
    secret = "plain-secret-value"
    sanitized = sanitize_payload({key: secret})

    assert sanitized.value[key] == "[REDACTED]"
    assert sanitized.redacted is True
    assert secret not in json.dumps(sanitized.value)


def test_secret_looking_value_redaction() -> None:
    secret = "Bearer abcdefghijklmnopqrstuvwxyz1234567890"
    sanitized = sanitize_payload({"harmless": secret})

    assert sanitized.value["harmless"] == "[REDACTED]"
    assert secret not in json.dumps(sanitized.value)


def test_email_redaction() -> None:
    email = "person@example.com"
    sanitized = sanitize_payload({"message": f"Contact {email}"})

    assert sanitized.value["message"] == "[REDACTED]"
    assert email not in json.dumps(sanitized.value)


def test_long_string_truncation() -> None:
    sanitized = sanitize_payload(
        {"text": "x" * 1000},
        StackmintTelemetrySecurityConfig(max_string_length=32),
    )

    assert sanitized.truncated is True
    assert sanitized.value["text"].endswith("[TRUNCATED]")
    assert len(sanitized.value["text"]) < 100


def test_large_input_payload_is_not_recorded_raw() -> None:
    large_value = "x" * 1_000_000
    client = FakeTelemetryClient()
    agent = GovernedAgent(FakeAgent(), record_outputs=False)
    agent.client = client

    agent.record_execution({"large": large_value}, status="completed")

    recorded = client.execution_payloads[-1]
    serialized = json.dumps(recorded.model_dump(mode="json"))
    assert large_value not in serialized
    assert recorded.metadata["stackmint_truncated"] is True


def test_nested_large_output_payload_is_truncated() -> None:
    large_value = "y" * 1_000_000
    client = FakeTelemetryClient()
    agent = GovernedAgent(FakeAgent(), record_inputs=False)
    agent.client = client

    agent.record_execution("input", output={"nested": {"large": large_value}})

    recorded = client.execution_payloads[-1]
    serialized = json.dumps(recorded.model_dump(mode="json"))
    assert large_value not in serialized
    assert recorded.metadata["stackmint_truncated"] is True


def test_circular_payload_safety() -> None:
    payload: dict[str, Any] = {}
    payload["self"] = payload

    sanitized = sanitize_payload(payload)

    assert sanitized.serialization_error is True
    assert sanitized.value["self"]["stackmint_serialization_error"] is True
    assert sanitized.value["self"]["reason"] == "circular_reference"


def test_non_serializable_object_safety() -> None:
    sanitized = sanitize_payload({"object": object()})

    assert sanitized.serialization_error is True
    assert sanitized.value["object"]["stackmint_serialization_error"] is True
    assert sanitized.value["object"]["type"] == "object"


def test_record_inputs_false() -> None:
    client = FakeTelemetryClient()
    agent = GovernedAgent(FakeAgent(), record_inputs=False)
    agent.client = client

    agent.record_execution({"api_key": "secret"}, output={"ok": True})

    recorded = client.execution_payloads[-1]
    assert recorded.input_payload is None
    assert recorded.metadata["stackmint_record_inputs"] is False


def test_record_outputs_false() -> None:
    client = FakeTelemetryClient()
    agent = GovernedAgent(FakeAgent(), record_outputs=False)
    agent.client = client

    agent.record_execution("input", output={"secret": "secret"})

    recorded = client.execution_payloads[-1]
    assert recorded.result_payload is None
    assert recorded.metadata["stackmint_record_outputs"] is False


def test_record_errors_false() -> None:
    client = FakeTelemetryClient()
    agent = GovernedAgent(FakeAgent(), record_errors=False)
    agent.client = client

    agent.record_execution("input", error=RuntimeError("api_key=secret"))

    recorded = client.execution_payloads[-1]
    assert recorded.error_payload["type"] == "RuntimeError"
    assert "secret" not in recorded.error_payload["message"]
    assert recorded.metadata["stackmint_record_errors"] is False


def test_raw_secrets_do_not_appear_in_recorded_payloads() -> None:
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"
    client = FakeTelemetryClient()
    agent = GovernedAgent(FakeAgent())
    agent.client = client

    agent.record_execution(
        {"prompt": secret},
        output={"authorization": f"Bearer {secret}"},
        error=None,
    )

    serialized = json.dumps(client.execution_payloads[-1].model_dump(mode="json"))
    assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_metadata_secret_redaction() -> None:
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"
    client = FakeTelemetryClient()
    agent = GovernedAgent(FakeAgent())
    agent.client = client

    agent.record_execution(
        "input",
        output={"ok": True},
        metadata={"api_key": secret},
    )

    recorded = client.execution_payloads[-1]
    serialized = json.dumps(recorded.model_dump(mode="json"))
    assert secret not in serialized
    assert recorded.metadata["api_key"] == "[REDACTED]"
    assert recorded.metadata["stackmint_redacted"] is True


def test_metadata_secret_looking_value_redaction() -> None:
    secret = "Bearer abcdefghijklmnopqrstuvwxyz1234567890"
    client = FakeTelemetryClient()
    agent = GovernedAgent(FakeAgent())
    agent.client = client

    agent.record_execution(
        "input",
        output={"ok": True},
        metadata={"harmless": secret},
    )

    recorded = client.execution_payloads[-1]
    assert recorded.metadata["harmless"] == "[REDACTED]"
    assert secret not in json.dumps(recorded.model_dump(mode="json"))


def test_metadata_reserved_keys_cannot_override_sdk_values() -> None:
    client = FakeTelemetryClient()
    agent = GovernedAgent(FakeAgent())
    agent.client = client

    agent.record_execution(
        "input",
        output={"ok": True},
        metadata={
            "source": "caller",
            "stackmint_redacted": False,
            "stackmint_truncated": False,
            "custom": "safe",
        },
    )

    recorded = client.execution_payloads[-1]
    assert recorded.metadata["source"] == "langchain"
    assert recorded.metadata["custom"] == "safe"
    assert recorded.metadata.get("stackmint_redacted") is not False
    assert recorded.metadata.get("stackmint_truncated") is not False


def test_metadata_reserved_key_filtering_is_case_insensitive() -> None:
    client = FakeTelemetryClient()
    agent = GovernedAgent(FakeAgent())
    agent.client = client

    agent.record_execution(
        "input",
        output={"ok": True},
        metadata={
            "Source": "caller",
            "STACKMINT_REDACTED": False,
            "stackmint-truncated": False,
            "custom": "safe",
        },
    )

    recorded = client.execution_payloads[-1]
    assert recorded.metadata["source"] == "langchain"
    assert "Source" not in recorded.metadata
    assert "STACKMINT_REDACTED" not in recorded.metadata
    assert "stackmint-truncated" not in recorded.metadata
    assert recorded.metadata["custom"] == "safe"


def test_metadata_large_and_non_serializable_values_are_safe() -> None:
    circular: dict[str, Any] = {"large": "x" * 1_000_000, "object": object()}
    circular["self"] = circular
    client = FakeTelemetryClient()
    agent = GovernedAgent(FakeAgent())
    agent.client = client

    agent.record_execution("input", output={"ok": True}, metadata=circular)

    recorded = client.execution_payloads[-1]
    serialized = json.dumps(recorded.model_dump(mode="json"))
    assert "x" * 1_000_000 not in serialized
    assert recorded.metadata["stackmint_truncated"] is True
    assert recorded.metadata["stackmint_serialization_error"] is True


def test_internal_budget_metadata_is_preserved() -> None:
    client = FakeTelemetryClient()
    agent = GovernedAgent(FakeAgent())
    agent.client = client

    agent.record_execution(
        "input",
        status="blocked",
        metadata={
            "reason": "budget_exceeded",
            "budget_ceiling_cents": 1,
            "current_session_cost_cents": 2,
            "estimated_input_cost_cents": 0.1,
        },
    )

    recorded = client.execution_payloads[-1]
    assert recorded.metadata["reason"] == "budget_exceeded"
    assert recorded.metadata["budget_ceiling_cents"] == 1
    assert recorded.metadata["current_session_cost_cents"] == 2
    assert recorded.metadata["estimated_input_cost_cents"] == 0.1


def test_record_execution_idempotency_key_matches_external_execution_ref() -> None:
    client = FakeTelemetryClient()
    agent = GovernedAgent(FakeAgent())
    agent.client = client

    agent.record_execution("input", output={"ok": True})

    recorded = client.execution_payloads[-1]
    assert client.idempotency_keys[-1] == recorded.external_execution_ref


def test_core_record_execution_passes_idempotency_header() -> None:
    calls: list[dict[str, Any]] = []

    def request_func(**kwargs: Any) -> requests.Response:
        calls.append(kwargs)
        return response(
            200,
            {
                "execution_id": "execution",
                "gateway_agent_id": "agent",
                "workspace_id": "workspace",
                "status": "completed",
            },
        )

    client = CoreStackmintGateway(
        "test-api-key",
        request_func=request_func,
        retry_config=StackmintRetryConfig(max_attempts=1),
    )
    payload = GatewayExternalExecutionCreateRequest(status="completed")

    client.record_execution(payload, idempotency_key="execution-key")

    assert calls[-1]["headers"]["Idempotency-Key"] == "execution-key"


def test_control_plane_models_validate_minimal_payloads() -> None:
    assert GatewayExternalAuthorizeRequest().metadata == {}
    assert GatewayExternalBudgetReserveRequest(
        external_execution_ref="execution"
    ).metadata == {}
    assert GatewayExternalBudgetCommitRequest(
        external_execution_ref="execution",
        status="completed",
    ).status == "completed"
    assert GatewayExternalToolEventCreateRequest(
        external_tool_ref="tool",
        status="allowed",
    ).status == "allowed"
    assert GatewayExternalApprovalCreateRequest().metadata == {}


def test_authorize_execution_path_and_idempotency_header() -> None:
    calls: list[dict[str, Any]] = []

    def request_func(**kwargs: Any) -> requests.Response:
        calls.append(kwargs)
        return response(
            200,
            {
                "decision": "allow",
                "gateway_agent_id": "agent",
                "workspace_id": "workspace",
            },
        )

    client = CoreStackmintGateway("test-api-key", request_func=request_func)

    result = client.authorize_execution(
        GatewayExternalAuthorizeRequest(external_execution_ref="execution"),
        idempotency_key="execution",
    )

    assert result.decision == "allow"
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["url"].endswith("/gateway/external/me/authorize")
    assert calls[-1]["headers"]["Idempotency-Key"] == "execution"


def test_budget_reserve_path_and_idempotency_header() -> None:
    calls: list[dict[str, Any]] = []

    def request_func(**kwargs: Any) -> requests.Response:
        calls.append(kwargs)
        return response(200, {"status": "reserved", "budget_reservation_id": "r1"})

    client = CoreStackmintGateway("test-api-key", request_func=request_func)

    result = client.reserve_budget(
        GatewayExternalBudgetReserveRequest(external_execution_ref="execution"),
        idempotency_key="execution",
    )

    assert result.status == "reserved"
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["url"].endswith("/gateway/external/me/budget/reserve")
    assert calls[-1]["headers"]["Idempotency-Key"] == "execution"


def test_budget_commit_path_and_idempotency_header() -> None:
    calls: list[dict[str, Any]] = []

    def request_func(**kwargs: Any) -> requests.Response:
        calls.append(kwargs)
        return response(200, {"status": "committed", "budget_reservation_id": "r1"})

    client = CoreStackmintGateway("test-api-key", request_func=request_func)

    result = client.commit_budget(
        GatewayExternalBudgetCommitRequest(
            external_execution_ref="execution",
            budget_reservation_id="r1",
            status="completed",
        ),
        idempotency_key="execution:budget_commit",
    )

    assert result.status == "committed"
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["url"].endswith("/gateway/external/me/budget/commit")
    assert calls[-1]["headers"]["Idempotency-Key"] == "execution:budget_commit"


def test_record_tool_event_path_and_idempotency_header() -> None:
    calls: list[dict[str, Any]] = []

    def request_func(**kwargs: Any) -> requests.Response:
        calls.append(kwargs)
        return response(
            200,
            {
                "tool_event_id": "tool-event",
                "gateway_agent_id": "agent",
                "workspace_id": "workspace",
                "external_tool_ref": "tool",
                "status": "allowed",
            },
        )

    client = CoreStackmintGateway("test-api-key", request_func=request_func)

    result = client.record_tool_event(
        GatewayExternalToolEventCreateRequest(
            external_execution_ref="execution",
            external_tool_ref="tool",
            status="allowed",
        ),
        idempotency_key="tool-event",
    )

    assert result.status == "allowed"
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["url"].endswith("/gateway/external/me/tool-events")
    assert calls[-1]["headers"]["Idempotency-Key"] == "tool-event"


def test_approval_methods_paths_and_idempotency_header() -> None:
    calls: list[dict[str, Any]] = []

    def request_func(**kwargs: Any) -> requests.Response:
        calls.append(kwargs)
        if kwargs["method"] == "GET":
            return response(
                200,
                {
                    "approval_request_id": "approval",
                    "status": "pending",
                },
            )
        return response(
            200,
            {
                "approval_request_id": "approval",
                "gateway_agent_id": "agent",
                "workspace_id": "workspace",
                "status": "pending",
            },
        )

    client = CoreStackmintGateway("test-api-key", request_func=request_func)

    created = client.create_approval_request(
        GatewayExternalApprovalCreateRequest(external_execution_ref="execution"),
        idempotency_key="execution:approval",
    )
    decision = client.get_approval_decision("approval")

    assert created.approval_request_id == "approval"
    assert decision.status == "pending"
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/gateway/external/me/approvals")
    assert calls[0]["headers"]["Idempotency-Key"] == "execution:approval"
    assert calls[1]["method"] == "GET"
    assert calls[1]["url"].endswith("/gateway/external/me/approvals/approval")


def test_retry_on_503_then_success() -> None:
    calls = {"count": 0}
    sleeps: list[float] = []

    def request_func(**kwargs: Any) -> requests.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return response(503)
        return response(
            200,
            {
                "gateway_agent_id": "agent",
                "workspace_id": "workspace",
                "name": "agent",
                "framework": "langchain",
                "status": "active",
            },
        )

    client = CoreStackmintGateway(
        "test-api-key",
        request_func=request_func,
        sleep_func=sleeps.append,
        retry_config=StackmintRetryConfig(max_attempts=3, initial_backoff_seconds=0),
    )

    assert client.get_me().status == "active"
    assert calls["count"] == 2


def test_record_execution_retries_on_503_with_idempotency_key() -> None:
    calls = {"count": 0}

    def request_func(**kwargs: Any) -> requests.Response:
        calls["count"] += 1
        assert kwargs["headers"]["Idempotency-Key"] == "execution-key"
        if calls["count"] == 1:
            return response(503)
        return response(
            200,
            {
                "execution_id": "execution",
                "gateway_agent_id": "agent",
                "workspace_id": "workspace",
                "status": "completed",
            },
        )

    client = CoreStackmintGateway(
        "test-api-key",
        request_func=request_func,
        retry_config=StackmintRetryConfig(max_attempts=3, initial_backoff_seconds=0),
    )

    client.record_execution(
        GatewayExternalExecutionCreateRequest(status="completed"),
        idempotency_key="execution-key",
    )

    assert calls["count"] == 2


def test_authorize_execution_retries_on_503_with_idempotency_key() -> None:
    calls = {"count": 0}

    def request_func(**kwargs: Any) -> requests.Response:
        calls["count"] += 1
        assert kwargs["headers"]["Idempotency-Key"] == "execution-key"
        if calls["count"] == 1:
            return response(503)
        return response(
            200,
            {
                "decision": "allow",
                "gateway_agent_id": "agent",
                "workspace_id": "workspace",
            },
        )

    client = CoreStackmintGateway(
        "test-api-key",
        request_func=request_func,
        retry_config=StackmintRetryConfig(max_attempts=3, initial_backoff_seconds=0),
    )

    client.authorize_execution(
        GatewayExternalAuthorizeRequest(external_execution_ref="execution-key"),
        idempotency_key="execution-key",
    )

    assert calls["count"] == 2


def test_authorize_execution_does_not_retry_post_without_idempotency_key() -> None:
    calls = {"count": 0}

    def request_func(**kwargs: Any) -> requests.Response:
        calls["count"] += 1
        return response(503)

    client = CoreStackmintGateway(
        "test-api-key",
        request_func=request_func,
        retry_config=StackmintRetryConfig(max_attempts=3, initial_backoff_seconds=0),
    )

    with pytest.raises(StackmintGatewayServerError):
        client.authorize_execution(GatewayExternalAuthorizeRequest())

    assert calls["count"] == 1


@pytest.mark.parametrize(
    ("method_name", "payload", "response_payload"),
    [
        (
            "reserve_budget",
            GatewayExternalBudgetReserveRequest(external_execution_ref="execution"),
            {"status": "reserved", "budget_reservation_id": "reservation"},
        ),
        (
            "commit_budget",
            GatewayExternalBudgetCommitRequest(
                external_execution_ref="execution",
                status="completed",
            ),
            {"status": "committed", "budget_reservation_id": "reservation"},
        ),
        (
            "record_tool_event",
            GatewayExternalToolEventCreateRequest(
                external_tool_ref="tool",
                status="allowed",
            ),
            {
                "tool_event_id": "tool-event",
                "gateway_agent_id": "agent",
                "workspace_id": "workspace",
                "external_tool_ref": "tool",
                "status": "allowed",
            },
        ),
        (
            "create_approval_request",
            GatewayExternalApprovalCreateRequest(external_execution_ref="execution"),
            {
                "approval_request_id": "approval",
                "gateway_agent_id": "agent",
                "workspace_id": "workspace",
                "status": "pending",
            },
        ),
    ],
)
def test_new_idempotent_post_methods_retry_on_503_then_success(
    method_name: str,
    payload: Any,
    response_payload: dict[str, Any],
) -> None:
    calls = {"count": 0}

    def request_func(**kwargs: Any) -> requests.Response:
        calls["count"] += 1
        assert kwargs["headers"]["Idempotency-Key"] == "operation-key"
        if calls["count"] == 1:
            return response(503)
        return response(200, response_payload)

    client = CoreStackmintGateway(
        "test-api-key",
        request_func=request_func,
        retry_config=StackmintRetryConfig(max_attempts=3, initial_backoff_seconds=0),
    )

    getattr(client, method_name)(payload, idempotency_key="operation-key")

    assert calls["count"] == 2


def test_sync_agent_does_not_retry_post_without_idempotency_key() -> None:
    calls = {"count": 0}

    def request_func(**kwargs: Any) -> requests.Response:
        calls["count"] += 1
        return response(503)

    client = CoreStackmintGateway(
        "test-api-key",
        request_func=request_func,
        retry_config=StackmintRetryConfig(max_attempts=3, initial_backoff_seconds=0),
    )

    with pytest.raises(StackmintGatewayServerError):
        client.sync_agent(GatewayExternalSyncRequest())

    assert calls["count"] == 1


def test_patch_does_not_retry_by_default() -> None:
    calls = {"count": 0}

    def request_func(**kwargs: Any) -> requests.Response:
        calls["count"] += 1
        return response(503)

    client = CoreStackmintGateway(
        "test-api-key",
        request_func=request_func,
        retry_config=StackmintRetryConfig(max_attempts=3, initial_backoff_seconds=0),
    )

    with pytest.raises(StackmintGatewayServerError):
        client._request("PATCH", "gateway/external/me/config", json={})

    assert calls["count"] == 1


@pytest.mark.parametrize("status_code", [401, 403])
def test_no_retry_on_auth_errors(status_code: int) -> None:
    calls = {"count": 0}

    def request_func(**kwargs: Any) -> requests.Response:
        calls["count"] += 1
        return response(status_code)

    client = CoreStackmintGateway(
        "raw-secret-key",
        request_func=request_func,
        retry_config=StackmintRetryConfig(max_attempts=3, initial_backoff_seconds=0),
    )

    with pytest.raises(StackmintGatewayAuthError) as exc_info:
        client.get_me()

    assert calls["count"] == 1
    assert "raw-secret-key" not in str(exc_info.value)
    assert "X-Gateway-Api-Key" not in str(exc_info.value)


def test_timeout_mapped_and_retried() -> None:
    calls = {"count": 0}

    def request_func(**kwargs: Any) -> requests.Response:
        calls["count"] += 1
        raise requests.Timeout("timeout with raw-secret-key")

    client = CoreStackmintGateway(
        "raw-secret-key",
        request_func=request_func,
        retry_config=StackmintRetryConfig(max_attempts=2, initial_backoff_seconds=0),
    )

    with pytest.raises(StackmintGatewayTimeoutError) as exc_info:
        client.get_me()

    assert calls["count"] == 2
    assert "raw-secret-key" not in str(exc_info.value)


def test_max_retry_attempts_are_respected() -> None:
    calls = {"count": 0}

    def request_func(**kwargs: Any) -> requests.Response:
        calls["count"] += 1
        return response(503)

    client = CoreStackmintGateway(
        "test-api-key",
        request_func=request_func,
        retry_config=StackmintRetryConfig(max_attempts=3, initial_backoff_seconds=0),
    )

    with pytest.raises(StackmintGatewayServerError):
        client.get_me()

    assert calls["count"] == 3


def test_invalid_json_response_raises_sdk_response_error() -> None:
    def request_func(**kwargs: Any) -> requests.Response:
        return raw_response(200, b"not-json with raw-secret-key")

    client = CoreStackmintGateway(
        "raw-secret-key",
        request_func=request_func,
        retry_config=StackmintRetryConfig(max_attempts=1),
    )

    with pytest.raises(StackmintGatewayResponseError) as exc_info:
        client.get_me()

    message = str(exc_info.value)
    assert "GET gateway/external/me" in message
    assert "status=200" in message
    assert "request_id=request-123" in message
    assert "raw-secret-key" not in message
    assert "not-json" not in message


def test_langchain_example_import_does_not_require_cerebras_extra() -> None:
    module = importlib.import_module("examples.langchain_example")

    assert hasattr(module, "build_agent")
