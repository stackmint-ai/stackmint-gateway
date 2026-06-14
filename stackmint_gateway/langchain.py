from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Optional, Sequence

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import Runnable
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.tools import tool as langchain_tool

from stackmint_gateway.core import (
    BASE_URL,
    AgentStatus,
    CoreStackmintGateway,
    ExecutionStatus,
    GatewayExternalApprovalCreateRequest,
    GatewayExternalAuthorizeRequest,
    GatewayExternalAuthorizeResponse,
    GatewayExternalBudgetCommitRequest,
    GatewayExternalBudgetReserveRequest,
    GatewayExternalBudgetReserveResponse,
    GatewayExternalConfigPatchRequest,
    GatewayExternalExecutionCreateRequest,
    GatewayExternalMeResponse,
    GatewayExternalSyncRequest,
    GatewayExternalToolEventCreateRequest,
    GatewayExternalToolSyncItem,
    ToolEventStatus,
)
from stackmint_gateway.security import (
    StackmintTelemetrySecurityConfig,
    sanitize_payload,
)

BREAKER_FAILURE_THRESHOLD = 3
BREAKER_COOLDOWN_SECONDS = 60
DEFAULT_COST_PER_1K_TOKENS_CENTS = 1.0
HITL_REJECTION_FALLBACK_MESSAGE = "Action rejected by human supervisor."
_TOKEN_USAGE_CONTAINER_KEYS = (
    "usage_metadata",
    "response_metadata",
    "llm_output",
    "token_usage",
    "usage",
)
_LOCAL_HITL_TELEMETRY: list[dict[str, Any]] = []
_LOCAL_TOOL_POLICY_TELEMETRY: list[dict[str, Any]] = []
_CURRENT_EXECUTION_REF: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "stackmint_current_execution_ref",
    default=None,
)
logger = logging.getLogger(__name__)
ApprovalFn = Callable[[str], bool]
ToolEventRecorder = Callable[[dict[str, Any]], None]
_RESERVED_METADATA_KEYS = {
    "source",
    "stackmint_payload_security_version",
    "stackmint_redacted",
    "stackmint_truncated",
    "stackmint_serialization_error",
    "stackmint_record_inputs",
    "stackmint_record_outputs",
    "stackmint_record_errors",
}
_GOVERNANCE_METADATA_KEYS = {
    "reason",
    "agent_status",
    "approval_request_id",
    "budget_reservation_id",
    "budget_ceiling_cents",
    "current_session_cost_cents",
    "estimated_input_cost_cents",
    "policy_version",
    "remaining_budget_cents",
}
_TOTAL_TOKEN_KEYS = ("total_tokens", "total_token_count")
_PARTIAL_TOKEN_KEYS = (
    "input_tokens",
    "prompt_tokens",
    "input_token_count",
    "prompt_token_count",
    "output_tokens",
    "completion_tokens",
    "output_token_count",
    "completion_token_count",
)


def _normalize_metadata_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


class StackmintBudgetExceededError(RuntimeError):
    """Raised when local session cost exceeds the configured budget ceiling."""


class StackmintCircuitBreakerError(RuntimeError):
    """Raised when local circuit breaker state blocks execution."""


class StackmintAgentBlockedError(RuntimeError):
    """Raised when Stackmint control-plane status blocks agent execution."""


class StackmintAuthorizationBlockedError(RuntimeError):
    """Raised when Stackmint centralized authorization blocks execution."""


class StackmintApprovalRequiredError(RuntimeError):
    """Raised when Stackmint requires approval before execution can continue."""

    def __init__(
        self,
        message: str,
        *,
        approval_request_id: str | None = None,
    ) -> None:
        self.approval_request_id = approval_request_id
        super().__init__(message)


class StackmintToolNotAllowedError(RuntimeError):
    """Raised when a tool is not permitted by Stackmint policy."""


_POLICY_EXCEPTIONS = (
    StackmintBudgetExceededError,
    StackmintCircuitBreakerError,
    StackmintAgentBlockedError,
    StackmintAuthorizationBlockedError,
    StackmintApprovalRequiredError,
    StackmintToolNotAllowedError,
)


@dataclass
class StackmintRuntimePolicy:
    agent_status: AgentStatus | None = None
    permitted_tool_slugs: set[str] = field(default_factory=set)
    require_approval_for: set[str] = field(default_factory=set)
    budget_ceiling_cents: int | None = None


@dataclass
class StackmintToolPolicy:
    permitted_tool_slugs: set[str] = field(default_factory=set)
    require_approval_for: set[str] = field(default_factory=set)
    fallback_message: str = HITL_REJECTION_FALLBACK_MESSAGE
    approval_fn: ApprovalFn | None = None
    tool_event_recorder: ToolEventRecorder | None = None

    def wrap_tool(self, tool: Any) -> Any:
        return wrap_tool_with_stackmint_policy(
            tool,
            permitted_tool_slugs=self.permitted_tool_slugs,
            require_approval_for=self.require_approval_for,
            fallback_message=self.fallback_message,
            approval_fn=self.approval_fn,
            tool_event_recorder=self.tool_event_recorder,
        )

    def governed_tools(self, tools: Sequence[Any]) -> list[Any]:
        return [self.wrap_tool(tool) for tool in tools]


class StackmintCallbackHandler(BaseCallbackHandler):
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._started_at: dict[str, float] = {}

    def _run_key(self, run_id: Any) -> str:
        return str(run_id)

    def _record(
        self,
        event: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **metadata: Any,
    ) -> None:
        run_key = self._run_key(run_id)
        payload = {
            "event": event,
            "run_id": run_key,
            "parent_run_id": None if parent_run_id is None else str(parent_run_id),
            "created_at": datetime.now(UTC).isoformat(),
        }
        payload.update(
            {key: value for key, value in metadata.items() if value is not None}
        )
        self.events.append(payload)

    def _start(
        self,
        event: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **metadata: Any,
    ) -> None:
        self._started_at[self._run_key(run_id)] = time.perf_counter()
        self._record(event, run_id=run_id, parent_run_id=parent_run_id, **metadata)

    def _end(
        self,
        event: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **metadata: Any,
    ) -> None:
        started_at = self._started_at.pop(self._run_key(run_id), None)
        if started_at is not None:
            metadata["latency_ms"] = round(
                (time.perf_counter() - started_at) * 1000,
                3,
            )
        self._record(event, run_id=run_id, parent_run_id=parent_run_id, **metadata)

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        self._start(
            "chain_start",
            run_id=run_id,
            parent_run_id=parent_run_id,
            name=serialized.get("name") if isinstance(serialized, dict) else None,
        )

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        self._end("chain_end", run_id=run_id, parent_run_id=parent_run_id)

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        self._start(
            "llm_start",
            run_id=run_id,
            parent_run_id=parent_run_id,
            prompt_count=len(prompts),
        )

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        self._end(
            "llm_end",
            run_id=run_id,
            parent_run_id=parent_run_id,
            token_count=_extract_token_usage(response) or None,
        )

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        self._start(
            "tool_start",
            run_id=run_id,
            parent_run_id=parent_run_id,
            tool_name=serialized.get("name") if isinstance(serialized, dict) else None,
        )

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        self._end("tool_end", run_id=run_id, parent_run_id=parent_run_id)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        self._end(
            "chain_error",
            run_id=run_id,
            parent_run_id=parent_run_id,
            error_type=error.__class__.__name__,
            error_message=str(error),
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        self._end(
            "llm_error",
            run_id=run_id,
            parent_run_id=parent_run_id,
            error_type=error.__class__.__name__,
            error_message=str(error),
        )

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        self._end(
            "tool_error",
            run_id=run_id,
            parent_run_id=parent_run_id,
            error_type=error.__class__.__name__,
            error_message=str(error),
        )


def get_local_hitl_telemetry() -> list[dict[str, Any]]:
    return list(_LOCAL_HITL_TELEMETRY)


def get_local_tool_policy_telemetry() -> list[dict[str, Any]]:
    return list(_LOCAL_TOOL_POLICY_TELEMETRY)


def _payload(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _payload(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple, set)):
        return [_payload(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json", exclude_none=True)
        except TypeError:
            return value.model_dump(exclude_none=True)
    return value


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(
            tool.get("external_tool_ref")
            or tool.get("name")
            or tool.get("id")
            or "tool"
        )
    name = getattr(tool, "name", None)
    if name:
        return str(name)
    if callable(tool):
        return getattr(tool, "__name__", "tool")
    return "tool"


def _tool_metadata(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict):
        metadata = tool.get("metadata")
        return dict(metadata) if isinstance(metadata, dict) else {}
    raw_metadata = getattr(tool, "metadata", None)
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    description = getattr(tool, "description", None)
    if description:
        metadata["description"] = description
    doc = inspect.getdoc(tool)
    if doc and "description" not in metadata:
        metadata["description"] = doc
    return metadata


def _normalize_tool(tool: Any) -> GatewayExternalToolSyncItem:
    if isinstance(tool, GatewayExternalToolSyncItem):
        return tool
    if isinstance(tool, dict):
        external_tool_ref = str(
            tool.get("external_tool_ref")
            or tool.get("name")
            or tool.get("id")
            or "tool"
        )
        return GatewayExternalToolSyncItem(
            external_tool_ref=external_tool_ref,
            name=str(tool.get("name") or external_tool_ref),
            status=tool.get("status", "active"),
            metadata=dict(tool.get("metadata") or {}),
        )
    name = _tool_name(tool)
    return GatewayExternalToolSyncItem(
        external_tool_ref=name,
        name=name,
        metadata=_tool_metadata(tool),
    )


def _default_api_key() -> str | None:
    for env_name in (
        "STACKMINT_GATEWAY_API_KEY",
        "AGENT_API_KEY",
    ):
        value = os.getenv(env_name)
        if value:
            return value
    return None


def _as_string_set(values: Any) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        return {values}
    return {str(value) for value in values if value is not None}


def _tool_input_from_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if kwargs:
        return kwargs
    if len(args) == 1:
        return args[0]
    if args:
        return args
    return {}


def _tool_ref_candidates(tool: Any) -> set[str]:
    candidates = {_tool_name(tool)}
    metadata = _tool_metadata(tool)
    for key in ("external_tool_ref", "slug", "id", "name"):
        value = metadata.get(key)
        if value:
            candidates.add(str(value))
    if isinstance(tool, dict):
        for key in ("external_tool_ref", "slug", "id", "name"):
            value = tool.get(key)
            if value:
                candidates.add(str(value))
    return {candidate for candidate in candidates if candidate}


def _record_hitl_rejection(
    *,
    tool_name: str,
    telemetry: list[dict[str, Any]],
) -> None:
    event = {
        "event": "hitl_tool_rejected",
        "tool_name": tool_name,
        "created_at": datetime.now(UTC).isoformat(),
    }
    telemetry.append(event)
    _LOCAL_HITL_TELEMETRY.append(event)
    logger.info("Stackmint HITL rejected tool execution: %s", tool_name)


def _record_tool_policy_block(
    *,
    tool_name: str,
    telemetry: list[dict[str, Any]],
    reason: str,
) -> None:
    event = {
        "event": "tool_policy_blocked",
        "tool_name": tool_name,
        "reason": reason,
        "created_at": datetime.now(UTC).isoformat(),
    }
    telemetry.append(event)
    _LOCAL_TOOL_POLICY_TELEMETRY.append(event)
    logger.info("Stackmint policy blocked tool execution: %s", tool_name)


def default_terminal_approval(tool_name: str) -> bool:
    decision = input(
        f"Stackmint HITL: Tool [{tool_name}] requires approval to execute. "
        "Approve? (y/n):"
    )
    return decision.strip().lower() == "y"


def wrap_tool_with_stackmint_policy(
    tool: Any,
    *,
    permitted_tool_slugs: set[str] | None = None,
    require_approval_for: set[str] | None = None,
    fallback_message: str = HITL_REJECTION_FALLBACK_MESSAGE,
    approval_fn: ApprovalFn | None = None,
    tool_event_recorder: ToolEventRecorder | None = None,
) -> Any:
    original_tool = tool if isinstance(tool, BaseTool) else langchain_tool(tool)
    tool_name = _tool_name(original_tool)
    tool_refs = _tool_ref_candidates(original_tool)
    permitted_refs = _as_string_set(permitted_tool_slugs)
    approval_refs = _as_string_set(require_approval_for)
    requires_approval = bool(tool_refs & approval_refs)
    approve = approval_fn or default_terminal_approval
    telemetry: list[dict[str, Any]] = []
    metadata = dict(getattr(original_tool, "metadata", None) or {})
    metadata["stackmint_permitted_tool_slugs"] = sorted(permitted_refs)
    metadata["stackmint_require_approval_for"] = sorted(approval_refs)
    metadata["stackmint_requires_approval"] = requires_approval
    metadata["stackmint_hitl_events"] = telemetry
    metadata["stackmint_tool_policy_events"] = telemetry

    def record_tool_event(
        status: ToolEventStatus,
        *,
        tool_input: Any = None,
        result: Any = None,
        error: Exception | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if tool_event_recorder is None:
            return
        event = {
            "external_tool_ref": tool_name,
            "status": status,
            "input": tool_input,
            "result": result,
            "error": error,
            "metadata": metadata or {},
        }
        tool_event_recorder(event)

    def enforce_tool_policy(tool_input: Any) -> None:
        if permitted_refs and not (tool_refs & permitted_refs):
            _record_tool_policy_block(
                tool_name=tool_name,
                telemetry=telemetry,
                reason="tool_not_permitted",
            )
            record_tool_event(
                "blocked",
                tool_input=tool_input,
                metadata={"reason": "tool_not_permitted"},
            )
            raise StackmintToolNotAllowedError(
                f"Tool '{tool_name}' is not permitted by Stackmint policy."
            )

    def guarded_call(
        *args: Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        tool_input = _tool_input_from_args(args, kwargs)
        enforce_tool_policy(tool_input)
        if not requires_approval or approve(tool_name):
            try:
                result = original_tool.invoke(tool_input, config=config)
            except Exception as exc:
                record_tool_event("failed", tool_input=tool_input, error=exc)
                raise
            record_tool_event("allowed", tool_input=tool_input, result=result)
            return result
        _record_hitl_rejection(tool_name=tool_name, telemetry=telemetry)
        record_tool_event(
            "rejected",
            tool_input=tool_input,
            result=fallback_message,
            metadata={"reason": "hitl_rejected"},
        )
        return fallback_message

    async def guarded_acall(
        *args: Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        tool_input = _tool_input_from_args(args, kwargs)
        enforce_tool_policy(tool_input)
        if not requires_approval or await asyncio.to_thread(approve, tool_name):
            try:
                result = await original_tool.ainvoke(tool_input, config=config)
            except Exception as exc:
                record_tool_event("failed", tool_input=tool_input, error=exc)
                raise
            record_tool_event("allowed", tool_input=tool_input, result=result)
            return result
        _record_hitl_rejection(tool_name=tool_name, telemetry=telemetry)
        record_tool_event(
            "rejected",
            tool_input=tool_input,
            result=fallback_message,
            metadata={"reason": "hitl_rejected"},
        )
        return fallback_message

    return StructuredTool.from_function(
        func=guarded_call,
        coroutine=guarded_acall,
        name=tool_name,
        description=getattr(original_tool, "description", None) or tool_name,
        return_direct=getattr(original_tool, "return_direct", False),
        args_schema=getattr(original_tool, "args_schema", None),
        response_format=getattr(original_tool, "response_format", "content"),
        metadata=metadata,
    )


def wrap_tool_with_hitl_approval(
    tool: Any,
    *,
    requires_approval: bool = False,
    fallback_message: str = HITL_REJECTION_FALLBACK_MESSAGE,
    approval_fn: ApprovalFn | None = None,
) -> Any:
    if not requires_approval:
        return tool
    return wrap_tool_with_stackmint_policy(
        tool,
        require_approval_for=_tool_ref_candidates(tool),
        fallback_message=fallback_message,
        approval_fn=approval_fn,
    )


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _tokens_from_usage_metadata(metadata: Any) -> int | None:
    if not isinstance(metadata, dict):
        return None

    for key in _TOKEN_USAGE_CONTAINER_KEYS:
        nested_tokens = _tokens_from_usage_metadata(metadata.get(key))
        if nested_tokens is not None:
            return nested_tokens

    for key in _TOTAL_TOKEN_KEYS:
        total = _coerce_number(metadata.get(key))
        if total is not None:
            return int(total)

    token_count = 0
    found_token_key = False
    for key in _PARTIAL_TOKEN_KEYS:
        count = _coerce_number(metadata.get(key))
        if count is not None:
            token_count += int(count)
            found_token_key = True

    return token_count if found_token_key else None


def _extract_token_usage(value: Any, seen: set[int] | None = None) -> int:
    if value is None:
        return 0

    seen = seen or set()
    value_id = id(value)
    if value_id in seen:
        return 0
    seen.add(value_id)

    for attr_name in ("usage_metadata", "response_metadata", "llm_output"):
        tokens = _tokens_from_usage_metadata(getattr(value, attr_name, None))
        if tokens is not None:
            return tokens

    if isinstance(value, dict):
        tokens = _tokens_from_usage_metadata(value)
        if tokens is not None:
            return tokens
        return sum(_extract_token_usage(item, seen) for item in value.values())

    if isinstance(value, (list, tuple, set)):
        return sum(_extract_token_usage(item, seen) for item in value)

    return 0


def _estimate_token_count(value: Any) -> int:
    if value is None:
        return 0
    try:
        text = json.dumps(_payload(value), default=str, ensure_ascii=True)
    except Exception:
        text = str(value)
    return max(1, (len(text) + 3) // 4) if text else 0


def _tokens_for_run(input: Any, output: Any) -> int:
    metadata_tokens = _extract_token_usage(output)
    if metadata_tokens > 0:
        return metadata_tokens
    return _estimate_token_count(input) + _estimate_token_count(output)


def _tokens_to_cost_cents(token_count: int) -> float:
    return token_count * DEFAULT_COST_PER_1K_TOKENS_CENTS / 1000


@dataclass
class GovernedAgentState:
    last_me_response: Any = None
    last_sync_response: Any = None
    last_execution_response: Any = None
    last_authorize_response: Any = None
    last_budget_reservation_response: Any = None
    last_budget_commit_response: Any = None
    last_approval_response: Any = None
    last_tool_event_response: Any = None
    last_sync_error: Exception | None = None
    last_execution_error: Exception | None = None
    last_control_plane_error: Exception | None = None
    last_policy: StackmintRuntimePolicy | None = None
    callback_events: list[dict[str, Any]] = field(default_factory=list)


class GovernedAgent(Runnable[Any, Any]):
    def __init__(
        self,
        agent: Runnable[Any, Any],
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        name: str | None = None,
        description: str | None = None,
        framework: str = "langchain",
        model: str | None = None,
        status: AgentStatus | None = None,
        external_agent_ref: str | None = None,
        permitted_tool_slugs: list[str] | None = None,
        require_approval_for: list[str] | None = None,
        budget_ceiling_cents: int | None = None,
        tools: Sequence[Any] | None = None,
        sync_on_init: bool = False,
        sync_on_invoke: bool = True,
        fail_open: bool = True,
        telemetry_security: StackmintTelemetrySecurityConfig | None = None,
        record_inputs: bool = True,
        record_outputs: bool = True,
        record_errors: bool = True,
        remote_authorization: bool = False,
        remote_budget: bool = False,
        remote_approvals: bool = False,
        record_tool_events: bool = False,
    ) -> None:
        self.agent = agent
        self.api_key = api_key or _default_api_key()
        self.client = (
            CoreStackmintGateway(
                api_key=self.api_key,
                base_url=base_url or BASE_URL,
            )
            if self.api_key
            else None
        )
        self.name = name
        self.description = description
        self.framework = framework
        self.model = model
        self.status = status
        self.external_agent_ref = external_agent_ref
        self.permitted_tool_slugs = permitted_tool_slugs or []
        self.require_approval_for = require_approval_for or []
        self.budget_ceiling_cents = budget_ceiling_cents
        self.raw_tools = list(tools or [])
        self.tools = [_normalize_tool(tool) for tool in self.raw_tools]
        self.sync_on_invoke = sync_on_invoke
        self.fail_open = fail_open
        self.telemetry_security = (
            telemetry_security or StackmintTelemetrySecurityConfig()
        )
        self.record_inputs = record_inputs
        self.record_outputs = record_outputs
        self.record_errors = record_errors
        self.remote_authorization = remote_authorization
        self.remote_budget = remote_budget
        self.remote_approvals = remote_approvals
        self.record_tool_events = record_tool_events
        self.current_session_cost = 0.0
        self.consecutive_failures = 0
        self.breaker_open = False
        self.breaker_cooldown_until: datetime | None = None
        self.callback_handler = StackmintCallbackHandler()
        self.state = GovernedAgentState()
        self.state.last_policy = self._local_policy()
        self.state.callback_events = self.callback_handler.events

        if sync_on_init:
            self.sync_agent()

    def _config_payload(self) -> GatewayExternalConfigPatchRequest:
        return GatewayExternalConfigPatchRequest(
            name=self.name,
            description=self.description,
            framework=self.framework,
            model=self.model,
            status=self.status,  # type: ignore[arg-type]
            external_agent_ref=self.external_agent_ref,
            permitted_tool_slugs=self.permitted_tool_slugs or None,
            require_approval_for=self.require_approval_for or None,
            budget_ceiling_cents=self.budget_ceiling_cents,
        )

    def _sync_request(self) -> GatewayExternalSyncRequest:
        config = self._config_payload()
        return GatewayExternalSyncRequest(
            config=config if config.model_dump(exclude_none=True) else None,
            tools=self.tools or None,
        )

    def _local_policy(self) -> StackmintRuntimePolicy:
        return StackmintRuntimePolicy(
            agent_status=self.status,
            permitted_tool_slugs=_as_string_set(self.permitted_tool_slugs),
            require_approval_for=_as_string_set(self.require_approval_for),
            budget_ceiling_cents=self.budget_ceiling_cents,
        )

    def _policy_from_remote(
        self,
        remote: GatewayExternalMeResponse,
        *,
        local: StackmintRuntimePolicy | None = None,
    ) -> StackmintRuntimePolicy:
        local_policy = local or self._local_policy()
        return StackmintRuntimePolicy(
            agent_status=remote.status or local_policy.agent_status,
            permitted_tool_slugs=_as_string_set(remote.permitted_tool_slugs),
            require_approval_for=_as_string_set(remote.require_approval_for),
            budget_ceiling_cents=(
                remote.budget_ceiling_cents
                if remote.budget_ceiling_cents is not None
                else local_policy.budget_ceiling_cents
            ),
        )

    def refresh_policy(self) -> StackmintRuntimePolicy | None:
        local_policy = self._local_policy()
        if self.client is None:
            self.state.last_policy = local_policy
            return local_policy

        try:
            response = self.client.get_me()
            self.state.last_me_response = response
            policy = self._policy_from_remote(response, local=local_policy)
            self.state.last_policy = policy
            self.state.last_sync_error = None
            return policy
        except Exception as exc:
            self.state.last_sync_error = exc
            if not self.fail_open:
                raise
            self.state.last_policy = local_policy
            return local_policy

    def governed_tools(self) -> list[Any]:
        policy = self.state.last_policy
        if policy is None or self.client is not None:
            policy = self.refresh_policy()
        if policy is None:
            policy = self._local_policy()
        return [
            wrap_tool_with_stackmint_policy(
                tool,
                permitted_tool_slugs=policy.permitted_tool_slugs,
                require_approval_for=policy.require_approval_for,
                tool_event_recorder=(
                    self._record_tool_event_from_wrapper
                    if self.record_tool_events
                    else None
                ),
            )
            for tool in self.raw_tools
        ]

    def _config_with_stackmint_callbacks(
        self,
        config: Optional[RunnableConfig],
    ) -> RunnableConfig:
        merged_config: RunnableConfig = dict(config or {})
        callbacks = merged_config.get("callbacks")
        if callbacks is None:
            merged_config["callbacks"] = [self.callback_handler]
        elif isinstance(callbacks, list):
            merged_config["callbacks"] = [*callbacks, self.callback_handler]
        elif isinstance(callbacks, tuple):
            merged_config["callbacks"] = [*callbacks, self.callback_handler]
        elif hasattr(callbacks, "copy") and hasattr(callbacks, "add_handler"):
            callback_manager = callbacks.copy()
            callback_manager.add_handler(self.callback_handler)
            merged_config["callbacks"] = callback_manager
        else:
            merged_config["callbacks"] = [callbacks, self.callback_handler]
        return merged_config

    def _sync_circuit_breaker_to_client(self) -> None:
        if self.client is None:
            return
        self.client.consecutive_failures = self.consecutive_failures
        self.client.breaker_open = self.breaker_open
        self.client.breaker_cooldown_until = self.breaker_cooldown_until

    def _reset_circuit_breaker(self) -> None:
        self.consecutive_failures = 0
        self.breaker_open = False
        self.breaker_cooldown_until = None
        self._sync_circuit_breaker_to_client()

    def _open_circuit_breaker(self) -> None:
        self.breaker_open = True
        self.breaker_cooldown_until = datetime.now(UTC) + timedelta(
            seconds=BREAKER_COOLDOWN_SECONDS
        )
        self._sync_circuit_breaker_to_client()

    def _record_underlying_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= BREAKER_FAILURE_THRESHOLD:
            self._open_circuit_breaker()
            return
        self._sync_circuit_breaker_to_client()

    def _policy_block_metadata(self, error: Exception) -> dict[str, Any]:
        if isinstance(error, StackmintToolNotAllowedError):
            return {"reason": "tool_not_permitted"}
        if isinstance(error, StackmintAgentBlockedError):
            return {"reason": "agent_status"}
        if isinstance(error, StackmintAuthorizationBlockedError):
            return {"reason": "remote_authorization_blocked"}
        if isinstance(error, StackmintApprovalRequiredError):
            return {
                "reason": "waiting_approval",
                "approval_request_id": error.approval_request_id,
            }
        if isinstance(error, StackmintBudgetExceededError):
            return self._budget_metadata(self.state.last_policy)
        if isinstance(error, StackmintCircuitBreakerError):
            return {"reason": "circuit_breaker_open"}
        return {"reason": "policy_blocked"}

    def _check_circuit_breaker(self) -> None:
        if not self.breaker_open:
            return
        now = datetime.now(UTC)
        if (
            self.breaker_cooldown_until is not None
            and now >= self.breaker_cooldown_until
        ):
            self._reset_circuit_breaker()
            return
        raise StackmintCircuitBreakerError(
            "Circuit breaker is open. Execution blocked."
        )

    def _set_session_cost(self, cost_cents: float) -> None:
        self.current_session_cost = cost_cents
        if self.client is not None:
            self.client.current_session_cost = cost_cents

    def _safe_record_execution(
        self,
        input: Any,
        *,
        output: Any = None,
        error: Exception | None = None,
        status: ExecutionStatus | None = None,
        metadata: dict[str, Any] | None = None,
        external_tool_ref: str | None = None,
        external_execution_ref: str | None = None,
    ) -> None:
        try:
            self.record_execution(
                input,
                output=output,
                error=error,
                status=status,
                metadata=metadata,
                external_tool_ref=external_tool_ref,
                external_execution_ref=external_execution_ref,
            )
        except Exception:
            return

    def _secure_payload(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        sanitized = sanitize_payload(payload, self.telemetry_security)
        if isinstance(sanitized.value, dict):
            return sanitized.value, sanitized.metadata()
        return {"value": sanitized.value}, sanitized.metadata()

    def _secure_metadata(
        self,
        metadata: dict[str, Any] | None,
        *,
        allow_reserved: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if metadata is None:
            return {}, {}

        metadata_allow_keys = set(self.telemetry_security.allow_keys or set())
        metadata_allow_keys.update(_GOVERNANCE_METADATA_KEYS)
        sanitized = sanitize_payload(
            metadata,
            replace(self.telemetry_security, allow_keys=metadata_allow_keys),
        )
        if isinstance(sanitized.value, dict):
            safe_metadata = dict(sanitized.value)
        else:
            safe_metadata = {"metadata": sanitized.value}

        if not allow_reserved:
            reserved_keys = {
                _normalize_metadata_key(key) for key in _RESERVED_METADATA_KEYS
            }
            safe_metadata = {
                key: value
                for key, value in safe_metadata.items()
                if _normalize_metadata_key(key) not in reserved_keys
            }
        return safe_metadata, sanitized.metadata()

    def _execution_payloads(
        self,
        input: Any,
        *,
        output: Any = None,
        error: Exception | None = None,
    ) -> tuple[
        dict[str, Any] | None,
        dict[str, Any] | None,
        dict[str, Any] | None,
        dict[str, Any],
    ]:
        metadata: dict[str, Any] = {}

        input_payload = None
        if self.record_inputs:
            input_payload, input_metadata = self._secure_payload({"input": input})
            metadata.update(input_metadata)
        else:
            metadata["stackmint_record_inputs"] = False

        result_payload = None
        if error is None:
            if self.record_outputs:
                result_payload, result_metadata = self._secure_payload(
                    {"output": output}
                )
                metadata.update(result_metadata)
            else:
                metadata["stackmint_record_outputs"] = False

        error_payload = None
        if error is not None:
            if self.record_errors:
                error_payload, error_metadata = self._secure_payload(
                    {
                        "type": error.__class__.__name__,
                        "message": str(error),
                    }
                )
                metadata.update(error_metadata)
            else:
                error_payload = {
                    "type": error.__class__.__name__,
                    "message": "Error recording disabled by telemetry security policy.",
                }
                metadata["stackmint_record_errors"] = False

        return input_payload, result_payload, error_payload, metadata

    def _effective_budget_ceiling(
        self,
        policy: StackmintRuntimePolicy | None = None,
    ) -> int | None:
        if policy is not None:
            return policy.budget_ceiling_cents
        if self.state.last_policy is not None:
            return self.state.last_policy.budget_ceiling_cents
        return self.budget_ceiling_cents

    def _budget_metadata(
        self,
        policy: StackmintRuntimePolicy | None,
        *,
        estimated_input_cost_cents: float | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "reason": "budget_exceeded",
            "current_session_cost_cents": self.current_session_cost,
            "budget_ceiling_cents": self._effective_budget_ceiling(policy),
        }
        if estimated_input_cost_cents is not None:
            metadata["estimated_input_cost_cents"] = estimated_input_cost_cents
        return metadata

    def _budget_exceeded_error(
        self,
        policy: StackmintRuntimePolicy | None,
        *,
        estimated_input_cost_cents: float | None = None,
    ) -> StackmintBudgetExceededError:
        ceiling = self._effective_budget_ceiling(policy)
        if estimated_input_cost_cents is None:
            return StackmintBudgetExceededError(
                "Stackmint budget exceeded: current session cost "
                f"{self.current_session_cost:.4f} cents exceeds budget ceiling "
                f"{ceiling} cents."
            )
        projected = self.current_session_cost + estimated_input_cost_cents
        return StackmintBudgetExceededError(
            "Stackmint budget preflight blocked execution: projected session cost "
            f"{projected:.4f} cents exceeds budget ceiling {ceiling} cents."
        )

    def _check_budget_preflight(
        self,
        input: Any,
        policy: StackmintRuntimePolicy | None,
        *,
        external_execution_ref: str | None = None,
    ) -> None:
        ceiling = self._effective_budget_ceiling(policy)
        if ceiling is None:
            return
        estimated_input_cost_cents = _tokens_to_cost_cents(_estimate_token_count(input))
        if self.current_session_cost + estimated_input_cost_cents <= ceiling:
            return

        error = self._budget_exceeded_error(
            policy,
            estimated_input_cost_cents=estimated_input_cost_cents,
        )
        self._safe_record_execution(
            input,
            error=error,
            status="blocked",
            metadata=self._budget_metadata(
                policy,
                estimated_input_cost_cents=estimated_input_cost_cents,
            ),
            external_execution_ref=external_execution_ref,
        )
        raise error

    def _track_budget_usage(self, input: Any, output: Any) -> None:
        try:
            token_count = _tokens_for_run(input, output)
            if token_count <= 0:
                return
            self._set_session_cost(
                self.current_session_cost + _tokens_to_cost_cents(token_count)
            )
        except Exception:
            return

    def _control_plane_unavailable(self, error: Exception) -> None:
        self.state.last_control_plane_error = error
        if not self.fail_open:
            raise error

    def _authorization_metadata(
        self,
        response: GatewayExternalAuthorizeResponse,
        *,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "reason": reason,
            "approval_request_id": response.approval_request_id,
            "budget_reservation_id": response.budget_reservation_id,
            "budget_ceiling_cents": response.budget_ceiling_cents,
            "remaining_budget_cents": response.remaining_budget_cents,
            "policy_version": response.policy_version,
        }

    def _create_remote_approval_request(
        self,
        input: Any,
        *,
        external_execution_ref: str,
        reason: str | None = None,
    ) -> str | None:
        if not self.remote_approvals or self.client is None:
            return None
        input_payload, security_metadata = self._secure_payload({"input": input})
        payload = GatewayExternalApprovalCreateRequest(
            external_execution_ref=external_execution_ref,
            reason=reason,
            input_payload=input_payload,
            metadata={"source": "langchain", **security_metadata},
        )
        try:
            response = self.client.create_approval_request(
                payload,
                idempotency_key=f"{external_execution_ref}:approval",
            )
            self.state.last_approval_response = response
            self.state.last_control_plane_error = None
            return response.approval_request_id
        except Exception as exc:
            self._control_plane_unavailable(exc)
            return None

    def get_approval_decision(self, approval_request_id: str) -> Any | None:
        if self.client is None:
            return None
        try:
            response = self.client.get_approval_decision(approval_request_id)
            self.state.last_approval_response = response
            self.state.last_control_plane_error = None
            return response
        except Exception as exc:
            self._control_plane_unavailable(exc)
            return None

    def _authorize_execution_remote(
        self,
        input: Any,
        *,
        external_execution_ref: str,
    ) -> GatewayExternalAuthorizeResponse | None:
        if not self.remote_authorization or self.client is None:
            return None

        estimated_input_tokens = _estimate_token_count(input)
        estimated_input_cost_cents = _tokens_to_cost_cents(estimated_input_tokens)
        input_payload, security_metadata = self._secure_payload({"input": input})
        payload = GatewayExternalAuthorizeRequest(
            external_execution_ref=external_execution_ref,
            input_payload=input_payload,
            estimated_input_tokens=estimated_input_tokens,
            estimated_input_cost_cents=estimated_input_cost_cents,
            metadata={"source": "langchain", **security_metadata},
        )

        try:
            response = self.client.authorize_execution(
                payload,
                idempotency_key=external_execution_ref,
            )
            self.state.last_authorize_response = response
            self.state.last_control_plane_error = None
        except Exception as exc:
            self._control_plane_unavailable(exc)
            return None

        if response.decision == "allow":
            return response

        if response.decision == "block":
            error = StackmintAuthorizationBlockedError(
                response.message
                or response.reason
                or "Stackmint authorization blocked execution."
            )
            self._safe_record_execution(
                input,
                error=error,
                status="blocked",
                metadata=self._authorization_metadata(
                    response,
                    reason="remote_authorization_blocked",
                ),
                external_execution_ref=external_execution_ref,
            )
            raise error

        if response.decision == "budget_exceeded":
            error = StackmintBudgetExceededError(
                response.message
                or response.reason
                or "Stackmint remote budget authorization blocked execution."
            )
            self._safe_record_execution(
                input,
                error=error,
                status="blocked",
                metadata=self._authorization_metadata(
                    response,
                    reason="budget_exceeded",
                ),
                external_execution_ref=external_execution_ref,
            )
            raise error

        approval_request_id = response.approval_request_id
        if approval_request_id is None:
            approval_request_id = self._create_remote_approval_request(
                input,
                external_execution_ref=external_execution_ref,
                reason=response.reason or response.message,
            )
        error = StackmintApprovalRequiredError(
            response.message
            or response.reason
            or "Stackmint approval is required before execution can continue.",
            approval_request_id=approval_request_id,
        )
        self._safe_record_execution(
            input,
            error=error,
            status="waiting_approval",
            metadata={
                **self._authorization_metadata(response, reason="waiting_approval"),
                "approval_request_id": approval_request_id,
            },
            external_execution_ref=external_execution_ref,
        )
        raise error

    def _budget_reservation_from_authorization(
        self,
        response: GatewayExternalAuthorizeResponse | None,
    ) -> GatewayExternalBudgetReserveResponse | None:
        if (
            not self.remote_budget
            or response is None
            or not response.budget_reservation_id
        ):
            return None

        reservation = GatewayExternalBudgetReserveResponse(
            status="reserved",
            budget_reservation_id=response.budget_reservation_id,
            reason=response.reason,
            approved_cost_cents=None,
            remaining_budget_cents=response.remaining_budget_cents,
            metadata={
                "source": "authorization",
                "policy_version": response.policy_version,
            },
        )
        self.state.last_budget_reservation_response = reservation
        return reservation

    def _reserve_budget_remote(
        self,
        input: Any,
        *,
        external_execution_ref: str,
    ) -> GatewayExternalBudgetReserveResponse | None:
        if not self.remote_budget or self.client is None:
            return None

        estimated_tokens = _estimate_token_count(input)
        estimated_cost_cents = _tokens_to_cost_cents(estimated_tokens)
        payload = GatewayExternalBudgetReserveRequest(
            external_execution_ref=external_execution_ref,
            estimated_tokens=estimated_tokens,
            estimated_cost_cents=estimated_cost_cents,
            metadata={
                "source": "langchain",
                "estimated_input_cost_cents": estimated_cost_cents,
            },
        )
        try:
            response = self.client.reserve_budget(
                payload,
                idempotency_key=external_execution_ref,
            )
            self.state.last_budget_reservation_response = response
            self.state.last_control_plane_error = None
        except Exception as exc:
            self._control_plane_unavailable(exc)
            return None

        if response.status != "rejected":
            return response

        error = StackmintBudgetExceededError(
            response.reason or "Stackmint remote budget reservation was rejected."
        )
        self._safe_record_execution(
            input,
            error=error,
            status="blocked",
            metadata={
                "reason": "budget_exceeded",
                "budget_reservation_id": response.budget_reservation_id,
                "remaining_budget_cents": response.remaining_budget_cents,
                "estimated_input_cost_cents": estimated_cost_cents,
            },
            external_execution_ref=external_execution_ref,
        )
        raise error

    def _commit_budget_remote(
        self,
        input: Any,
        *,
        output: Any = None,
        error: Exception | None = None,
        status: ExecutionStatus,
        reservation: GatewayExternalBudgetReserveResponse | None,
        external_execution_ref: str,
    ) -> Any | None:
        if not self.remote_budget or self.client is None or reservation is None:
            return None

        actual_tokens = _tokens_for_run(input, output) if error is None else 0
        actual_cost_cents = _tokens_to_cost_cents(actual_tokens)
        payload = GatewayExternalBudgetCommitRequest(
            external_execution_ref=external_execution_ref,
            budget_reservation_id=reservation.budget_reservation_id,
            actual_tokens=actual_tokens,
            actual_cost_cents=actual_cost_cents,
            status=status,
            metadata={"source": "langchain"},
        )
        try:
            response = self.client.commit_budget(
                payload,
                idempotency_key=f"{external_execution_ref}:budget_commit",
            )
            self.state.last_budget_commit_response = response
            self.state.last_control_plane_error = None
            return response
        except Exception as exc:
            self._control_plane_unavailable(exc)
            return None

    def _record_tool_event_from_wrapper(self, event: dict[str, Any]) -> None:
        if not self.record_tool_events or self.client is None:
            return

        external_tool_ref = str(event.get("external_tool_ref") or "tool")
        status = event.get("status")
        external_execution_ref = event.get(
            "external_execution_ref"
        ) or _CURRENT_EXECUTION_REF.get()
        metadata = dict(event.get("metadata") or {})
        input_payload = result_payload = error_payload = None
        security_metadata: dict[str, Any] = {}

        if "input" in event:
            input_payload, input_metadata = self._secure_payload(
                {"input": event.get("input")}
            )
            security_metadata.update(input_metadata)
        if "result" in event and event.get("error") is None:
            result_payload, result_metadata = self._secure_payload(
                {"output": event.get("result")}
            )
            security_metadata.update(result_metadata)
        if event.get("error") is not None:
            tool_error = event["error"]
            error_payload, error_metadata = self._secure_payload(
                {
                    "type": tool_error.__class__.__name__,
                    "message": str(tool_error),
                }
            )
            security_metadata.update(error_metadata)

        safe_metadata, metadata_security = self._secure_metadata(metadata)
        event_metadata = {"source": "langchain"}
        event_metadata.update(safe_metadata)
        event_metadata.update(security_metadata)
        event_metadata.update(metadata_security)

        event_ref = uuid.uuid4().hex
        payload = GatewayExternalToolEventCreateRequest(
            external_execution_ref=external_execution_ref,
            external_tool_ref=external_tool_ref,
            status=status,
            created_at=datetime.now(UTC),
            input_payload=input_payload,
            result_payload=result_payload,
            error_payload=error_payload,
            metadata=event_metadata,
        )
        try:
            response = self.client.record_tool_event(
                payload,
                idempotency_key=event_ref,
            )
            self.state.last_tool_event_response = response
            self.state.last_control_plane_error = None
        except Exception as exc:
            self._control_plane_unavailable(exc)

    def _raise_if_agent_blocked(
        self,
        input: Any,
        policy: StackmintRuntimePolicy | None,
        *,
        external_execution_ref: str | None = None,
    ) -> None:
        if policy is None or policy.agent_status not in ("blocked", "suspended"):
            return
        error = StackmintAgentBlockedError(
            "Stackmint policy blocked agent execution: "
            f"remote agent status is '{policy.agent_status}'."
        )
        self._safe_record_execution(
            input,
            error=error,
            status="blocked",
            metadata={
                "reason": "agent_status",
                "agent_status": policy.agent_status,
            },
            external_execution_ref=external_execution_ref,
        )
        raise error

    def _refresh_policy_for_execution(
        self,
        input: Any,
        *,
        external_execution_ref: str | None = None,
    ) -> StackmintRuntimePolicy | None:
        policy = self.refresh_policy()
        self._raise_if_agent_blocked(
            input,
            policy,
            external_execution_ref=external_execution_ref,
        )
        self._check_budget_preflight(
            input,
            policy,
            external_execution_ref=external_execution_ref,
        )

        if self.sync_on_invoke:
            sync_response = self.sync_agent()
            if getattr(sync_response, "agent", None) is not None:
                policy = self._policy_from_remote(sync_response.agent)
                self.state.last_policy = policy
            elif self.state.last_policy is not None:
                policy = self.state.last_policy
            self._raise_if_agent_blocked(
                input,
                policy,
                external_execution_ref=external_execution_ref,
            )
            self._check_budget_preflight(
                input,
                policy,
                external_execution_ref=external_execution_ref,
            )

        return policy

    def get_me(self) -> Any | None:
        if self.client is None:
            return None
        try:
            response = self.client.get_me()
            self.state.last_me_response = response
            self.state.last_policy = self._policy_from_remote(response)
            self.state.last_sync_error = None
            return response
        except Exception as exc:
            self.state.last_sync_error = exc
            if not self.fail_open:
                raise
            return None

    def sync_agent(self) -> Any | None:
        if self.client is None:
            return None
        try:
            me = self.client.get_me()
            self.state.last_me_response = me
            self.state.last_policy = self._policy_from_remote(me)
            response = self.client.sync_agent(self._sync_request())
            self.state.last_sync_response = response
            if getattr(response, "agent", None) is not None:
                self.state.last_policy = self._policy_from_remote(response.agent)
            self.state.last_sync_error = None
            return response
        except Exception as exc:
            self.state.last_sync_error = exc
            if not self.fail_open:
                raise
            return None

    def record_execution(
        self,
        input: Any,
        *,
        output: Any = None,
        error: Exception | None = None,
        status: ExecutionStatus | None = None,
        metadata: dict[str, Any] | None = None,
        external_tool_ref: str | None = None,
        external_execution_ref: str | None = None,
    ) -> Any | None:
        if self.client is None:
            return None

        (
            input_payload,
            result_payload,
            error_payload,
            security_metadata,
        ) = self._execution_payloads(input, output=output, error=error)
        execution_status = status or ("failed" if error else "completed")
        user_metadata, user_security_metadata = self._secure_metadata(metadata)
        execution_metadata = {"source": "langchain"}
        execution_metadata.update(user_metadata)
        execution_metadata.update(security_metadata)
        execution_metadata.update(user_security_metadata)
        execution_ref = external_execution_ref or uuid.uuid4().hex
        payload = GatewayExternalExecutionCreateRequest(
            external_execution_ref=execution_ref,
            external_tool_ref=external_tool_ref,
            status=execution_status,
            created_at=datetime.now(UTC),
            input_payload=input_payload,
            result_payload=result_payload,
            error_payload=error_payload,
            metadata=execution_metadata,
        )

        try:
            response = self.client.record_execution(
                payload,
                idempotency_key=execution_ref,
            )
            self.state.last_execution_response = response
            self.state.last_execution_error = None
            return response
        except Exception as exc:
            self.state.last_execution_error = exc
            if not self.fail_open:
                raise
            return None

    def invoke(
        self,
        input: Any,
        config: Optional[RunnableConfig] = None,
        **kwargs: Any,
    ) -> Any:
        external_execution_ref = uuid.uuid4().hex
        try:
            self._check_circuit_breaker()
        except StackmintCircuitBreakerError as exc:
            self._safe_record_execution(
                input,
                error=exc,
                status="blocked",
                metadata=self._policy_block_metadata(exc),
                external_execution_ref=external_execution_ref,
            )
            raise
        self._refresh_policy_for_execution(
            input,
            external_execution_ref=external_execution_ref,
        )
        authorization_response = self._authorize_execution_remote(
            input,
            external_execution_ref=external_execution_ref,
        )
        budget_reservation = self._budget_reservation_from_authorization(
            authorization_response
        )
        if budget_reservation is None:
            budget_reservation = self._reserve_budget_remote(
                input,
                external_execution_ref=external_execution_ref,
            )
        governed_config = self._config_with_stackmint_callbacks(config)
        context_token = _CURRENT_EXECUTION_REF.set(external_execution_ref)

        try:
            result = self.agent.invoke(input, config=governed_config, **kwargs)
        except _POLICY_EXCEPTIONS as exc:
            _CURRENT_EXECUTION_REF.reset(context_token)
            self._commit_budget_remote(
                input,
                error=exc,
                status="blocked",
                reservation=budget_reservation,
                external_execution_ref=external_execution_ref,
            )
            self._safe_record_execution(
                input,
                error=exc,
                status="blocked",
                metadata=self._policy_block_metadata(exc),
                external_execution_ref=external_execution_ref,
            )
            raise
        except Exception as exc:
            _CURRENT_EXECUTION_REF.reset(context_token)
            self._record_underlying_failure()
            self._commit_budget_remote(
                input,
                error=exc,
                status="failed",
                reservation=budget_reservation,
                external_execution_ref=external_execution_ref,
            )
            self.record_execution(
                input,
                error=exc,
                external_execution_ref=external_execution_ref,
            )
            raise
        _CURRENT_EXECUTION_REF.reset(context_token)
        self._reset_circuit_breaker()

        self._track_budget_usage(input, result)
        self.record_execution(
            input,
            output=result,
            external_execution_ref=external_execution_ref,
        )
        self._commit_budget_remote(
            input,
            output=result,
            status="completed",
            reservation=budget_reservation,
            external_execution_ref=external_execution_ref,
        )
        return result

    async def ainvoke(
        self,
        input: Any,
        config: Optional[RunnableConfig] = None,
        **kwargs: Any,
    ) -> Any:
        external_execution_ref = uuid.uuid4().hex
        try:
            self._check_circuit_breaker()
        except StackmintCircuitBreakerError as exc:
            await asyncio.to_thread(
                self._safe_record_execution,
                input,
                error=exc,
                status="blocked",
                metadata=self._policy_block_metadata(exc),
                external_execution_ref=external_execution_ref,
            )
            raise
        await asyncio.to_thread(
            self._refresh_policy_for_execution,
            input,
            external_execution_ref=external_execution_ref,
        )
        authorization_response = await asyncio.to_thread(
            self._authorize_execution_remote,
            input,
            external_execution_ref=external_execution_ref,
        )
        budget_reservation = self._budget_reservation_from_authorization(
            authorization_response
        )
        if budget_reservation is None:
            budget_reservation = await asyncio.to_thread(
                self._reserve_budget_remote,
                input,
                external_execution_ref=external_execution_ref,
            )
        governed_config = self._config_with_stackmint_callbacks(config)
        context_token = _CURRENT_EXECUTION_REF.set(external_execution_ref)

        try:
            result = await self.agent.ainvoke(input, config=governed_config, **kwargs)
        except _POLICY_EXCEPTIONS as exc:
            _CURRENT_EXECUTION_REF.reset(context_token)
            await asyncio.to_thread(
                self._commit_budget_remote,
                input,
                error=exc,
                status="blocked",
                reservation=budget_reservation,
                external_execution_ref=external_execution_ref,
            )
            await asyncio.to_thread(
                self._safe_record_execution,
                input,
                error=exc,
                status="blocked",
                metadata=self._policy_block_metadata(exc),
                external_execution_ref=external_execution_ref,
            )
            raise
        except Exception as exc:
            _CURRENT_EXECUTION_REF.reset(context_token)
            self._record_underlying_failure()
            await asyncio.to_thread(
                self._commit_budget_remote,
                input,
                error=exc,
                status="failed",
                reservation=budget_reservation,
                external_execution_ref=external_execution_ref,
            )
            await asyncio.to_thread(
                self.record_execution,
                input,
                error=exc,
                external_execution_ref=external_execution_ref,
            )
            raise
        _CURRENT_EXECUTION_REF.reset(context_token)
        self._reset_circuit_breaker()

        self._track_budget_usage(input, result)
        await asyncio.to_thread(
            self.record_execution,
            input,
            output=result,
            external_execution_ref=external_execution_ref,
        )
        await asyncio.to_thread(
            self._commit_budget_remote,
            input,
            output=result,
            status="completed",
            reservation=budget_reservation,
            external_execution_ref=external_execution_ref,
        )
        return result

    def __getattr__(self, item: str) -> Any:
        return getattr(self.agent, item)
