from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from stackmint_gateway.core import (
    BASE_URL,
    CoreStackmintGateway,
    ExecutionStatus,
    GatewayExternalApprovalCreateRequest,
    GatewayExternalAuthorizeRequest,
    GatewayExternalBudgetCommitRequest,
    GatewayExternalBudgetReserveRequest,
    GatewayExternalExecutionCreateRequest,
    GatewayExternalToolEventCreateRequest,
    ToolEventStatus,
)
from stackmint_gateway.security import (
    StackmintTelemetrySecurityConfig,
    sanitize_payload,
)


@dataclass
class StackmintMCPConfig:
    api_key: str | None
    base_url: str
    read_only: bool = False
    require_confirmation: bool = True
    record_payloads: bool = True

    @classmethod
    def from_env(cls) -> StackmintMCPConfig:
        return cls(
            api_key=os.getenv("STACKMINT_GATEWAY_API_KEY"),
            base_url=os.getenv("STACKMINT_GATEWAY_BASE_URL", BASE_URL),
            read_only=_env_bool("STACKMINT_MCP_READ_ONLY", False),
            require_confirmation=_env_bool(
                "STACKMINT_MCP_REQUIRE_CONFIRMATION",
                True,
            ),
            record_payloads=_env_bool("STACKMINT_MCP_RECORD_PAYLOADS", True),
        )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def confirmation_required(action: str) -> dict[str, Any]:
    return {
        "ok": False,
        "requires_confirmation": True,
        "action": action,
        "message": "Re-run with confirmed=true to perform this governance action.",
    }


def read_only_error(action: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "read_only_mode",
        "action": action,
        "message": "This MCP server is running in read-only mode.",
    }


def _missing_api_key_error(action: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "missing_api_key",
        "action": action,
        "message": (
            "Set STACKMINT_GATEWAY_API_KEY before calling this governance action."
        ),
    }


def _validation_error(action: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "validation_error",
        "action": action,
        "message": message,
    }


def _safe_error(
    action: str,
    error: Exception,
    config: StackmintMCPConfig,
) -> dict[str, Any]:
    message = str(error)
    if config.api_key and len(config.api_key) >= 8:
        message = message.replace(config.api_key, "[REDACTED]")
    sanitized = sanitize_payload({"message": message}).value
    safe_message = sanitized.get("message") if isinstance(sanitized, dict) else None
    return {
        "ok": False,
        "error": "gateway_error",
        "action": action,
        "error_type": error.__class__.__name__,
        "message": safe_message or "Stackmint gateway action failed.",
    }


def _safe_response(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = sanitize_payload(payload).value
    if isinstance(sanitized, dict):
        return sanitized
    return {"ok": True, "value": sanitized}


def _model_dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if item is not None}
    return {
        key: item
        for key, item in vars(value).items()
        if not key.startswith("_") and item is not None
    }


def _client_or_error(
    config: StackmintMCPConfig,
    client: Any | None,
    *,
    action: str,
) -> tuple[Any | None, dict[str, Any] | None]:
    if client is not None:
        return client, None
    if not config.api_key:
        return None, _missing_api_key_error(action)
    return CoreStackmintGateway(config.api_key, base_url=config.base_url), None


def _mutation_guard(
    config: StackmintMCPConfig,
    *,
    action: str,
    confirmed: bool,
) -> dict[str, Any] | None:
    if config.read_only:
        return read_only_error(action)
    if config.require_confirmation and not confirmed:
        return confirmation_required(action)
    return None


def _sanitize_payload_field(
    value: Any,
    config: StackmintMCPConfig,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if value is None:
        return None, {}
    if not config.record_payloads:
        return None, {"stackmint_mcp_record_payloads": False}

    sanitized = sanitize_payload(value, StackmintTelemetrySecurityConfig())
    payload = sanitized.value if isinstance(sanitized.value, dict) else {
        "value": sanitized.value
    }
    return payload, sanitized.metadata()


def _sanitize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    sanitized = sanitize_payload(metadata or {}, StackmintTelemetrySecurityConfig())
    safe_metadata = sanitized.value if isinstance(sanitized.value, dict) else {
        "metadata": sanitized.value
    }
    safe_metadata["source"] = "mcp"
    safe_metadata.update(sanitized.metadata())
    return safe_metadata


def _policy_response(me: Any) -> dict[str, Any]:
    payload = _model_dump(me)
    return _safe_response(
        {
            "ok": True,
            "gateway_agent_id": payload.get("gateway_agent_id"),
            "workspace_id": payload.get("workspace_id"),
            "status": payload.get("status"),
            "permitted_tool_slugs": payload.get("permitted_tool_slugs", []),
            "budget_ceiling_cents": payload.get("budget_ceiling_cents"),
            "require_approval_for": payload.get("require_approval_for", []),
            "latest_execution_status": payload.get("latest_execution_status"),
            "latest_execution_at": payload.get("latest_execution_at"),
        }
    )


def handle_stackmint_get_agent_policy(
    *,
    config: StackmintMCPConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    action = "stackmint_get_agent_policy"
    runtime_config = config or StackmintMCPConfig.from_env()
    gateway_client, error = _client_or_error(runtime_config, client, action=action)
    if error is not None:
        return error
    try:
        return _policy_response(gateway_client.get_me())
    except Exception as exc:
        return _safe_error(action, exc, runtime_config)


def handle_stackmint_get_agent_me(
    *,
    config: StackmintMCPConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    action = "stackmint_get_agent_me"
    runtime_config = config or StackmintMCPConfig.from_env()
    gateway_client, error = _client_or_error(runtime_config, client, action=action)
    if error is not None:
        return error
    try:
        return _safe_response(
            {"ok": True, "agent": _model_dump(gateway_client.get_me())}
        )
    except Exception as exc:
        return _safe_error(action, exc, runtime_config)


def handle_stackmint_authorize_execution(
    *,
    external_execution_ref: str | None = None,
    input_payload: dict[str, Any] | None = None,
    estimated_input_tokens: int | None = None,
    estimated_input_cost_cents: float | None = None,
    metadata: dict[str, Any] | None = None,
    confirmed: bool = False,
    config: StackmintMCPConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    action = "stackmint_authorize_execution"
    runtime_config = config or StackmintMCPConfig.from_env()
    guarded = _mutation_guard(runtime_config, action=action, confirmed=confirmed)
    if guarded is not None:
        return guarded
    gateway_client, error = _client_or_error(runtime_config, client, action=action)
    if error is not None:
        return error

    execution_ref = external_execution_ref or uuid.uuid4().hex
    safe_input, input_metadata = _sanitize_payload_field(input_payload, runtime_config)
    request_metadata = _sanitize_metadata(metadata)
    request_metadata.update(input_metadata)
    try:
        response = gateway_client.authorize_execution(
            GatewayExternalAuthorizeRequest(
                external_execution_ref=execution_ref,
                input_payload=safe_input,
                estimated_input_tokens=estimated_input_tokens,
                estimated_input_cost_cents=estimated_input_cost_cents,
                metadata=request_metadata,
            ),
            idempotency_key=execution_ref,
        )
        payload = _model_dump(response)
        return _safe_response(
            {
                "ok": True,
                "decision": payload.get("decision"),
                "reason": payload.get("reason"),
                "message": payload.get("message"),
                "approval_request_id": payload.get("approval_request_id"),
                "budget_reservation_id": payload.get("budget_reservation_id"),
                "policy_version": payload.get("policy_version"),
                "metadata": payload.get("metadata", {}),
            }
        )
    except Exception as exc:
        return _safe_error(action, exc, runtime_config)


def handle_stackmint_record_execution(
    *,
    external_execution_ref: str | None = None,
    status: ExecutionStatus = "completed",
    input_payload: dict[str, Any] | None = None,
    result_payload: dict[str, Any] | None = None,
    error_payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    confirmed: bool = False,
    config: StackmintMCPConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    action = "stackmint_record_execution"
    runtime_config = config or StackmintMCPConfig.from_env()
    guarded = _mutation_guard(runtime_config, action=action, confirmed=confirmed)
    if guarded is not None:
        return guarded
    gateway_client, error = _client_or_error(runtime_config, client, action=action)
    if error is not None:
        return error

    execution_ref = external_execution_ref or uuid.uuid4().hex
    safe_input, input_metadata = _sanitize_payload_field(input_payload, runtime_config)
    safe_result, result_metadata = _sanitize_payload_field(
        result_payload,
        runtime_config,
    )
    safe_error, error_metadata = _sanitize_payload_field(error_payload, runtime_config)
    request_metadata = _sanitize_metadata(metadata)
    request_metadata.update(input_metadata)
    request_metadata.update(result_metadata)
    request_metadata.update(error_metadata)
    try:
        response = gateway_client.record_execution(
            GatewayExternalExecutionCreateRequest(
                external_execution_ref=execution_ref,
                status=status,
                created_at=datetime.now(UTC),
                input_payload=safe_input,
                result_payload=safe_result,
                error_payload=safe_error,
                metadata=request_metadata,
            ),
            idempotency_key=execution_ref,
        )
        payload = _model_dump(response)
        return _safe_response(
            {
                "ok": True,
                "execution_id": payload.get("execution_id"),
                "external_execution_ref": payload.get("external_execution_ref"),
                "status": payload.get("status"),
            }
        )
    except Exception as exc:
        return _safe_error(action, exc, runtime_config)


def handle_stackmint_reserve_budget(
    *,
    external_execution_ref: str,
    estimated_tokens: int | None = None,
    estimated_cost_cents: float | None = None,
    metadata: dict[str, Any] | None = None,
    confirmed: bool = False,
    config: StackmintMCPConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    action = "stackmint_reserve_budget"
    runtime_config = config or StackmintMCPConfig.from_env()
    guarded = _mutation_guard(runtime_config, action=action, confirmed=confirmed)
    if guarded is not None:
        return guarded
    if not external_execution_ref:
        return _validation_error(action, "external_execution_ref is required.")
    gateway_client, error = _client_or_error(runtime_config, client, action=action)
    if error is not None:
        return error
    try:
        response = gateway_client.reserve_budget(
            GatewayExternalBudgetReserveRequest(
                external_execution_ref=external_execution_ref,
                estimated_tokens=estimated_tokens,
                estimated_cost_cents=estimated_cost_cents,
                metadata=_sanitize_metadata(metadata),
            ),
            idempotency_key=external_execution_ref,
        )
        payload = _model_dump(response)
        return _safe_response(
            {
                "ok": True,
                "status": payload.get("status"),
                "budget_reservation_id": payload.get("budget_reservation_id"),
                "approved_cost_cents": payload.get("approved_cost_cents"),
                "remaining_budget_cents": payload.get("remaining_budget_cents"),
                "reason": payload.get("reason"),
            }
        )
    except Exception as exc:
        return _safe_error(action, exc, runtime_config)


def handle_stackmint_commit_budget(
    *,
    external_execution_ref: str,
    budget_reservation_id: str | None = None,
    actual_tokens: int | None = None,
    actual_cost_cents: float | None = None,
    status: ExecutionStatus = "completed",
    metadata: dict[str, Any] | None = None,
    confirmed: bool = False,
    config: StackmintMCPConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    action = "stackmint_commit_budget"
    runtime_config = config or StackmintMCPConfig.from_env()
    guarded = _mutation_guard(runtime_config, action=action, confirmed=confirmed)
    if guarded is not None:
        return guarded
    if not external_execution_ref:
        return _validation_error(action, "external_execution_ref is required.")
    gateway_client, error = _client_or_error(runtime_config, client, action=action)
    if error is not None:
        return error
    try:
        response = gateway_client.commit_budget(
            GatewayExternalBudgetCommitRequest(
                external_execution_ref=external_execution_ref,
                budget_reservation_id=budget_reservation_id,
                actual_tokens=actual_tokens,
                actual_cost_cents=actual_cost_cents,
                status=status,
                metadata=_sanitize_metadata(metadata),
            ),
            idempotency_key=f"{external_execution_ref}:budget_commit",
        )
        payload = _model_dump(response)
        return _safe_response(
            {
                "ok": True,
                "status": payload.get("status"),
                "budget_reservation_id": payload.get("budget_reservation_id"),
                "committed_cost_cents": payload.get("committed_cost_cents"),
                "remaining_budget_cents": payload.get("remaining_budget_cents"),
            }
        )
    except Exception as exc:
        return _safe_error(action, exc, runtime_config)


def handle_stackmint_create_approval_request(
    *,
    external_execution_ref: str | None = None,
    external_tool_ref: str | None = None,
    reason: str | None = None,
    input_payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    confirmed: bool = False,
    config: StackmintMCPConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    action = "stackmint_create_approval_request"
    runtime_config = config or StackmintMCPConfig.from_env()
    guarded = _mutation_guard(runtime_config, action=action, confirmed=confirmed)
    if guarded is not None:
        return guarded
    gateway_client, error = _client_or_error(runtime_config, client, action=action)
    if error is not None:
        return error
    safe_input, input_metadata = _sanitize_payload_field(input_payload, runtime_config)
    request_metadata = _sanitize_metadata(metadata)
    request_metadata.update(input_metadata)
    idempotency_key = (
        f"{external_execution_ref}:approval"
        if external_execution_ref
        else uuid.uuid4().hex
    )
    try:
        response = gateway_client.create_approval_request(
            GatewayExternalApprovalCreateRequest(
                external_execution_ref=external_execution_ref,
                external_tool_ref=external_tool_ref,
                reason=reason,
                input_payload=safe_input,
                metadata=request_metadata,
            ),
            idempotency_key=idempotency_key,
        )
        payload = _model_dump(response)
        return _safe_response(
            {
                "ok": True,
                "approval_request_id": payload.get("approval_request_id"),
                "status": payload.get("status"),
                "message": payload.get("message"),
                "metadata": payload.get("metadata", {}),
            }
        )
    except Exception as exc:
        return _safe_error(action, exc, runtime_config)


def handle_stackmint_get_approval_decision(
    *,
    approval_request_id: str,
    config: StackmintMCPConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    action = "stackmint_get_approval_decision"
    runtime_config = config or StackmintMCPConfig.from_env()
    if not approval_request_id:
        return _validation_error(action, "approval_request_id is required.")
    gateway_client, error = _client_or_error(runtime_config, client, action=action)
    if error is not None:
        return error
    try:
        response = gateway_client.get_approval_decision(approval_request_id)
        payload = _model_dump(response)
        return _safe_response(
            {
                "ok": True,
                "approval_request_id": payload.get("approval_request_id"),
                "status": payload.get("status"),
                "approved_by": payload.get("approved_by"),
                "decided_at": payload.get("decided_at"),
                "message": payload.get("message"),
                "metadata": payload.get("metadata", {}),
            }
        )
    except Exception as exc:
        return _safe_error(action, exc, runtime_config)


def handle_stackmint_record_tool_event(
    *,
    external_execution_ref: str | None = None,
    external_tool_ref: str,
    status: ToolEventStatus,
    input_payload: dict[str, Any] | None = None,
    result_payload: dict[str, Any] | None = None,
    error_payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    confirmed: bool = False,
    config: StackmintMCPConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    action = "stackmint_record_tool_event"
    runtime_config = config or StackmintMCPConfig.from_env()
    guarded = _mutation_guard(runtime_config, action=action, confirmed=confirmed)
    if guarded is not None:
        return guarded
    if not external_tool_ref:
        return _validation_error(action, "external_tool_ref is required.")
    gateway_client, error = _client_or_error(runtime_config, client, action=action)
    if error is not None:
        return error
    safe_input, input_metadata = _sanitize_payload_field(input_payload, runtime_config)
    safe_result, result_metadata = _sanitize_payload_field(
        result_payload,
        runtime_config,
    )
    safe_error, error_metadata = _sanitize_payload_field(error_payload, runtime_config)
    request_metadata = _sanitize_metadata(metadata)
    request_metadata.update(input_metadata)
    request_metadata.update(result_metadata)
    request_metadata.update(error_metadata)
    idempotency_key = uuid.uuid4().hex
    try:
        response = gateway_client.record_tool_event(
            GatewayExternalToolEventCreateRequest(
                external_execution_ref=external_execution_ref,
                external_tool_ref=external_tool_ref,
                status=status,
                created_at=datetime.now(UTC),
                input_payload=safe_input,
                result_payload=safe_result,
                error_payload=safe_error,
                metadata=request_metadata,
            ),
            idempotency_key=idempotency_key,
        )
        payload = _model_dump(response)
        return _safe_response(
            {
                "ok": True,
                "tool_event_id": payload.get("tool_event_id"),
                "external_execution_ref": payload.get("external_execution_ref"),
                "external_tool_ref": payload.get("external_tool_ref"),
                "status": payload.get("status"),
            }
        )
    except Exception as exc:
        return _safe_error(action, exc, runtime_config)


def stackmint_governance_review_prompt(
    action_description: str,
    tool_name: str = "",
    risk_level: str = "unknown",
    business_context: str = "",
) -> str:
    return (
        "Review this proposed agent action for governance risk.\n"
        f"Action: {action_description}\n"
        f"Tool: {tool_name or 'none'}\n"
        f"Risk level: {risk_level}\n"
        f"Business context: {business_context or 'not provided'}\n\n"
        "Identify relevant policy concerns, whether human approval is needed, "
        "and recommend allow, block, or approve-with-conditions. Do not make "
        "final policy claims without evidence from the available policy state."
    )


def stackmint_incident_summary_prompt(
    execution_metadata: str,
    error_metadata: str = "",
    policy_decision: str = "",
) -> str:
    return (
        "Summarize this governed agent incident for human review.\n"
        f"Execution metadata: {execution_metadata}\n"
        f"Error metadata: {error_metadata or 'not provided'}\n"
        f"Policy decision: {policy_decision or 'not provided'}\n\n"
        "Focus on what was blocked or failed, the likely governance reason, "
        "and what evidence a reviewer should inspect next. Do not include or "
        "infer secrets."
    )


def _load_fastmcp() -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "Install MCP server dependencies with `uv sync --extra mcp` "
            "before running `python -m stackmint_gateway.mcp_server`."
        ) from exc
    return FastMCP


def create_server(
    config: StackmintMCPConfig | None = None,
    *,
    client: Any | None = None,
) -> Any:
    FastMCP = _load_fastmcp()
    runtime_config = config or StackmintMCPConfig.from_env()
    server = FastMCP("Stackmint Governance")

    @server.tool()
    def stackmint_get_agent_policy() -> dict[str, Any]:
        return handle_stackmint_get_agent_policy(
            config=runtime_config,
            client=client,
        )

    @server.tool()
    def stackmint_authorize_execution(
        external_execution_ref: str | None = None,
        input_payload: dict[str, Any] | None = None,
        estimated_input_tokens: int | None = None,
        estimated_input_cost_cents: float | None = None,
        metadata: dict[str, Any] | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        return handle_stackmint_authorize_execution(
            external_execution_ref=external_execution_ref,
            input_payload=input_payload,
            estimated_input_tokens=estimated_input_tokens,
            estimated_input_cost_cents=estimated_input_cost_cents,
            metadata=metadata,
            confirmed=confirmed,
            config=runtime_config,
            client=client,
        )

    @server.tool()
    def stackmint_record_execution(
        external_execution_ref: str | None = None,
        status: str = "completed",
        input_payload: dict[str, Any] | None = None,
        result_payload: dict[str, Any] | None = None,
        error_payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        return handle_stackmint_record_execution(
            external_execution_ref=external_execution_ref,
            status=status,
            input_payload=input_payload,
            result_payload=result_payload,
            error_payload=error_payload,
            metadata=metadata,
            confirmed=confirmed,
            config=runtime_config,
            client=client,
        )

    @server.tool()
    def stackmint_reserve_budget(
        external_execution_ref: str,
        estimated_tokens: int | None = None,
        estimated_cost_cents: float | None = None,
        metadata: dict[str, Any] | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        return handle_stackmint_reserve_budget(
            external_execution_ref=external_execution_ref,
            estimated_tokens=estimated_tokens,
            estimated_cost_cents=estimated_cost_cents,
            metadata=metadata,
            confirmed=confirmed,
            config=runtime_config,
            client=client,
        )

    @server.tool()
    def stackmint_commit_budget(
        external_execution_ref: str,
        budget_reservation_id: str | None = None,
        actual_tokens: int | None = None,
        actual_cost_cents: float | None = None,
        status: str = "completed",
        metadata: dict[str, Any] | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        return handle_stackmint_commit_budget(
            external_execution_ref=external_execution_ref,
            budget_reservation_id=budget_reservation_id,
            actual_tokens=actual_tokens,
            actual_cost_cents=actual_cost_cents,
            status=status,
            metadata=metadata,
            confirmed=confirmed,
            config=runtime_config,
            client=client,
        )

    @server.tool()
    def stackmint_create_approval_request(
        external_execution_ref: str | None = None,
        external_tool_ref: str | None = None,
        reason: str | None = None,
        input_payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        return handle_stackmint_create_approval_request(
            external_execution_ref=external_execution_ref,
            external_tool_ref=external_tool_ref,
            reason=reason,
            input_payload=input_payload,
            metadata=metadata,
            confirmed=confirmed,
            config=runtime_config,
            client=client,
        )

    @server.tool()
    def stackmint_get_approval_decision(
        approval_request_id: str,
    ) -> dict[str, Any]:
        return handle_stackmint_get_approval_decision(
            approval_request_id=approval_request_id,
            config=runtime_config,
            client=client,
        )

    @server.tool()
    def stackmint_record_tool_event(
        external_tool_ref: str,
        status: str,
        external_execution_ref: str | None = None,
        input_payload: dict[str, Any] | None = None,
        result_payload: dict[str, Any] | None = None,
        error_payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        return handle_stackmint_record_tool_event(
            external_execution_ref=external_execution_ref,
            external_tool_ref=external_tool_ref,
            status=status,
            input_payload=input_payload,
            result_payload=result_payload,
            error_payload=error_payload,
            metadata=metadata,
            confirmed=confirmed,
            config=runtime_config,
            client=client,
        )

    @server.resource("stackmint://agent/me")
    def stackmint_agent_me_resource() -> str:
        return json.dumps(
            handle_stackmint_get_agent_me(config=runtime_config, client=client)
        )

    @server.resource("stackmint://agent/policy")
    def stackmint_agent_policy_resource() -> str:
        return json.dumps(
            handle_stackmint_get_agent_policy(config=runtime_config, client=client)
        )

    @server.resource("stackmint://agent/executions/latest")
    def stackmint_latest_execution_resource() -> str:
        policy = handle_stackmint_get_agent_policy(config=runtime_config, client=client)
        return json.dumps(
            {
                "ok": policy.get("ok", False),
                "latest_execution_status": policy.get("latest_execution_status"),
                "latest_execution_at": policy.get("latest_execution_at"),
            }
        )

    @server.resource("stackmint://approvals/{approval_request_id}")
    def stackmint_approval_resource(approval_request_id: str) -> str:
        return json.dumps(
            handle_stackmint_get_approval_decision(
                approval_request_id=approval_request_id,
                config=runtime_config,
                client=client,
            )
        )

    @server.prompt()
    def stackmint_governance_review(
        action_description: str,
        tool_name: str = "",
        risk_level: str = "unknown",
        business_context: str = "",
    ) -> str:
        return stackmint_governance_review_prompt(
            action_description=action_description,
            tool_name=tool_name,
            risk_level=risk_level,
            business_context=business_context,
        )

    @server.prompt()
    def stackmint_incident_summary(
        execution_metadata: str,
        error_metadata: str = "",
        policy_decision: str = "",
    ) -> str:
        return stackmint_incident_summary_prompt(
            execution_metadata=execution_metadata,
            error_metadata=error_metadata,
            policy_decision=policy_decision,
        )

    return server


def main() -> None:
    server = create_server()
    try:
        server.run(transport="stdio")
    except TypeError:
        server.run()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
