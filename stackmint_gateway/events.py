from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

GatewayEventKind = Literal[
    "boot",
    "policy",
    "run",
    "check",
    "allow",
    "approval",
    "block",
    "security",
    "record",
    "result",
    "error",
    "info",
]

_TAGS: dict[GatewayEventKind, str] = {
    "boot": "BOOT",
    "policy": "POLICY",
    "run": "RUN",
    "check": "CHECK",
    "allow": "ALLOW",
    "approval": "APPROVAL",
    "block": "BLOCK",
    "security": "SECURITY",
    "record": "RECORD",
    "result": "RESULT",
    "error": "ERROR",
    "info": "INFO",
}
_COLORS: dict[GatewayEventKind, str] = {
    "boot": "\033[36m",
    "policy": "\033[36m",
    "run": "\033[36m",
    "check": "\033[34m",
    "allow": "\033[32m",
    "approval": "\033[33m",
    "block": "\033[31m",
    "security": "\033[35m",
    "record": "\033[2m",
    "result": "\033[1m",
    "error": "\033[91m",
    "info": "\033[2m",
}
_RESET = "\033[0m"


@dataclass
class GatewayEvent:
    kind: GatewayEventKind
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def event(kind: GatewayEventKind, message: str, **details: Any) -> GatewayEvent:
    return GatewayEvent(kind=kind, message=message, details=_json_safe(details))


def event_to_dict(gateway_event: GatewayEvent) -> dict[str, Any]:
    return {
        "kind": gateway_event.kind,
        "tag": _TAGS[gateway_event.kind],
        "message": gateway_event.message,
        "details": _json_safe(gateway_event.details),
    }


def event_to_line(gateway_event: GatewayEvent, *, color: bool = True) -> str:
    tag = _TAGS[gateway_event.kind]
    prefix = f"[{tag}]"
    if color:
        prefix = f"{_COLORS[gateway_event.kind]}{prefix}{_RESET}"
    return f"{prefix} {gateway_event.message}"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_safe(dataclasses.asdict(value))
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump(mode="json", exclude_none=True))
        except Exception:
            return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    return str(value)
