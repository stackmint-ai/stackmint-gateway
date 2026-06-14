from __future__ import annotations

import importlib.util
import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping

DEFAULT_BASE_URL = "http://127.0.0.1:5173/api"


@dataclass
class DetectionResult:
    name: str
    category: str
    installed: bool
    import_name: str | None = None
    package_hint: str | None = None
    env_var: str | None = None
    env_present: bool | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None
        }


@dataclass
class DoctorReport:
    detections: list[DetectionResult]
    config: dict[str, Any]
    readiness: dict[str, str]


def module_available(import_name: str) -> bool:
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def build_doctor_report(
    environ: Mapping[str, str] | None = None,
) -> DoctorReport:
    env = environ if environ is not None else os.environ
    detections = [*_framework_detections(), *_provider_detections(env)]
    config = stackmint_config(env)
    readiness = readiness_summary(detections, config)
    return DoctorReport(
        detections=detections,
        config=config,
        readiness=readiness,
    )


def stackmint_config(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    return {
        "gateway_api_key_present": bool(env.get("STACKMINT_GATEWAY_API_KEY")),
        "gateway_base_url": env.get("STACKMINT_GATEWAY_BASE_URL", DEFAULT_BASE_URL),
        "example_provider": env.get("STACKMINT_EXAMPLE_PROVIDER", "fake"),
        "example_model": env.get("STACKMINT_EXAMPLE_MODEL") or None,
        "mcp_read_only": env.get("STACKMINT_MCP_READ_ONLY", "false"),
        "mcp_require_confirmation": env.get(
            "STACKMINT_MCP_REQUIRE_CONFIRMATION",
            "true",
        ),
        "mcp_record_payloads": env.get("STACKMINT_MCP_RECORD_PAYLOADS", "true"),
    }


def readiness_summary(
    detections: list[DetectionResult],
    config: dict[str, Any],
) -> dict[str, str]:
    by_name = {detection.name: detection for detection in detections}
    mcp = by_name["MCP SDK"]
    openai = by_name["OpenAI provider package"]
    anthropic = by_name["Anthropic provider package"]
    cerebras = by_name["Cerebras provider package"]
    provider_detections = [openai, anthropic, cerebras]

    return {
        "local_demo": "ready",
        "fake_langchain_example": "ready",
        "provider_backed_langchain_examples": _provider_backed_examples_readiness(
            provider_detections
        ),
        "mcp_governance_server": (
            "ready" if mcp.installed else "install `uv sync --extra mcp`"
        ),
        "openai_example": _provider_readiness(openai),
        "anthropic_example": _provider_readiness(anthropic),
        "cerebras_example": _provider_readiness(cerebras),
        "local_guardrails": "available",
        "safe_telemetry": "available",
        "remote_policy_sync": (
            "ready"
            if config["gateway_api_key_present"]
            else "requires STACKMINT_GATEWAY_API_KEY"
        ),
        "managed_control_plane": "requires compatible Stackmint backend",
    }


def _framework_detections() -> list[DetectionResult]:
    openai_agents_installed = module_available("agents") or module_available(
        "openai_agents"
    )
    return [
        DetectionResult(
            name="LangChain Core",
            category="framework",
            installed=module_available("langchain_core"),
            import_name="langchain_core",
            package_hint="core dependency",
        ),
        DetectionResult(
            name="LangChain",
            category="framework",
            installed=module_available("langchain"),
            import_name="langchain",
            package_hint="uv sync --extra examples or pip install langchain",
        ),
        DetectionResult(
            name="OpenAI Agents SDK",
            category="framework",
            installed=openai_agents_installed,
            import_name="agents/openai_agents",
            package_hint="planned connector / install separately",
            note="Import name can vary; this is a conservative local check.",
        ),
        DetectionResult(
            name="CrewAI",
            category="framework",
            installed=module_available("crewai"),
            import_name="crewai",
            package_hint="install separately",
        ),
        DetectionResult(
            name="AutoGen",
            category="framework",
            installed=module_available("autogen"),
            import_name="autogen",
            package_hint="install separately",
        ),
        DetectionResult(
            name="MCP SDK",
            category="framework",
            installed=module_available("mcp"),
            import_name="mcp",
            package_hint="uv sync --extra mcp",
        ),
    ]


def _provider_detections(env: Mapping[str, str]) -> list[DetectionResult]:
    return [
        DetectionResult(
            name="OpenAI provider package",
            category="provider",
            installed=module_available("langchain_openai"),
            import_name="langchain_openai",
            package_hint="uv sync --extra examples-openai",
            env_var="OPENAI_API_KEY",
            env_present=bool(env.get("OPENAI_API_KEY")),
        ),
        DetectionResult(
            name="Anthropic provider package",
            category="provider",
            installed=module_available("langchain_anthropic"),
            import_name="langchain_anthropic",
            package_hint="uv sync --extra examples-anthropic",
            env_var="ANTHROPIC_API_KEY",
            env_present=bool(env.get("ANTHROPIC_API_KEY")),
        ),
        DetectionResult(
            name="Cerebras provider package",
            category="provider",
            installed=module_available("langchain_cerebras"),
            import_name="langchain_cerebras",
            package_hint="uv sync --extra examples-cerebras",
            env_var="CEREBRAS_API_KEY",
            env_present=bool(env.get("CEREBRAS_API_KEY")),
        ),
    ]


def _provider_readiness(detection: DetectionResult) -> str:
    if detection.installed and detection.env_present:
        return "ready"
    if detection.installed:
        return f"set {detection.env_var}"
    if detection.env_present:
        return f"install `{detection.package_hint}`"
    return f"install package + set {detection.env_var}"


def _provider_backed_examples_readiness(
    detections: list[DetectionResult],
) -> str:
    if all(detection.installed and detection.env_present for detection in detections):
        return "ready"
    if any(detection.installed or detection.env_present for detection in detections):
        return "partially_configured"
    return "optional_dependencies_missing"
