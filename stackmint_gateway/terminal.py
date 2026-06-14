from __future__ import annotations

import os
import sys
from importlib import resources
from typing import Literal, TextIO

SplashMode = Literal["full", "compact"]
TableRow = tuple[str, str | int | float]

_ASSET_PACKAGE = "stackmint_gateway.assets"
_SPLASH_TXT = "stackmint_terminal_splash.txt"
_SPLASH_ANSI = "stackmint_terminal_splash.ansi"
_FALLBACK_COMPACT = "Stackmint Gateway\nRuntime governance for AI agents"
_FALLBACK_FULL = f"{_FALLBACK_COMPACT}\n"


def should_show_splash(command: str, args: object) -> bool:
    if command == "mcp" and not getattr(args, "preview", False):
        return False
    if command in {"version", "check"}:
        return False
    if getattr(args, "no_splash", False):
        return False
    if getattr(args, "quiet", False):
        return False
    if getattr(args, "json", False):
        return False
    if os.getenv("STACKMINT_NO_SPLASH") == "1":
        return False
    stdout_is_tty = getattr(args, "_stdout_is_tty", sys.stdout.isatty())
    return bool(stdout_is_tty)


def should_use_color(args: object | None = None) -> bool:
    if args is not None:
        if getattr(args, "no_color", False):
            return False
        if getattr(args, "json", False):
            return False
        stdout_is_tty = getattr(args, "_stdout_is_tty", sys.stdout.isatty())
    else:
        stdout_is_tty = sys.stdout.isatty()
    if os.getenv("NO_COLOR") is not None:
        return False
    if os.getenv("TERM", "").lower() == "dumb":
        return False
    return bool(stdout_is_tty)


def print_splash(
    mode: SplashMode = "full",
    *,
    no_color: bool = False,
    force: bool = False,
    file: TextIO | None = None,
) -> None:
    stream = file or sys.stdout
    text = _select_splash_text(mode, no_color=no_color, force=force, stream=stream)
    print(text.rstrip(), file=stream)
    print(file=stream)


def render_table(
    rows: list[TableRow],
    *,
    title: str | None = None,
    ascii: bool = False,
) -> str:
    label_header = "Metric"
    value_header = "Count"
    label_width = max(
        [len(label_header), *(len(str(label)) for label, _value in rows)],
        default=len(label_header),
    )
    value_width = max(
        [len(value_header), *(len(str(value)) for _label, value in rows)],
        default=len(value_header),
    )

    if ascii:
        chars = {
            "top_left": "+",
            "top_mid": "+",
            "top_right": "+",
            "mid_left": "+",
            "mid_mid": "+",
            "mid_right": "+",
            "bottom_left": "+",
            "bottom_mid": "+",
            "bottom_right": "+",
            "horizontal": "-",
            "vertical": "|",
        }
    else:
        chars = {
            "top_left": "┌",
            "top_mid": "┬",
            "top_right": "┐",
            "mid_left": "├",
            "mid_mid": "┼",
            "mid_right": "┤",
            "bottom_left": "└",
            "bottom_mid": "┴",
            "bottom_right": "┘",
            "horizontal": "─",
            "vertical": "│",
        }

    label_border = chars["horizontal"] * (label_width + 2)
    value_border = chars["horizontal"] * (value_width + 2)
    lines: list[str] = []
    if title:
        lines.append(title)
    lines.append(
        f"{chars['top_left']}{label_border}{chars['top_mid']}"
        f"{value_border}{chars['top_right']}"
    )
    lines.append(
        _table_row(label_header, value_header, label_width, value_width, chars)
    )
    lines.append(
        f"{chars['mid_left']}{label_border}{chars['mid_mid']}"
        f"{value_border}{chars['mid_right']}"
    )
    for label, value in rows:
        lines.append(
            _table_row(str(label), str(value), label_width, value_width, chars)
        )
    lines.append(
        f"{chars['bottom_left']}{label_border}{chars['bottom_mid']}"
        f"{value_border}{chars['bottom_right']}"
    )
    return "\n".join(lines)


def should_use_unicode_table(stream: TextIO | None = None) -> bool:
    if os.getenv("STACKMINT_ASCII_TABLE") == "1":
        return False
    if os.getenv("TERM", "").lower() == "dumb":
        return False
    return _isatty(stream or sys.stdout)


def _table_row(
    label: str,
    value: str,
    label_width: int,
    value_width: int,
    chars: dict[str, str],
) -> str:
    centered_value = value.center(value_width)
    return (
        f"{chars['vertical']} {label:<{label_width}} {chars['vertical']} "
        f"{centered_value} {chars['vertical']}"
    )


def _select_splash_text(
    mode: SplashMode,
    *,
    no_color: bool,
    force: bool,
    stream: TextIO,
) -> str:
    if mode == "compact":
        return _FALLBACK_COMPACT

    use_color = _color_allowed(no_color) and (force or _isatty(stream))
    if use_color:
        ansi_text = _read_asset(_SPLASH_ANSI)
        if ansi_text:
            return ansi_text
    text = _read_asset(_SPLASH_TXT)
    return text or _FALLBACK_FULL


def _color_allowed(no_color: bool) -> bool:
    if no_color:
        return False
    if os.getenv("NO_COLOR") is not None:
        return False
    if os.getenv("TERM", "").lower() == "dumb":
        return False
    return True


def _isatty(stream: TextIO) -> bool:
    checker = getattr(stream, "isatty", None)
    return bool(checker and checker())


def _read_asset(filename: str) -> str | None:
    try:
        return (
            resources.files(_ASSET_PACKAGE)
            .joinpath(filename)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, UnicodeDecodeError):
        return None
