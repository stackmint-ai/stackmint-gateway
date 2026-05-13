from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Literal

import requests
from pydantic import BaseModel, Field

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


def build_header(api_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Gateway-Api-Key": api_key,
    }


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


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
    framework: Literal["langchain", "crewai", "claude", "openai", "autogen", "custom"]
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
    framework: Literal["langchain", "crewai", "claude", "openai", "autogen", "custom"] | None = None
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
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        response = requests.request(
            method=method,
            url=f"{self.base_url}/{path.lstrip('/')}",
            headers=build_header(self.api_key),
            json=_json_safe(json),
            params=_drop_none(params or {}),
            timeout=self.timeout,
        )
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

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
    ) -> GatewayExternalExecutionCreateResponse:
        return GatewayExternalExecutionCreateResponse.model_validate(
            self._request("POST", "gateway/external/me/executions", json=payload)
        )

    def sync_agent(
        self,
        payload: GatewayExternalSyncRequest,
    ) -> GatewayExternalSyncResponse:
        return GatewayExternalSyncResponse.model_validate(
            self._request("POST", "gateway/external/me/sync", json=payload)
        )
