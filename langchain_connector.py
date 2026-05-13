from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
import inspect
from typing import Any, Optional, Sequence

from langchain_core.runnables import Runnable
from langchain_core.runnables.config import RunnableConfig

from core import (
    BASE_URL,
    CoreStackmintGateway,
    AgentStatus,
    GatewayExternalConfigPatchRequest,
    GatewayExternalExecutionCreateRequest,
    GatewayExternalSyncRequest,
    GatewayExternalToolSyncItem,
)


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
        return str(tool.get("external_tool_ref") or tool.get("name") or tool.get("id") or "tool")
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
    metadata: dict[str, Any] = {}
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


@dataclass
class GovernedAgentState:
    last_me_response: Any = None
    last_sync_response: Any = None
    last_execution_response: Any = None
    last_sync_error: Exception | None = None
    last_execution_error: Exception | None = None


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
        budget_ceiling_cents: int | None = None,
        tools: Sequence[Any] | None = None,
        sync_on_init: bool = False,
        sync_on_invoke: bool = True,
        fail_open: bool = True,
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
        self.budget_ceiling_cents = budget_ceiling_cents
        self.tools = [_normalize_tool(tool) for tool in (tools or [])]
        self.sync_on_invoke = sync_on_invoke
        self.fail_open = fail_open
        self.state = GovernedAgentState()

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
            budget_ceiling_cents=self.budget_ceiling_cents,
        )

    def _sync_request(self) -> GatewayExternalSyncRequest:
        config = self._config_payload()
        return GatewayExternalSyncRequest(
            config=config if config.model_dump(exclude_none=True) else None,
            tools=self.tools or None,
        )

    def get_me(self) -> Any | None:
        if self.client is None:
            return None
        try:
            response = self.client.get_me()
            self.state.last_me_response = response
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
            response = self.client.sync_agent(self._sync_request())
            self.state.last_sync_response = response
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
    ) -> Any | None:
        if self.client is None:
            return None

        payload = GatewayExternalExecutionCreateRequest(
            external_execution_ref=uuid.uuid4().hex,
            status="failed" if error else "completed",
            created_at=datetime.now(UTC),
            input_payload={"input": _payload(input)},
            result_payload=None if error else {"output": _payload(output)},
            error_payload=(
                None
                if error is None
                else {"type": error.__class__.__name__, "message": str(error)}
            ),
            metadata={"source": "langchain"},
        )

        try:
            response = self.client.record_execution(payload)
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
        if self.sync_on_invoke:
            self.sync_agent()

        try:
            result = self.agent.invoke(input, config=config, **kwargs)
        except Exception as exc:
            self.record_execution(input, error=exc)
            raise

        self.record_execution(input, output=result)
        return result

    async def ainvoke(
        self,
        input: Any,
        config: Optional[RunnableConfig] = None,
        **kwargs: Any,
    ) -> Any:
        if self.sync_on_invoke:
            self.sync_agent()

        try:
            result = await self.agent.ainvoke(input, config=config, **kwargs)
        except Exception as exc:
            self.record_execution(input, error=exc)
            raise

        self.record_execution(input, output=result)
        return result

    def __getattr__(self, item: str) -> Any:
        return getattr(self.agent, item)
