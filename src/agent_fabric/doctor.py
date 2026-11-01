"""`kb doctor`: is this machine set up for agent notes? (docs/design.md §4.10)"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from agent_fabric.paths import expand, inbox_dir, tilde
from agent_fabric.sandboxes import claude_code, codex


@dataclass(frozen=True)
class Agents:
    """Which agents this machine has and where they keep their files, decided once before `--fix` writes anything
    (a skill copy creates ~/.claude, which alone doesn't mean Claude Code is installed)."""

    home: Path
    claude: Path  # Claude Code's folder: settings.json, skills/
    codex: Path  # Codex's folder: config.toml, skills/
    has_claude: bool
    has_codex: bool
    has_copilot_or_cursor: bool


def _folder(env: Mapping[str, str], variable: str, default: Path) -> Path:
    folder = expand(env.get(variable) or "", env)
    return folder if folder.is_absolute() else default


def agents(env: Mapping[str, str]) -> Agents:
    home = Path(env["HOME"])
    claude = _folder(env, "CLAUDE_CONFIG_DIR", home / ".claude")  # moves settings, skills and .claude.json
    codex = _folder(env, "CODEX_HOME", home / ".codex")
    state = home / ".claude.json" if claude == home / ".claude" else claude / ".claude.json"
    return Agents(
        home=home,
        claude=claude,
        codex=codex,
        has_claude=state.exists() or (claude / "settings.json").exists(),
        has_codex=codex.is_dir(),
        has_copilot_or_cursor=any((home / folder).is_dir() for folder in (".copilot", ".cursor")),
    )


def skill_folders(found: Agents) -> list[tuple[Path, str]]:
    """The skills folders to keep a copy in, with the agents that read each. Copilot CLI and Cursor CLI read
    Claude Code's folder too, so two copies cover all four CLI agents; Cursor CLI alone doesn't need the second."""
    folders = []
    shared = (
        found.home / ".claude" / "skills"
    )  # where Copilot CLI and Cursor CLI look, whatever CLAUDE_CONFIG_DIR says
    if found.claude / "skills" == shared:
        if found.has_claude or found.has_copilot_or_cursor:
            folders.append((shared, "Claude Code, Copilot CLI, Cursor CLI"))
    else:
        if found.has_claude:
            folders.append((found.claude / "skills", "Claude Code"))
        if found.has_copilot_or_cursor:
            folders.append((shared, "Copilot CLI, Cursor CLI"))
    if found.has_codex:
        readers = "Codex, Cursor CLI" if found.codex == found.home / ".codex" else "Codex"
        folders.append((found.codex / "skills", readers))
    return folders


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


def _skills(folders: list[tuple[Path, str]], env: Mapping[str, str], *, fix: bool) -> list[Line]:
    lines: list[Line] = []
    source = skill_source()
    for folder, readers in folders:
        skill = folder / "kb-note" / "SKILL.md"
        where = f"{tilde(skill, env)} ({readers})"
        try:
            current = skill.read_bytes() if skill.is_file() else None
        except OSError as e:
            lines.append(("problem", "skill", f"{where}: can't read it ({e.strerror or e})"))
            continue
        if current == source.encode("utf-8"):
            lines.append(("ok", "skill", where))
        elif fix:
            action = "updated" if skill.exists() else "added"
            try:
                skill.parent.mkdir(parents=True, exist_ok=True)
                skill.write_text(source, encoding="utf-8")
                lines.append(("fixed", "skill", f"{where}: {action}"))
            except OSError as e:
                lines.append(("problem", "skill", f"{where}: can't write it ({e.strerror or e})"))
        elif current is None:
            lines.append(("missing", "skill", f"{where}: `kb doctor --fix` adds it"))
        else:
            lines.append(("outdated", "skill", f"{where}: `kb doctor --fix` updates it"))
    return lines


def doctor(env: Mapping[str, str], *, fix: bool = False) -> tuple[int, str]:
    """Check the inbox, the skill copies and the sandboxes' write lists; with `fix`, set up what's missing."""
    found = agents(env)
    inbox = inbox_dir(env)
    lines = [_inbox(inbox, env, fix=fix), *_skills(skill_folders(found), env, fix=fix)]
    # Copilot CLI has no OS sandbox; Cursor CLI's doesn't run here (docs/design.md §4.10).
    sandboxes = (
        (found.has_claude, claude_code, found.claude / "settings.json"),
        (found.has_codex, codex, found.codex / "config.toml"),
    )
    for installed, check, settings in sandboxes:
        if installed:
            status, detail = check(settings, inbox, env, fix=fix)
            lines.append((status, "sandbox", detail))
    report = "".join(f"{status:<8} {what:<7} {detail}\n" for status, what, detail in lines)
    return (0 if all(status in ("ok", "fixed") for status, _, _ in lines) else 1), report
