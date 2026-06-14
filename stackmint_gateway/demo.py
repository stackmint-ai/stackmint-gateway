from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, TextIO

from stackmint_gateway.events import GatewayEvent, event, event_to_dict, event_to_line
from stackmint_gateway.langchain import (
    StackmintToolNotAllowedError,
    StackmintToolPolicy,
)
from stackmint_gateway.security import sanitize_payload
from stackmint_gateway.terminal import (
    print_splash,
    render_table,
    should_use_color,
    should_use_unicode_table,
)

LOCAL_MODE = "local"
MOCK_CONTROL_PLANE_MODE = "mock-control-plane"
POSITIONING_MESSAGE = (
    "Observability shows what happened. Stackmint governs what is allowed."
)
SUPPORTED_DEMO_MODES = {LOCAL_MODE, MOCK_CONTROL_PLANE_MODE}
SUPPORTED_DEMO_PROVIDERS = {"fake"}
SPEED_MULTIPLIERS = {
    "fast": 0.35,
    "normal": 1.0,
    "cinematic": 1.6,
}
SPLASH_DELAY_SECONDS = 1.2
INTRO_DELAY_SECONDS = 0.7
POLICY_DELAY_SECONDS = 0.7
RUN_DELAY_SECONDS = 0.6
APPROVAL_DELAY_SECONDS = 0.8
BLOCK_DELAY_SECONDS = 0.8
SECURITY_DELAY_SECONDS = 0.7
RESULT_DELAY_SECONDS = 0.7


@dataclass(frozen=True)
class DemoPacket:
    text: str
    delay_after_seconds: float = 0.0


@dataclass(frozen=True)
class DemoPolicy:
    agent_status: str = "active"
    budget_ceiling_cents: float = 1.0
    permitted_tool_slugs: frozenset[str] = frozenset(
        {"add_numbers", "get_current_time", "create_client_note"}
    )
    require_approval_for: frozenset[str] = frozenset({"create_client_note"})
    blocked_tool_slugs: frozenset[str] = frozenset({"export_customer_records"})


def add_numbers(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


def get_current_time(timezone: str = "Europe/Paris") -> str:
    """Return a deterministic demo timestamp."""
    return f"2026-06-13 18:00 {timezone}"


def create_client_note(note: str) -> str:
    """Create a deterministic demo client note."""
    _ = note
    return "Client note created."


def export_customer_records() -> str:
    """Demo-only blocked export tool."""
    return "should never run"


def build_demo_events(
    *,
    mode: str = LOCAL_MODE,
    provider: str = "fake",
) -> dict[str, Any]:
    normalized_mode = mode.strip().lower()
    normalized_provider = provider.strip().lower()
    _validate_demo_inputs(normalized_mode, normalized_provider)

    if normalized_mode == MOCK_CONTROL_PLANE_MODE:
        events, summary = _build_mock_control_plane_demo(normalized_provider)
    else:
        events, summary = _build_local_demo(normalized_provider)

    return {
        "ok": True,
        "command": "demo",
        "mode": normalized_mode,
        "provider": normalized_provider,
        "events": [event_to_dict(item) for item in events],
        "summary": summary,
    }


def run_governance_demo(
    *,
    mode: str = LOCAL_MODE,
    provider: str = "fake",
    json_output: bool = False,
    quiet: bool = False,
    show_splash: bool = True,
    no_color: bool = False,
    speed: str | None = None,
    no_delay: bool = False,
    clear: bool = False,
    stdout: TextIO | None = None,
) -> int:
    stream = stdout or sys.stdout
    payload = build_demo_events(mode=mode, provider=provider)
    selected_speed = resolve_demo_speed(speed)

    if json_output:
        json.dump(payload, stream, indent=2, sort_keys=True)
        print(file=stream)
        return 0

    delay_enabled = should_delay_demo(
        json_output=json_output,
        no_delay=no_delay,
        quiet=quiet,
        stdout=stream,
    )
    if should_clear_demo(clear=clear, json_output=json_output, stdout=stream):
        print("\033[2J\033[H", end="", file=stream)

    if show_splash:
        print_splash("full", no_color=no_color, file=stream)
        _sleep_after(SPLASH_DELAY_SECONDS, selected_speed, enabled=delay_enabled)

    color_args = type(
        "ColorArgs",
        (),
        {
            "no_color": no_color,
            "json": False,
            "_stdout_is_tty": _isatty(stream),
        },
    )()
    use_color = should_use_color(color_args)
    packets = build_demo_packets(
        payload,
        quiet=quiet,
        color=use_color,
        ascii_table=not should_use_unicode_table(stream),
    )
    for index, packet in enumerate(packets):
        if index:
            print(file=stream)
        print(packet.text, file=stream)
        _sleep_after(
            packet.delay_after_seconds,
            selected_speed,
            enabled=delay_enabled,
        )
    return 0


def resolve_demo_speed(speed: str | None = None) -> str:
    selected = (speed or os.getenv("STACKMINT_DEMO_SPEED") or "normal").strip().lower()
    return selected if selected in SPEED_MULTIPLIERS else "normal"


def demo_delay_seconds(base_delay_seconds: float, speed: str | None = None) -> float:
    return base_delay_seconds * SPEED_MULTIPLIERS[resolve_demo_speed(speed)]


def should_delay_demo(
    *,
    json_output: bool,
    no_delay: bool,
    quiet: bool = False,
    stdout: TextIO | None = None,
) -> bool:
    if json_output or no_delay or quiet:
        return False
    if os.getenv("STACKMINT_NO_DELAY") == "1":
        return False
    if os.getenv("CI"):
        return False
    return _isatty(stdout or sys.stdout)


def should_clear_demo(
    *,
    clear: bool,
    json_output: bool,
    stdout: TextIO | None = None,
) -> bool:
    if not clear or json_output:
        return False
    if os.getenv("CI"):
        return False
    return _isatty(stdout or sys.stdout)


def _build_local_demo(provider: str) -> tuple[list[GatewayEvent], dict[str, int]]:
    events: list[GatewayEvent] = []
    policy = DemoPolicy()
    tool_policy = StackmintToolPolicy(
        permitted_tool_slugs=set(policy.permitted_tool_slugs),
        require_approval_for=set(policy.require_approval_for),
        approval_fn=lambda tool_name: True,
    )
    tools = {
        getattr(tool, "name", "tool"): tool
        for tool in tool_policy.governed_tools(
            [
                add_numbers,
                get_current_time,
                create_client_note,
                export_customer_records,
            ]
        )
    }

    events.extend(
        [
            event(
                "boot",
                "Stackmint Gateway demo started",
                mode=LOCAL_MODE,
                provider=provider,
            ),
            event("info", "Mode: local"),
            event("info", "Provider: fake/local"),
            event("info", "No model-provider API key required"),
            event("info", "No Stackmint backend required"),
            event("info", "No network calls are made in local demo mode"),
            event("info", POSITIONING_MESSAGE),
            event(
                "policy",
                "Loaded local runtime policy",
                agent_status=policy.agent_status,
                permitted_tools=[
                    "add_numbers",
                    "get_current_time",
                    "create_client_note",
                ],
                require_approval_for=sorted(policy.require_approval_for),
                blocked_tools=sorted(policy.blocked_tool_slugs),
                budget_ceiling_cents=policy.budget_ceiling_cents,
            ),
        ]
    )

    events.extend(
        [
            event(
                "run",
                "User request: Add 24 and 18, then get the current time",
            ),
            event("check", "Agent status is active"),
            event("check", "Budget preflight passed"),
            event("check", "Tool add_numbers is permitted"),
        ]
    )
    add_result = tools["add_numbers"].invoke({"a": 24, "b": 18})
    events.append(
        event("allow", "Tool executed: add_numbers", tool="add_numbers")
    )
    events.append(event("check", "Tool get_current_time is permitted"))
    current_time = tools["get_current_time"].invoke({"timezone": "Europe/Paris"})
    events.extend(
        [
            event(
                "allow",
                "Tool executed: get_current_time",
                tool="get_current_time",
            ),
            event(
                "result",
                "24 + 18 = 42",
                result=add_result,
                current_time=current_time,
            ),
            event("run", "User request: Create a client note"),
            event("approval", "Tool create_client_note requires human approval"),
            event("approval", "Approval decision: approved"),
        ]
    )
    tools["create_client_note"].invoke({"note": "Follow up after onboarding call."})
    events.extend(
        [
            event(
                "allow",
                "Tool executed: create_client_note",
                tool="create_client_note",
            ),
            event("record", "HITL decision recorded locally"),
            event("run", "User request: Export all customer records"),
        ]
    )

    try:
        tools["export_customer_records"].invoke({})
    except StackmintToolNotAllowedError:
        events.extend(
            [
                event(
                    "block",
                    "Tool export_customer_records is not permitted by policy",
                    reason="tool_not_permitted",
                    tool="export_customer_records",
                ),
                event("result", "Execution blocked before the tool ran"),
                event(
                    "record",
                    "Execution recorded as blocked",
                    reason="tool_not_permitted",
                    status="blocked",
                ),
            ]
        )

    projected_session_cost_cents = 1.24
    events.extend(
        [
            event(
                "run",
                "User request: Generate a long account strategy report",
            ),
            event(
                "block",
                "Budget preflight failed",
                reason="budget_exceeded",
                projected_session_cost_cents=projected_session_cost_cents,
                budget_ceiling_cents=policy.budget_ceiling_cents,
            ),
            event("result", "Execution blocked before model execution"),
            event(
                "record",
                "Execution recorded as blocked",
                reason="budget_exceeded",
                status="blocked",
            ),
            event("run", "User request includes sensitive content"),
        ]
    )

    sanitized = sanitize_payload(
        {
            "email": "person@example.com",
            "api_key": "sk-proj-demo-secret",
        }
    )
    events.extend(
        [
            event(
                "security",
                "Telemetry redaction applied",
                recorded_payload=sanitized.value,
                redacted=bool(sanitized.redacted),
            ),
            event("record", "Execution telemetry sanitized before recording"),
            event("result", "Demo complete"),
        ]
    )

    summary = {
        "allowed_tool_executions": 3,
        "approval_gated_tools": 1,
        "blocked_tool_attempts": 1,
        "budget_blocks": 1,
        "redacted_telemetry_records": 1 if sanitized.redacted else 0,
    }
    return events, summary


def _build_mock_control_plane_demo(
    provider: str,
) -> tuple[list[GatewayEvent], dict[str, int]]:
    sanitized = sanitize_payload(
        {
            "email": "person@example.com",
            "api_key": "sk-proj-demo-secret",
        }
    )
    events = [
        event(
            "boot",
            "Stackmint Gateway demo started",
            mode=MOCK_CONTROL_PLANE_MODE,
            provider=provider,
        ),
        event("info", "Mode: mock-control-plane"),
        event("info", "Provider: fake/local"),
        event("info", "Running with a mock control plane. No network calls are made."),
        event("info", POSITIONING_MESSAGE),
        event("policy", "Loaded mock remote runtime policy"),
        event("run", "User request: Authorize a safe action"),
        event("check", "Remote authorization decision: allow"),
        event("allow", "Execution authorized by mock control plane"),
        event("record", "Execution recorded through mock control plane"),
        event("run", "User request: Export all customer records"),
        event(
            "block",
            "Remote authorization decision: block",
            reason="agent_status",
        ),
        event("record", "Execution recorded as blocked", status="blocked"),
        event("run", "User request: Generate a long account strategy report"),
        event(
            "block",
            "Remote budget reservation rejected",
            reason="budget_exceeded",
        ),
        event("record", "Budget reservation rejection recorded"),
        event("run", "User request: Create a client note"),
        event(
            "approval",
            "Remote authorization decision: waiting_approval",
            approval_request_id="approval_demo_001",
        ),
        event("record", "Approval request recorded through mock control plane"),
        event("record", "Tool event recorded through mock control plane"),
        event(
            "security",
            "Telemetry redaction applied",
            recorded_payload=sanitized.value,
            redacted=bool(sanitized.redacted),
        ),
        event("result", "Demo complete"),
    ]
    summary = {
        "allowed_tool_executions": 1,
        "approval_gated_tools": 1,
        "blocked_tool_attempts": 1,
        "budget_blocks": 1,
        "redacted_telemetry_records": 1 if sanitized.redacted else 0,
        "remote_authorization_checks": 2,
        "remote_budget_reservations": 1,
        "remote_approval_requests": 1,
        "tool_events_recorded": 1,
    }
    return events, summary


def build_demo_packets(
    payload: dict[str, Any],
    *,
    quiet: bool,
    color: bool,
    ascii_table: bool = False,
) -> list[DemoPacket]:
    if quiet:
        result_lines = [
            event_to_line(
                GatewayEvent(
                    kind=item["kind"],
                    message=item["message"],
                    details=item.get("details", {}),
                ),
                color=color,
            )
            for item in payload["events"]
            if item["kind"] == "result"
        ]
        return [DemoPacket("\n".join(result_lines))]

    packets: list[DemoPacket] = []
    intro_lines: list[str] = []
    current_run_lines: list[str] = []

    def flush_intro() -> None:
        if intro_lines:
            packets.append(DemoPacket("\n".join(intro_lines), INTRO_DELAY_SECONDS))
            intro_lines.clear()

    def flush_run() -> None:
        if current_run_lines:
            packets.append(
                DemoPacket(
                    "\n".join(current_run_lines),
                    _delay_for_packet_lines(current_run_lines),
                )
            )
            current_run_lines.clear()

    for item in payload["events"]:
        if item["kind"] == "policy":
            flush_intro()
            packets.append(
                DemoPacket(
                    "\n".join(_event_lines(item, color=color)),
                    POLICY_DELAY_SECONDS,
                )
            )
            continue

        if item["message"] == "Demo complete":
            flush_run()
            packets.append(
                DemoPacket(
                    event_to_line(
                        GatewayEvent(
                            kind=item["kind"],
                            message=item["message"],
                            details=item.get("details", {}),
                        ),
                        color=color,
                    ),
                    RESULT_DELAY_SECONDS,
                )
            )
            continue

        if item["kind"] == "run":
            flush_run()
            current_run_lines.extend(_event_lines(item, color=color))
            continue

        if current_run_lines:
            current_run_lines.extend(_event_lines(item, color=color))
        else:
            intro_lines.extend(_event_lines(item, color=color))

    flush_run()
    packets.append(
        DemoPacket(_render_demo_recap(payload["summary"], ascii_table=ascii_table))
    )
    return packets


def _event_lines(item: dict[str, Any], *, color: bool) -> list[str]:
    gateway_event = GatewayEvent(
        kind=item["kind"],
        message=item["message"],
        details=item.get("details", {}),
    )
    lines = [event_to_line(gateway_event, color=color)]
    if item["kind"] == "policy":
        lines.extend(_policy_detail_lines(item["details"]))
    elif _should_print_reason(item):
        lines.append(f"Reason: {item['details']['reason']}")
    elif item["message"] == "Budget preflight failed":
        lines.extend(_budget_detail_lines(item["details"]))
    elif item["message"] == "Telemetry redaction applied":
        lines.extend(_recorded_payload_lines(item["details"].get("recorded_payload")))
    return lines


def _policy_detail_lines(details: dict[str, Any]) -> list[str]:
    if "agent_status" not in details:
        return []
    return [
        "",
        f"Agent status:         {details['agent_status']}",
        "Permitted tools:      "
        f"{', '.join(details.get('permitted_tools', []))}",
        "Requires approval:    "
        f"{', '.join(details.get('require_approval_for', []))}",
        "Blocked tools:        "
        f"{', '.join(details.get('blocked_tools', []))}",
        f"Budget ceiling:       {details['budget_ceiling_cents']:.2f} cents",
    ]


def _budget_detail_lines(details: dict[str, Any]) -> list[str]:
    return [
        "",
        "Projected session cost:   "
        f"{details['projected_session_cost_cents']:.2f} cents",
        f"Budget ceiling:           {details['budget_ceiling_cents']:.2f} cents",
        f"Reason:                   {details['reason']}",
        "",
    ]


def _recorded_payload_lines(payload: Any) -> list[str]:
    return [
        "",
        "Recorded payload:",
        json.dumps(payload, indent=2),
        "",
    ]


def _render_demo_recap(summary: dict[str, int], *, ascii_table: bool = False) -> str:
    rows = [
        ("Allowed tool executions", summary["allowed_tool_executions"]),
        ("Approval-gated tools", summary["approval_gated_tools"]),
        ("Blocked tool attempts", summary["blocked_tool_attempts"]),
        ("Budget blocks", summary["budget_blocks"]),
        ("Redacted telemetry records", summary["redacted_telemetry_records"]),
    ]
    if "remote_authorization_checks" in summary:
        rows.extend(
            [
                ("Remote authorization checks", summary["remote_authorization_checks"]),
                ("Remote budget reservations", summary["remote_budget_reservations"]),
                ("Remote approval requests", summary["remote_approval_requests"]),
                ("Tool events recorded", summary["tool_events_recorded"]),
            ]
        )
    lines = [render_table(rows, title="Governance recap:", ascii=ascii_table)]
    lines.extend(
        [
            "",
            "Managed control-plane path:",
            "  centralized policy",
            "  budget authorization",
            "  approval workflows",
            "  audit trails",
            "  SSO/SAML",
            "",
            "Contact: hello@stackmint.ai",
        ]
    )
    return "\n".join(lines)


def _delay_for_packet_lines(lines: list[str]) -> float:
    joined = "\n".join(lines)
    if "[APPROVAL]" in joined:
        return APPROVAL_DELAY_SECONDS
    if "[BLOCK]" in joined:
        return BLOCK_DELAY_SECONDS
    if "[SECURITY]" in joined:
        return SECURITY_DELAY_SECONDS
    return RUN_DELAY_SECONDS


def _sleep_after(base_delay_seconds: float, speed: str, *, enabled: bool) -> None:
    if enabled and base_delay_seconds > 0:
        time.sleep(demo_delay_seconds(base_delay_seconds, speed))


def _validate_demo_inputs(mode: str, provider: str) -> None:
    if mode not in SUPPORTED_DEMO_MODES:
        raise RuntimeError(f"Unsupported demo mode: {mode}")
    if provider not in SUPPORTED_DEMO_PROVIDERS:
        raise RuntimeError(f"Unsupported demo provider: {provider}")


def _should_print_reason(item: dict[str, Any]) -> bool:
    return (
        item["kind"] == "record"
        and item["message"] == "Execution recorded as blocked"
        and item.get("details", {}).get("reason") == "tool_not_permitted"
    )


def _isatty(stream: TextIO) -> bool:
    checker = getattr(stream, "isatty", None)
    return bool(checker and checker())
