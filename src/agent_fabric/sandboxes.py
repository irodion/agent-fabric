"""Do the agents' sandboxes let `kb note` write the inbox? (docs/design.md §4.10)

Each check reads one agent's settings file and, with `fix`, adds the inbox to its write list and changes
nothing else. A file it can't read, or can't edit safely, is reported and left alone."""

import json
import os
import re
import shutil
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_fabric.paths import expand, tilde

CLAUDE_KEY = "sandbox.filesystem.allowWrite"
CODEX_KEY = "sandbox_workspace_write.writable_roots"
CODEX_TABLE = re.compile(r"^[ \t]*\[[ \t]*sandbox_workspace_write[ \t]*\][^\n]*\n", re.MULTILINE)


def _paths(value: object, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} is not a list of paths")
    return value


def _cant_read(shown: str, e: Exception) -> tuple[str, str]:
    reason = (e.strerror or e) if isinstance(e, OSError) else e
    return "problem", f"{shown}: can't read it ({reason})"


def _cant_write(shown: str, e: OSError) -> tuple[str, str]:
    return "problem", f"{shown}: can't write it ({e.strerror or e})"


def _write(path: Path, text: str) -> None:
    """Replace the file in one step, through a symlink and keeping its mode: the agent may read it any time. The new
    copy is private from the start, since agent settings can hold tokens, and removed if the replace fails."""
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    new = target.with_name(f".{target.name}.kb-new")
    new.unlink(missing_ok=True)
    with os.fdopen(os.open(new, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        try:
            f.write(text.encode("utf-8"))
            f.flush()
            if target.exists():
                shutil.copymode(target, new)
            os.replace(new, target)
        except BaseException:
            new.unlink(missing_ok=True)
            raise


def _claude_roots(settings: Any) -> list[str]:
    """The allowWrite list in the settings, added (empty) where it's missing."""
    if not isinstance(settings, dict):
        raise ValueError("the settings are not a JSON object")
    node: dict[str, Any] = settings
    for key in ("sandbox", "sandbox.filesystem"):
        child = node.setdefault(key.rpartition(".")[2], {})
        if not isinstance(child, dict):
            raise ValueError(f"{key} is not an object")
        node = child
    return _paths(node.setdefault("allowWrite", []), CLAUDE_KEY)


def claude_code(path: Path, inbox: Path, env: Mapping[str, str], *, fix: bool) -> tuple[str, str]:
    """Claude Code's `settings.json`; the JSON is rewritten whole."""
    home = Path(env["HOME"])
    shown = tilde(path, env)
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        roots = _claude_roots(settings)
    except (OSError, ValueError) as e:
        return _cant_read(shown, e)
    if any(inbox.is_relative_to(expand(root, env)) for root in roots):  # `~/` is the home folder, `/` absolute
        return "ok", f"{shown} lets Claude Code write the inbox"
    if not fix:
        return "missing", f"{shown}: the inbox is not in {CLAUDE_KEY}; `kb doctor --fix` adds it"
    entry = f"~/{inbox.relative_to(home)}" if inbox.is_relative_to(home) else str(inbox)
    roots.append(entry)
    try:
        _write(path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    except OSError as e:
        return _cant_write(shown, e)
    return "fixed", f"{shown}: added {entry} to {CLAUDE_KEY}"


def _codex_table(text: str) -> dict[str, Any] | None:
    """The [sandbox_workspace_write] table, if the config has one."""
    table = tomllib.loads(text).get("sandbox_workspace_write")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ValueError("sandbox_workspace_write is not a table")
    if "writable_roots" in table:
        _paths(table["writable_roots"], CODEX_KEY)
    return table


def _codex_lists(table: dict[str, Any] | None, inbox: Path) -> bool:
    return any(inbox.is_relative_to(root) for root in (table or {}).get("writable_roots", []))


def _codex_edit(text: str, table: dict[str, Any] | None, inbox: Path) -> str | None:
    """The config with the inbox added, or None where adding lines can't do it (the user edits it by hand)."""
    if table is not None and "writable_roots" in table:
        return None
    nl = "\r\n" if "\r\n" in text else "\n"
    line = f"writable_roots = [{json.dumps(str(inbox), ensure_ascii=False)}]{nl}"
    new = text if not text or text.endswith("\n") else text + nl
    if table is not None and (header := CODEX_TABLE.search(new)):
        new = new[: header.end()] + line + new[header.end() :]
    else:
        new += f"{nl if new else ''}[sandbox_workspace_write]{nl}{line}"
    try:
        return new if _codex_lists(_codex_table(new), inbox) else None
    except ValueError:
        return None


def codex(path: Path, inbox: Path, env: Mapping[str, str], *, fix: bool) -> tuple[str, str]:
    """Codex's config is TOML, which the standard library only reads: a fix adds lines, keeps every other byte,
    and writes only if the result reads back with the inbox in it."""
    shown = tilde(path, env)
    try:
        text = path.read_bytes().decode("utf-8") if path.exists() else ""  # bytes: keeps CRLF line endings
        table = _codex_table(text)
    except (OSError, ValueError) as e:
        return _cant_read(shown, e)
    if _codex_lists(table, inbox):
        return "ok", f"{shown} lets Codex write the inbox"
    if not fix:
        return "missing", f"{shown}: the inbox is not in {CODEX_KEY}; `kb doctor --fix` adds it"
    new = _codex_edit(text, table, inbox)
    if new is None:
        return "problem", f"{shown}: add {json.dumps(str(inbox), ensure_ascii=False)} to {CODEX_KEY} by hand"
    try:
        _write(path, new)
    except OSError as e:
        return _cant_write(shown, e)
    return "fixed", f"{shown}: added {inbox} to {CODEX_KEY}"
