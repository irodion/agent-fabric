"""kb's own settings in ~/.config/agent-fabric/config.toml (docs/design.md §4.10, §5).

So far only `hooks` under `[notes]`: which of the agents' hooks `kb doctor` installs. kb edits the file the way it
edits Codex's config: it only changes or adds the one line, and writes only if the result reads back as intended."""

import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_fabric.paths import config_file, expand, tilde
from agent_fabric.sandboxes import replace_file

HOOKS = ("start", "stop")
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


def setting(env: Mapping[str, str], root: Path | None, table: str, key: str) -> str:
    """Where a workspace's setting is, for a message: `[publish] allow`, or `allow in the [[workspace]] for ~/x`."""
    return f"[{table}] {key}" if root is None else f"{key} in the [[workspace]] for {tilde(root, env)}"


def _settings(env: Mapping[str, str], root: Path | None, table: str) -> dict[str, Any]:
    """A workspace's settings: the default's under `[table]`, a root's in the `[[workspace]]` entry whose root is
    `root` (resolved), the first where two name it (docs/design.md §4.11). ValueError where the file can't be read, or
    has no entry for `root`: defaults in its place could send a client's notes to another backend."""
    data: Any = tomllib.loads(_text(env))
    if root is None:
        found = data.get(table, {})
    else:
        entries = zip(workspace_roots(env), data.get("workspace", []), strict=True)
        found = next((entry for written, entry in entries if expand(written, env).resolve() == root), None)
        if found is None:
            raise ValueError(f"no [[workspace]] has the root {tilde(root, env)}")
    if not isinstance(found, dict):
        raise ValueError(f"{table} is not a table")
    return found


def allowed_projects(env: Mapping[str, str], root: Path | None = None) -> tuple[str, ...]:
    """The workspace's allow list of project ids (docs/design.md §4.5). It starts empty: nothing is published until a
    project is added. ValueError where the file can't be read."""
    allow = _settings(env, root, "publish").get("allow", [])
    if not isinstance(allow, list) or not all(isinstance(project, str) for project in allow):
        raise ValueError(f"{setting(env, root, 'publish', 'allow')} must be a list of project ids")
    return tuple(allow)


def ingest_backend(env: Mapping[str, str], root: Path | None = None) -> str:
    """The workspace's backend (docs/design.md §4.7): the default's under `[ingest]`; `codex` where none is set.
    ValueError where the file can't be read."""
    backend = _settings(env, root, "ingest").get("backend", "codex")
    if not isinstance(backend, str):
        raise ValueError(f"{setting(env, root, 'ingest', 'backend')} must be a name, such as codex")
    return backend


def workspace_roots(env: Mapping[str, str]) -> tuple[str, ...]:
    """The roots of the `[[workspace]]` entries, as written (docs/design.md §4.11). ValueError where the file can't be
    read."""
    entries: Any = tomllib.loads(_text(env)).get("workspace", [])
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("root"), str) for e in entries
    ):
        raise ValueError("each [[workspace]] needs a root, a folder")
    roots = tuple(entry["root"] for entry in entries)
    if relative := [root for root in roots if not expand(root, env).is_absolute()]:
        raise ValueError(f"a [[workspace]] root is a full path or starts with ~/, not {relative[0]!r}")
    return roots


def machine_opt_outs(env: Mapping[str, str]) -> tuple[str | None, ...]:
    """The workspaces with `share_machine_notes = false` (docs/design.md §4.11): roots as written, and None for the
    default workspace, whose setting is under `[publish]`. Sharing is on by default. ValueError where the file can't
    be read."""
    data: Any = tomllib.loads(_text(env))
    publish = data.get("publish", {})
    if not isinstance(publish, dict):
        raise ValueError("publish is not a table")
    entries = [publish, *data.get("workspace", [])]
    roots = (None, *workspace_roots(env))
    shares = [entry.get("share_machine_notes", True) for entry in entries]
    if not all(isinstance(share, bool) for share in shares):
        raise ValueError("share_machine_notes is true or false")
    return tuple(root for root, share in zip(roots, shares, strict=True) if not share)


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
