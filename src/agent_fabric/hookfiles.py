"""Do the agents' hooks files run kb's hooks? (docs/design.md §4.10)

kb knows its own handlers by their command, which runs `kb hook`, and changes no other handler."""

import json
import shlex
import tomllib
from collections.abc import Mapping
from pathlib import Path, PurePath
from typing import Any

from agent_fabric.config import HOOKS
from agent_fabric.hooks import EVENTS, last_ran
from agent_fabric.paths import tilde
from agent_fabric.report import Row
from agent_fabric.sandboxes import cant_read, cant_write, replace_file

VERSIONED = ("copilot", "cursor")  # their hooks files start with "version": 1
APPROVES = ("codex",)  # runs a new or changed user hook only once approved in /hooks
CODEX_STATE_EVENTS = {"start": "session_start", "stop": "stop"}  # the names in its config.toml's [hooks.state]
APPROVE = "Open Codex and approve kb's hooks in /hooks: Codex runs new or changed hooks only then"
TURN_ON = "Turn kb's hooks back on in Codex's /hooks, or leave them out with kb doctor --fix --hooks"


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


def _codex_state(path: Path) -> dict[str, Any] | None:
    """What Codex records about hooks, in the config.toml beside its hooks.json; None where kb can't read it."""
    try:
        config = tomllib.loads((path.parent / "config.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    hooks = config.get("hooks")
    state = hooks.get("state") if isinstance(hooks, dict) else None
    return state if isinstance(state, dict) else {}


def _codex_record(state: dict[str, Any], path: Path, hook: str, hooks: dict[str, Any]) -> dict[str, Any]:
    """Codex's record of kb's hook, by the hooks file and the places of kb's group and handler in it (kb's group holds
    only kb's handler). A hash means approved; no `enabled` still runs (docs/design.md §11)."""
    entries = hooks[EVENTS["codex"][hook]]
    group = next(place for place, entry in enumerate(entries) if _holds_kb(entry))
    for file in (path, path.resolve()):
        record = state.get(f"{file}:{CODEX_STATE_EVENTS[hook]}:{group}:0")
        if isinstance(record, dict):
            return record
    return {}


def _in_place(path: Path, agent: str, chosen: tuple[str, ...], hooks: dict[str, Any], env: Mapping[str, str]) -> Row:
    """kb's entries are right. Do they run: has Codex approved them, and when did one last run?"""
    shown = tilde(path, env)
    ran = last_ran(agent, env)
    last = f"last ran {ran:%Y-%m-%d %H:%M}" if ran else "hasn't run yet"
    if agent not in APPROVES:
        return Row("ok", "hooks", shown, f"{_named(chosen)}; {last}")
    state = _codex_state(path)
    if state is None:
        return Row("ok", "hooks", shown, f"{_named(chosen)}, once approved in Codex's /hooks; {last}")
    records = {hook: _codex_record(state, path, hook, hooks) for hook in chosen}
    waiting = [hook for hook, record in records.items() if "trusted_hash" not in record]
    off = [hook for hook, record in records.items() if "trusted_hash" in record and record.get("enabled") is False]
    if not waiting and not off:
        return Row("ok", "hooks", shown, f"{_named(chosen)}, approved in Codex; {last}")
    trouble = []
    if waiting:
        it = _by_count(waiting, "it", "them")
        trouble.append(f"{_named(waiting)} not approved in Codex, which won't run {it}")
    if off:
        trouble.append(f"{_named(off)} turned off in Codex")
    return Row("problem", "hooks", shown, "; ".join(trouble), step=APPROVE if waiting else TURN_ON)


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
        return _in_place(path, agent, chosen, hooks, env) if chosen else None
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
    return Row("fixed", "hooks", shown, done, step=APPROVE if agent in APPROVES and (missing or differ) else None)
