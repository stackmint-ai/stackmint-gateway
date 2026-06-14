from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import shlex
import subprocess  # nosec B404
import sys
import traceback
from pathlib import Path
from typing import Any, TextIO

from stackmint_gateway.demo import (
    MOCK_CONTROL_PLANE_MODE,
    SPEED_MULTIPLIERS,
    SUPPORTED_DEMO_PROVIDERS,
    run_governance_demo,
)
from stackmint_gateway.doctor import (
    DetectionResult,
    build_doctor_report,
)
from stackmint_gateway.events import (
    GatewayEvent,
    event_to_dict,
    event_to_line,
)
from stackmint_gateway.events import (
    event as gateway_event,
)
from stackmint_gateway.terminal import (
    print_splash,
    render_table,
    should_show_splash,
    should_use_color,
    should_use_unicode_table,
)

PACKAGE_NAME = "stackmint-gateway"
DISPLAY_NAME = "Stackmint Gateway"
ENV_SCAFFOLD = """STACKMINT_GATEWAY_API_KEY=
STACKMINT_GATEWAY_BASE_URL=http://127.0.0.1:5173/api
STACKMINT_EXAMPLE_PROVIDER=fake
STACKMINT_EXAMPLE_MODEL=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
CEREBRAS_API_KEY=
STACKMINT_MCP_READ_ONLY=false
STACKMINT_MCP_REQUIRE_CONFIRMATION=true
STACKMINT_MCP_RECORD_PAYLOADS=true
"""
CHECK_COMMANDS = [
    "uv run ruff check .",
    "uv run python -m compileall stackmint_gateway examples tests",
    "uv run pytest",
    "uv run bandit --ini .bandit -r stackmint_gateway examples tests",
    "uv run pip-audit",
    "uv run detect-secrets scan --baseline .secrets.baseline",
    "uv run stackmint demo --no-splash --no-delay",
    "uv run stackmint demo --json",
    "uv run stackmint doctor --no-splash",
    "uv run stackmint doctor --json",
    "uv run stackmint help",
    "uv run stackmint mcp --preview --no-splash",
    "uv run python -m build",
    "uv run twine check dist/*",
]
DETECT_SECRETS_BASELINE_NOTE = (
    "# Do not commit timestamp-only .secrets.baseline churn."
)
MCP_TOOLS = [
    "stackmint_get_agent_policy",
    "stackmint_authorize_execution",
    "stackmint_record_execution",
    "stackmint_reserve_budget",
    "stackmint_commit_budget",
    "stackmint_create_approval_request",
    "stackmint_get_approval_decision",
    "stackmint_record_tool_event",
]
MCP_RESOURCES = [
    "stackmint://agent/me",
    "stackmint://agent/policy",
    "stackmint://agent/executions/latest",
    "stackmint://approvals/{approval_request_id}",
]
MCP_PROMPTS = [
    "stackmint_governance_review",
    "stackmint_incident_summary",
]
HELP_COMMANDS = [
    ("demo", "Run the no-key local governance demo"),
    ("doctor", "Check environment, providers, and gateway readiness"),
    ("example langchain", "Run the LangChain wrapper smoke test"),
    ("mcp", "Start the MCP governance server"),
    ("mcp --preview", "Preview available MCP governance tools"),
    ("init", "Create a local .env.example scaffold"),
    ("version", "Print version information"),
    ("help [command]", "Show help for a command"),
]
HELP_WORKFLOWS = [
    "stackmint demo",
    "stackmint demo --clear --speed cinematic",
    "stackmint doctor",
    "stackmint example langchain",
    "stackmint mcp --preview",
]
HELP_GLOBAL_OPTIONS = [
    ("--no-splash", "Hide the terminal splash/header"),
    ("--no-color", "Disable ANSI color"),
    ("--quiet", "Reduce human-readable output"),
    ("--json", "Return machine-readable JSON where supported"),
    ("--debug", "Show tracebacks for debugging"),
]
TOP_LEVEL_COMMANDS = {
    "version",
    "doctor",
    "demo",
    "init",
    "mcp",
    "example",
    "check",
    "help",
}
GLOBAL_FLAG_NAMES = {"--no-splash", "--quiet", "--no-color", "--json", "--debug"}


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    help_result = _maybe_handle_help_request(raw_argv, stdout=out, stderr=err)
    if help_result is not None:
        return help_result
    invalid_command = _invalid_top_level_command(raw_argv)
    if invalid_command is not None:
        return _unknown_command_error(invalid_command, raw_argv, stdout=out, stderr=err)
    args: argparse.Namespace | None = None
    try:
        args = _build_parser().parse_args(raw_argv)
        args._stdout_is_tty = _isatty(out)
        return _dispatch(args, stdout=out, stderr=err)
    except KeyboardInterrupt:
        _emit_error("Interrupted", args=args, stdout=out, stderr=err)
        return 130
    except RuntimeError as exc:
        _emit_error(str(exc), args=args, stdout=out, stderr=err)
        if args is not None and getattr(args, "debug", False):
            traceback.print_exc(file=err)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common_flags(common)
    command_parsers: dict[str, argparse.ArgumentParser] = {}

    parser = argparse.ArgumentParser(
        prog="stackmint",
        description="Stackmint Gateway command line interface.",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command")

    command_parsers["version"] = subparsers.add_parser(
        "version",
        parents=[common],
        help="Print version info",
        description="Print version info",
    )

    doctor = command_parsers["doctor"] = subparsers.add_parser(
        "doctor",
        parents=[common],
        help="Check environment and gateway configuration",
        description="Check environment and gateway configuration",
    )
    doctor.add_argument(
        "--gateway-check",
        action="store_true",
        help="Try a non-fatal Stackmint gateway API check when an API key is set.",
    )

    demo = command_parsers["demo"] = subparsers.add_parser(
        "demo",
        parents=[common],
        help="Run the local governance demo",
        description="Run the local governance demo",
    )
    demo.add_argument(
        "--mode",
        choices=["local", MOCK_CONTROL_PLANE_MODE],
        default="local",
        help="Demo mode to run.",
    )
    demo.add_argument(
        "--provider",
        default="fake",
        help="Demo provider. Only fake is supported for stackmint demo.",
    )
    demo.add_argument(
        "--speed",
        choices=sorted(SPEED_MULTIPLIERS),
        default=None,
        help="Demo pacing speed.",
    )
    demo.add_argument(
        "--no-delay",
        action="store_true",
        help="Disable human-mode demo pacing.",
    )
    demo.add_argument(
        "--clear",
        action="store_true",
        help="Clear the terminal before the demo in human mode.",
    )

    init = command_parsers["init"] = subparsers.add_parser(
        "init",
        parents=[common],
        help="Create safe local environment scaffolding",
        description="Create safe local environment scaffolding",
    )
    init.add_argument("--env", action="store_true", help="Also create .env")
    init.add_argument(
        "--write-env",
        action="store_true",
        help="Also create .env",
    )
    init.add_argument("--force", action="store_true", help="Overwrite target files")

    mcp = command_parsers["mcp"] = subparsers.add_parser(
        "mcp",
        parents=[common],
        help="Start or preview the MCP governance server",
        description="Start or preview the MCP governance server",
    )
    mcp.add_argument(
        "--preview",
        action="store_true",
        help="Print available MCP tools/resources without starting stdio.",
    )

    example = command_parsers["example"] = subparsers.add_parser(
        "example",
        parents=[common],
        help="Run packaged examples",
        description="Run packaged examples",
    )
    example_subparsers = example.add_subparsers(dest="example")
    example_subparsers.add_parser(
        "langchain",
        parents=[common],
        help="Run the LangChain example",
    )

    check = command_parsers["check"] = subparsers.add_parser(
        "check",
        parents=[common],
        help="Print or run local verification commands",
        description="Print or run local verification commands",
    )
    check_group = check.add_mutually_exclusive_group()
    check_group.add_argument(
        "--print",
        dest="print_checks",
        action="store_true",
        help="Print local verification commands.",
    )
    check_group.add_argument(
        "--run",
        action="store_true",
        help="Run local verification commands.",
    )

    help_parser = command_parsers["help"] = subparsers.add_parser(
        "help",
        parents=[common],
        help="Show command help",
        description="Show command help",
    )
    help_parser.add_argument(
        "topic",
        nargs="*",
        help="Command to show help for.",
    )

    parser._stackmint_command_parsers = command_parsers  # type: ignore[attr-defined]
    return parser


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-splash", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-color", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)


def _dispatch(args: argparse.Namespace, *, stdout: TextIO, stderr: TextIO) -> int:
    command = args.command or "home"
    if command == "home":
        return _command_home(args, stdout)
    if command == "help":
        return _command_help(args.topic, args, stdout, stderr)
    if command == "version":
        return _command_version(args, stdout)
    if command == "doctor":
        _maybe_splash("doctor", args, "compact", stdout)
        return _command_doctor(args, stdout=stdout, stderr=stderr)
    if command == "demo":
        return _command_demo(args, stdout=stdout, stderr=stderr)
    if command == "init":
        _maybe_splash("init", args, "compact", stdout)
        return _command_init(args, stdout)
    if command == "mcp":
        if args.preview:
            _maybe_splash("mcp", args, "compact", stdout)
            return _command_mcp_preview(args, stdout)
        return _command_mcp(stderr)
    if command == "example":
        if args.example == "langchain":
            _maybe_splash("example", args, "compact", stdout)
            return _command_example_langchain(args, stdout=stdout, stderr=stderr)
        _print_event(
            gateway_event("error", "Choose an example: stackmint example langchain"),
            args,
            stderr,
        )
        return 2
    if command == "check":
        return _command_check(args, stdout)
    _print_event(gateway_event("error", f"Unknown command: {command}"), args, stderr)
    return 2


def _invalid_top_level_command(argv: list[str]) -> str | None:
    for item in argv:
        if item in GLOBAL_FLAG_NAMES:
            continue
        if item.startswith("-"):
            return None
        if item not in TOP_LEVEL_COMMANDS:
            return item
        return None
    return None


def _unknown_command_error(
    command: str,
    argv: list[str],
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if "--json" in argv:
        _write_json(
            {
                "ok": False,
                "command": command,
                "error": {
                    "code": "unknown_command",
                    "message": f"Unknown command: {command}",
                },
                "events": _event_dicts(
                    [
                        gateway_event("error", f"Unknown command: {command}"),
                        gateway_event(
                            "info",
                            "Run `stackmint help` to see available commands.",
                        ),
                    ]
                ),
            },
            stdout,
        )
        return 2
    print(f"[ERROR] Unknown command: {command}", file=stderr)
    print("[INFO] Run `stackmint help` to see available commands.", file=stderr)
    return 2


def _command_home(args: argparse.Namespace, stdout: TextIO) -> int:
    if args.json:
        _write_json(_main_help_payload(), stdout)
        return 0
    print(render_main_help(show_header=_should_show_help_header(args)), file=stdout)
    return 0


def _command_help(
    topic: list[str],
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    return _render_help_topic(topic, args=args, stdout=stdout, stderr=stderr)


def _maybe_handle_help_request(
    argv: list[str],
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int | None:
    if not _is_help_request(argv):
        return None

    args = _help_args_from_argv(argv, stdout)
    topic = _help_topic_from_argv(argv)
    return _render_help_topic(topic, args=args, stdout=stdout, stderr=stderr)


def _is_help_request(argv: list[str]) -> bool:
    if not argv:
        return False
    return argv[0] == "help" or "--help" in argv or "-h" in argv


def _help_topic_from_argv(argv: list[str]) -> list[str]:
    if not argv:
        return []
    if argv[0] == "help":
        return [
            item
            for item in argv[1:]
            if item not in {"--help", "-h", "--json", "--no-splash", "--quiet"}
            and item not in {"--no-color", "--debug"}
        ]

    topic: list[str] = []
    for item in argv:
        if item in {"--help", "-h"}:
            break
        if item.startswith("-"):
            continue
        topic.append(item)
        if item != "example":
            break
        if len(topic) == 2:
            break
    return topic


def _help_args_from_argv(argv: list[str], stdout: TextIO) -> argparse.Namespace:
    return argparse.Namespace(
        no_splash="--no-splash" in argv,
        quiet="--quiet" in argv,
        no_color="--no-color" in argv,
        json="--json" in argv,
        debug="--debug" in argv,
        _stdout_is_tty=_isatty(stdout),
    )


def _render_help_topic(
    topic: list[str],
    *,
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    normalized = _normalize_help_topic(topic)
    if normalized is None:
        message = "Unknown help topic"
        if topic:
            message = f"Unknown help topic: {' '.join(topic)}"
        _print_event(gateway_event("error", message), args, stderr)
        _print_event(
            gateway_event("info", "Run `stackmint help` to see commands"),
            args,
            stderr,
        )
        return 2

    if args.json:
        payload = (
            _main_help_payload()
            if not normalized
            else _command_help_payload(normalized)
        )
        _write_json(payload, stdout)
        return 0

    if not normalized:
        print(render_main_help(show_header=_should_show_help_header(args)), file=stdout)
    else:
        print(render_command_help(normalized), file=stdout)
    return 0


def _normalize_help_topic(topic: list[str]) -> tuple[str, ...] | None:
    if not topic:
        return ()
    if topic == ["example", "langchain"]:
        return ("example", "langchain")
    if len(topic) == 1 and topic[0] in {
        "demo",
        "doctor",
        "mcp",
        "example",
        "init",
        "version",
        "check",
        "help",
    }:
        return (topic[0],)
    return None


def render_main_help(*, show_header: bool = True) -> str:
    lines: list[str] = []
    if show_header:
        lines.extend([DISPLAY_NAME, "Runtime governance for AI agents", ""])
    lines.extend(
        [
            "Usage:",
            "  stackmint <command> [options]",
            "",
            "Core commands:",
            *_help_rows(HELP_COMMANDS, command_width=21),
            "",
            "Common workflows:",
            *[f"  {workflow}" for workflow in HELP_WORKFLOWS],
            "",
            "Global options:",
            *_help_rows(HELP_GLOBAL_OPTIONS, command_width=21),
            "",
            "Learn more:",
            "  README.md",
            "  https://stackmint.ai",
        ]
    )
    return "\n".join(lines)


def render_command_help(topic: tuple[str, ...]) -> str:
    return _COMMAND_HELP_RENDERERS[topic]()


def _help_rows(rows: list[tuple[str, str]], *, command_width: int) -> list[str]:
    return [
        f"  {command:<{command_width}} {description}"
        for command, description in rows
    ]


def _main_help_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "command": "help",
        "usage": "stackmint <command> [options]",
        "commands": [
            {"name": command, "description": description}
            for command, description in HELP_COMMANDS
        ],
        "common_workflows": HELP_WORKFLOWS,
        "global_options": [
            {"name": option, "description": description}
            for option, description in HELP_GLOBAL_OPTIONS
        ],
    }


def _command_help_payload(topic: tuple[str, ...]) -> dict[str, Any]:
    return {
        "ok": True,
        "command": "help",
        "topic": " ".join(topic),
        "text": render_command_help(topic),
    }


def _should_show_help_header(args: argparse.Namespace) -> bool:
    if getattr(args, "no_splash", False):
        return False
    if getattr(args, "quiet", False):
        return False
    if getattr(args, "json", False):
        return False
    if os.getenv("STACKMINT_NO_SPLASH") == "1":
        return False
    return True


def _help_demo() -> str:
    return "\n".join(
        [
            "Stackmint demo",
            "",
            "Run the local no-key governance demo.",
            "",
            "Usage:",
            "  stackmint demo [options]",
            "",
            "Options:",
            "  --mode local|mock-control-plane",
            "  --speed fast|normal|cinematic",
            "  --clear",
            "  --no-delay",
            "  --no-splash",
            "  --no-color",
            "  --json",
            "  --quiet",
            "",
            "Examples:",
            "  stackmint demo",
            "  stackmint demo --clear --speed cinematic",
            "  stackmint demo --json",
        ]
    )


def _help_doctor() -> str:
    return "\n".join(
        [
            "Stackmint doctor",
            "",
            "Check local readiness for Stackmint Gateway.",
            "",
            "Usage:",
            "  stackmint doctor [options]",
            "",
            "Checks:",
            "  Python version",
            "  Stackmint package imports",
            "  Stackmint environment variables",
            "  Optional model-provider packages",
            "  Optional provider API key presence",
            "  MCP SDK availability",
            "",
            "Options:",
            "  --gateway-check",
            "  --json",
            "  --no-splash",
            "  --no-color",
            "  --quiet",
            "",
            "Examples:",
            "  stackmint doctor",
            "  stackmint doctor --json",
        ]
    )


def _help_mcp() -> str:
    return "\n".join(
        [
            "Stackmint MCP",
            "",
            "Start or preview the MCP governance server.",
            "",
            "Usage:",
            "  stackmint mcp [options]",
            "",
            "Options:",
            "  --preview             Show MCP tools/resources without starting "
            "stdio server",
            "  --no-splash           Hide compact header in preview mode",
            "  --json                Return preview as JSON where supported",
            "",
            "Examples:",
            "  stackmint mcp --preview",
            "  STACKMINT_GATEWAY_API_KEY=... stackmint mcp",
            "",
            "Note:",
            "  `stackmint mcp` does not print splash output in stdio mode because",
            "  MCP stdout must remain protocol-clean.",
        ]
    )


def _help_example() -> str:
    return "\n".join(
        [
            "Stackmint examples",
            "",
            "Run packaged Stackmint Gateway examples.",
            "",
            "Usage:",
            "  stackmint example <name> [options]",
            "",
            "Available examples:",
            "  langchain             Run the LangChain wrapper smoke test",
            "",
            "Examples:",
            "  stackmint example langchain",
            "  stackmint example langchain --json",
            "  stackmint example langchain --debug",
        ]
    )


def _help_example_langchain() -> str:
    return "\n".join(
        [
            "Stackmint example langchain",
            "",
            "Run the provider-aware LangChain Runnable wrapper smoke test.",
            "",
            "Usage:",
            "  stackmint example langchain [options]",
            "",
            "Options:",
            "  --json                Return structured smoke-test output",
            "  --debug               Show raw LangChain message details",
            "  --no-splash           Hide compact header",
            "  --no-color            Disable ANSI color",
            "",
            "Examples:",
            "  stackmint example langchain",
            "  stackmint example langchain --json",
            "  STACKMINT_EXAMPLE_PROVIDER=openai OPENAI_API_KEY=... stackmint "
            "example langchain",
        ]
    )


def _help_init() -> str:
    return "\n".join(
        [
            "Stackmint init",
            "",
            "Create safe local environment scaffolding.",
            "",
            "Usage:",
            "  stackmint init [options]",
            "",
            "Options:",
            "  --env                 Also create .env",
            "  --write-env           Also create .env",
            "  --force               Overwrite target files",
            "",
            "Examples:",
            "  stackmint init",
            "  stackmint init --write-env",
        ]
    )


def _help_version() -> str:
    return "\n".join(
        [
            "Stackmint version",
            "",
            "Print version information.",
            "",
            "Usage:",
            "  stackmint version [options]",
            "",
            "Options:",
            "  --json                Return version metadata as JSON",
            "",
            "Examples:",
            "  stackmint version",
            "  stackmint version --json",
        ]
    )


def _help_check() -> str:
    return "\n".join(
        [
            "Stackmint check",
            "",
            "Print or run local verification commands.",
            "",
            "Usage:",
            "  stackmint check [options]",
            "",
            "Options:",
            "  --print               Print local verification commands",
            "  --run                 Run local verification commands",
            "",
            "Examples:",
            "  stackmint check --print",
            "  stackmint check --run",
        ]
    )


def _help_help() -> str:
    return "\n".join(
        [
            "Stackmint help",
            "",
            "Show curated Stackmint CLI help.",
            "",
            "Usage:",
            "  stackmint help [command]",
            "",
            "Examples:",
            "  stackmint help",
            "  stackmint help demo",
            "  stackmint help example langchain",
        ]
    )


_COMMAND_HELP_RENDERERS = {
    ("demo",): _help_demo,
    ("doctor",): _help_doctor,
    ("mcp",): _help_mcp,
    ("example",): _help_example,
    ("example", "langchain"): _help_example_langchain,
    ("init",): _help_init,
    ("version",): _help_version,
    ("check",): _help_check,
    ("help",): _help_help,
}


def _command_version(args: argparse.Namespace, stdout: TextIO) -> int:
    version = _package_version()
    payload = {
        "ok": True,
        "command": "version",
        "version": _display_version(version),
        "python": platform.python_version(),
        "package": PACKAGE_NAME,
        "capabilities": {
            "mcp_sdk": _capability_status("mcp.server.fastmcp"),
            "langchain_core": _capability_status("langchain_core"),
            "langchain_package": _capability_status("langchain"),
        },
    }
    if args.json:
        _write_json(payload, stdout)
        return 0
    print(f"{DISPLAY_NAME} {payload['version']}", file=stdout)
    print(f"Python {payload['python']}", file=stdout)
    print(f"Package: {payload['package']}", file=stdout)
    print(f"MCP SDK: {payload['capabilities']['mcp_sdk']}", file=stdout)
    print(
        f"LangChain Core: {payload['capabilities']['langchain_core']}",
        file=stdout,
    )
    print(
        f"LangChain package: {payload['capabilities']['langchain_package']}",
        file=stdout,
    )
    return 0


def _command_doctor(
    args: argparse.Namespace,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    ok = True
    events: list[GatewayEvent] = []
    python_ok = sys.version_info >= (3, 10)
    events.append(
        gateway_event(
            "check",
            f"Python version: {'ok' if python_ok else 'unsupported'}",
            python=platform.python_version(),
        )
    )
    ok = ok and python_ok

    try:
        import stackmint_gateway  # noqa: F401
    except Exception:
        events.append(gateway_event("check", "stackmint_gateway import: failed"))
        ok = False
    else:
        events.append(gateway_event("check", "stackmint_gateway import: ok"))

    report = build_doctor_report()
    config = report.config
    events.extend(_doctor_config_events(config))
    if not config["gateway_api_key_present"]:
        events.append(
            gateway_event("info", "Gateway API key is optional for local demos")
        )
    events.extend(_doctor_detection_events(report.detections))

    if config["gateway_api_key_present"] and args.gateway_check:
        events.extend(_gateway_check())
    elif config["gateway_api_key_present"]:
        events.append(
            gateway_event(
                "info",
                "Gateway connectivity check skipped; use --gateway-check to try it",
            )
        )
    events.append(gateway_event("result", "Doctor complete"))

    if args.json:
        _write_json(
            {
                "ok": ok,
                "command": "doctor",
                "events": _event_dicts(events),
                "detections": [
                    detection.to_dict() for detection in report.detections
                ],
                "config": config,
                "readiness": report.readiness,
                "summary": {
                    "python_ok": python_ok,
                    "gateway_api_key_present": config["gateway_api_key_present"],
                    "langchain_core": _detection_status(
                        report.detections,
                        "LangChain Core",
                    ),
                    "langchain_package": _detection_status(
                        report.detections,
                        "LangChain",
                    ),
                    "openai_provider": _detection_status(
                        report.detections,
                        "OpenAI provider package",
                    ),
                    "anthropic_provider": _detection_status(
                        report.detections,
                        "Anthropic provider package",
                    ),
                    "cerebras_provider": _detection_status(
                        report.detections,
                        "Cerebras provider package",
                    ),
                    "mcp_sdk": _detection_status(report.detections, "MCP SDK"),
                },
            },
            stdout,
        )
    else:
        _print_events(events, args, stdout)
        if not args.quiet:
            _print_doctor_recap(report.readiness, stdout)
    return 0 if ok else 1


def _command_demo(
    args: argparse.Namespace,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    provider = args.provider.strip().lower()
    if provider not in SUPPORTED_DEMO_PROVIDERS:
        events = [
            gateway_event("error", f"Unsupported demo provider: {provider}"),
            gateway_event(
                "info",
                "Use `stackmint example langchain` for provider-backed examples",
            ),
        ]
        if args.json:
            _write_json(
                {
                    "ok": False,
                    "command": "demo",
                    "error": {
                        "code": "unsupported_demo_provider",
                        "message": f"Unsupported demo provider: {provider}",
                    },
                    "events": _event_dicts(events),
                },
                stdout,
            )
        else:
            _print_events(events, args, stderr)
        return 2

    try:
        return run_governance_demo(
            mode=args.mode,
            provider=provider,
            json_output=args.json,
            quiet=args.quiet,
            show_splash=should_show_splash("demo", args),
            no_color=args.no_color,
            speed=args.speed,
            no_delay=args.no_delay,
            clear=args.clear,
            stdout=stdout,
        )
    except RuntimeError as exc:
        if args.json:
            _write_json(
                {
                    "ok": False,
                    "command": "demo",
                    "error": {"code": "demo_error", "message": str(exc)},
                    "events": _event_dicts([gateway_event("error", str(exc))]),
                },
                stdout,
            )
        else:
            _print_event(gateway_event("error", str(exc)), args, stderr)
        return 1


def _command_init(args: argparse.Namespace, stdout: TextIO) -> int:
    events: list[GatewayEvent] = []
    events.append(_write_env_file(Path(".env.example"), force=args.force))
    if args.env or args.write_env:
        events.append(_write_env_file(Path(".env"), force=args.force))
    else:
        events.append(
            gateway_event("info", "Copy to .env and add keys only when needed")
        )
    if args.json:
        _write_json(
            {"ok": True, "command": "init", "events": _event_dicts(events)},
            stdout,
        )
    else:
        _print_events(events, args, stdout)
    return 0


def _command_mcp(stderr: TextIO) -> int:
    try:
        return _run_mcp_server()
    except RuntimeError:
        print("[ERROR] MCP support is not installed.", file=stderr)
        print("[INFO] Install with: uv sync --extra mcp", file=stderr)
        return 1


def _command_mcp_preview(args: argparse.Namespace, stdout: TextIO) -> int:
    events = [gateway_event("info", "Stackmint Governance MCP Server preview")]
    for tool_name in MCP_TOOLS:
        events.append(gateway_event("info", f"Tool available: {tool_name}"))
    for resource in MCP_RESOURCES:
        events.append(gateway_event("info", f"Resource available: {resource}"))
    for prompt in MCP_PROMPTS:
        events.append(gateway_event("info", f"Prompt available: {prompt}"))
    if args.json:
        _write_json(
            {
                "ok": True,
                "command": "mcp_preview",
                "events": _event_dicts(events),
                "tools": MCP_TOOLS,
                "resources": MCP_RESOURCES,
                "prompts": MCP_PROMPTS,
            },
            stdout,
        )
    else:
        _print_events(events, args, stdout)
    return 0


def _command_example_langchain(
    args: argparse.Namespace,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        module = importlib.import_module("examples.langchain_example")
    except ImportError:
        _print_event(
            gateway_event("error", "LangChain example dependencies are missing"),
            args,
            stderr,
        )
        _print_event(
            gateway_event("info", "Run: uv sync --extra examples"),
            args,
            stderr,
        )
        return 1
    try:
        payload = _build_langchain_example_payload(module)
    except RuntimeError as exc:
        events = _events_for_langchain_example_error(str(exc))
        if args.json:
            _write_json(
                {
                    "ok": False,
                    "command": "example langchain",
                    "error": {
                        "code": _langchain_example_error_code(str(exc)),
                        "message": str(exc),
                    },
                    "events": _event_dicts(events),
                },
                stdout,
            )
        else:
            _print_events(events, args, stderr)
        if args.debug:
            traceback.print_exc(file=stderr)
        return 1
    except Exception:
        message = "LangChain example failed"
        if args.json:
            _write_json(
                {
                    "ok": False,
                    "command": "example langchain",
                    "error": {"code": "example_failed", "message": message},
                    "events": _event_dicts([gateway_event("error", message)]),
                },
                stdout,
            )
        else:
            _print_event(gateway_event("error", message), args, stderr)
        if args.debug:
            traceback.print_exc(file=stderr)
        return 1

    if args.json:
        if not args.debug:
            payload.pop("debug_messages", None)
        _write_json(payload, stdout)
    else:
        _print_langchain_example_payload(payload, args, stdout)
        if args.debug:
            _print_langchain_debug_messages(payload["debug_messages"], stdout)
    return 0


def _build_langchain_example_payload(module: Any) -> dict[str, Any]:
    provider = module.selected_provider()
    model_name = module.resolved_model_name(provider)
    gateway_api_key_present = bool(os.getenv("STACKMINT_GATEWAY_API_KEY"))
    provider_label = _langchain_provider_label(provider)
    events: list[GatewayEvent] = [
        gateway_event(
            "boot",
            "LangChain example started",
            provider=provider,
            model=model_name,
        ),
        gateway_event("info", f"Provider: {provider_label}"),
        gateway_event("info", f"Model: {model_name}"),
    ]

    if provider == "fake":
        events.append(gateway_event("info", "No model-provider API key required"))
    else:
        env_var = module.PROVIDER_API_KEYS[provider]
        if not os.getenv(env_var):
            provider_name = module.PROVIDER_DISPLAY_NAMES[provider]
            raise RuntimeError(
                f"{env_var} is required for the {provider_name} example"
            )
        events.append(gateway_event("check", f"{env_var}: present"))

        import_name = _langchain_provider_import_name(provider)
        if import_name and importlib.util.find_spec(import_name) is None:
            raise RuntimeError(f"Missing optional dependency for provider: {provider}")
        provider_name = module.PROVIDER_DISPLAY_NAMES[provider]
        events.append(
            gateway_event(
                "check",
                f"{provider_name} provider package: installed",
            )
        )

    if gateway_api_key_present:
        events.append(gateway_event("check", "STACKMINT_GATEWAY_API_KEY: present"))
    else:
        events.extend(
            [
                gateway_event("info", "STACKMINT_GATEWAY_API_KEY is not set"),
                gateway_event(
                    "info",
                    "Running local wrapper behavior without workspace sync",
                ),
            ]
        )

    local_tool_policy = module.StackmintToolPolicy(
        permitted_tool_slugs={tool.name for tool in module.RAW_TOOLS},
        require_approval_for={"multiply_numbers"},
    )
    governed_tools = local_tool_policy.governed_tools(module.RAW_TOOLS)
    events.append(
        gateway_event(
            "policy",
            "Local tool policy configured",
            permitted_tools=[tool.name for tool in module.RAW_TOOLS],
            require_approval_for=sorted(local_tool_policy.require_approval_for),
            telemetry_security="redaction on",
            fail_open="enabled for gateway connectivity",
        )
    )

    agent = module.GovernedAgent(
        module.build_agent(governed_tools),
        api_key=os.getenv("STACKMINT_GATEWAY_API_KEY"),
        name="langchain_example",
        model=model_name,
        tools=module.RAW_TOOLS,
        permitted_tool_slugs=sorted(local_tool_policy.permitted_tool_slugs),
        require_approval_for=sorted(local_tool_policy.require_approval_for),
        sync_on_init=True,
    )

    prompt = "Run a short demonstration with the available tools."
    events.append(gateway_event("run", "Prompt sent to LangChain agent"))
    result = agent.invoke({"messages": [module.HumanMessage(content=prompt)]})
    result_message = _extract_langchain_result_message(result)
    result_type = (
        "fake_provider_response" if provider == "fake" else "provider_response"
    )
    result_title = (
        "Fake provider response" if provider == "fake" else "Provider response"
    )
    events.append(gateway_event("result", result_title))

    sync_error = getattr(agent.state, "last_sync_error", None)
    execution_error = getattr(agent.state, "last_execution_error", None)
    workspace_sync_attempted = gateway_api_key_present
    execution_record_attempted = gateway_api_key_present
    if gateway_api_key_present:
        if sync_error is None:
            events.append(
                gateway_event("record", "Agent config synced to Stackmint workspace")
            )
        else:
            events.append(
                gateway_event(
                    "info",
                    "Gateway sync unavailable; local wrapper behavior completed",
                    reason="gateway_unavailable",
                )
            )
        if execution_error is None:
            events.append(gateway_event("record", "Execution recorded"))
        else:
            events.append(
                gateway_event(
                    "info",
                    "Execution recording unavailable; local wrapper behavior completed",
                    reason="gateway_unavailable",
                )
            )
    else:
        events.append(gateway_event("record", "Example completed locally"))

    return {
        "ok": True,
        "command": "example langchain",
        "provider": provider,
        "model": model_name,
        "gateway_api_key_present": gateway_api_key_present,
        "workspace_sync_attempted": workspace_sync_attempted,
        "execution_record_attempted": execution_record_attempted,
        "events": _event_dicts(events),
        "policy": {
            "permitted_tools": [tool.name for tool in module.RAW_TOOLS],
            "require_approval_for": sorted(local_tool_policy.require_approval_for),
            "telemetry_security": "redaction on",
            "fail_open": "enabled for gateway connectivity",
        },
        "prompt": prompt,
        "result": {
            "type": result_type,
            "message": result_message,
        },
        "debug_messages": _langchain_debug_messages(result),
    }


def _print_langchain_example_payload(
    payload: dict[str, Any],
    args: argparse.Namespace,
    stdout: TextIO,
) -> None:
    previous_kind: str | None = None
    for item in payload["events"]:
        if previous_kind == "run" and item["kind"] == "result":
            print(file=stdout)
        elif previous_kind == "result" and item["kind"] == "record":
            print(file=stdout)
        gateway_event_item = GatewayEvent(
            kind=item["kind"],
            message=item["message"],
            details=item.get("details", {}),
        )
        _print_event(gateway_event_item, args, stdout)
        if item["kind"] == "policy":
            _print_langchain_policy_summary(payload["policy"], stdout)
        elif item["kind"] == "run":
            print(f"\"{payload['prompt']}\"", file=stdout)
        elif item["kind"] == "result":
            print(payload["result"]["message"], file=stdout)
        previous_kind = item["kind"]


def _print_langchain_policy_summary(
    policy: dict[str, Any],
    stdout: TextIO,
) -> None:
    approvals = policy["require_approval_for"] or ["none"]
    print(file=stdout)
    print(f"Permitted tools:      {', '.join(policy['permitted_tools'])}", file=stdout)
    print(f"Requires approval:    {', '.join(approvals)}", file=stdout)
    print(f"Telemetry security:   {policy['telemetry_security']}", file=stdout)
    print(f"Fail-open:            {policy['fail_open']}", file=stdout)
    print(file=stdout)


def _extract_langchain_result_message(result: Any) -> str:
    if isinstance(result, dict) and isinstance(result.get("messages"), list):
        messages = result["messages"]
        if messages:
            return str(getattr(messages[-1], "content", ""))
    return str(result)


def _langchain_debug_messages(result: Any) -> list[dict[str, Any]]:
    if not (isinstance(result, dict) and isinstance(result.get("messages"), list)):
        return []
    debug_messages: list[dict[str, Any]] = []
    for message in result["messages"]:
        item: dict[str, Any] = {
            "type": message.__class__.__name__,
            "content": str(getattr(message, "content", "")),
        }
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            item["tool_calls"] = tool_calls
        debug_messages.append(item)
    return debug_messages


def _print_langchain_debug_messages(
    debug_messages: list[dict[str, Any]],
    stdout: TextIO,
) -> None:
    if not debug_messages:
        return
    print(file=stdout)
    print("Debug messages:", file=stdout)
    for item in debug_messages:
        if "tool_calls" in item:
            print(f"{item['type']}: tool_calls={item['tool_calls']}", file=stdout)
        else:
            print(f"{item['type']}: {item['content']}", file=stdout)


def _events_for_langchain_example_error(message: str) -> list[GatewayEvent]:
    events = [gateway_event("error", message)]
    if message.startswith("Missing optional dependency for provider: "):
        provider = message.rsplit(": ", maxsplit=1)[-1]
        events.append(
            gateway_event(
                "info",
                f"Install with: uv sync --extra examples-{provider}",
            )
        )
    elif "_API_KEY is required" in message:
        events.append(
            gateway_event(
                "info",
                "Run with `STACKMINT_EXAMPLE_PROVIDER=fake` for the no-key local "
                "smoke test",
            )
        )
    elif "Unsupported STACKMINT_EXAMPLE_PROVIDER" in message:
        events.append(
            gateway_event(
                "info",
                "Set STACKMINT_EXAMPLE_PROVIDER to fake, openai, anthropic, or "
                "cerebras",
            )
        )
    return events


def _langchain_example_error_code(message: str) -> str:
    if message.startswith("Missing optional dependency for provider: "):
        return "missing_dependency"
    if "_API_KEY is required" in message:
        return "missing_provider_api_key"
    if "Unsupported STACKMINT_EXAMPLE_PROVIDER" in message:
        return "unsupported_provider"
    return "example_configuration_error"


def _langchain_provider_label(provider: str) -> str:
    labels = {
        "fake": "fake/local",
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "cerebras": "Cerebras",
    }
    return labels.get(provider, provider)


def _langchain_provider_import_name(provider: str) -> str | None:
    return {
        "openai": "langchain_openai",
        "anthropic": "langchain_anthropic",
        "cerebras": "langchain_cerebras",
    }.get(provider)


def _command_check(args: argparse.Namespace, stdout: TextIO) -> int:
    if args.run:
        for command in CHECK_COMMANDS:
            command_args = _command_args_for_check_run(command)
            _print_event(
                gateway_event("check", f"Running: {command}"),
                args,
                stdout,
            )
            result = subprocess.run(command_args, check=False)  # nosec B603
            if result.returncode != 0:
                _print_event(
                    gateway_event("error", f"Check failed: {command}"),
                    args,
                    stdout,
                )
                return result.returncode
        _print_event(gateway_event("result", "All checks passed"), args, stdout)
        return 0

    _print_event(
        gateway_event("check", "Standard local verification commands"),
        args,
        stdout,
    )
    for command in CHECK_COMMANDS:
        print(command, file=stdout)
        if command == "uv run detect-secrets scan --baseline .secrets.baseline":
            print(DETECT_SECRETS_BASELINE_NOTE, file=stdout)
    return 0


def _command_args_for_check_run(command: str) -> list[str]:
    command_args = shlex.split(command)
    if command_args == ["uv", "run", "twine", "check", "dist/*"]:
        dist_files = sorted(str(path) for path in Path("dist").glob("*"))
        return ["uv", "run", "twine", "check", *dist_files]
    return command_args


def _maybe_splash(
    command: str,
    args: argparse.Namespace,
    mode: str,
    stdout: TextIO,
) -> None:
    if should_show_splash(command, args):
        print_splash(mode, no_color=args.no_color, file=stdout)


def _write_env_file(path: Path, *, force: bool) -> GatewayEvent:
    if path.exists() and not force:
        return gateway_event(
            "info",
            f"No changes needed: {path} exists",
            path=str(path),
            changed=False,
        )
    path.write_text(ENV_SCAFFOLD, encoding="utf-8")
    return gateway_event("record", f"Wrote {path}", path=str(path), changed=True)


def _doctor_config_events(config: dict[str, Any]) -> list[GatewayEvent]:
    example_model = config["example_model"] or "unset"
    return [
        gateway_event(
            "check",
            "STACKMINT_GATEWAY_API_KEY: "
            f"{_presence(config['gateway_api_key_present'])}",
            present=config["gateway_api_key_present"],
        ),
        gateway_event(
            "check",
            f"STACKMINT_GATEWAY_BASE_URL: {config['gateway_base_url']}",
            base_url=config["gateway_base_url"],
        ),
        gateway_event(
            "check",
            f"STACKMINT_EXAMPLE_PROVIDER: {config['example_provider']}",
            provider=config["example_provider"],
        ),
        gateway_event(
            "check",
            f"STACKMINT_EXAMPLE_MODEL: {example_model}",
            configured=config["example_model"] is not None,
        ),
        gateway_event(
            "check",
            f"STACKMINT_MCP_READ_ONLY: {config['mcp_read_only']}",
        ),
        gateway_event(
            "check",
            "STACKMINT_MCP_REQUIRE_CONFIRMATION: "
            f"{config['mcp_require_confirmation']}",
        ),
        gateway_event(
            "check",
            f"STACKMINT_MCP_RECORD_PAYLOADS: {config['mcp_record_payloads']}",
        ),
    ]


def _doctor_detection_events(
    detections: list[DetectionResult],
) -> list[GatewayEvent]:
    events: list[GatewayEvent] = []
    for detection in detections:
        if detection.category == "provider":
            events.extend(_provider_detection_events(detection))
            continue

        events.append(
            gateway_event(
                "check",
                f"{detection.name}: {_installed_status(detection.installed)}",
                import_name=detection.import_name,
                installed=detection.installed,
            )
        )
        if detection.name == "MCP SDK" and not detection.installed:
            events.append(
                gateway_event(
                    "info",
                    "Run `uv sync --extra mcp` to enable the MCP governance server",
                )
            )
        elif detection.name == "LangChain" and not detection.installed:
            events.append(
                gateway_event(
                    "info",
                    "Run `uv sync --extra examples` to enable LangChain examples",
                )
            )
    return events


def _provider_detection_events(detection: DetectionResult) -> list[GatewayEvent]:
    events = [
        gateway_event(
            "check",
            f"{detection.name}: {_installed_status(detection.installed)}",
            import_name=detection.import_name,
            installed=detection.installed,
        )
    ]
    if detection.env_var is not None:
        events.append(
            gateway_event(
                "check",
                f"{detection.env_var}: {_presence(bool(detection.env_present))}",
                present=bool(detection.env_present),
            )
        )

    provider_name = detection.name.removesuffix(" provider package")
    if detection.installed and detection.env_present:
        return events
    if detection.installed and detection.env_var is not None:
        events.append(
            gateway_event(
                "info",
                f"Set {detection.env_var} to run {provider_name}-backed examples",
            )
        )
    elif detection.env_present:
        events.append(
            gateway_event(
                "info",
                f"Run `{detection.package_hint}` to enable {provider_name} examples",
            )
        )
    else:
        events.append(
            gateway_event(
                "info",
                f"Run `{detection.package_hint}` and set {detection.env_var} "
                f"to enable {provider_name} examples",
            )
        )
    return events


def _detection_status(
    detections: list[DetectionResult],
    name: str,
) -> str:
    for detection in detections:
        if detection.name == name:
            return _installed_status(detection.installed)
    return "missing"


def _installed_status(installed: bool) -> str:
    return "installed" if installed else "missing"


def _gateway_check() -> list[GatewayEvent]:
    try:
        from stackmint_gateway.core import CoreStackmintGateway

        CoreStackmintGateway(os.environ["STACKMINT_GATEWAY_API_KEY"]).get_me()
    except Exception:
        return [
            gateway_event(
                "check",
                "Gateway connectivity: unavailable",
                reason="gateway_unavailable",
            ),
            gateway_event(
                "info",
                "Local demos still work; fail-open applies to connectivity failures",
            ),
        ]
    return [gateway_event("check", "Gateway connectivity: ok")]


def _run_mcp_server() -> int:
    from stackmint_gateway.mcp_server import main as mcp_main

    result = mcp_main()
    return int(result) if result is not None else 0


def _package_version() -> str:
    try:
        return importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0a1"


def _display_version(version: str) -> str:
    if version == "0.1.0a1":
        return "0.1.0-alpha"
    return version


def _capability_status(module_name: str) -> str:
    try:
        return "installed" if importlib.util.find_spec(module_name) else "missing"
    except (ImportError, ModuleNotFoundError, ValueError):
        return "missing"


def _presence(value: bool) -> str:
    return "present" if value else "missing"


def _isatty(stream: TextIO) -> bool:
    checker = getattr(stream, "isatty", None)
    return bool(checker and checker())


def _print_event(
    gateway_event_item: GatewayEvent,
    args: argparse.Namespace,
    stdout: TextIO,
) -> None:
    print(
        event_to_line(gateway_event_item, color=should_use_color(args)),
        file=stdout,
    )


def _print_events(
    events: list[GatewayEvent],
    args: argparse.Namespace,
    stdout: TextIO,
) -> None:
    for gateway_event_item in events:
        if args.quiet and gateway_event_item.kind != "result":
            continue
        _print_event(gateway_event_item, args, stdout)


def _event_dicts(events: list[GatewayEvent]) -> list[dict[str, Any]]:
    return [event_to_dict(gateway_event_item) for gateway_event_item in events]


def _write_json(payload: dict[str, Any], stdout: TextIO) -> None:
    json.dump(payload, stdout, indent=2, sort_keys=True)
    print(file=stdout)


def _print_demo_recap(summary: dict[str, int], stdout: TextIO) -> None:
    print(file=stdout)
    print(
        render_table(
            [
                ("Allowed tool executions", summary["allowed_tool_executions"]),
                ("Approval-gated tools", summary["approval_gated_tools"]),
                ("Blocked tool attempts", summary["blocked_tool_attempts"]),
                ("Budget blocks", summary["budget_blocks"]),
                (
                    "Redacted telemetry records",
                    summary["redacted_telemetry_records"],
                ),
            ],
            title="Governance recap:",
            ascii=not should_use_unicode_table(stdout),
        ),
        file=stdout,
    )
    print(file=stdout)
    print("Managed control-plane path:", file=stdout)
    print("  centralized policy", file=stdout)
    print("  budget authorization", file=stdout)
    print("  approval workflows", file=stdout)
    print("  audit trails", file=stdout)
    print("  SSO/SAML", file=stdout)
    print(file=stdout)
    print("Contact: hello@stackmint.ai", file=stdout)


def _print_doctor_recap(readiness: dict[str, str], stdout: TextIO) -> None:
    print(file=stdout)
    print("Local readiness:", file=stdout)
    _print_recap_row("local demo", readiness["local_demo"], stdout)
    _print_recap_row(
        "fake LangChain example",
        readiness["fake_langchain_example"],
        stdout,
    )
    _print_recap_row(
        "provider-backed examples",
        readiness["provider_backed_langchain_examples"],
        stdout,
    )
    _print_recap_row(
        "MCP governance server",
        readiness["mcp_governance_server"],
        stdout,
    )
    _print_recap_row("OpenAI example", readiness["openai_example"], stdout)
    _print_recap_row("Anthropic example", readiness["anthropic_example"], stdout)
    _print_recap_row("Cerebras example", readiness["cerebras_example"], stdout)

    print(file=stdout)
    print("Governance coverage:", file=stdout)
    _print_recap_row("local guardrails", readiness["local_guardrails"], stdout)
    _print_recap_row("safe telemetry", readiness["safe_telemetry"], stdout)
    _print_recap_row("remote policy sync", readiness["remote_policy_sync"], stdout)
    _print_recap_row(
        "managed control plane",
        readiness["managed_control_plane"],
        stdout,
    )


def _print_recap_row(label: str, value: str, stdout: TextIO) -> None:
    print(f"  {label + ':':<25} {value}", file=stdout)


def _emit_error(
    message: str,
    *,
    args: argparse.Namespace | None,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    if args is not None and getattr(args, "json", False):
        _write_json(
            {
                "ok": False,
                "command": getattr(args, "command", None) or "stackmint",
                "error": {"code": "cli_error", "message": message},
                "events": [
                    event_to_dict(gateway_event("error", message)),
                ],
            },
            stdout,
        )
        return
    if args is None:
        print(f"[ERROR] {message}", file=stderr)
        return
    _print_event(gateway_event("error", message), args, stderr)


if __name__ == "__main__":
    raise SystemExit(main())
