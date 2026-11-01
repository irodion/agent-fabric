"""`kb doctor`: is this machine set up for agent notes? (docs/design.md §4.10)"""

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from agent_fabric import config, hookfiles
from agent_fabric.paths import config_file, expand, inbox_dir, tilde, uv_tool_dir
from agent_fabric.report import Row, render
from agent_fabric.sandboxes import cant_read, claude_code, codex


@dataclass(frozen=True)
class Agents:
    """Which agents this machine has and where they keep their files, decided once before `--fix` writes anything
    (a skill copy creates ~/.claude, which alone doesn't mean Claude Code is installed)."""

    home: Path
    claude: Path  # Claude Code's folder: settings.json, skills/
    codex: Path  # Codex's folder: config.toml, skills/
    copilot: Path  # Copilot CLI's folder: skills/ when COPILOT_HOME moves it
    has_claude: bool
    has_codex: bool
    has_copilot: bool
    has_cursor: bool


def _folder(env: Mapping[str, str], variable: str, default: Path) -> Path:
    folder = expand(env.get(variable) or "", env)
    return folder if folder.is_absolute() else default


def agents(env: Mapping[str, str]) -> Agents:
    home = Path(env["HOME"])
    claude = _folder(env, "CLAUDE_CONFIG_DIR", home / ".claude")  # moves settings, skills and .claude.json
    codex = _folder(env, "CODEX_HOME", home / ".codex")
    copilot = _folder(env, "COPILOT_HOME", home / ".copilot")
    state = home / ".claude.json" if claude == home / ".claude" else claude / ".claude.json"
    return Agents(
        home=home,
        claude=claude,
        codex=codex,
        copilot=copilot,
        has_claude=state.exists() or (claude / "settings.json").exists(),
        has_codex=codex.is_dir(),
        has_copilot=copilot.is_dir(),
        has_cursor=(home / ".cursor").is_dir(),
    )


def skill_folders(found: Agents) -> list[tuple[Path, str]]:
    """The skills folders to keep a copy in, with the agents that read each: Claude Code's folder, which Cursor CLI
    reads too, and ~/.agents/skills for Codex and Copilot CLI (docs/design.md §4.10)."""
    folders = []
    shared = found.home / ".claude" / "skills"  # where Cursor CLI looks, whatever CLAUDE_CONFIG_DIR says
    if found.claude / "skills" == shared:
        if found.has_claude or found.has_cursor:
            folders.append((shared, "Claude Code and Cursor CLI"))
    else:
        if found.has_claude:
            folders.append((found.claude / "skills", "Claude Code"))
        if found.has_cursor:
            folders.append((shared, "Cursor CLI"))
    agents = found.home / ".agents" / "skills"  # Copilot CLI's too, unless COPILOT_HOME moves it
    if found.copilot == found.home / ".copilot":
        if found.has_codex or found.has_copilot:
            folders.append((agents, "Codex and Copilot CLI"))
    else:
        if found.has_codex:
            folders.append((agents, "Codex"))
        if found.has_copilot:
            folders.append((found.copilot / "skills", "Copilot CLI"))
    return folders


def skill_source() -> str:
    return (files("agent_fabric") / "skills" / "kb-note" / "SKILL.md").read_text(encoding="utf-8")


def _writable(folder: Path) -> bool:
    existing = next(p for p in (folder, *folder.parents) if p.exists())
    return os.access(existing, os.W_OK | os.X_OK)


def _on_path(env: Mapping[str, str]) -> Row:
    """Agents run `kb` by name, so the first kb on PATH must be the one scripts/install.sh installed: uv links it
    from ~/.local/bin to its tool folder (docs/design.md §4.10). Another, such as a virtualenv's, runs instead."""
    found = shutil.which("kb", path=env.get("PATH", ""))
    if found is None:
        step = "Put kb on PATH: run uv tool update-shell, then open a new terminal"
        return Row("problem", "kb", "kb", "not on PATH, where agents look for it", step=step)
    command = Path(found)
    shown = tilde(command, env)
    if not command.resolve().is_relative_to((uv_tool_dir(env) / "agent-fabric").resolve()):
        step = f"Take {tilde(command.parent, env)} off PATH, or remove its kb: agents would run it instead"
        return Row(
            "problem", "kb", shown, "comes first on PATH but isn't the kb scripts/install.sh installed", step=step
        )
    return Row("ok", "kb", shown, "on PATH")


def _inbox(inbox: Path, env: Mapping[str, str], *, fix: bool) -> Row:
    shown = tilde(inbox, env)
    if not _writable(inbox):
        return Row("problem", "inbox", shown, "not writable")
    if inbox.is_dir():
        return Row("ok", "inbox", shown, "writable")
    if not fix:
        return Row("missing", "inbox", shown, "a sandbox can only grant writes to a folder that exists")
    try:
        inbox.mkdir(parents=True)
    except OSError as e:
        return Row("problem", "inbox", shown, f"can't create it ({e.strerror or e})")
    return Row("fixed", "inbox", shown, "created")


def _skills(folders: list[tuple[Path, str]], env: Mapping[str, str], *, fix: bool) -> list[Row]:
    rows: list[Row] = []
    source = skill_source()
    for folder, readers in folders:
        skill = folder / "kb-note" / "SKILL.md"
        shown = tilde(skill, env)
        try:
            current = skill.read_bytes() if skill.is_file() else None
        except OSError as e:
            rows.append(Row("problem", "skill", shown, f"can't read it ({e.strerror or e})"))
            continue
        if current == source.encode("utf-8"):
            rows.append(Row("ok", "skill", shown, f"for {readers}"))
        elif fix:
            action = "updated" if skill.exists() else "added"
            try:
                skill.parent.mkdir(parents=True, exist_ok=True)
                skill.write_text(source, encoding="utf-8")
                rows.append(Row("fixed", "skill", shown, f"{action} for {readers}"))
            except OSError as e:
                rows.append(Row("problem", "skill", shown, f"can't write it ({e.strerror or e})"))
        else:
            rows.append(Row("missing" if current is None else "outdated", "skill", shown, f"for {readers}"))
    return rows


def _old_copy(found: Agents, env: Mapping[str, str], *, fix: bool) -> list[Row]:
    """Codex's copy from before ~/.agents/skills, removed once the new copy is in place (docs/design.md §4.10)."""
    old = found.codex / "skills" / "kb-note" / "SKILL.md"
    new = found.home / ".agents" / "skills" / "kb-note" / "SKILL.md"
    if old == new or not old.is_file():
        return []
    shown = tilde(old, env)
    try:
        in_place = new.is_file() and new.read_bytes() == skill_source().encode("utf-8")
    except OSError:
        in_place = False
    if not fix:
        return [Row("outdated", "skill", shown, "an old copy that Codex would load twice")]
    if not in_place:
        return [Row("outdated", "skill", shown, f"an old copy kept until {tilde(new.parent.parent, env)} has one")]
    try:
        old.unlink()
        if not any(old.parent.iterdir()):
            old.parent.rmdir()
    except OSError as e:
        return [Row("problem", "skill", shown, f"can't remove the old copy ({e.strerror or e})")]
    return [Row("fixed", "skill", shown, "removed the old copy so Codex doesn't load the skill twice")]


def _hooks_choice(env: Mapping[str, str], hooks: tuple[str, ...] | None) -> tuple[tuple[str, ...] | None, list[Row]]:
    """The hooks to install: `--hooks`, recorded for later runs, or the recorded choice; None if it can't tell."""
    shown = tilde(config_file(env), env)
    if hooks is None:
        try:
            return config.hooks_choice(env), []
        except (OSError, ValueError) as e:
            return None, [cant_read("hooks", shown, e)]
    try:
        config.record_hooks(env, hooks)
    except (OSError, ValueError):
        step = f"Set hooks = {json.dumps(list(hooks))} under [notes] in {shown}"
        return None, [Row("problem", "hooks", shown, "can't record the choice of hooks", step=step)]
    return hooks, []


def doctor(env: Mapping[str, str], *, fix: bool = False, hooks: tuple[str, ...] | None = None) -> tuple[int, str]:
    """Check the inbox, the skill copies, the sandboxes' write lists and the agents' hooks; with `fix`, set up
    what's missing. `hooks` (with `fix`) records which hooks to install."""
    found = agents(env)
    inbox = inbox_dir(env)
    rows = [
        _on_path(env),
        _inbox(inbox, env, fix=fix),
        *_skills(skill_folders(found), env, fix=fix),
        *_old_copy(found, env, fix=fix),
    ]
    # Copilot CLI has no OS sandbox; Cursor CLI's doesn't run here (docs/design.md §4.10).
    sandboxes = (
        (found.has_claude, claude_code, found.claude / "settings.json"),
        (found.has_codex, codex, found.codex / "config.toml"),
    )
    broken = set()
    for installed, check, settings in sandboxes:
        if installed:
            rows.append(row := check(settings, inbox, env, fix=fix))
            if row.status == "problem":
                broken.add(settings)
    hooks_files = (
        (found.has_claude, found.claude / "settings.json", "claude-code"),
        (found.has_codex, found.codex / "hooks.json", "codex"),
        (found.has_copilot, found.copilot / "hooks" / "kb.json", "copilot"),
        (found.has_cursor, found.home / ".cursor" / "hooks.json", "cursor"),
    )
    chosen, problems = _hooks_choice(env, hooks)
    rows += problems
    for installed, path, agent in hooks_files:
        if not installed or chosen is None:
            continue
        if path in broken:  # never rewrite a settings file with a part kb can't read
            shown = tilde(path, env)
            again = f"After fixing {shown}, run kb doctor --fix again"
            rows.append(Row("problem", "hooks", shown, "left alone until the sandbox problem is fixed", step=again))
        elif hooks_row := hookfiles.check(path, agent, chosen, env, fix=fix):
            rows.append(hooks_row)
    return (0 if all(row.status in ("ok", "fixed") for row in rows) else 1), render(rows, fix=fix)
