"""Terminal user interface for craft-suite.

Pure standard library. Uses raw ANSI escapes for a modern look and falls back
to plain numbered prompts when the terminal is not interactive (for example
when autoupdate runs from a timer).
"""

from __future__ import annotations

import os
import shutil
import sys
import termios
import tty
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Colour and glyphs
# --------------------------------------------------------------------------

ESCAPE = "\033["


class C:
    """ANSI helpers that no-op when colour is disabled."""

    enabled = True

    @classmethod
    def _wrap(cls, code: str, text: str) -> str:
        return f"{ESCAPE}{code}m{text}{ESCAPE}0m" if cls.enabled else text

    @classmethod
    def bold(cls, t): return cls._wrap("1", t)
    @classmethod
    def dim(cls, t): return cls._wrap("2", t)
    @classmethod
    def red(cls, t): return cls._wrap("31", t)
    @classmethod
    def green(cls, t): return cls._wrap("32", t)
    @classmethod
    def yellow(cls, t): return cls._wrap("33", t)
    @classmethod
    def blue(cls, t): return cls._wrap("34", t)
    @classmethod
    def magenta(cls, t): return cls._wrap("35", t)
    @classmethod
    def cyan(cls, t): return cls._wrap("36", t)
    @classmethod
    def bold_cyan(cls, t): return cls._wrap("1;36", t)
    @classmethod
    def bold_green(cls, t): return cls._wrap("1;32", t)
    @classmethod
    def bold_yellow(cls, t): return cls._wrap("1;33", t)
    @classmethod
    def invert(cls, t): return cls._wrap("7", t)


def setup_colour() -> None:
    C.enabled = (
        sys.stdout.isatty()
        and os.environ.get("TERM", "") != "dumb"
        and not os.environ.get("NO_COLOR")
    )


def terminal_width(default: int = 78) -> int:
    return min(shutil.get_terminal_size((default, 24)).columns, 100)


def banner() -> None:
    width = terminal_width()
    title = "ArtCraft Suite"
    subtitle = "install and update the Craft apps"
    inner = width - 4
    print()
    print(C.cyan("  ╭" + "─" * (inner) + "╮"))
    print(C.cyan("  │") + " " + C.bold_cyan(title.center(inner - 1)) + C.cyan("│"))
    print(C.cyan("  │") + " " + C.dim(subtitle.center(inner - 1)) + C.cyan("│"))
    print(C.cyan("  ╰" + "─" * (inner) + "╯"))
    print()


# --------------------------------------------------------------------------
# Raw-mode key reader
# --------------------------------------------------------------------------


class Terminal:
    """Context manager that puts the tty in cbreak mode and reads keys.

    Enter this once per screen rather than per keypress: repeated
    tcsetattr calls between redraws can drop a key that arrived during the
    redraw, which makes checkbox toggles feel unresponsive.
    """

    def __enter__(self) -> "Terminal":
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc) -> None:
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def key(self) -> str:
        char = os.read(self.fd, 1)
        if char == b"\x1b":
            # Could be an escape sequence (arrow key) or a bare Escape.
            import select
            if select.select([self.fd], [], [], 0.05)[0]:
                rest = os.read(self.fd, 2).decode("utf-8", "replace")
                return {"[A": "up", "[B": "down", "[C": "right",
                        "[D": "left", "[Z": "backtab"}.get(rest, "escape")
            return "escape"
        if char in (b"\r", b"\n"):
            return "enter"
        if char == b" ":
            return "space"
        if char == b"\x03":
            return "ctrl-c"
        if char == b"\x04":
            return "ctrl-d"
        if char == b"a":
            return "a"
        if char == b"\t":
            return "tab"
        return char.decode("utf-8", "replace")


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _hide_cursor() -> None:
    if C.enabled:
        sys.stdout.write(f"{ESCAPE}?25l")


def _show_cursor() -> None:
    if C.enabled:
        sys.stdout.write(f"{ESCAPE}?25h")


# --------------------------------------------------------------------------
# App row model
# --------------------------------------------------------------------------


@dataclass
class Row:
    slug: str
    name: str
    description: str
    latest: str = ""
    installed: str = ""
    method: str = ""
    status: str = "new"  # new | installed | update | unknown
    selected: bool = False


STATUS_STYLE = {
    "installed": (lambda t: C.green("● " + t), "installed"),
    "update": (lambda t: C.yellow("▲ " + t), "update"),
    "new": (lambda t: C.dim("○ " + t), "not installed"),
    "unknown": (lambda t: C.dim("? " + t), "unknown"),
}


def render_rows(rows: list[Row], cursor: int, *, scroll: int) -> None:
    """Draw the selectable list in place."""
    width = terminal_width()
    height = shutil.get_terminal_size((80, 24)).lines
    # Leave room for the header and the help line.
    visible = max(5, height - 12)
    start = max(0, min(scroll, len(rows) - visible))
    end = min(len(rows), start + visible)

    for index in range(start, end):
        row = rows[index]
        pointer = C.bold_cyan("❯") if index == cursor else " "
        box = C.bold_green("[x]") if row.selected else C.dim("[ ]")
        name = C.bold(row.name) if index == cursor else row.name
        name_width = 16
        padded = name + " " * max(0, name_width - len(row.name))

        if row.status == "update":
            badge = C.bold_yellow(f"{row.installed} → {row.latest}")
        elif row.status == "installed":
            badge = C.dim(row.latest)
        else:
            badge = C.dim("–")

        hint = row.description
        room = width - 4 - name_width - 22
        if len(hint) > room:
            hint = hint[: max(0, room - 1)] + "…"

        line = f" {pointer} {box} {padded}{badge:<10}  {C.dim(hint)}"
        sys.stdout.write(f"\r{ESCAPE}2K{line}\n")

    if len(rows) > visible:
        sys.stdout.write(C.dim(f"   … {len(rows)} apps, showing {start + 1}–{end}\n"))
    sys.stdout.flush()


def select_apps(rows: list[Row], *, title: str, allow_all: bool = True) -> list[Row] | None:
    """Checkbox picker. Returns the chosen rows, or None when cancelled."""
    if not interactive():
        return _select_apps_plain(rows, title=title)

    cursor, scroll = 0, 0
    _hide_cursor()
    try:
        with Terminal() as term:
            while True:
                sys.stdout.write(f"{ESCAPE}2J{ESCAPE}H")
                banner()
                print("  " + C.bold(title))
                print("  " + C.dim("─" * (terminal_width() - 6)))
                print()
                render_rows(rows, cursor, scroll=scroll)
                print()
                legend = (C.green("●") + " installed  " +
                          C.yellow("▲") + " update  " +
                          C.dim("○") + " not installed")
                print("  " + legend)
                keys = "↑↓ move · space select · enter confirm · esc cancel"
                if allow_all:
                    keys = "a all/none · " + keys
                print("  " + C.dim(keys))
                sys.stdout.flush()

                key = term.key()

                if key == "up":
                    cursor = max(0, cursor - 1)
                elif key == "down":
                    cursor = min(len(rows) - 1, cursor + 1)
                elif key in ("space", "x"):
                    rows[cursor].selected = not rows[cursor].selected
                elif key == "a" and allow_all:
                    every = all(r.selected for r in rows)
                    for row in rows:
                        row.selected = not every
                elif key == "enter":
                    return [r for r in rows if r.selected]
                elif key in ("escape", "ctrl-c"):
                    return None

                # Keep the cursor inside the visible window.
                visible = max(5, shutil.get_terminal_size((80, 24)).lines - 12)
                if cursor < scroll:
                    scroll = cursor
                elif cursor >= scroll + visible:
                    scroll = cursor - visible + 1
    finally:
        _show_cursor()


def _select_apps_plain(rows: list[Row], *, title: str) -> list[Row] | None:
    """Non-interactive fallback: comma separated numbers."""
    print(title)
    for index, row in enumerate(rows, start=1):
        print(f"  {index:2}. [{('x' if row.selected else ' ')}] {row.name} "
              f"({row.latest or '?'})")
    answer = input("Choose numbers (comma separated, empty = none): ").strip()
    if not answer:
        return []
    chosen = set()
    for token in answer.replace(" ", "").split(","):
        if token.isdigit() and 1 <= int(token) <= len(rows):
            chosen.add(int(token))
    selected = [rows[i - 1] for i in sorted(chosen)]
    for row in selected:
        row.selected = True
    return selected


def choose(prompt: str, options: list[str], *, default: int = 0) -> int | None:
    """Simple radio picker. Returns the index or None when cancelled."""
    if not interactive():
        print(prompt)
        for index, option in enumerate(options, start=1):
            print(f"  {index}. {option}")
        answer = input(f"Choose [1–{len(options)}], enter = {default + 1}: ").strip()
        if not answer:
            return default
        return int(answer) - 1 if answer.isdigit() and 1 <= int(answer) <= len(options) else None

    cursor = default
    _hide_cursor()
    try:
        with Terminal() as term:
            while True:
                sys.stdout.write(f"{ESCAPE}2J{ESCAPE}H")
                banner()
                print("  " + C.bold(prompt))
                print()
                for index, option in enumerate(options):
                    pointer = C.bold_cyan("❯") if index == cursor else " "
                    marker = C.bold_green("●") if index == cursor else C.dim("○")
                    text = C.bold(option) if index == cursor else option
                    print(f"  {pointer} {marker} {text}")
                print()
                print("  " + C.dim("↑↓ move · enter select · esc cancel"))
                sys.stdout.flush()

                key = term.key()
                if key == "up":
                    cursor = max(0, cursor - 1)
                elif key == "down":
                    cursor = min(len(options) - 1, cursor + 1)
                elif key == "enter":
                    return cursor
                elif key in ("escape", "ctrl-c"):
                    return None
    finally:
        _show_cursor()


def confirm(prompt: str, *, default: bool = False) -> bool:
    if not interactive():
        suffix = " [Y/n]" if default else " [y/N]"
        answer = input(prompt + suffix).strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes")
    options = ["Yes", "No"] if default else ["No", "Yes"]
    result = choose(prompt, options, default=0)
    if result is None:
        return False
    return options[result] == "Yes"


# --------------------------------------------------------------------------
# Progress output
# --------------------------------------------------------------------------


class Progress:
    """One line per app, rewritten in place as work proceeds.

    Each reporter owns a fixed screen line. Lines are reserved up front, so
    concurrent updates never overlap and the final output is a clean list
    rather than a pile of half-overwritten rows.
    """

    def __init__(self) -> None:
        self._lines: dict[str, int] = {}
        self._next = 0
        self._started = False

    def start(self) -> None:
        _hide_cursor()
        self._started = True
        sys.stdout.flush()

    def reserve(self, label: str) -> None:
        """Claim a screen line for an app before the work begins."""
        if label in self._lines:
            return
        self._lines[label] = self._next
        # Print a placeholder so the line exists and is counted.
        sys.stdout.write("\n")
        self._next += 1
        sys.stdout.flush()

    def reporter(self, label: str):
        """Return a callable that redraws this app's own line."""
        self.reserve(label)
        line = self._lines[label]

        def report(message: str, *, replace: bool = False) -> None:
            text = f"  {C.cyan('•')} {label}: {message}"
            up = self._next - 1 - line
            # Save, walk up to our line, rewrite it, restore. Using the
            # DECSC/DECRC pair (ESC 7 / ESC 8) rather than ESC[s/ESC[u, which
            # some terminals render literally.
            sys.stdout.write(f"{ESCAPE}7")
            if up > 0:
                sys.stdout.write(f"{ESCAPE}{up}A")
            sys.stdout.write(f"\r{ESCAPE}2K{text}")
            sys.stdout.write(f"{ESCAPE}8")
            sys.stdout.flush()

        return report

    def finish(self) -> None:
        if self._started:
            # Land on a fresh line below the reserved block.
            sys.stdout.write("\n")
            _show_cursor()
            sys.stdout.flush()


def success(message: str) -> None:
    print(f"  {C.bold_green('✓')} {message}")


def warning(message: str) -> None:
    print(f"  {C.bold_yellow('!')} {message}")


def failure(message: str) -> None:
    print(f"  {C.red('✗')} {message}")


def info(message: str) -> None:
    print(f"  {C.cyan('ℹ')} {message}")
