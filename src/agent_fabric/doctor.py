"""`kb doctor`: is this machine set up for agent notes? (docs/design.md §4.10)"""

import os
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path

from agent_fabric.paths import inbox_dir, tilde
from agent_fabric.sandboxes import claude_code, codex

# User skills folders: (folder, the agents that read it, the agent home folders that make it worth checking).
# Copilot CLI and Cursor CLI read Claude Code's folder too, so these two copies cover all four CLI agents; Cursor
# CLI alone doesn't need the second one.
SKILL_FOLDERS = (
    (".claude/skills", "Claude Code, Copilot CLI, Cursor CLI", (".claude", ".copilot", ".cursor")),
    (".codex/skills", "Codex, Cursor CLI", (".codex",)),
)
# Agents whose sandbox needs the inbox on its write list, by home folder (Copilot CLI has no OS sandbox).
SANDBOXES = ((".claude", claude_code), (".codex", codex))


def skill_source() -> str:
    return (files("agent_fabric") / "skills" / "kb-note" / "SKILL.md").read_text(encoding="utf-8")


def _writable(folder: Path) -> bool:
    existing = next(p for p in (folder, *folder.parents) if p.exists())
    return os.access(existing, os.W_OK | os.X_OK)


Line = tuple[str, str, str]  # status, what, detail


def _inbox(inbox: Path, env: Mapping[str, str], *, fix: bool) -> Line:
    shown = tilde(inbox, env)
    if not _writable(inbox):
        return "problem", "inbox", f"{shown} is not writable"
    if inbox.is_dir():
        return "ok", "inbox", f"{shown} is writable"
    if not fix:
        return (
            "missing",
            "inbox",
            f"{shown}: `kb doctor --fix` creates it (a sandbox can only grant writes to a folder that exists)",
        )
    try:
        inbox.mkdir(parents=True)
    except OSError as e:
        return "problem", "inbox", f"{shown}: can't create it ({e.strerror or e})"
    return "fixed", "inbox", f"{shown}: created"


def _skills(home: Path, env: Mapping[str, str], *, fix: bool) -> list[Line]:
    lines: list[Line] = []
    source = skill_source()
    for folder, readers, agent_homes in SKILL_FOLDERS:
        if not any((home / agent_home).is_dir() for agent_home in agent_homes):
            continue
        skill = home / folder / "kb-note" / "SKILL.md"
        where = f"{tilde(skill, env)} ({readers})"
        if skill.is_file() and skill.read_bytes() == source.encode("utf-8"):
            lines.append(("ok", "skill", where))
        elif fix:
            action = "updated" if skill.exists() else "added"
            try:
                skill.parent.mkdir(parents=True, exist_ok=True)
                skill.write_text(source, encoding="utf-8")
                lines.append(("fixed", "skill", f"{where}: {action}"))
            except OSError as e:
                lines.append(("problem", "skill", f"{where}: can't write it ({e.strerror or e})"))
        elif not skill.is_file():
            lines.append(("missing", "skill", f"{where}: `kb doctor --fix` adds it"))
        else:
            lines.append(("outdated", "skill", f"{where}: `kb doctor --fix` updates it"))
    return lines


def doctor(env: Mapping[str, str], *, fix: bool = False) -> tuple[int, str]:
    """Check the inbox, the skill copies and the sandboxes' write lists; with `fix`, set up what's missing."""
    home = Path(env["HOME"])
    inbox = inbox_dir(env)
    lines = [_inbox(inbox, env, fix=fix), *_skills(home, env, fix=fix)]
    for agent_home, check in SANDBOXES:
        if (home / agent_home).is_dir():
            status, detail = check(home, inbox, env, fix=fix)
            lines.append((status, "sandbox", detail))
    report = "".join(f"{status:<8} {what:<7} {detail}\n" for status, what, detail in lines)
    return (0 if all(status in ("ok", "fixed") for status, _, _ in lines) else 1), report
