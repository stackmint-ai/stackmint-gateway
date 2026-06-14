from __future__ import annotations

import importlib
import io
import json
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest

from stackmint_gateway import cli, terminal
from stackmint_gateway import demo as demo_module
from stackmint_gateway import doctor as doctor_module
from stackmint_gateway.demo import build_demo_events
from stackmint_gateway.events import event, event_to_line


def run_cli(args: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setenv("STACKMINT_NO_SPLASH", "1")
    code = cli.main(args, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_stackmint_command_shows_command_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    code, stdout, stderr = run_cli([], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "Usage:" in stdout
    assert "Core commands:" in stdout
    assert "Common workflows:" in stdout
    assert "stackmint demo" in stdout
    assert "stackmint doctor" in stdout
    assert "stackmint example langchain" in stdout


def test_help_command_shows_command_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    code, stdout, stderr = run_cli(["help"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "Usage:" in stdout
    assert "Core commands:" in stdout
    assert "Common workflows:" in stdout
    assert "stackmint demo" in stdout
    assert "help [command]" in stdout
    assert stdout.count("Stackmint Gateway") == 0


def test_help_command_can_show_header_once(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.delenv("STACKMINT_NO_SPLASH", raising=False)

    code = cli.main(["help"], stdout=stdout, stderr=stderr)

    output = stdout.getvalue()
    assert code == 0
    assert stderr.getvalue() == ""
    assert output.count("Stackmint Gateway") == 1
    assert "Runtime governance for AI agents" in output


def test_help_demo_shows_demo_help(monkeypatch: pytest.MonkeyPatch) -> None:
    code, stdout, stderr = run_cli(["help", "demo"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "Stackmint demo" in stdout
    assert "Run the local no-key governance demo" in stdout
    assert "Usage:" in stdout
    assert "stackmint demo [options]" in stdout


def test_help_mcp_shows_mcp_help(monkeypatch: pytest.MonkeyPatch) -> None:
    code, stdout, stderr = run_cli(["help", "mcp"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "Stackmint MCP" in stdout
    assert "--preview" in stdout
    assert "Start or preview the MCP governance server" in stdout
    assert "protocol-clean" in stdout


def test_help_doctor_mentions_readiness_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, stdout, stderr = run_cli(["help", "doctor"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "Check local readiness for Stackmint Gateway" in stdout
    assert "Stackmint environment variables" in stdout
    assert "Optional model-provider packages" in stdout


def test_help_example_langchain_is_custom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, stdout, stderr = run_cli(["help", "example", "langchain"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "Stackmint example langchain" in stdout
    assert "provider-aware LangChain Runnable wrapper smoke test" in stdout
    assert "--debug" in stdout


def test_root_help_flag_uses_curated_help(monkeypatch: pytest.MonkeyPatch) -> None:
    code, stdout, stderr = run_cli(["--help"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "Core commands:" in stdout
    assert "Common workflows:" in stdout
    assert "usage: stackmint" not in stdout


def test_demo_help_flag_uses_curated_help(monkeypatch: pytest.MonkeyPatch) -> None:
    code, stdout, stderr = run_cli(["demo", "--help"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "Stackmint demo" in stdout
    assert "Run the local no-key governance demo" in stdout
    assert "usage: stackmint demo" not in stdout


def test_no_color_help_contains_no_ansi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    code, stdout, stderr = run_cli(["help"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "\x1b[" not in stdout


def test_invalid_help_topic_is_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, stdout, stderr = run_cli(["help", "unknown"], monkeypatch)

    assert code == 2
    assert stdout == ""
    assert "[ERROR] Unknown help topic: unknown" in stderr
    assert "[INFO] Run `stackmint help` to see commands" in stderr
    assert "Traceback" not in stderr


def test_demo_no_splash_runs_without_provider_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_var in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CEREBRAS_API_KEY",
        "STACKMINT_GATEWAY_API_KEY",
    ):
        monkeypatch.delenv(env_var, raising=False)

    code, stdout, stderr = run_cli(["demo", "--no-splash"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "████" not in stdout
    assert "[RUN]" in stdout
    assert "[POLICY]" in stdout
    assert "[APPROVAL]" in stdout
    assert "[BLOCK]" in stdout
    assert "[SECURITY]" in stdout
    assert "[RECORD]" in stdout
    assert "[RESULT]" in stdout
    assert "No network calls are made in local demo mode" in stdout
    assert "Observability shows what happened." in stdout
    assert "Reason: tool_not_permitted" in stdout
    assert "[RESULT] 24 + 18 = 42\n\n[RUN] User request: Create a client note" in stdout
    assert "Governance recap:" in stdout
    assert "| Metric                     | Count |" in stdout
    assert "| Allowed tool executions    |   3   |" in stdout
    assert "| Approval-gated tools       |   1   |" in stdout
    assert "| Blocked tool attempts      |   1   |" in stdout
    assert "| Budget blocks              |   1   |" in stdout
    assert "| Redacted telemetry records |   1   |" in stdout
    assert "Managed control-plane path:" in stdout
    assert "Contact: hello@stackmint.ai" in stdout
    assert "dev server starting" not in stdout
    assert "sk-proj-demo-secret" not in stdout
    assert "person@example.com" not in stdout


def test_build_demo_events_local_is_deterministic() -> None:
    payload = build_demo_events(mode="local", provider="fake")

    assert payload["ok"] is True
    assert payload["command"] == "demo"
    assert payload["mode"] == "local"
    assert payload["provider"] == "fake"
    assert payload["summary"] == {
        "allowed_tool_executions": 3,
        "approval_gated_tools": 1,
        "blocked_tool_attempts": 1,
        "budget_blocks": 1,
        "redacted_telemetry_records": 1,
    }
    event_tags = {item["tag"] for item in payload["events"]}
    assert {"ALLOW", "APPROVAL", "BLOCK", "SECURITY", "RECORD", "RESULT"} <= event_tags


def test_build_demo_events_rejects_unsupported_provider() -> None:
    with pytest.raises(RuntimeError, match="Unsupported demo provider: openai"):
        build_demo_events(mode="local", provider="openai")


def test_demo_json_outputs_parseable_json_and_no_splash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, stdout, stderr = run_cli(["demo", "--json"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert stdout.lstrip().startswith("{")
    payload = json.loads(stdout)
    assert payload["ok"] is True
    assert payload["command"] == "demo"
    assert payload["summary"]["allowed_tool_executions"] == 3
    assert payload["summary"]["approval_gated_tools"] == 1
    assert payload["summary"]["blocked_tool_attempts"] == 1
    assert payload["summary"]["budget_blocks"] == 1
    assert payload["summary"]["redacted_telemetry_records"] == 1
    assert payload["events"][0]["kind"] == "boot"
    assert payload["events"][0]["tag"] == "BOOT"
    assert any(
        "Observability shows what happened." in item["message"]
        for item in payload["events"]
    )
    assert any(
        item["message"] == "No network calls are made in local demo mode"
        for item in payload["events"]
    )
    assert "████" not in stdout
    assert "\x1b[" not in stdout
    assert "Governance recap:" not in stdout
    assert "┌" not in stdout
    assert "│" not in stdout
    assert "sk-proj-demo-secret" not in stdout
    assert "person@example.com" not in stdout


def test_demo_no_delay_completes_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = TtyStringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(
        demo_module.time,
        "sleep",
        lambda seconds: pytest.fail(f"unexpected sleep: {seconds}"),
    )

    code = cli.main(["demo", "--no-delay", "--no-splash"], stdout=stdout, stderr=stderr)

    assert code == 0
    assert stderr.getvalue() == ""
    assert "[RESULT] Demo complete" in stdout.getvalue()


def test_demo_json_has_no_delay_even_for_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = TtyStringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(
        demo_module.time,
        "sleep",
        lambda seconds: pytest.fail(f"unexpected sleep: {seconds}"),
    )

    code = cli.main(["demo", "--json"], stdout=stdout, stderr=stderr)

    assert code == 0
    assert stderr.getvalue() == ""
    payload = json.loads(stdout.getvalue())
    assert payload["ok"] is True


def test_demo_env_no_delay_disables_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = TtyStringIO()
    stderr = io.StringIO()
    monkeypatch.setenv("STACKMINT_NO_DELAY", "1")
    monkeypatch.setattr(
        demo_module.time,
        "sleep",
        lambda seconds: pytest.fail(f"unexpected sleep: {seconds}"),
    )

    code = cli.main(["demo", "--no-splash"], stdout=stdout, stderr=stderr)

    assert code == 0
    assert stderr.getvalue() == ""


def test_demo_ci_disables_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = TtyStringIO()
    stderr = io.StringIO()
    monkeypatch.setenv("CI", "1")
    monkeypatch.setattr(
        demo_module.time,
        "sleep",
        lambda seconds: pytest.fail(f"unexpected sleep: {seconds}"),
    )

    code = cli.main(["demo", "--no-splash"], stdout=stdout, stderr=stderr)

    assert code == 0
    assert stderr.getvalue() == ""


def test_demo_human_tty_uses_packet_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = TtyStringIO()
    stderr = io.StringIO()
    sleeps: list[float] = []
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("STACKMINT_NO_DELAY", raising=False)
    monkeypatch.setattr(demo_module.time, "sleep", sleeps.append)

    code = cli.main(["demo", "--no-splash"], stdout=stdout, stderr=stderr)

    assert code == 0
    assert stderr.getvalue() == ""
    assert len(sleeps) >= 7
    assert len(sleeps) < stdout.getvalue().count("\n")
    assert max(sleeps) > min(sleeps)


def test_demo_speed_multipliers() -> None:
    normal = demo_module.demo_delay_seconds(1.0, "normal")

    assert demo_module.demo_delay_seconds(1.0, "cinematic") > normal
    assert demo_module.demo_delay_seconds(1.0, "fast") < normal


def test_demo_speed_env_and_cli_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STACKMINT_DEMO_SPEED", "cinematic")

    assert demo_module.resolve_demo_speed(None) == "cinematic"
    assert demo_module.resolve_demo_speed("fast") == "fast"


def test_demo_clear_json_does_not_emit_clear_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, stdout, stderr = run_cli(["demo", "--clear", "--json"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "\033[2J\033[H" not in stdout
    assert json.loads(stdout)["ok"] is True


def test_demo_clear_human_tty_emits_clear_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = TtyStringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(demo_module.time, "sleep", lambda seconds: None)

    code = cli.main(["demo", "--clear", "--no-delay"], stdout=stdout, stderr=stderr)

    assert code == 0
    assert stderr.getvalue() == ""
    assert stdout.getvalue().startswith("\033[2J\033[H")


def test_demo_mock_control_plane_json_includes_remote_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, stdout, stderr = run_cli(
        ["demo", "--mode", "mock-control-plane", "--json"],
        monkeypatch,
    )

    assert code == 0
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["ok"] is True
    assert payload["mode"] == "mock-control-plane"
    assert payload["summary"]["allowed_tool_executions"] == 1
    assert payload["summary"]["remote_authorization_checks"] == 2
    assert payload["summary"]["remote_budget_reservations"] == 1
    assert payload["summary"]["remote_approval_requests"] == 1
    assert payload["summary"]["tool_events_recorded"] == 1
    assert "Running with a mock control plane. No network calls are made." in stdout
    assert "\x1b[" not in stdout


def test_demo_unsupported_provider_returns_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, stdout, stderr = run_cli(
        ["demo", "--provider", "openai", "--no-splash"],
        monkeypatch,
    )

    assert code == 2
    assert stdout == ""
    assert "[ERROR] Unsupported demo provider: openai" in stderr
    assert "Use `stackmint example langchain` for provider-backed examples" in stderr
    assert "Traceback" not in stderr


def test_doctor_does_not_print_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = "do-not-print-this-value"
    monkeypatch.setenv("STACKMINT_GATEWAY_API_KEY", marker)
    code, stdout, stderr = run_cli(["doctor", "--no-splash"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "STACKMINT_GATEWAY_API_KEY: present" in stdout
    assert "[RESULT] Doctor complete" in stdout
    assert "Local readiness:" in stdout
    assert marker not in stdout


def test_module_available_uses_find_spec() -> None:
    assert doctor_module.module_available("json") is True
    assert doctor_module.module_available("stackmint_gateway_missing_package") is False


def test_doctor_detector_reports_missing_optionals_without_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_module_available(import_name: str) -> bool:
        return import_name == "langchain_core"

    monkeypatch.setattr(doctor_module, "module_available", fake_module_available)
    for env_var in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CEREBRAS_API_KEY",
        "STACKMINT_GATEWAY_API_KEY",
    ):
        monkeypatch.delenv(env_var, raising=False)

    code, stdout, stderr = run_cli(["doctor", "--no-splash"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "[CHECK] LangChain Core: installed" in stdout
    assert "[CHECK] LangChain: missing" in stdout
    assert "[CHECK] MCP SDK: missing" in stdout
    assert "[CHECK] OpenAI provider package: missing" in stdout
    assert "[CHECK] OPENAI_API_KEY: missing" in stdout
    assert "install package + set OPENAI_API_KEY" in stdout
    assert "fake LangChain example:" in stdout
    assert "provider-backed examples:" in stdout
    assert "optional_dependencies_missing" in stdout


def test_doctor_provider_env_presence_does_not_print_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "openai-key-should-not-print"
    monkeypatch.setenv("OPENAI_API_KEY", marker)
    monkeypatch.setattr(doctor_module, "module_available", lambda import_name: False)

    code, stdout, stderr = run_cli(["doctor", "--no-splash"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "[CHECK] OPENAI_API_KEY: present" in stdout
    assert marker not in stdout


def test_init_writes_env_example_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    code, stdout, stderr = run_cli(["init", "--no-splash"], monkeypatch)

    assert code == 0
    assert stderr == ""
    env_example = tmp_path / ".env.example"
    assert env_example.exists()
    contents = env_example.read_text()
    assert "STACKMINT_EXAMPLE_PROVIDER=fake" in contents
    assert "OPENAI_API_KEY=" in contents
    assert "[RECORD] Wrote .env.example" in stdout


def test_init_write_env_does_not_overwrite_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=true\n")

    code, stdout, stderr = run_cli(
        ["init", "--write-env", "--no-splash"],
        monkeypatch,
    )

    assert code == 0
    assert stderr == ""
    assert env_file.read_text() == "EXISTING=true\n"
    assert "No changes needed: .env exists" in stdout


def test_mcp_command_does_not_print_splash_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_run_mcp_server", lambda: 0)
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = cli.main(["mcp"], stdout=stdout, stderr=stderr)

    assert code == 0
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""


def test_mcp_missing_extra_error_is_singular(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_missing_mcp() -> int:
        raise RuntimeError(
            "Install MCP server dependencies with `uv sync --extra mcp`."
        )

    monkeypatch.setattr(cli, "_run_mcp_server", raise_missing_mcp)
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = cli.main(["mcp"], stdout=stdout, stderr=stderr)

    assert code == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        "[ERROR] MCP support is not installed.\n"
        "[INFO] Install with: uv sync --extra mcp\n"
    )
    assert "Traceback" not in stderr.getvalue()


def test_mcp_preview_prints_governance_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, stdout, stderr = run_cli(["mcp", "--preview", "--no-splash"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "[INFO] Stackmint Governance MCP Server preview" in stdout
    assert "stackmint_authorize_execution" in stdout
    assert "stackmint://agent/policy" in stdout


def test_stackmint_no_splash_env_suppresses_splash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STACKMINT_NO_SPLASH", "1")
    args = SimpleNamespace(
        no_splash=False,
        quiet=False,
        json=False,
        preview=False,
        _stdout_is_tty=True,
    )

    assert terminal.should_show_splash("demo", args) is False


def test_no_color_uses_plain_text_splash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    stdout = io.StringIO()

    terminal.print_splash("full", force=True, file=stdout)

    assert "\x1b[" not in stdout.getvalue()
    assert "████" in stdout.getvalue()


def test_gateway_event_line_renders_standard_tag() -> None:
    line = event_to_line(event("allow", "Tool executed: add_numbers"), color=False)

    assert line == "[ALLOW] Tool executed: add_numbers"


def test_gateway_event_line_can_render_color() -> None:
    line = event_to_line(event("block", "Tool blocked"), color=True)

    assert "\x1b[" in line
    assert "[BLOCK]" in line


def test_render_table_outputs_unicode_table() -> None:
    table = terminal.render_table(
        [("Allowed tool executions", 3)],
        title="Governance recap:",
    )

    assert table.startswith("Governance recap:\n┌")
    assert "│ Metric                  │ Count │" in table
    assert "│ Allowed tool executions │   3   │" in table
    assert "└" in table


def test_render_table_ascii_fallback() -> None:
    table = terminal.render_table(
        [("Redacted telemetry records", 1)],
        title="Governance recap:",
        ascii=True,
    )

    assert table.startswith("Governance recap:\n+")
    assert "| Redacted telemetry records |   1   |" in table
    assert "┌" not in table


def test_color_suppressed_when_no_color_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    args = SimpleNamespace(no_color=False, json=False, _stdout_is_tty=True)

    assert terminal.should_use_color(args) is False


def test_color_suppressed_when_term_is_dumb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    args = SimpleNamespace(no_color=False, json=False, _stdout_is_tty=True)

    assert terminal.should_use_color(args) is False


def test_doctor_json_is_structured_and_does_not_expose_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "do-not-print-this-value"
    monkeypatch.setenv("STACKMINT_GATEWAY_API_KEY", marker)
    code, stdout, stderr = run_cli(["doctor", "--json"], monkeypatch)

    assert code == 0
    assert stderr == ""
    payload = json.loads(stdout)
    serialized = json.dumps(payload)
    assert payload["ok"] is True
    assert payload["command"] == "doctor"
    assert payload["events"][0]["kind"] == "check"
    assert payload["detections"]
    assert payload["readiness"]["local_demo"] == "ready"
    assert payload["readiness"]["fake_langchain_example"] == "ready"
    assert (
        "provider_backed_langchain_examples"
        in payload["readiness"]
    )
    assert "remote_policy_sync" in payload["readiness"]
    assert payload["summary"]["langchain_core"] in {"installed", "missing"}
    assert payload["summary"]["langchain_package"] in {"installed", "missing"}
    assert "langchain_examples" not in payload["summary"]
    assert "mcp_extra" not in payload["summary"]
    assert marker not in serialized
    assert "\x1b[" not in stdout


def test_version_json_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    code, stdout, stderr = run_cli(["version", "--json"], monkeypatch)

    assert code == 0
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["ok"] is True
    assert payload["command"] == "version"
    assert payload["package"] == "stackmint-gateway"
    assert "langchain_core" in payload["capabilities"]
    assert "langchain_package" in payload["capabilities"]
    assert "langchain_examples" not in payload["capabilities"]


def test_mcp_preview_json_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    code, stdout, stderr = run_cli(
        ["mcp", "--preview", "--json"],
        monkeypatch,
    )

    assert code == 0
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["ok"] is True
    assert payload["command"] == "mcp_preview"
    assert "stackmint_get_agent_policy" in payload["tools"]
    assert "\x1b[" not in stdout


def test_missing_example_dependency_is_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import_module = importlib.import_module

    def guarded_import_module(name: str, *args, **kwargs):
        if name == "examples.langchain_example":
            raise ImportError(name)
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(cli.importlib, "import_module", guarded_import_module)
    code, stdout, stderr = run_cli(["example", "langchain"], monkeypatch)

    assert code == 1
    assert stdout == ""
    assert "[ERROR] LangChain example dependencies are missing" in stderr
    assert "[INFO] Run: uv sync --extra examples" in stderr
    assert "Traceback" not in stderr


def test_langchain_example_default_output_is_polished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_var in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CEREBRAS_API_KEY",
        "STACKMINT_GATEWAY_API_KEY",
        "STACKMINT_EXAMPLE_PROVIDER",
        "STACKMINT_EXAMPLE_MODEL",
    ):
        monkeypatch.delenv(env_var, raising=False)

    code, stdout, stderr = run_cli(["example", "langchain"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "[BOOT] LangChain example started" in stdout
    assert "[INFO] Provider: fake/local" in stdout
    assert "[INFO] Model: stackmint-fake-chat" in stdout
    assert "[INFO] No model-provider API key required" in stdout
    assert "[POLICY] Local tool policy configured" in stdout
    assert (
        "Permitted tools:      add_numbers, multiply_numbers, get_current_time"
        in stdout
    )
    assert "Requires approval:    multiply_numbers" in stdout
    assert "[RUN] Prompt sent to LangChain agent" in stdout
    assert '"Run a short demonstration with the available tools."' in stdout
    assert "[RESULT] Fake provider response" in stdout
    assert "[RECORD] Example completed locally" in stdout
    assert "HumanMessage:" not in stdout
    assert "AIMessage:" not in stdout
    assert "sk-" not in stdout


def test_langchain_example_debug_can_show_raw_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STACKMINT_EXAMPLE_PROVIDER", raising=False)

    code, stdout, stderr = run_cli(["example", "langchain", "--debug"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "Debug messages:" in stdout
    assert "HumanMessage:" in stdout
    assert "AIMessage:" in stdout


def test_langchain_example_json_is_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_var in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CEREBRAS_API_KEY",
        "STACKMINT_GATEWAY_API_KEY",
        "STACKMINT_EXAMPLE_PROVIDER",
        "STACKMINT_EXAMPLE_MODEL",
    ):
        monkeypatch.delenv(env_var, raising=False)

    code, stdout, stderr = run_cli(["example", "langchain", "--json"], monkeypatch)

    assert code == 0
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["ok"] is True
    assert payload["command"] == "example langchain"
    assert payload["provider"] == "fake"
    assert payload["model"] == "stackmint-fake-chat"
    assert payload["gateway_api_key_present"] is False
    assert payload["workspace_sync_attempted"] is False
    assert payload["result"]["type"] == "fake_provider_response"
    assert payload["events"][0]["message"] == "LangChain example started"
    assert "debug_messages" not in payload
    assert "Stackmint Gateway" not in stdout
    assert "\x1b[" not in stdout
    assert "HumanMessage:" not in stdout
    assert "AIMessage:" not in stdout


def test_langchain_example_fake_requires_no_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_var in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CEREBRAS_API_KEY",
        "STACKMINT_GATEWAY_API_KEY",
    ):
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("STACKMINT_EXAMPLE_PROVIDER", "fake")

    code, stdout, stderr = run_cli(["example", "langchain"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "No model-provider API key required" in stdout


def test_langchain_example_missing_real_provider_key_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STACKMINT_EXAMPLE_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    code, stdout, stderr = run_cli(
        ["example", "langchain", "--no-splash"],
        monkeypatch,
    )

    assert code == 1
    assert stdout == ""
    assert "[ERROR] OPENAI_API_KEY is required for the OpenAI example" in stderr
    assert (
        "[INFO] Run with `STACKMINT_EXAMPLE_PROVIDER=fake` for the no-key local "
        "smoke test"
    ) in stderr
    assert "Traceback" not in stderr


def test_langchain_example_missing_optional_dependency_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STACKMINT_EXAMPLE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "present-but-not-printed")
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None)

    code, stdout, stderr = run_cli(
        ["example", "langchain", "--no-splash"],
        monkeypatch,
    )

    assert code == 1
    assert stdout == ""
    assert "[ERROR] Missing optional dependency for provider: openai" in stderr
    assert "[INFO] Install with: uv sync --extra examples-openai" in stderr
    assert "present-but-not-printed" not in stderr
    assert "Traceback" not in stderr


def test_missing_splash_assets_fall_back_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terminal, "_read_asset", lambda filename: None)
    stdout = io.StringIO()

    terminal.print_splash("full", force=True, file=stdout)

    assert "Stackmint Gateway" in stdout.getvalue()


def test_package_import_exposes_cli_entrypoint() -> None:
    from stackmint_gateway.cli import main

    assert callable(main)


def test_pyproject_declares_stackmint_scripts() -> None:
    pyproject = Path("pyproject.toml").read_text()

    assert 'stackmint = "stackmint_gateway.cli:main"' in pyproject
    assert 'stackmint-gateway = "stackmint_gateway.cli:main"' in pyproject


def test_splash_assets_are_packaged_resources() -> None:
    asset_root = resources.files("stackmint_gateway.assets")

    assert asset_root.joinpath("stackmint_terminal_splash.txt").is_file()
    assert asset_root.joinpath("stackmint_terminal_splash.ansi").is_file()
    assert asset_root.joinpath("stackmint_terminal_splash.cast").is_file()
    assert "dev server starting" not in asset_root.joinpath(
        "stackmint_terminal_splash.ansi",
    ).read_text()
    assert "dev server starting" not in asset_root.joinpath(
        "stackmint_terminal_splash.cast",
    ).read_text()


def test_readme_documents_cli_commands() -> None:
    readme = Path("README.md").read_text()

    assert "## CLI Commands" in readme
    assert "stackmint demo" in readme
    assert "stackmint mcp --preview" in readme
    assert "stackmint example langchain --json" in readme
    assert "stackmint example langchain --debug" in readme
    assert "STACKMINT_NO_SPLASH=1" in readme
    assert "### Terminal output standard" in readme
    assert "### Environment and provider detection" in readme
    assert "## Governance Demo" in readme
    assert "No OpenAI, Anthropic, Cerebras, or Stackmint API key" in readme
    assert "stackmint demo --mode mock-control-plane" in readme
    assert "stackmint demo --clear --speed cinematic" in readme
    assert "STACKMINT_NO_DELAY=1" in readme
    assert "STACKMINT_DEMO_SPEED=cinematic" in readme
    assert "not a full SBOM scanner" in readme
    assert "governance recap table" in readme
    assert "structured data" in readme
    assert "provider-aware smoke test" in readme
    assert "`--json`" in readme


def test_check_print_includes_release_verification_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, stdout, stderr = run_cli(["check", "--print"], monkeypatch)

    assert code == 0
    assert stderr == ""
    assert "uv run stackmint demo --no-splash --no-delay" in stdout
    assert "uv run stackmint doctor --json" in stdout
    assert "uv run stackmint help" in stdout
    assert "uv run python -m build" in stdout
    assert "uv run twine check dist/*" in stdout


def test_demo_help_does_not_add_live_or_dashboard_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = monkeypatch
    cli_source = Path("stackmint_gateway/cli.py").read_text()
    demo_source = Path("stackmint_gateway/demo.py").read_text()

    assert "--live" not in cli_source
    assert "--dashboard" not in cli_source
    assert "--live" not in demo_source
    assert "--dashboard" not in demo_source
