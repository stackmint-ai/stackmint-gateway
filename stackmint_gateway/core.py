from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal

import requests
from pydantic import BaseModel, Field

from stackmint_gateway.security import to_json_safe

BASE_URL = os.getenv("STACKMINT_GATEWAY_BASE_URL", "http://127.0.0.1:5173/api")
DEFAULT_TIMEOUT = 30.0

AgentStatus = Literal["active", "blocked", "suspended"]
ExecutionStatus = Literal[
    "queued",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "blocked",
    "canceled",
]
FrameworkName = Literal["langchain", "crewai", "claude", "openai", "autogen", "custom"]
AuthorizationDecision = Literal[
    "allow",
    "block",
    "waiting_approval",
    "budget_exceeded",
]
BudgetReservationStatus = Literal[
    "reserved",
    "rejected",
    "not_required",
]
BudgetCommitStatus = Literal[
    "committed",
    "released",
    "failed",
]
ApprovalStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "expired",
    "canceled",
]
ToolEventStatus = Literal[
    "allowed",
    "blocked",
    "waiting_approval",
    "approved",
    "rejected",
    "failed",
]


@dataclass
class StackmintRetryConfig:
    max_attempts: int = 3
    initial_backoff_seconds: float = 0.25
    max_backoff_seconds: float = 2.0
    retry_statuses: set[int] = field(
        default_factory=lambda: {429, 500, 502, 503, 504}
    )


class StackmintGatewayError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        path: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.request_id = request_id
        details = []
        if method and path:
            details.append(f"{method.upper()} {path}")
        if status_code is not None:
            details.append(f"status={status_code}")
        if request_id:
            details.append(f"request_id={request_id}")
        suffix = f" ({', '.join(details)})" if details else ""
        super().__init__(f"{message}{suffix}")


class StackmintGatewayAuthError(StackmintGatewayError):
    pass


class StackmintGatewayRateLimitError(StackmintGatewayError):
    pass


class StackmintGatewayServerError(StackmintGatewayError):
    pass


class StackmintGatewayTimeoutError(StackmintGatewayError):
    pass


class StackmintGatewayConnectionError(StackmintGatewayError):
    pass


class StackmintGatewayResponseError(StackmintGatewayError):
    pass


def build_header(api_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Gateway-Api-Key": api_key,
    }


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def _json_safe(value: Any) -> Any:
    return to_json_safe(value)


def _request_id(response: requests.Response) -> str | None:
    for header_name in ("X-Request-Id", "X-Stackmint-Request-Id", "Request-Id"):
        value = response.headers.get(header_name)
        if value:
            return value
    return None


def _gateway_error_for_response(
    response: requests.Response,
    *,
    method: str,
    path: str,
) -> StackmintGatewayError:
    status_code = response.status_code
    kwargs = {
        "method": method,
        "path": path,
        "status_code": status_code,
        "request_id": _request_id(response),
    }
    if status_code in (401, 403):
        return StackmintGatewayAuthError(
            "Stackmint gateway authentication failed",
            **kwargs,
        )
    if status_code == 429:
        return StackmintGatewayRateLimitError(
            "Stackmint gateway rate limit exceeded",
            **kwargs,
        )
    if 500 <= status_code <= 599:
        return StackmintGatewayServerError("Stackmint gateway server error", **kwargs)
    return StackmintGatewayError("Stackmint gateway request failed", **kwargs)


class GatewayExternalAuthHeaders(BaseModel):
    x_gateway_api_key: str = Field(
        min_length=1,
        description="Raw Gateway API key passed in the X-Gateway-Api-Key header.",
    )


class GatewayExternalMeResponse(BaseModel):
    gateway_agent_id: str
    workspace_id: str
    key_prefix: str | None = None
    key_version: int = 0
    name: str
    description: str | None = None
    framework: FrameworkName
    model: str | None = None
    status: AgentStatus
    external_agent_ref: str | None = None
    permitted_tool_slugs: list[str] = Field(default_factory=list)
    budget_ceiling_cents: int | None = None
    hitl_conditions: list[dict[str, Any]] = Field(default_factory=list)
    require_approval_for: list[str] = Field(default_factory=list)
    tool_count: int = 0
    execution_count: int = 0
    latest_execution_status: ExecutionStatus | None = None
    latest_execution_at: datetime | None = None


class GatewayExternalConfigPatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    framework: FrameworkName | None = None
    model: str | None = None
    status: AgentStatus | None = None
    external_agent_ref: str | None = Field(default=None, max_length=160)
    permitted_tool_slugs: list[str] | None = None
    budget_ceiling_cents: int | None = None
    hitl_conditions: list[dict[str, Any]] | None = None
    require_approval_for: list[str] | None = None


class GatewayExternalToolSyncItem(BaseModel):
    external_tool_ref: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=120)
    status: AgentStatus = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExternalToolsSyncRequest(BaseModel):
    tools: list[GatewayExternalToolSyncItem] = Field(default_factory=list)


class GatewayExternalToolsSyncResponse(BaseModel):
    gateway_agent_id: str
    workspace_id: str
    synced_count: int
    tool_refs: list[str] = Field(default_factory=list)


class GatewayExternalExecutionCreateRequest(BaseModel):
    external_execution_ref: str | None = Field(default=None, max_length=160)
    external_tool_ref: str | None = Field(default=None, max_length=160)
    status: ExecutionStatus
    created_at: datetime | None = None
    input_payload: dict[str, Any] | None = None
    result_payload: dict[str, Any] | None = None
    error_payload: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExternalExecutionCreateResponse(BaseModel):
    execution_id: str
    gateway_agent_id: str
    workspace_id: str
    gateway_tool_id: str | None = None
    external_execution_ref: str | None = None
    external_tool_ref: str | None = None
    status: ExecutionStatus
    created_at: datetime | None = None


class GatewayExternalAuthorizeRequest(BaseModel):
    external_execution_ref: str | None = Field(default=None, max_length=160)
    input_payload: dict[str, Any] | None = None
    estimated_input_tokens: int | None = None
    estimated_input_cost_cents: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExternalAuthorizeResponse(BaseModel):
    decision: AuthorizationDecision
    gateway_agent_id: str
    workspace_id: str
    reason: str | None = None
    message: str | None = None
    approval_request_id: str | None = None
    budget_reservation_id: str | None = None
    budget_ceiling_cents: int | None = None
    remaining_budget_cents: float | None = None
    policy_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExternalBudgetReserveRequest(BaseModel):
    external_execution_ref: str = Field(min_length=1, max_length=160)
    estimated_tokens: int | None = None
    estimated_cost_cents: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExternalBudgetReserveResponse(BaseModel):
    status: BudgetReservationStatus
    budget_reservation_id: str | None = None
    reason: str | None = None
    approved_cost_cents: float | None = None
    remaining_budget_cents: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExternalBudgetCommitRequest(BaseModel):
    external_execution_ref: str = Field(min_length=1, max_length=160)
    budget_reservation_id: str | None = None
    actual_tokens: int | None = None
    actual_cost_cents: float | None = None
    status: ExecutionStatus
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExternalBudgetCommitResponse(BaseModel):
    status: BudgetCommitStatus
    budget_reservation_id: str | None = None
    committed_cost_cents: float | None = None
    remaining_budget_cents: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExternalToolEventCreateRequest(BaseModel):
    external_execution_ref: str | None = Field(default=None, max_length=160)
    external_tool_ref: str = Field(min_length=1, max_length=160)
    status: ToolEventStatus
    created_at: datetime | None = None
    input_payload: dict[str, Any] | None = None
    result_payload: dict[str, Any] | None = None
    error_payload: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExternalToolEventCreateResponse(BaseModel):
    tool_event_id: str
    gateway_agent_id: str
    workspace_id: str
    gateway_tool_id: str | None = None
    external_execution_ref: str | None = None
    external_tool_ref: str
    status: ToolEventStatus
    created_at: datetime | None = None


class GatewayExternalApprovalCreateRequest(BaseModel):
    external_execution_ref: str | None = Field(default=None, max_length=160)
    external_tool_ref: str | None = Field(default=None, max_length=160)
    reason: str | None = None
    input_payload: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExternalApprovalCreateResponse(BaseModel):
    approval_request_id: str
    gateway_agent_id: str
    workspace_id: str
    status: ApprovalStatus
    message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExternalApprovalDecisionResponse(BaseModel):
    approval_request_id: str
    status: ApprovalStatus
    approved_by: str | None = None
    decided_at: datetime | None = None
    message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExternalSyncRequest(BaseModel):
    config: GatewayExternalConfigPatchRequest | None = None
    tools: list[GatewayExternalToolSyncItem] | None = None
    executions: list[GatewayExternalExecutionCreateRequest] | None = None


class GatewayExternalSyncResponse(BaseModel):
    agent: GatewayExternalMeResponse
    config_updated: bool = False
    tools_synced: int = 0
    tool_refs: list[str] = Field(default_factory=list)
    executions_created: int = 0
    execution_ids: list[str] = Field(default_factory=list)


class CoreStackmintGateway:
    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retry_config: StackmintRetryConfig | None = None,
        request_func: Callable[..., requests.Response] | None = None,
        sleep_func: Callable[[float], None] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retry_config = retry_config or StackmintRetryConfig()
        self._request_func = request_func or requests.request
        self._sleep_func = sleep_func or time.sleep
        self.current_session_cost = 0.0
        self.consecutive_failures = 0
        self.breaker_open = False
        self.breaker_cooldown_until = None

    def _sleep_before_retry(self, attempt_index: int) -> None:
        backoff = min(
            self.retry_config.initial_backoff_seconds
            * (2 ** max(attempt_index - 1, 0)),
            self.retry_config.max_backoff_seconds,
        )
        if backoff <= 0:
            return
        jitter = random.uniform(0, backoff * 0.1)  # nosec B311
        self._sleep_func(backoff + jitter)

    def _is_retry_allowed(
        self,
        method: str,
        *,
        extra_headers: dict[str, str] | None = None,
        retry_safe: bool | None = None,
    ) -> bool:
        if retry_safe is not None:
            return retry_safe

        normalized_method = method.upper()
        if normalized_method in {"GET", "PUT", "DELETE"}:
            return True
        if normalized_method == "POST":
            headers = extra_headers or {}
            return any(key.lower() == "idempotency-key" for key in headers)
        return False

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        retry_safe: bool | None = None,
    ) -> Any:
        headers = build_header(self.api_key)
        headers.update(extra_headers or {})
        attempts = max(self.retry_config.max_attempts, 1)
        retry_allowed = self._is_retry_allowed(
            method,
            extra_headers=extra_headers,
            retry_safe=retry_safe,
        )
        response: requests.Response | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = self._request_func(
                    method=method,
                    url=f"{self.base_url}/{path.lstrip('/')}",
                    headers=headers,
                    json=_json_safe(json),
                    params=_drop_none(params or {}),
                    timeout=self.timeout,
                )
            except requests.Timeout as exc:
                if retry_allowed and attempt < attempts:
                    self._sleep_before_retry(attempt)
                    continue
                raise StackmintGatewayTimeoutError(
                    "Stackmint gateway request timed out",
                    method=method,
                    path=path,
                ) from exc
            except requests.ConnectionError as exc:
                if retry_allowed and attempt < attempts:
                    self._sleep_before_retry(attempt)
                    continue
                raise StackmintGatewayConnectionError(
                    "Stackmint gateway connection failed",
                    method=method,
                    path=path,
                ) from exc

            if (
                retry_allowed
                and response.status_code in self.retry_config.retry_statuses
                and attempt < attempts
            ):
                self._sleep_before_retry(attempt)
                continue
            break

        if response is None:
            raise StackmintGatewayConnectionError(
                "Stackmint gateway request was not sent",
                method=method,
                path=path,
            )
        if response.status_code >= 400:
            raise _gateway_error_for_response(response, method=method, path=path)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise StackmintGatewayResponseError(
                "Stackmint gateway returned invalid JSON",
                method=method,
                path=path,
                status_code=response.status_code,
                request_id=_request_id(response),
            ) from exc

    def get_me(self) -> GatewayExternalMeResponse:
        return GatewayExternalMeResponse.model_validate(
            self._request("GET", "gateway/external/me")
        )

    def patch_config(
        self,
        payload: GatewayExternalConfigPatchRequest,
    ) -> GatewayExternalMeResponse:
        return GatewayExternalMeResponse.model_validate(
            self._request("PATCH", "gateway/external/me/config", json=payload)
        )

    def sync_tools(
        self,
        payload: GatewayExternalToolsSyncRequest,
    ) -> GatewayExternalToolsSyncResponse:
        return GatewayExternalToolsSyncResponse.model_validate(
            self._request("PUT", "gateway/external/me/tools", json=payload)
        )

    def record_execution(
        self,
        payload: GatewayExternalExecutionCreateRequest,
        *,
        idempotency_key: str | None = None,
    ) -> GatewayExternalExecutionCreateResponse:
        extra_headers = (
            {"Idempotency-Key": idempotency_key}
            if idempotency_key is not None
            else None
        )
        return GatewayExternalExecutionCreateResponse.model_validate(
            self._request(
                "POST",
                "gateway/external/me/executions",
                json=payload,
                extra_headers=extra_headers,
            )
        )

    def authorize_execution(
        self,
        payload: GatewayExternalAuthorizeRequest,
        *,
        idempotency_key: str | None = None,
    ) -> GatewayExternalAuthorizeResponse:
        extra_headers = (
            {"Idempotency-Key": idempotency_key}
            if idempotency_key is not None
            else None
        )
        return GatewayExternalAuthorizeResponse.model_validate(
            self._request(
                "POST",
                "gateway/external/me/authorize",
                json=payload,
                extra_headers=extra_headers,
            )
        )

    def reserve_budget(
        self,
        payload: GatewayExternalBudgetReserveRequest,
        *,
        idempotency_key: str | None = None,
    ) -> GatewayExternalBudgetReserveResponse:
        extra_headers = (
            {"Idempotency-Key": idempotency_key}
            if idempotency_key is not None
            else None
        )
        return GatewayExternalBudgetReserveResponse.model_validate(
            self._request(
                "POST",
                "gateway/external/me/budget/reserve",
                json=payload,
                extra_headers=extra_headers,
            )
        )

    def commit_budget(
        self,
        payload: GatewayExternalBudgetCommitRequest,
        *,
        idempotency_key: str | None = None,
    ) -> GatewayExternalBudgetCommitResponse:
        extra_headers = (
            {"Idempotency-Key": idempotency_key}
            if idempotency_key is not None
            else None
        )
        return GatewayExternalBudgetCommitResponse.model_validate(
            self._request(
                "POST",
                "gateway/external/me/budget/commit",
                json=payload,
                extra_headers=extra_headers,
            )
        )

    def record_tool_event(
        self,
        payload: GatewayExternalToolEventCreateRequest,
        *,
        idempotency_key: str | None = None,
    ) -> GatewayExternalToolEventCreateResponse:
        extra_headers = (
            {"Idempotency-Key": idempotency_key}
            if idempotency_key is not None
            else None
        )
        return GatewayExternalToolEventCreateResponse.model_validate(
            self._request(
                "POST",
                "gateway/external/me/tool-events",
                json=payload,
                extra_headers=extra_headers,
            )
        )

    def create_approval_request(
        self,
        payload: GatewayExternalApprovalCreateRequest,
        *,
        idempotency_key: str | None = None,
    ) -> GatewayExternalApprovalCreateResponse:
        extra_headers = (
            {"Idempotency-Key": idempotency_key}
            if idempotency_key is not None
            else None
        )
        return GatewayExternalApprovalCreateResponse.model_validate(
            self._request(
                "POST",
                "gateway/external/me/approvals",
                json=payload,
                extra_headers=extra_headers,
            )
        )

    def get_approval_decision(
        self,
        approval_request_id: str,
    ) -> GatewayExternalApprovalDecisionResponse:
        return GatewayExternalApprovalDecisionResponse.model_validate(
            self._request(
                "GET",
                f"gateway/external/me/approvals/{approval_request_id}",
            )
        )

    def sync_agent(
        self,
        payload: GatewayExternalSyncRequest,
    ) -> GatewayExternalSyncResponse:
        return GatewayExternalSyncResponse.model_validate(
            self._request("POST", "gateway/external/me/sync", json=payload)
        )
