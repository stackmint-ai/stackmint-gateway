from __future__ import annotations

import dataclasses
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

STACKMINT_PAYLOAD_SECURITY_VERSION = "1"


def _default_redact_keys() -> set[str]:
    return {
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "auth",
        "credential",
        "credentials",
        "authorization",
        "bearer",
        "password",
        "secret",
        "client_secret",
        "private_key",
        "session",
        "cookie",
        "set_cookie",
        "x_api_key",
        "x_gateway_api_key",
        "stackmint_gateway_api_key",
        "cerebras_api_key",
        "openai_api_key",
        "anthropic_api_key",
    }


@dataclass
class StackmintTelemetrySecurityConfig:
    redact_payloads: bool = True
    max_payload_bytes: int = 64_000
    max_string_length: int = 8_000
    redacted_value: str = "[REDACTED]"
    truncated_value: str = "[TRUNCATED]"
    redact_keys: set[str] = field(default_factory=_default_redact_keys)
    allow_keys: set[str] | None = None
    redact_emails: bool = True


@dataclass
class SanitizedPayload:
    value: Any
    redacted: bool = False
    truncated: bool = False
    serialization_error: bool = False

    def metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "stackmint_payload_security_version": STACKMINT_PAYLOAD_SECURITY_VERSION
        }
        if self.redacted:
            metadata["stackmint_redacted"] = True
        if self.truncated:
            metadata["stackmint_truncated"] = True
        if self.serialization_error:
            metadata["stackmint_serialization_error"] = True
        return metadata


_BEARER_PATTERN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_OPENAI_KEY_PATTERN = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")
_ANTHROPIC_KEY_PATTERN = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")
_GITHUB_TOKEN_PATTERN = re.compile(
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{30,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"
)
_JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_TOKEN_CANDIDATE_PATTERN = re.compile(r"[A-Za-z0-9_+/=-]{32,}")


def _normalize_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def _is_sensitive_key(key: Any, config: StackmintTelemetrySecurityConfig) -> bool:
    normalized = _normalize_key(key)
    allow_keys = {_normalize_key(item) for item in config.allow_keys or set()}
    if normalized in allow_keys:
        return False
    redact_keys = {_normalize_key(item) for item in config.redact_keys}
    return any(
        normalized == redact_key or redact_key in normalized
        for redact_key in redact_keys
    )


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    frequencies = {char: value.count(char) for char in set(value)}
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length)
        for count in frequencies.values()
    )


def _looks_high_entropy(value: str) -> bool:
    for candidate in _TOKEN_CANDIDATE_PATTERN.findall(value):
        if len(candidate) >= 48 and _entropy(candidate) >= 4.2:
            return True
        if len(candidate) >= 32 and _entropy(candidate) >= 4.7:
            return True
    return False


def _looks_sensitive_string(
    value: str,
    config: StackmintTelemetrySecurityConfig,
) -> bool:
    if _PRIVATE_KEY_PATTERN.search(value):
        return True
    if _BEARER_PATTERN.search(value):
        return True
    if _OPENAI_KEY_PATTERN.search(value):
        return True
    if _ANTHROPIC_KEY_PATTERN.search(value):
        return True
    if _GITHUB_TOKEN_PATTERN.search(value):
        return True
    if _JWT_PATTERN.search(value):
        return True
    if config.redact_emails and _EMAIL_PATTERN.search(value):
        return True
    return _looks_high_entropy(value)


def _serialization_error_value(value: Any, reason: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "stackmint_serialization_error": True,
        "type": type(value).__name__,
    }
    if reason:
        payload["reason"] = reason
    return payload


def _sanitize_string(
    value: str,
    config: StackmintTelemetrySecurityConfig,
) -> tuple[Any, bool, bool, bool]:
    if config.redact_payloads and _looks_sensitive_string(value, config):
        return config.redacted_value, True, False, False
    if len(value) > config.max_string_length:
        keep = max(config.max_string_length, 0)
        return f"{value[:keep]}{config.truncated_value}", False, True, False
    return value, False, False, False


def _sanitize(
    value: Any,
    config: StackmintTelemetrySecurityConfig,
    seen: set[int],
) -> tuple[Any, bool, bool, bool]:
    if value is None or isinstance(value, bool | int):
        return value, False, False, False
    if isinstance(value, float):
        if math.isfinite(value):
            return value, False, False, False
        return str(value), False, False, False
    if isinstance(value, str):
        return _sanitize_string(value, config)
    if isinstance(value, bytes | bytearray | memoryview):
        return _serialization_error_value(value), False, False, True
    if isinstance(value, datetime | date):
        return value.isoformat(), False, False, False
    if isinstance(value, BaseModel):
        try:
            return _sanitize(
                value.model_dump(mode="json", exclude_none=True),
                config,
                seen,
            )
        except Exception:
            return _serialization_error_value(value), False, False, True
    if hasattr(value, "model_dump"):
        try:
            return _sanitize(value.model_dump(exclude_none=True), config, seen)
        except Exception:
            return _serialization_error_value(value), False, False, True
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            data = {
                field.name: getattr(value, field.name)
                for field in dataclasses.fields(value)
            }
            return _sanitize(data, config, seen)
        except Exception:
            return _serialization_error_value(value), False, False, True

    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in seen:
            return (
                _serialization_error_value(value, "circular_reference"),
                False,
                False,
                True,
            )
        seen.add(value_id)
        redacted = False
        truncated = False
        serialization_error = False
        output: dict[str, Any] = {}
        for raw_key, raw_item in value.items():
            key = str(raw_key)
            if config.redact_payloads and _is_sensitive_key(key, config):
                output[key] = config.redacted_value
                redacted = True
                continue
            item, item_redacted, item_truncated, item_error = _sanitize(
                raw_item,
                config,
                seen,
            )
            output[key] = item
            redacted = redacted or item_redacted
            truncated = truncated or item_truncated
            serialization_error = serialization_error or item_error
        seen.remove(value_id)
        return output, redacted, truncated, serialization_error

    if isinstance(value, list | tuple | set | frozenset):
        value_id = id(value)
        if value_id in seen:
            return (
                _serialization_error_value(value, "circular_reference"),
                False,
                False,
                True,
            )
        seen.add(value_id)
        redacted = False
        truncated = False
        serialization_error = False
        output = []
        for item_value in value:
            item, item_redacted, item_truncated, item_error = _sanitize(
                item_value,
                config,
                seen,
            )
            output.append(item)
            redacted = redacted or item_redacted
            truncated = truncated or item_truncated
            serialization_error = serialization_error or item_error
        seen.remove(value_id)
        return output, redacted, truncated, serialization_error

    return _serialization_error_value(value), False, False, True


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8"))


def _enforce_payload_size(
    value: Any,
    config: StackmintTelemetrySecurityConfig,
) -> tuple[Any, bool]:
    try:
        size = _json_size(value)
    except Exception:
        return _serialization_error_value(value), False

    if size <= config.max_payload_bytes:
        return value, False

    json_value = json.dumps(value, default=str, ensure_ascii=True, sort_keys=True)
    preview_size = max(min(config.max_string_length, config.max_payload_bytes - 256), 0)
    return (
        {
            "stackmint_truncated": True,
            "stackmint_payload_security_version": STACKMINT_PAYLOAD_SECURITY_VERSION,
            "stackmint_original_size_bytes": size,
            "stackmint_payload_preview": (
                f"{json_value[:preview_size]}{config.truncated_value}"
            ),
        },
        True,
    )


def sanitize_payload(
    value: Any,
    config: StackmintTelemetrySecurityConfig | None = None,
) -> SanitizedPayload:
    security_config = config or StackmintTelemetrySecurityConfig()
    try:
        sanitized, redacted, truncated, serialization_error = _sanitize(
            value,
            security_config,
            seen=set(),
        )
        sanitized, size_truncated = _enforce_payload_size(
            sanitized,
            security_config,
        )
        json.dumps(sanitized, default=str, ensure_ascii=True)
        return SanitizedPayload(
            sanitized,
            redacted=redacted,
            truncated=truncated or size_truncated,
            serialization_error=serialization_error,
        )
    except Exception:
        return SanitizedPayload(
            _serialization_error_value(value),
            serialization_error=True,
        )


def to_json_safe(value: Any) -> Any:
    config = StackmintTelemetrySecurityConfig(
        redact_payloads=False,
        max_payload_bytes=10_000_000,
        max_string_length=1_000_000,
    )
    return sanitize_payload(value, config).value
