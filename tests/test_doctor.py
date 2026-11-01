"""Seam D: `kb doctor`, through the in-process CLI with a fake home holding the agents' skills folders."""

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import toon_format
from conftest import Kb

SKILL = (files("agent_fabric") / "skills" / "kb-note" / "SKILL.md").read_text()


def a_command(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def install_kb(kb: Kb, tool_dir: Path) -> None:
    """As `uv tool install` does: the tool in its own folder, and a link to its command in ~/.local/bin."""
    link = kb.home / ".local" / "bin" / "kb"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(a_command(tool_dir / "agent-fabric" / "bin" / "kb"))
    kb.env["PATH"] = f"{link.parent}:/usr/bin"


@pytest.fixture(autouse=True)
def kb_on_path(kb: Kb) -> None:
    """Agents find kb on PATH, where scripts/install.sh put it with uv."""
    install_kb(kb, kb.home / ".local" / "share" / "uv" / "tools")


def report(output: str) -> dict[str, Any]:
    """The report as the official TOON decoder reads it: an independent check of the format."""
    decoded = toon_format.decode(output)
    assert isinstance(decoded, dict)
    return decoded


def rows(output: str, check: str) -> list[tuple[str, str, str]]:
    """One kind of check's rows (inbox, skill, sandbox, hooks) as (status, path, detail)."""
    return [(status, path, detail) for status, kind, path, detail in all_rows(output) if kind == check]


def hook_groups(agent: str) -> dict[str, list[dict[str, object]]]:
    """kb's hooks as Claude Code and Codex take them (docs/design.md §4.10)."""
    return {
        event: [{"hooks": [{"type": "command", "command": f"kb hook {name} --agent {agent}", "timeout": 10}]}]
        for event, name in (("SessionStart", "start"), ("Stop", "stop"))
    }


def all_rows(output: str) -> list[tuple[str, str, str, str]]:
    """Every row as (status, check, path, detail)."""
    checks = report(output)["checks"]
    assert isinstance(checks, list)
    return [(row["status"], row["check"], row["path"], row["detail"]) for row in checks]


def install_skill(kb: Kb, agent_home: str, content: str = SKILL) -> None:
    folder = kb.home / agent_home / "skills" / "kb-note"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(content)


def uses_claude_code(kb: Kb) -> None:
    """Claude Code keeps its state in ~/.claude.json; a ~/.claude folder alone may be just a skill copy."""
    (kb.home / ".claude.json").write_text("{}")


def make_inbox(kb: Kb) -> None:
    (kb.home / ".local" / "share" / "agent-fabric" / "inbox").mkdir(parents=True)


def claude_settings(kb: Kb, settings: object) -> Path:
    path = kb.home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return path


def codex_config(kb: Kb, text: str) -> Path:
    path = kb.home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_doctor_is_happy_with_a_writable_inbox_and_matching_skills(kb: Kb) -> None:
    make_inbox(kb)
    install_skill(kb, ".claude")
    install_skill(kb, ".agents")
    claude_settings(
        kb,
        {
            "sandbox": {"filesystem": {"allowWrite": ["~/.local/share/agent-fabric/inbox"]}},
            "hooks": hook_groups("claude-code"),
        },
    )
    codex_config(kb, f'[sandbox_workspace_write]\nwritable_roots = ["{kb.home}/.local/share/agent-fabric/inbox"]\n')
    (kb.home / ".codex" / "hooks.json").write_text(json.dumps({"hooks": hook_groups("codex")}))

    result = kb("doctor")

    checks = [
        ("kb", "~/.local/bin/kb", "on PATH"),
        ("inbox", "~/.local/share/agent-fabric/inbox", "writable"),
        ("skill", "~/.claude/skills/kb-note/SKILL.md", "for Claude Code and Cursor CLI"),
        ("skill", "~/.agents/skills/kb-note/SKILL.md", "for Codex and Copilot CLI"),
        ("sandbox", "~/.claude/settings.json", "Claude Code can write the inbox"),
        ("sandbox", "~/.codex/config.toml", "Codex can write the inbox"),
        ("hooks", "~/.claude/settings.json", "start and stop"),
        ("hooks", "~/.codex/hooks.json", "start and stop, once approved in Codex's /hooks"),
    ]
    assert (result.exit_code, result.stdout.splitlines()[0], report(result.stdout)) == (
        0,
        "summary: all 8 checks ok; nothing to do",
        {
            "summary": "all 8 checks ok; nothing to do",
            "checks": [{"status": "ok", "check": c, "path": p, "detail": d} for c, p, d in checks],
            "next": [],
        },
    )


def test_doctor_reports_a_missing_skill_copy_and_how_to_add_it(kb: Kb) -> None:
    uses_claude_code(kb)
    make_inbox(kb)
    install_skill(kb, ".claude")
    (kb.home / ".codex").mkdir()

    result = kb("doctor")

    missing = [row for row in rows(result.stdout, "skill") if row[0] == "missing"]
    assert (result.exit_code, missing) == (
        1,
        [("missing", "~/.agents/skills/kb-note/SKILL.md", "for Codex and Copilot CLI")],
    )


def test_doctor_reports_an_outdated_skill_copy(kb: Kb) -> None:
    uses_claude_code(kb)
    install_skill(kb, ".claude", content=SKILL.replace("Most sessions", "Every session"))

    result = kb("doctor")

    assert (result.exit_code, rows(result.stdout, "skill")) == (
        1,
        [("outdated", "~/.claude/skills/kb-note/SKILL.md", "for Claude Code and Cursor CLI")],
    )


def test_doctor_skips_agents_that_are_not_installed(kb: Kb) -> None:
    uses_claude_code(kb)
    install_skill(kb, ".claude")

    assert ".codex" not in kb("doctor").stdout


def test_doctor_reports_an_inbox_it_cannot_write(kb: Kb) -> None:
    install_skill(kb, ".claude")
    share = kb.home / ".local" / "share"
    share.mkdir(parents=True, exist_ok=True)
    share.chmod(0o555)
    try:
        result = kb("doctor")
    finally:
        share.chmod(0o755)

    assert (result.exit_code, rows(result.stdout, "inbox")) == (
        1,
        [("problem", "~/.local/share/agent-fabric/inbox", "not writable")],
    )


def test_fix_adds_missing_and_updates_outdated_skill_copies(kb: Kb) -> None:
    uses_claude_code(kb)
    install_skill(kb, ".claude", content="an old version\n")
    (kb.home / ".codex").mkdir()
    kb("doctor", "--fix")

    assert kb("doctor").exit_code == 0


def test_doctor_reports_a_missing_inbox_folder_because_sandboxes_need_it(kb: Kb) -> None:
    install_skill(kb, ".claude")

    result = kb("doctor")

    assert (result.exit_code, rows(result.stdout, "inbox")) == (
        1,
        [("missing", "~/.local/share/agent-fabric/inbox", "a sandbox can only grant writes to a folder that exists")],
    )


def test_fix_reports_a_skills_folder_it_cannot_write(kb: Kb) -> None:
    uses_claude_code(kb)
    make_inbox(kb)
    claude = kb.home / ".claude"
    claude.mkdir()
    claude.chmod(0o555)
    try:
        result = kb("doctor", "--fix")
    finally:
        claude.chmod(0o755)

    assert (result.exit_code, rows(result.stdout, "skill")) == (
        1,
        [("problem", "~/.claude/skills/kb-note/SKILL.md", "can't write it (Permission denied)")],
    )


@pytest.mark.parametrize(
    ("agent_home", "skill", "readers"),
    [
        (".copilot", "~/.agents/skills/kb-note/SKILL.md", "for Codex and Copilot CLI"),
        (".cursor", "~/.claude/skills/kb-note/SKILL.md", "for Claude Code and Cursor CLI"),
    ],
)
def test_doctor_checks_the_skills_folder_copilot_or_cursor_reads_when_alone(
    kb: Kb, agent_home: str, skill: str, readers: str
) -> None:
    make_inbox(kb)
    (kb.home / agent_home).mkdir()

    result = kb("doctor")

    assert (result.exit_code, rows(result.stdout, "skill")) == (1, [("missing", skill, readers)])


def test_doctor_reports_that_claude_codes_sandbox_cannot_write_the_inbox(kb: Kb) -> None:
    make_inbox(kb)
    install_skill(kb, ".claude")
    claude_settings(kb, {"sandbox": {"filesystem": {"allowWrite": ["~/.local/share/tuicr"]}}})

    result = kb("doctor")

    assert (result.exit_code, rows(result.stdout, "sandbox")) == (
        1,
        [("missing", "~/.claude/settings.json", "the inbox is not in sandbox.filesystem.allowWrite")],
    )


def test_fix_adds_the_inbox_to_claude_codes_sandbox_and_keeps_the_other_settings(kb: Kb) -> None:
    make_inbox(kb)
    path = claude_settings(
        kb,
        {
            "permissions": {"allow": ["Bash(tuicr review list *)"]},
            "sandbox": {"enabled": True, "filesystem": {"allowWrite": ["~/.local/share/tuicr"]}},
        },
    )

    fixed = rows(kb("doctor", "--fix").stdout, "sandbox")

    assert (fixed, json.loads(path.read_text())) == (
        [
            (
                "fixed",
                "~/.claude/settings.json",
                "added ~/.local/share/agent-fabric/inbox to sandbox.filesystem.allowWrite",
            )
        ],
        {
            "permissions": {"allow": ["Bash(tuicr review list *)"]},
            "sandbox": {
                "enabled": True,
                "filesystem": {"allowWrite": ["~/.local/share/tuicr", "~/.local/share/agent-fabric/inbox"]},
            },
            "hooks": hook_groups("claude-code"),
        },
    )


def test_doctor_reports_that_codexs_sandbox_cannot_write_the_inbox(kb: Kb) -> None:
    make_inbox(kb)
    install_skill(kb, ".agents")
    codex_config(kb, "[tui]\nscreen_reader_detection_done = true\n")

    result = kb("doctor")

    assert (result.exit_code, rows(result.stdout, "sandbox")) == (
        1,
        [("missing", "~/.codex/config.toml", "the inbox is not in sandbox_workspace_write.writable_roots")],
    )


def test_fix_adds_a_write_table_for_the_inbox_to_codexs_config_and_keeps_the_rest(kb: Kb) -> None:
    make_inbox(kb)
    path = codex_config(kb, '# my settings\n[tui]\nscreen_reader_detection_done = true\n\n[tui.nux]\n"m" = 1\n')
    inbox = kb.home / ".local" / "share" / "agent-fabric" / "inbox"

    fixed = rows(kb("doctor", "--fix").stdout, "sandbox")[-1]

    assert (fixed, path.read_text()) == (
        ("fixed", "~/.codex/config.toml", f"added {inbox} to sandbox_workspace_write.writable_roots"),
        '# my settings\n[tui]\nscreen_reader_detection_done = true\n\n[tui.nux]\n"m" = 1\n'
        f'\n[sandbox_workspace_write]\nwritable_roots = ["{inbox}"]\n',
    )


def test_fix_adds_writable_roots_under_codexs_existing_write_table(kb: Kb) -> None:
    make_inbox(kb)
    path = codex_config(kb, "[sandbox_workspace_write]  # writes\nnetwork_access = true\n\n[tui]\nx = 1\n")
    inbox = kb.home / ".local" / "share" / "agent-fabric" / "inbox"

    kb("doctor", "--fix")

    assert path.read_text() == (
        f'[sandbox_workspace_write]  # writes\nwritable_roots = ["{inbox}"]\nnetwork_access = true\n\n[tui]\nx = 1\n'
    )


def test_fix_leaves_codexs_existing_writable_roots_for_the_user_to_extend(kb: Kb) -> None:
    make_inbox(kb)
    config = '[sandbox_workspace_write]\nwritable_roots = [\n  "/srv/cache",\n]\n'
    path = codex_config(kb, config)

    result = kb("doctor", "--fix")

    assert (result.exit_code, rows(result.stdout, "sandbox")[-1], path.read_text()) == (
        1,
        (
            "problem",
            "~/.codex/config.toml",
            "kb can't add the inbox to sandbox_workspace_write.writable_roots without changing other lines",
        ),
        config,
    )


@pytest.mark.parametrize(
    "config",
    [
        "sandbox_workspace_write.network_access = true\n",
        'note = """\n[sandbox_workspace_write]\n"""\n[sandbox_workspace_write]\nnetwork_access = true\n',
    ],
    ids=["dotted keys", "a header inside a string"],
)
def test_fix_leaves_a_codex_config_it_cannot_edit_safely_unchanged(kb: Kb, config: str) -> None:
    make_inbox(kb)
    path = codex_config(kb, config)

    result = kb("doctor", "--fix")

    assert (result.exit_code, rows(result.stdout, "sandbox")[-1][0], path.read_text()) == (1, "problem", config)


@pytest.mark.parametrize(
    ("file", "content"),
    [
        (".claude/settings.json", '{"sandbox": '),
        (".claude/settings.json", '{"sandbox": {"filesystem": {"allowWrite": "~/.local/share/agent-fabric"}}}'),
        (".claude/settings.json", '{"sandbox": true}'),
        (".claude/settings.json", "[]"),
        (".codex/config.toml", "[tui\n"),
        (".codex/config.toml", "sandbox_workspace_write = 1\n"),
    ],
    ids=[
        "broken JSON",
        "a string for the list",
        "a value for the object",
        "not an object",
        "broken TOML",
        "a value for the table",
    ],
)
def test_fix_reports_and_keeps_an_agent_settings_file_it_cannot_read(kb: Kb, file: str, content: str) -> None:
    make_inbox(kb)
    path = kb.home / file
    path.parent.mkdir(parents=True)
    path.write_text(content)

    result = kb("doctor", "--fix")

    status, shown, detail = rows(result.stdout, "sandbox")[0]
    assert (result.exit_code, status, shown, detail.startswith("can't read it ("), path.read_text()) == (
        1,
        "problem",
        f"~/{file}",
        True,
        content,
    )


@pytest.mark.parametrize("agent_home", [".copilot", ".cursor"])
def test_fix_writes_no_claude_code_settings_for_copilot_or_cursor_alone(kb: Kb, agent_home: str) -> None:
    make_inbox(kb)
    (kb.home / agent_home).mkdir()
    kb("doctor", "--fix")

    assert ((kb.home / ".claude" / "settings.json").exists(), kb("doctor").exit_code) == (False, 0)


def test_doctor_checks_claude_codes_sandbox_before_it_has_a_settings_file(kb: Kb) -> None:
    make_inbox(kb)
    (kb.home / ".claude.json").write_text("{}")

    assert rows(kb("doctor").stdout, "sandbox") == [
        ("missing", "~/.claude/settings.json", "the inbox is not in sandbox.filesystem.allowWrite")
    ]


def test_doctor_reports_a_skill_copy_it_cannot_read(kb: Kb) -> None:
    uses_claude_code(kb)
    make_inbox(kb)
    install_skill(kb, ".claude")
    skill = kb.home / ".claude" / "skills" / "kb-note" / "SKILL.md"
    skill.chmod(0o000)
    try:
        result = kb("doctor", "--fix")
    finally:
        skill.chmod(0o644)

    assert (result.exit_code, rows(result.stdout, "skill")) == (
        1,
        [("problem", "~/.claude/skills/kb-note/SKILL.md", "can't read it (Permission denied)")],
    )


def test_fix_sets_up_claude_code_known_only_by_its_state_file(kb: Kb) -> None:
    make_inbox(kb)
    (kb.home / ".claude.json").write_text("{}")
    kb("doctor", "--fix")

    assert kb("doctor").exit_code == 0


def test_doctor_accepts_a_write_list_entry_for_a_folder_that_holds_the_inbox(kb: Kb) -> None:
    make_inbox(kb)
    claude_settings(kb, {"sandbox": {"filesystem": {"allowWrite": ["~/.local/share"]}}})
    codex_config(kb, f'[sandbox_workspace_write]\nwritable_roots = ["{kb.home}/.local/share/"]\n')

    assert rows(kb("doctor").stdout, "sandbox") == [
        ("ok", "~/.claude/settings.json", "Claude Code can write the inbox"),
        ("ok", "~/.codex/config.toml", "Codex can write the inbox"),
    ]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            b"[sandbox_workspace_write]\r\nnetwork_access = true\r\n",
            b"[sandbox_workspace_write]\r\n{line}\r\nnetwork_access = true\r\n",
        ),
        (b"[tui]\r\nx = 1\r\n", b"[tui]\r\nx = 1\r\n\r\n[sandbox_workspace_write]\r\n{line}\r\n"),
    ],
    ids=["existing-table", "new-table"],
)
def test_fix_keeps_the_line_endings_of_codexs_config(kb: Kb, before: bytes, after: bytes) -> None:
    make_inbox(kb)
    path = codex_config(kb, "")
    path.write_bytes(before)
    inbox = kb.home / ".local" / "share" / "agent-fabric" / "inbox"

    kb("doctor", "--fix")

    assert path.read_bytes() == after.replace(b"{line}", f'writable_roots = ["{inbox}"]'.encode())


def test_fix_follows_claude_config_dir(kb: Kb) -> None:
    make_inbox(kb)
    kb.env["CLAUDE_CONFIG_DIR"] = str(kb.home / ".claude-work")
    (kb.home / ".claude-work").mkdir()
    (kb.home / ".claude-work" / ".claude.json").write_text("{}")

    result = kb("doctor", "--fix")

    assert (all_rows(result.stdout)[2:], (kb.home / ".claude").exists()) == (
        [
            ("fixed", "skill", "~/.claude-work/skills/kb-note/SKILL.md", "added for Claude Code"),
            (
                "fixed",
                "sandbox",
                "~/.claude-work/settings.json",
                "added ~/.local/share/agent-fabric/inbox to sandbox.filesystem.allowWrite",
            ),
            ("fixed", "hooks", "~/.claude-work/settings.json", "installed start and stop"),
        ],
        False,
    )


def test_with_claude_config_dir_cursor_keeps_its_own_copy(kb: Kb) -> None:
    make_inbox(kb)
    kb.env["CLAUDE_CONFIG_DIR"] = str(kb.home / ".claude-work")
    (kb.home / ".cursor").mkdir()

    assert rows(kb("doctor").stdout, "skill") == [("missing", "~/.claude/skills/kb-note/SKILL.md", "for Cursor CLI")]


def test_fix_follows_codex_home(kb: Kb) -> None:
    make_inbox(kb)
    codex_home = kb.home / ".config" / "codex"
    codex_home.mkdir(parents=True)
    kb.env["CODEX_HOME"] = str(codex_home)

    result = kb("doctor", "--fix")

    assert (all_rows(result.stdout)[2:], (kb.home / ".codex").exists()) == (
        [
            ("fixed", "skill", "~/.agents/skills/kb-note/SKILL.md", "added for Codex and Copilot CLI"),
            (
                "fixed",
                "sandbox",
                "~/.config/codex/config.toml",
                f"added {kb.home}/.local/share/agent-fabric/inbox to sandbox_workspace_write.writable_roots",
            ),
            ("fixed", "hooks", "~/.config/codex/hooks.json", "installed start and stop"),
        ],
        False,
    )


@pytest.mark.parametrize("codex_home", [".codex", ".config/codex"])
def test_fix_moves_codexs_old_skill_copy_to_the_agents_folder(kb: Kb, codex_home: str) -> None:
    make_inbox(kb)
    install_skill(kb, codex_home)
    if codex_home != ".codex":
        kb.env["CODEX_HOME"] = str(kb.home / codex_home)

    result = kb("doctor", "--fix")

    assert (rows(result.stdout, "skill"), (kb.home / codex_home / "skills" / "kb-note").exists()) == (
        [
            ("fixed", "~/.agents/skills/kb-note/SKILL.md", "added for Codex and Copilot CLI"),
            (
                "fixed",
                f"~/{codex_home}/skills/kb-note/SKILL.md",
                "removed the old copy so Codex doesn't load the skill twice",
            ),
        ],
        False,
    )


def test_fix_keeps_codexs_old_skill_copy_until_the_agents_folder_has_one(kb: Kb) -> None:
    make_inbox(kb)
    install_skill(kb, ".codex")
    agents = kb.home / ".agents"
    agents.mkdir()
    agents.chmod(0o555)
    try:
        result = kb("doctor", "--fix")
    finally:
        agents.chmod(0o755)

    assert (rows(result.stdout, "skill")[-1], (kb.home / ".codex" / "skills" / "kb-note" / "SKILL.md").exists()) == (
        ("outdated", "~/.codex/skills/kb-note/SKILL.md", "an old copy kept until ~/.agents/skills has one"),
        True,
    )


def test_doctor_reports_codexs_old_skill_copy_for_fix_to_remove(kb: Kb) -> None:
    make_inbox(kb)
    install_skill(kb, ".agents")
    install_skill(kb, ".codex")

    result = kb("doctor")

    assert (result.exit_code, rows(result.stdout, "skill")[-1]) == (
        1,
        ("outdated", "~/.codex/skills/kb-note/SKILL.md", "an old copy that Codex would load twice"),
    )


def test_with_copilot_home_copilot_gets_its_own_copy(kb: Kb) -> None:
    make_inbox(kb)
    copilot_home = kb.home / ".config" / "copilot"
    copilot_home.mkdir(parents=True)
    kb.env["COPILOT_HOME"] = str(copilot_home)

    assert all_rows(kb("doctor").stdout)[2:] == [
        ("missing", "skill", "~/.config/copilot/skills/kb-note/SKILL.md", "for Copilot CLI"),
        ("missing", "hooks", "~/.config/copilot/hooks/kb.json", "start and stop missing"),
    ]


def test_doctor_reports_kbs_hooks_missing_from_claude_codes_settings(kb: Kb) -> None:
    make_inbox(kb)
    claude_settings(kb, {"sandbox": {"filesystem": {"allowWrite": ["~/.local/share/agent-fabric/inbox"]}}})

    result = kb("doctor")

    assert (result.exit_code, rows(result.stdout, "hooks")) == (
        1,
        [("missing", "~/.claude/settings.json", "start and stop missing")],
    )


def test_fix_adds_kbs_hooks_to_claude_codes_settings_and_keeps_the_rest(kb: Kb) -> None:
    make_inbox(kb)
    theirs = {"hooks": [{"type": "command", "command": "notify-send done"}]}
    path = claude_settings(kb, {"model": "opus", "hooks": {"Stop": [theirs]}})

    result = kb("doctor", "--fix")

    settings = json.loads(path.read_text())
    assert (rows(result.stdout, "hooks"), settings["model"], settings["hooks"]) == (
        [("fixed", "~/.claude/settings.json", "installed start and stop")],
        "opus",
        {
            "Stop": [theirs, *hook_groups("claude-code")["Stop"]],
            "SessionStart": hook_groups("claude-code")["SessionStart"],
        },
    )


def test_fix_installs_kbs_hooks_for_codex_and_says_to_approve_them(kb: Kb) -> None:
    make_inbox(kb)
    (kb.home / ".codex").mkdir()

    fixed = kb("doctor", "--fix").stdout

    assert (
        rows(fixed, "hooks"),
        report(fixed)["next"],
        json.loads((kb.home / ".codex" / "hooks.json").read_text()),
        rows(kb("doctor").stdout, "hooks"),
    ) == (
        [("fixed", "~/.codex/hooks.json", "installed start and stop")],
        [
            {
                "by": "user",
                "step": "Open Codex and approve kb's hooks in /hooks: Codex runs new or changed hooks only then",
            }
        ],
        {"hooks": hook_groups("codex")},
        [("ok", "~/.codex/hooks.json", "start and stop, once approved in Codex's /hooks")],
    )


@pytest.mark.parametrize(
    ("agent_home", "file", "hooks"),
    [
        (
            ".copilot",
            ".copilot/hooks/kb.json",
            {
                "sessionStart": [{"type": "command", "bash": "kb hook start --agent copilot", "timeoutSec": 10}],
                "agentStop": [{"type": "command", "bash": "kb hook stop --agent copilot", "timeoutSec": 10}],
            },
        ),
        (
            ".cursor",
            ".cursor/hooks.json",
            {
                "sessionStart": [{"type": "command", "command": "kb hook start --agent cursor", "timeout": 10}],
                "stop": [{"type": "command", "command": "kb hook stop --agent cursor", "timeout": 10}],
            },
        ),
    ],
    ids=["Copilot CLI", "Cursor CLI"],
)
def test_fix_installs_kbs_hooks_for_copilot_and_cursor(
    kb: Kb, agent_home: str, file: str, hooks: dict[str, object]
) -> None:
    make_inbox(kb)
    (kb.home / agent_home).mkdir()

    fixed = rows(kb("doctor", "--fix").stdout, "hooks")

    assert (fixed, json.loads((kb.home / file).read_text()), kb("doctor").exit_code) == (
        [("fixed", f"~/{file}", "installed start and stop")],
        {"version": 1, "hooks": hooks},
        0,
    )


def test_fix_updates_an_outdated_kb_hook_and_keeps_the_others(kb: Kb) -> None:
    make_inbox(kb)
    (kb.home / ".codex").mkdir()
    old = {"hooks": [{"type": "command", "command": "kb hook stop --agent codex", "timeout": 30}]}
    theirs = {"hooks": [{"type": "command", "command": "say done"}]}
    path = kb.home / ".codex" / "hooks.json"
    path.write_text(
        json.dumps({"hooks": {"SessionStart": hook_groups("codex")["SessionStart"], "Stop": [old, theirs]}})
    )

    before = rows(kb("doctor").stdout, "hooks")
    fixed = kb("doctor", "--fix").stdout

    assert (before, rows(fixed, "hooks"), report(fixed)["next"], json.loads(path.read_text())["hooks"]["Stop"]) == (
        [("outdated", "~/.codex/hooks.json", "stop differs from this kb's")],
        [("fixed", "~/.codex/hooks.json", "updated stop")],
        [
            {
                "by": "user",
                "step": "Open Codex and approve kb's hooks in /hooks: Codex runs new or changed hooks only then",
            }
        ],
        [theirs, *hook_groups("codex")["Stop"]],
    )


def test_fix_takes_kbs_hook_out_of_a_group_it_shares_and_keeps_the_other_handlers(kb: Kb) -> None:
    make_inbox(kb)
    theirs = {"type": "command", "command": "notify-send done"}
    ours = {"type": "command", "command": "kb hook stop --agent claude-code", "timeout": 10}
    start = hook_groups("claude-code")["SessionStart"]
    path = claude_settings(kb, {"hooks": {"SessionStart": start, "Stop": [{"matcher": "", "hooks": [theirs, ours]}]}})

    before = rows(kb("doctor").stdout, "hooks")
    kb("doctor", "--fix")

    assert (before, json.loads(path.read_text())["hooks"]["Stop"]) == (
        [("outdated", "~/.claude/settings.json", "stop differs from this kb's")],
        [{"matcher": "", "hooks": [theirs]}, *hook_groups("claude-code")["Stop"]],
    )


def test_kb_knows_its_hooks_by_the_program_and_its_first_argument(kb: Kb) -> None:
    make_inbox(kb)
    theirs = {"hooks": [{"type": "command", "command": "kb hookshot --notify"}]}
    by_path = {
        "hooks": [{"type": "command", "command": "/opt/kb/bin/kb hook stop --agent claude-code", "timeout": 10}]
    }
    start = hook_groups("claude-code")["SessionStart"]
    path = claude_settings(kb, {"hooks": {"SessionStart": start, "Stop": [theirs, by_path]}})

    before = rows(kb("doctor").stdout, "hooks")
    kb("doctor", "--fix")

    assert (before, json.loads(path.read_text())["hooks"]["Stop"]) == (
        [("outdated", "~/.claude/settings.json", "stop differs from this kb's")],
        [theirs, *hook_groups("claude-code")["Stop"]],
    )


@pytest.mark.parametrize(
    ("recorded", "detail", "fixed"),
    [
        (None, "start missing; stop differs from this kb's", "installed start; updated stop"),
        ('["start"]', "start missing; stop not in the recorded choice", "installed start; removed stop"),
    ],
    ids=["missing and outdated", "missing and left out"],
)
def test_doctor_names_every_kind_of_trouble_in_a_hooks_file(
    kb: Kb, recorded: str | None, detail: str, fixed: str
) -> None:
    make_inbox(kb)
    old = {"hooks": [{"type": "command", "command": "kb hook stop --agent claude-code", "timeout": 30}]}
    claude_settings(kb, {"hooks": {"Stop": [old]}})
    if recorded:
        config = kb.home / ".config" / "agent-fabric" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(f"[notes]\nhooks = {recorded}\n")

    before = rows(kb("doctor").stdout, "hooks")
    after = rows(kb("doctor", "--fix").stdout, "hooks")

    assert (before, after) == (
        [("outdated", "~/.claude/settings.json", detail)],
        [("fixed", "~/.claude/settings.json", fixed)],
    )


@pytest.mark.parametrize(
    "content",
    ['{"hooks": ', "[]", '{"hooks": []}', '{"hooks": {"Stop": {}}}', '{"hooks": {"Stop": [7]}}'],
    ids=["broken JSON", "not an object", "a list for the events", "an object for an event", "a number for an entry"],
)
def test_fix_reports_and_keeps_a_hooks_file_it_cannot_read(kb: Kb, content: str) -> None:
    make_inbox(kb)
    (kb.home / ".codex").mkdir()
    path = kb.home / ".codex" / "hooks.json"
    path.write_text(content)

    result = kb("doctor", "--fix")

    status, shown, detail = rows(result.stdout, "hooks")[0]
    assert (result.exit_code, status, shown, detail.startswith("can't read it ("), path.read_text()) == (
        1,
        "problem",
        "~/.codex/hooks.json",
        True,
        content,
    )


def test_fix_with_a_hooks_choice_records_it_and_installs_only_those(kb: Kb) -> None:
    make_inbox(kb)
    path = claude_settings(kb, {"sandbox": {"filesystem": {"allowWrite": ["~/.local/share/agent-fabric/inbox"]}}})

    kb("doctor", "--fix", "--hooks", "start")
    later = kb("doctor")

    assert (later.exit_code, rows(later.stdout, "hooks"), json.loads(path.read_text())["hooks"]) == (
        0,
        [("ok", "~/.claude/settings.json", "start")],
        {"SessionStart": hook_groups("claude-code")["SessionStart"]},
    )


def test_a_new_hooks_choice_removes_the_left_out_hook(kb: Kb) -> None:
    make_inbox(kb)
    (kb.home / ".codex").mkdir()
    kb("doctor", "--fix")

    fixed = kb("doctor", "--fix", "--hooks", "stop").stdout

    assert (
        rows(fixed, "hooks"),
        report(fixed)["next"],
        json.loads((kb.home / ".codex" / "hooks.json").read_text()),
    ) == (
        [("fixed", "~/.codex/hooks.json", "removed start")],
        [],  # nothing for Codex to approve
        {"hooks": {"Stop": hook_groups("codex")["Stop"]}},
    )


def test_hooks_none_removes_kbs_hooks_and_nothing_else(kb: Kb) -> None:
    make_inbox(kb)
    theirs = {"hooks": [{"type": "command", "command": "notify-send done"}]}
    path = claude_settings(
        kb, {"hooks": {"Stop": [theirs], **{k: v for k, v in hook_groups("claude-code").items() if k != "Stop"}}}
    )

    kb("doctor", "--fix", "--hooks", "none")
    later = kb("doctor")

    assert (rows(later.stdout, "hooks"), json.loads(path.read_text())["hooks"]) == ([], {"Stop": [theirs]})


def test_doctor_reports_a_kb_hook_the_recorded_choice_leaves_out(kb: Kb) -> None:
    make_inbox(kb)
    claude_settings(kb, {"hooks": hook_groups("claude-code")})
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[notes]\nhooks = ["start"]\n')

    assert rows(kb("doctor").stdout, "hooks") == [
        ("outdated", "~/.claude/settings.json", "stop not in the recorded choice")
    ]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("# mine\n[publish]\nallow = []\n", '# mine\n[publish]\nallow = []\n\n[notes]\nhooks = ["stop"]\n'),
        (
            '[notes]\nhooks = ["start", "stop"]\n\n[publish]\nallow = []\n',
            '[notes]\nhooks = ["stop"]\n\n[publish]\nallow = []\n',
        ),
        ("[notes]\nx = 1\n", '[notes]\nhooks = ["stop"]\nx = 1\n'),
        ("[notes]\n[publish]\nhooks = 1\n", '[notes]\nhooks = ["stop"]\n[publish]\nhooks = 1\n'),
        ("[publish]\nallow = []\n[notes]", '[publish]\nallow = []\n[notes]\nhooks = ["stop"]\n'),
    ],
    ids=[
        "no notes table",
        "a hooks line",
        "a notes table without one",
        "the next table right after",
        "a notes header at the end",
    ],
)
def test_the_hooks_choice_changes_one_line_of_kbs_config(kb: Kb, before: str, after: str) -> None:
    make_inbox(kb)
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(before)

    kb("doctor", "--fix", "--hooks", "stop")

    assert config.read_text() == after


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            b'[notes]\r\nhooks = ["start", "stop"]\r\n\r\n[publish]\r\nallow = []\r\n',
            b'[notes]\r\nhooks = ["stop"]\r\n\r\n[publish]\r\nallow = []\r\n',
        ),
        (b"[publish]\r\nallow = []\r\n", b'[publish]\r\nallow = []\r\n\r\n[notes]\r\nhooks = ["stop"]\r\n'),
    ],
    ids=["a hooks line", "no notes table"],
)
def test_the_hooks_choice_keeps_the_line_endings_of_kbs_config(kb: Kb, before: bytes, after: bytes) -> None:
    make_inbox(kb)
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_bytes(before)

    kb("doctor", "--fix", "--hooks", "stop")

    assert config.read_bytes() == after


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        ('notes = "x"\n', "notes is not a table"),
        ("[notes]\nhooks = [[1]]\n", "notes.hooks must list some of: start, stop"),
        ('[notes]\nhooks = "start"\n', "notes.hooks must list some of: start, stop"),
        ('[notes]\nhooks = ["end"]\n', "notes.hooks must list some of: start, stop"),
    ],
    ids=["a value for the table", "a list in the list", "a string for the list", "an unknown hook"],
)
def test_doctor_reports_a_hooks_choice_it_cannot_read(kb: Kb, config: str, reason: str) -> None:
    make_inbox(kb)
    claude_settings(kb, {})
    path = kb.home / ".config" / "agent-fabric" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(config)

    result = kb("doctor")

    assert (result.exit_code, rows(result.stdout, "hooks")) == (
        1,
        [("problem", "~/.config/agent-fabric/config.toml", f"can't read it ({reason})")],
    )


def test_a_config_kb_cannot_edit_safely_is_left_alone_and_so_are_the_hooks(kb: Kb) -> None:
    make_inbox(kb)
    path = claude_settings(kb, {})
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[notes]\nhooks = [\n  "start",\n]\n')

    result = kb("doctor", "--fix", "--hooks", "stop")

    assert (rows(result.stdout, "hooks"), config.read_text(), json.loads(path.read_text()).get("hooks")) == (
        [
            (
                "problem",
                "~/.config/agent-fabric/config.toml",
                "can't record the choice of hooks",
            )
        ],
        '[notes]\nhooks = [\n  "start",\n]\n',
        None,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--fix", "--hooks", "start,end"],
        ["--fix", "--hooks", ""],
        ["--fix", "--hooks", "start,start"],
        ["--hooks", "stop"],
    ],
    ids=["an unknown hook", "nothing", "a repeat", "without --fix"],
)
def test_a_bad_hooks_choice_is_refused(kb: Kb, argv: list[str]) -> None:
    result = kb("doctor", *argv)

    assert (result.exit_code, result.stdout, result.stderr.startswith("kb: ")) == (2, "", True)


@pytest.mark.parametrize(
    "config",
    [
        '[sandbox_workspace_write]\nwritable_roots = ["/srv/cache"]\n',
        "sandbox_workspace_write.network_access = true\n",
        'note = """\n[sandbox_workspace_write]\n"""\n[sandbox_workspace_write]\nnetwork_access = true\n',
    ],
    ids=["other writable roots", "dotted keys", "a header inside a string"],
)
def test_doctor_says_to_edit_by_hand_a_codex_config_fix_cannot_extend(kb: Kb, config: str) -> None:
    make_inbox(kb)
    codex_config(kb, config)

    result = kb("doctor")

    assert (result.exit_code, rows(result.stdout, "sandbox")) == (
        1,
        [
            (
                "problem",
                "~/.codex/config.toml",
                "kb can't add the inbox to sandbox_workspace_write.writable_roots without changing other lines",
            )
        ],
    )


RUN_FIX = "In your own terminal, not in an agent's session, run kb doctor --fix (or rerun scripts/install.sh)"
HELP = (
    "These steps are for the user: an agent's sandbox can't change the agents' settings. "
    "Agents: explain them to the user in plain words."
)


def test_doctor_tells_the_user_to_run_fix_for_what_fix_can_fix(kb: Kb) -> None:
    uses_claude_code(kb)
    make_inbox(kb)

    result = report(kb("doctor").stdout)

    assert (result["summary"], result["next"], result["help"]) == (
        "3 of 5 checks need fixing: 3 by kb doctor --fix",
        [{"by": "user", "step": RUN_FIX}],
        HELP,
    )


def test_doctor_gives_a_by_hand_step_for_each_problem(kb: Kb) -> None:
    make_inbox(kb)
    codex_config(kb, '[sandbox_workspace_write]\nwritable_roots = ["/srv/cache"]\n')
    inbox = kb.home / ".local" / "share" / "agent-fabric" / "inbox"

    result = report(kb("doctor").stdout)

    assert result["next"] == [
        {"by": "user", "step": RUN_FIX},
        {"by": "user", "step": f"Add {inbox} to sandbox_workspace_write.writable_roots in ~/.codex/config.toml"},
    ]


def test_a_hooks_choice_kb_cannot_record_gives_the_line_to_set(kb: Kb) -> None:
    make_inbox(kb)
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[notes]\nhooks = [\n  "start",\n]\n')

    result = report(kb("doctor", "--fix", "--hooks", "stop").stdout)

    assert result["next"] == [
        {"by": "user", "step": 'Set hooks = ["stop"] under [notes] in ~/.config/agent-fabric/config.toml'}
    ]


def test_hooks_left_alone_in_a_broken_settings_file_say_to_run_fix_again(kb: Kb) -> None:
    make_inbox(kb)
    path = kb.home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("[]")

    result = report(kb("doctor", "--fix").stdout)

    assert result["next"] == [
        {
            "by": "user",
            "step": "Fix ~/.claude/settings.json by hand: can't read it (the settings are not a JSON object)",
        },
        {"by": "user", "step": "After fixing ~/.claude/settings.json, run kb doctor --fix again"},
    ]


def test_fix_says_to_run_again_for_what_it_could_not_fix_yet(kb: Kb) -> None:
    make_inbox(kb)
    install_skill(kb, ".codex")
    agents = kb.home / ".agents"
    agents.mkdir()
    agents.chmod(0o555)
    try:
        result = report(kb("doctor", "--fix").stdout)
    finally:
        agents.chmod(0o755)

    assert result["next"][-1] == {"by": "user", "step": "After the steps above, run kb doctor --fix again"}


def test_the_report_keeps_paths_with_commas_colons_and_quotes_intact(kb: Kb) -> None:
    data = kb.home.parent / 'data "x", y: z'
    kb.env["XDG_DATA_HOME"] = str(data)

    result = kb("doctor")

    assert rows(result.stdout, "inbox") == [
        ("missing", f"{data}/agent-fabric/inbox", "a sandbox can only grant writes to a folder that exists")
    ]


def test_doctor_reports_another_kb_that_comes_first_on_path(kb: Kb) -> None:
    make_inbox(kb)
    venv = a_command(kb.cwd / ".venv" / "bin" / "kb").parent
    kb.env["PATH"] = f"{venv}:{kb.env['PATH']}"

    result = report(kb("doctor").stdout)

    assert ([row for row in result["checks"] if row["check"] == "kb"], result["next"]) == (
        [
            {
                "status": "problem",
                "check": "kb",
                "path": "~/code/scratch/.venv/bin/kb",
                "detail": "comes first on PATH but isn't the kb scripts/install.sh installed",
            }
        ],
        [
            {
                "by": "user",
                "step": "Take ~/code/scratch/.venv/bin off PATH, or remove its kb: agents would run it instead",
            }
        ],
    )


def test_doctor_finds_the_installed_kb_in_uvs_tool_dir(kb: Kb) -> None:
    make_inbox(kb)
    kb.env["UV_TOOL_DIR"] = str(kb.home / "uv-tools")
    install_kb(kb, kb.home / "uv-tools")

    assert all_rows(kb("doctor").stdout)[0] == ("ok", "kb", "~/.local/bin/kb", "on PATH")


def test_doctor_says_how_to_put_kb_on_path_for_the_agents(kb: Kb) -> None:
    make_inbox(kb)
    kb.env["PATH"] = "/usr/bin"

    result = report(kb("doctor").stdout)

    assert ([row for row in result["checks"] if row["check"] == "kb"], result["next"]) == (
        [{"status": "problem", "check": "kb", "path": "kb", "detail": "not on PATH, where agents look for it"}],
        [{"by": "user", "step": "Put kb on PATH: run uv tool update-shell, then open a new terminal"}],
    )
