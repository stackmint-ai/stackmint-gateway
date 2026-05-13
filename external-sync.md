# Gateway External Sync Contract

This document defines the Gateway registry API and the external sync contract used by a source system that owns an agent, pushes executions, and updates agent configuration through a Gateway API key.

## 1. Auth Contract

All external Gateway routes use the same API key header:

- `X-Gateway-Api-Key: <raw_key>`

Rules:

- The raw key is shown once at creation/rotation time only.
- The database stores `key_hash` and `encrypted_key`, never the raw key in clear text.
- The external API resolves the key to one authenticated agent and one workspace.
- External routes are key-scoped and use `/me` instead of path-scoped `agent_id`.
- Internal UI routes keep using workspace-auth and do not accept the Gateway key.

## 2. Current Internal Routes

These routes already exist in the repo and are used by the Gateway UI.

| Method | Path | Request BaseModel | Response BaseModel | Tables touched |
|---|---|---|---|---|
| `GET` | `/api/gateway/posture` | - | `GatewayPostureResponse` | `gateway_agents`, `gateway_keys`, `gateway_tools`, `gateway_executions` |
| `GET` | `/api/gateway/agents` | - | `list[GatewayAgentReadResponse]` | `gateway_agents`, `gateway_keys`, `gateway_tools`, `gateway_executions` |
| `POST` | `/api/gateway/agents` | `GatewayAgentCreatePayload` | `GatewayAgentCreateResponse` | `gateway_agents`, `gateway_keys` |
| `GET` | `/api/gateway/agents/{agent_id}` | - | `GatewayAgentReadResponse` | `gateway_agents`, `gateway_keys`, `gateway_tools`, `gateway_executions` |
| `PATCH` | `/api/gateway/agents/{agent_id}` | `GatewayAgentPatchPayload` | `GatewayAgentReadResponse` | `gateway_agents` |
| `DELETE` | `/api/gateway/agents/{agent_id}` | - | - | `gateway_agents`, `gateway_keys` |
| `POST` | `/api/gateway/agents/{agent_id}/rotate-key` | - | `GatewayAgentRotateKeyResponse` | `gateway_keys` |
| `GET` | `/api/gateway/tools` | query: `agent_id?` | `list[GatewayToolReadResponse]` | `gateway_tools`, `gateway_agents` |
| `GET` | `/api/gateway/executions` | query: `agent_id?`, `status?`, `tool_id?`, `limit?` | `list[GatewayExecutionReadResponse]` | `gateway_executions`, `gateway_agents`, `gateway_tools` |
| `GET` | `/api/gateway/executions/{execution_id}` | - | `GatewayExecutionReadResponse` | `gateway_executions`, `gateway_agents`, `gateway_tools` |

### Internal model notes

- `GatewayAgentCreatePayload` currently allows `model`, but the UI creation form only sends `name`, `framework`, and `description`.
- `GatewayAgentPatchPayload` is the internal editable agent payload.
- `GatewayAgentReadResponse`, `GatewayToolReadResponse`, and `GatewayExecutionReadResponse` are the canonical read shapes for the registry.

## 3. Proposed External Routes

These routes are the contract for the external source system. They should live under the same Gateway namespace, but use key-based auth instead of workspace-auth.

| Method | Path | Request BaseModel | Response BaseModel | Tables touched |
|---|---|---|---|---|
| `GET` | `/api/gateway/external/me` | `GatewayExternalAuthHeaders` via header | `GatewayExternalMeResponse` | `gateway_agents`, `gateway_keys`, `gateway_tools`, `gateway_executions` |
| `PATCH` | `/api/gateway/external/me/config` | `GatewayExternalConfigPatchRequest` + `GatewayExternalAuthHeaders` | `GatewayExternalMeResponse` | `gateway_agents` |
| `PUT` | `/api/gateway/external/me/tools` | `GatewayExternalToolsSyncRequest` + `GatewayExternalAuthHeaders` | `GatewayExternalToolsSyncResponse` | `gateway_tools` |
| `POST` | `/api/gateway/external/me/executions` | `GatewayExternalExecutionCreateRequest` + `GatewayExternalAuthHeaders` | `GatewayExternalExecutionCreateResponse` | `gateway_executions` |
| `POST` | `/api/gateway/external/me/sync` | `GatewayExternalSyncRequest` + `GatewayExternalAuthHeaders` | `GatewayExternalSyncResponse` | `gateway_agents`, `gateway_tools`, `gateway_executions` |

### External behavior notes

- The API key is the only identifier the external system must send.
- `GET /api/gateway/external/me` returns the authenticated agent context, including the internal `gateway_agent_id` as read-only metadata.
- `model` is part of the external config contract, even if it is not shown in the creation form anymore.
- `tools` are synced from the source system using external references, then mapped to `gateway_tools`.
- `executions` are append-only events coming from the source system and are stored in `gateway_executions`.
- `sync` is a convenience endpoint for pushing config, tools, and executions in one request.
- LEGACY: the retired path-scoped external routes that accepted `agent_id` were removed to avoid exposing internal IDs to the caller.

## 4. Pydantic Data Models

The following models are the canonical request/response shapes for the Gateway contract.

### Shared auth model

```python
class GatewayExternalAuthHeaders(BaseModel):
    x_gateway_api_key: str = Field(
        min_length=1,
        description="Raw Gateway API key passed in the X-Gateway-Api-Key header.",
    )
```

### Internal registry models

```python
class GatewayAgentCreatePayload(BaseModel):
    name: str
    description: Optional[str] = None
    framework: Literal["langchain", "crewai", "claude", "openai", "autogen", "custom"]
    model: Optional[str] = None
    status: Literal["active", "blocked", "suspended"] = "active"
    external_source: Optional[str] = None
    external_agent_ref: Optional[str] = None
    permitted_tool_slugs: Optional[list[str]] = None
    budget_ceiling_cents: Optional[int] = None
    hitl_conditions: list[dict[str, Any]] = Field(default_factory=list)
    require_approval_for: Optional[list[str]] = None


class GatewayAgentPatchPayload(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    framework: Optional[Literal["langchain", "crewai", "claude", "openai", "autogen", "custom"]] = None
    model: Optional[str] = None
    status: Optional[Literal["active", "blocked", "suspended"]] = None
    external_source: Optional[str] = None
    external_agent_ref: Optional[str] = None
    permitted_tool_slugs: Optional[list[str]] = None
    budget_ceiling_cents: Optional[int] = None
    hitl_conditions: Optional[list[dict[str, Any]]] = None
    require_approval_for: Optional[list[str]] = None


class GatewayPostureResponse(BaseModel):
    workspace_id: str
    agents_count: int
    suspended_agents_count: int
    tools_count: int
    blocked_last_hour: int
    hitl_pending: int
    active_execution_count: int


class GatewayAgentReadResponse(BaseModel):
    id: str
    workspace_id: str
    name: str
    description: Optional[str] = None
    framework: str
    model: Optional[str] = None
    status: Literal["active", "blocked", "suspended"]
    key_prefix: Optional[str] = None
    tool_count: int = 0
    execution_count: int = 0
    latest_execution_status: Optional[str] = None
    latest_execution_at: Optional[str] = None
    permitted_tool_slugs: list[str] = Field(default_factory=list)
    budget_ceiling_cents: Optional[int] = None


class GatewayAgentCreateResponse(BaseModel):
    agent: GatewayAgentReadResponse
    key: str
    key_prefix: str


class GatewayAgentRotateKeyResponse(BaseModel):
    key: str
    key_prefix: str


class GatewayToolReadResponse(BaseModel):
    id: str
    workspace_id: str
    gateway_agent_id: str
    agent_name: Optional[str] = None
    name: str
    status: Literal["active", "blocked", "suspended"]
    external_tool_ref: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExecutionReadResponse(BaseModel):
    id: str
    workspace_id: str
    gateway_agent_id: str
    gateway_tool_id: Optional[str] = None
    agent_name: Optional[str] = None
    tool_name: Optional[str] = None
    status: Literal["queued", "running", "waiting_approval", "completed", "failed", "blocked", "canceled"]
    created_at: Optional[datetime] = None
    input_payload: Optional[dict[str, Any]] = None
    result_payload: Optional[dict[str, Any]] = None
    error_payload: Optional[dict[str, Any]] = None
```

### External sync models

```python
class GatewayExternalMeResponse(BaseModel):
    gateway_agent_id: str
    workspace_id: str
    key_prefix: Optional[str] = None
    key_version: int = 0
    name: str
    description: Optional[str] = None
    framework: Literal["langchain", "crewai", "claude", "openai", "autogen", "custom"]
    model: Optional[str] = None
    status: Literal["active", "blocked", "suspended"]
    external_agent_ref: Optional[str] = None
    permitted_tool_slugs: list[str] = Field(default_factory=list)
    budget_ceiling_cents: Optional[int] = None
    hitl_conditions: list[dict[str, Any]] = Field(default_factory=list)
    require_approval_for: list[str] = Field(default_factory=list)
    tool_count: int = 0
    execution_count: int = 0
    latest_execution_status: Optional[Literal["queued", "running", "waiting_approval", "completed", "failed", "blocked", "canceled"]] = None
    latest_execution_at: Optional[datetime] = None


class GatewayExternalConfigPatchRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = None
    framework: Optional[Literal["langchain", "crewai", "claude", "openai", "autogen", "custom"]] = None
    model: Optional[str] = None
    status: Optional[Literal["active", "blocked", "suspended"]] = None
    external_agent_ref: Optional[str] = Field(default=None, max_length=160)
    permitted_tool_slugs: Optional[list[str]] = None
    budget_ceiling_cents: Optional[int] = None
    hitl_conditions: Optional[list[dict[str, Any]]] = None
    require_approval_for: Optional[list[str]] = None


class GatewayExternalToolSyncItem(BaseModel):
    external_tool_ref: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=120)
    status: Literal["active", "blocked", "suspended"] = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExternalToolsSyncRequest(BaseModel):
    tools: list[GatewayExternalToolSyncItem] = Field(default_factory=list)


class GatewayExternalToolsSyncResponse(BaseModel):
    gateway_agent_id: str
    workspace_id: str
    synced_count: int
    tool_refs: list[str] = Field(default_factory=list)


class GatewayExternalExecutionCreateRequest(BaseModel):
    external_execution_ref: Optional[str] = Field(default=None, max_length=160)
    external_tool_ref: Optional[str] = Field(default=None, max_length=160)
    status: Literal["queued", "running", "waiting_approval", "completed", "failed", "blocked", "canceled"]
    created_at: Optional[datetime] = None
    input_payload: Optional[dict[str, Any]] = None
    result_payload: Optional[dict[str, Any]] = None
    error_payload: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayExternalExecutionCreateResponse(BaseModel):
    execution_id: str
    gateway_agent_id: str
    workspace_id: str
    gateway_tool_id: Optional[str] = None
    external_execution_ref: Optional[str] = None
    external_tool_ref: Optional[str] = None
    status: Literal["queued", "running", "waiting_approval", "completed", "failed", "blocked", "canceled"]
    created_at: Optional[datetime] = None


class GatewayExternalSyncRequest(BaseModel):
    config: Optional[GatewayExternalConfigPatchRequest] = None
    tools: Optional[list[GatewayExternalToolSyncItem]] = None
    executions: Optional[list[GatewayExternalExecutionCreateRequest]] = None


class GatewayExternalSyncResponse(BaseModel):
    agent: GatewayExternalMeResponse
    config_updated: bool = False
    tools_synced: int = 0
    tool_refs: list[str] = Field(default_factory=list)
    executions_created: int = 0
    execution_ids: list[str] = Field(default_factory=list)
```

## 5. Storage Mapping

- `gateway_agents`
  - canonical agent record
  - internal UI + external config sync
- `gateway_keys`
  - encrypted key material, key hash, prefix, version
- `gateway_tools`
  - synced tool registry for an agent
  - stores external sync metadata and `external_tool_ref`
- `gateway_executions`
  - append-only execution feed from the external source
  - stores external sync metadata and `external_tool_ref`

## 6. Implementation Notes

- The external contract should accept `X-Gateway-Api-Key` on every write/read route.
- The internal UI remains on workspace-auth routes and can continue using `/api/gateway/*`.
- The external source should use `PATCH config` for model and policy changes, `PUT tools` for tool replacement, and `POST executions` for new execution events.
- The document intentionally keeps the external contract write-friendly and simple, so the source system can sync without needing to know internal Supabase IDs for tools.
