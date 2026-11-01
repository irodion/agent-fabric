"""kb's own settings in ~/.config/agent-fabric/config.toml (docs/design.md §4.10, §5).

So far only `hooks` under `[notes]`: which of the agents' hooks `kb doctor` installs. kb edits the file the way it
edits Codex's config: it only changes or adds the one line, and writes only if the result reads back as intended."""

import json
import re
import tomllib
from collections.abc import Mapping
from typing import Any

from agent_fabric.hooks import HOOKS
from agent_fabric.paths import config_file
from agent_fabric.sandboxes import replace_file

NOTES_TABLE = re.compile(r"^[ \t]*\[[ \t]*notes[ \t]*\][^\r\n]*(?:\r?\n|\Z)", re.MULTILINE)
NEXT_TABLE = re.compile(r"^[ \t]*\[", re.MULTILINE)
HOOKS_LINE = re.compile(r"^[ \t]*hooks[ \t]*=[^\n]*\n?", re.MULTILINE)


def _recorded(text: str) -> tuple[str, ...] | None:
    """The choice the config's text records; None where it records none, ValueError where it can't be read."""
    notes: Any = tomllib.loads(text).get("notes", {})
    if not isinstance(notes, dict):
        raise ValueError("notes is not a table")
    if "hooks" not in notes:
        return None
    chosen = notes["hooks"]
    if not isinstance(chosen, list) or not all(isinstance(hook, str) and hook in HOOKS for hook in chosen):
        raise ValueError(f"notes.hooks must list some of: {', '.join(HOOKS)}")
    return tuple(hook for hook in HOOKS if hook in chosen)


def _text(env: Mapping[str, str]) -> str:
    path = config_file(env)
    return path.read_bytes().decode("utf-8") if path.exists() else ""  # bytes: keeps CRLF line endings


def hooks_choice(env: Mapping[str, str]) -> tuple[str, ...]:
    """The recorded choice of hooks; both when nothing is recorded."""
    recorded = _recorded(_text(env))
    return HOOKS if recorded is None else recorded


def record_hooks(env: Mapping[str, str], chosen: tuple[str, ...]) -> None:
    """Record the choice: change the `hooks` line under [notes], or add it; ValueError where that can't do it."""
    text = _text(env)
    nl = "\r\n" if "\r\n" in text else "\n"
    line = f"hooks = {json.dumps(list(chosen))}{nl}"
    header = NOTES_TABLE.search(text)
    if header is None:
        gap = "" if not text else nl if text.endswith("\n") else nl * 2
        new = f"{text}{gap}[notes]{nl}{line}"
    else:
        head = text[: header.end()] + ("" if header[0].endswith("\n") else nl)  # a header on the last line
        rest = text[header.end() :]
        next_table = NEXT_TABLE.search(rest)
        current = HOOKS_LINE.search(rest[: next_table.start() if next_table else len(rest)])
        start, end = (current.start(), current.end()) if current else (0, 0)
        new = head + rest[:start] + line + rest[end:]
    try:
        recorded = _recorded(new)
    except ValueError:  # TOMLDecodeError too
        recorded = None
    if recorded != chosen:
        raise ValueError(f"can't record the choice; set hooks = {json.dumps(list(chosen))} under [notes] by hand")
    replace_file(config_file(env), new)
