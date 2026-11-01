"""Do the agents' hooks files run kb's hooks? (docs/design.md §4.10)

kb knows its own handlers by their command, which runs `kb hook`, and changes no other handler."""

import json
import shlex
from collections.abc import Mapping
from pathlib import Path, PurePath
from typing import Any

from agent_fabric.hooks import EVENTS, HOOKS
from agent_fabric.paths import tilde
from agent_fabric.report import Row
from agent_fabric.sandboxes import cant_read, cant_write, replace_file

VERSIONED = ("copilot", "cursor")  # their hooks files start with "version": 1
APPROVES = ("codex",)  # runs a new or changed user hook only once approved in /hooks


def _entry(agent: str, hook: str) -> dict[str, Any]:
    """kb's entry for one hook: a group of handlers in Claude Code and Codex, a handler in Copilot and Cursor."""
    command = f"kb hook {hook} --agent {agent}"
    if agent == "copilot":
        return {"type": "command", "bash": command, "timeoutSec": 10}
    if agent == "cursor":
        return {"type": "command", "command": command, "timeout": 10}
    return {"hooks": [{"type": "command", "command": command, "timeout": 10}]}


def _is_kb(handler: object) -> bool:
    """kb's own handler: its command runs a program named kb, by name or by path, with `hook` first."""
    if not isinstance(handler, dict):
        return False
    for key in ("command", "bash"):
        command = handler.get(key)
        try:
            words = shlex.split(command) if isinstance(command, str) else []
        except ValueError:  # unbalanced quotes: not a command kb wrote
            continue
        if len(words) >= 2 and PurePath(words[0]).name == "kb" and words[1] == "hook":
            return True
    return False


def _holds_kb(entry: dict[str, Any]) -> bool:
    """The entry is kb's handler, or a group with one among its handlers."""
    handlers = entry.get("hooks")
    return any(_is_kb(handler) for handler in handlers) if isinstance(handlers, list) else _is_kb(entry)


def _without_kb(entry: dict[str, Any]) -> dict[str, Any] | None:
    """The entry with kb's handlers taken out, and the user's kept; None where nothing is left."""
    handlers = entry.get("hooks")
    if not isinstance(handlers, list):
        return None if _is_kb(entry) else entry
    if not any(_is_kb(handler) for handler in handlers):
        return entry
    rest = [handler for handler in handlers if not _is_kb(handler)]
    return entry | {"hooks": rest} if rest else None


def _named(hooks: list[str] | tuple[str, ...]) -> str:
    """'start and stop', or 'stop'."""
    return " and ".join(hooks)


def _by_count(hooks: list[str], one: str, several: str) -> str:
    return several if len(hooks) > 1 else one


def _hooks(settings: Any, agent: str) -> dict[str, Any]:
    """The file's hooks object, added (empty) where it's missing; kb's events must hold lists of objects."""
    if not isinstance(settings, dict):
        raise ValueError("the file is not a JSON object")
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks is not an object")
    for event in EVENTS[agent].values():
        entries = hooks.get(event, [])
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise ValueError(f"hooks.{event} is not a list of objects")
    return hooks


def _report(shown: str, missing: list[str], differ: list[str], extra: list[str]) -> Row:
    """What `kb doctor` without `--fix` says about kb's entries in one file: every kind of trouble."""
    trouble = [
        (missing, "missing"),
        (differ, f"{_by_count(differ, 'differs', 'differ')} from this kb's"),
        (extra, "not in the recorded choice"),
    ]
    detail = "; ".join(f"{_named(hooks)} {said}" for hooks, said in trouble if hooks)
    return Row("outdated" if differ or extra else "missing", "hooks", shown, detail)


def check(path: Path, agent: str, chosen: tuple[str, ...], env: Mapping[str, str], *, fix: bool) -> Row | None:
    """An agent's hooks file holds kb's chosen hooks and no other kb entry; with `fix`, kb's entries are set and every
    other entry is kept. A file it can't read, or with the wrong shape, is reported and left alone. None: nothing to
    say (no hooks chosen, none there)."""
    shown = tilde(path, env)
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        hooks = _hooks(settings, agent)
    except (OSError, ValueError) as e:
        return cant_read("hooks", shown, e)
    found = {hook: [e for e in hooks.get(event, []) if _holds_kb(e)] for hook, event in EVENTS[agent].items()}
    missing = [hook for hook in chosen if not found[hook]]
    differ = [hook for hook in chosen if found[hook] and found[hook] != [_entry(agent, hook)]]
    extra = [hook for hook in HOOKS if hook not in chosen and found[hook]]
    if not (missing or differ or extra):
        once_approved = ", once approved in Codex's /hooks" if agent in APPROVES else ""
        return Row("ok", "hooks", shown, f"{_named(chosen)}{once_approved}") if chosen else None
    if not fix:
        return _report(shown, missing, differ, extra)
    if agent in VERSIONED:
        settings.setdefault("version", 1)
    for hook, event in EVENTS[agent].items():
        kept = [rest for e in hooks.get(event, []) if (rest := _without_kb(e)) is not None]
        hooks[event] = kept + ([_entry(agent, hook)] if hook in chosen else [])
        if not hooks[event]:
            del hooks[event]
    try:
        replace_file(path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    except OSError as e:
        return cant_write("hooks", shown, e)
    changes = [("installed", missing), ("updated", differ), ("removed", extra)]
    done = "; ".join(f"{verb} {_named(hooks)}" for verb, hooks in changes if hooks)
    approve = "Open Codex and approve kb's hooks in /hooks: Codex runs new or changed hooks only then"
    return Row("fixed", "hooks", shown, done, step=approve if agent in APPROVES and (missing or differ) else None)
