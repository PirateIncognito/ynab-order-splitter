"""Everything that talks to the user on the console.

All questionary calls live here. Choices are (label, value) pairs: choose()
and menu() return the chosen option's value, or None for Back/Exit (or
Ctrl+C). The other prompts return None on Ctrl+C.

Colours come from the theme: Gruvbox on a dark terminal, Gruvbox light on a
light one (set_theme, at startup and from Settings).
"""

import logging
import os
import queue
import re
import select
import shutil
import subprocess
import sys
import termios
import tty
import webbrowser
from collections.abc import Callable
from typing import Any

import questionary
from prompt_toolkit import Application
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.filters import emacs_insert_mode, vi_insert_mode
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import FormattedTextControl, Layout, Window
from questionary import Choice, Question
from tabulate import tabulate

# How choices are picked: "arrows", "arrows+keys" (arrows, or press an
# option's number to jump straight to it; 0 is Back), or "type number"
# (type a number, then Enter).
NAVIGATION = "arrows+keys"

# Status messages logged since the last screen was drawn. core/main.py
# routes app log records here (via logging.handlers.QueueHandler) so a menu
# can show them again after it clears the screen.
messages: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()

_BACK = object()
# Menu option keys: 1-9, then letters (0 is Back)
_MENU_KEYS = "123456789abcdefghijklmnopqrstuvwxyz"

# Colour roles -> hex. Gruvbox's palettes, lightened (dark) or darkened
# (light) where needed to at least 7:1 contrast on the theme's background --
# the terminal's 16 basic colours were unreadable (dark red 2.9:1, grey 4.2:1).
# title: transaction line; head: column titles; out / in: amounts; muted: row
# keys, order number, prompt marks; missing: "(none)"; warn: warnings;
# answer, menu_*, current_*: questionary answers and the suggestion menu.
THEMES = {
    "dark": {  # Gruvbox
        "title": "#EBDBB2", "head": "#8EC07C", "out": "#FD978B", "in": "#B8BB26", "muted": "#9EB9AF",
        "missing": "#FABD2F", "warn": "#FE9B49", "answer": "#EBDBB2", "menu_bg": "#3C3836",
        "current_fg": "#282828", "current_bg": "#FABD2F",
    },
    "light": {  # Gruvbox light
        "title": "#3C3836", "head": "#30593F", "out": "#9D0006", "in": "#56520A", "muted": "#065968",
        "missing": "#6E480C", "warn": "#913002", "answer": "#3C3836", "menu_bg": "#EBDBB2",
        "current_fg": "#FFFFFF", "current_bg": "#065968",
    },
}
_BOLD_ROLES = {"title", "head"}
theme_name = "dark"
theme = THEMES[theme_name]


def background_kind(answer: str) -> str | None:
    """"light" or "dark" from a terminal's reply to the OSC 11 background
    query (e.g. "\033]11;rgb:fbfb/f1f1/c7c7\033\\"; 1-4 hex digits per
    channel), None if there is no colour in it."""
    found = re.search(r"rgb:([0-9a-f]+)/([0-9a-f]+)/([0-9a-f]+)", answer, re.IGNORECASE)
    if not found:
        return None
    r, g, b = (int(x, 16) / (16 ** len(x) - 1) for x in found.groups())
    return "light" if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.5 else "dark"


def terminal_background() -> str | None:
    """Ask the terminal for its background colour: "light", "dark", or None
    if it isn't a terminal or doesn't answer within 0.2 s."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    answer = b""
    try:
        tty.setraw(fd)  # read the reply without echoing it
        print("\033]11;?\033\\", end="", flush=True)
        while not answer.endswith((b"\a", b"\033\\")) and select.select([fd], [], [], 0.2)[0]:
            answer += os.read(fd, 64)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return background_kind(answer.decode(errors="replace"))


def set_theme(choice: str) -> None:
    """Use the "dark" or "light" theme, or for "auto" the one that matches
    the terminal's background (dark if it can't be told)."""
    global theme_name, theme
    theme_name = (terminal_background() or "dark") if choice == "auto" else choice
    theme = THEMES[theme_name]


def styled(segments: list[tuple[str, str]]) -> str:
    """(role, text) segments as text in the theme's colours; role "" is
    left in the terminal's own colour."""
    out = []
    for role, text in segments:
        if not role:
            out.append(text)
            continue
        r, g, b = (int(theme[role][i:i + 2], 16) for i in (1, 3, 5))
        out.append(f"\033[{'1;' if role in _BOLD_ROLES else ''}38;2;{r};{g};{b}m{text}\033[0m")
    return "".join(out)


def print_table(headers: list[str], rows: list[list[str]], align: tuple[str, ...], roles: list[list[str]]) -> None:
    """Print `rows` as a table (tabulate's "simple" style), each cell in the
    colour of its role in `roles` (same shape as `rows`)."""
    lines = tabulate(rows, headers=headers, tablefmt="simple", colalign=align, disable_numparse=True).splitlines()
    print(styled([("head", lines[0])]))
    print(styled([("muted", lines[1])]))
    spans = [m.span() for m in re.finditer(r"-+", lines[1])]  # each column's extent, from the dashes
    for line, row_roles in zip(lines[2:], roles):
        segments = []
        end = 0
        for (start, stop), role in zip(spans, row_roles):
            segments += [("", line[end:start]), (role, line[start:stop])]
            end = stop
        print(styled(segments))


def _style() -> questionary.Style:
    """questionary's colours for the theme. Its own defaults draw answers and
    suggestions in bold orange, on a light grey menu: unreadable."""
    return questionary.Style([
        ("qmark", f"fg:{theme['muted']}"),
        ("answer", f"fg:{theme['answer']} bold"),
        ("completion-menu", f"bg:{theme['menu_bg']}"),
        ("selected", f"fg:{theme['current_fg']} bg:{theme['current_bg']} noreverse"),  # highlighted suggestion
    ])


def clear_screen() -> None:
    print("\033[H\033[J", end="")


def show_messages() -> None:
    """Print and clear the status messages logged since the last screen."""
    if messages.empty():
        return
    while not messages.empty():
        print(messages.get().getMessage())
    print()


def show_background_messages() -> None:
    """Print and clear the status messages background threads (the AI
    agent) logged since the last screen; the main thread's stay queued, in
    order, for the next menu."""
    records = []
    while not messages.empty():
        records.append(messages.get())
    shown = False
    for record in records:
        if record.threadName == "MainThread":
            messages.put(record)
        else:
            print(record.getMessage())
            shown = True
    if shown:
        print()


def choose(message: str, options: list[tuple[str, Any]], back_label: str | None = None) -> Any:
    """Ask the user to pick one of `options` ((label, value) pairs) and
    return its value. With `back_label`, a Back/Exit option is added (on
    the 0 key); picking it returns None. A label's later lines are indented
    to start under its first line's text, past the " » 1) " prefix."""
    prefix = " " * (3 if NAVIGATION == "arrows" else 6)  # no "1) " without shortcuts
    choices = [Choice(label.replace("\n", "\n" + prefix), value=value, shortcut_key=key)
               for (label, value), key in zip(options, _MENU_KEYS)]
    if back_label:
        choices.append(Choice(back_label, value=_BACK, shortcut_key="0"))
    if NAVIGATION == "type number":
        answer = questionary.rawselect(message, choices=choices, style=_style()).ask()
    else:
        question = questionary.select(
            message, choices=choices, use_shortcuts=NAVIGATION == "arrows+keys", style=_style(),
            use_jk_keys=False,  # j and k are option keys (19th, 20th); arrows still move
        )
        if NAVIGATION == "arrows+keys":  # an option's key picks it at once, no Enter
            picks = KeyBindings()
            for c in choices:
                picks.add(c.shortcut_key, eager=True)(lambda event, value=c.value: event.app.exit(result=value))
            question.application.key_bindings = merge_key_bindings([question.application.key_bindings, picks])
        answer = question.ask()
    return None if answer is _BACK else answer


def menu(title: str, options: list[tuple[str, Any]], back_label: str = "Back") -> Any:
    """Clear the screen and show a full-screen menu, with any status
    messages logged since the last screen under the title."""
    clear_screen()
    print(f"\n{title}\n")
    show_messages()
    return choose("Select:", options, back_label=back_label)


def text(message: str, default: str = "", validate: Callable[[str], bool | str] | None = None) -> str | None:
    """Ask for a line of text. `validate` returns True, or an error message
    to show while the user corrects their answer."""
    return questionary.text(message, default=default, validate=validate, style=_style()).ask()


def pick_numbered(
    message: str,
    headers: list[str],
    rows: list[list[str]],
    align: tuple[str, ...],
    roles: list[list[str]],
    values: list[Any],
) -> Any:
    """Print `rows` as a table (see print_table), numbered from 1, and ask
    for a row's number; return that row's value from `values` (same order
    as `rows`), or None on an empty answer (back) or Ctrl+C. Unlike
    choose(), every row is listed however many there are, and the list
    stays printed above the prompt, so it can be scrolled back."""
    n = len(rows)
    print_table(
        ["#", *headers],
        [[str(i), *row] for i, row in enumerate(rows, 1)],
        ("right", *align),
        [["muted", *row_roles] for row_roles in roles],
    )
    print()
    answer = text(
        f"{message} (1-{n}), or Enter to go back:",
        validate=lambda s: not s or s.isdecimal() and 1 <= int(s) <= n or f"A number from 1 to {n}.",
    )
    return values[int(answer) - 1] if answer else None


def secret(message: str, required: bool = True) -> str | None:
    """Ask for a password or other secret without echoing it."""
    if not required:
        return questionary.password(message, style=_style()).ask()
    return questionary.password(message, validate=lambda s: bool(s.strip()) or "Required.", style=_style()).ask()


def confirm(message: str, default: bool = False) -> bool | None:
    return questionary.confirm(message, default=default, style=_style()).ask()


def key(message: str, keys: str) -> str | None:
    """Show `message` and wait for a single key press, without Enter, of
    one of the (lowercase) characters in `keys`, in either case; return it
    lowercased. Other keys, and pasted text, are ignored."""
    bindings = KeyBindings()

    @bindings.add(Keys.Any)
    def _(event):
        if len(event.data) == 1 and event.data.lower() in keys:
            event.app.exit(result=event.data.lower())

    @bindings.add("c-c")
    def _(event):
        event.app.exit(exception=KeyboardInterrupt)

    # No cursor: nothing is typed here, and it would sit on the message.
    text = FormattedTextControl(message, show_cursor=False)
    return Question(Application(Layout(Window(text)), key_bindings=bindings)).ask()


def autocomplete(message: str, choices: list[str], validate: Callable[[str], bool | str]) -> str | None:
    """Ask for a line of text, suggesting the `choices` that contain what
    has been typed so far (any case)."""
    bindings = KeyBindings()
    # When one suggestion is left after a typed character the line becomes
    # that suggestion (`fill`); `typed` keeps what the user typed, so the
    # next character or Backspace works on that instead.
    typed = fill = None

    @bindings.add(Keys.Any, filter=vi_insert_mode | emacs_insert_mode)  # as self-insert
    def _(event):
        nonlocal typed, fill
        buffer = event.current_buffer
        if buffer.text == fill:
            buffer.document = Document(typed)
        buffer.insert_text(event.data)
        matches = list(buffer.completer.get_completions(buffer.document, CompleteEvent()))
        fill = None
        if len(matches) == 1:
            typed, fill = buffer.text, matches[0].text
            buffer.document = Document(fill)

    # Backspace on a filled line goes back to what was typed, less a letter,
    # without filling in again (or that line could never be shortened).
    # prompt_toolkit only suggests after typing; suggest again here too.
    @bindings.add("backspace")
    def _(event):
        nonlocal fill
        buffer = event.current_buffer
        if buffer.text == fill:
            buffer.document = Document(typed[:-1])
            fill = None
        else:
            buffer.delete_before_cursor()
        buffer.start_completion(select_first=False)

    # Tab fills in the highlighted suggestion, or the first one (computed
    # here: the menu fills in the background and may not be up yet).
    @bindings.add("tab")
    def _(event):
        buffer = event.current_buffer
        completion = buffer.complete_state and buffer.complete_state.current_completion
        if not completion:
            completion = next(iter(buffer.completer.get_completions(buffer.document, CompleteEvent())), None)
        if completion:
            buffer.document = Document(completion.text)

    return questionary.autocomplete(
        message, choices=choices, validate=validate, style=_style(), key_bindings=bindings
    ).ask()


def open_in_browser(url: str) -> bool:
    """Open `url` in the default browser, its output thrown away so it can't
    write onto the screen; False if no browser could be started."""
    if not shutil.which("xdg-open"):
        return webbrowser.open(url)
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True
