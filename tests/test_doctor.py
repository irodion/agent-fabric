"""Seam D: `kb doctor`, through the in-process CLI with a fake home holding the agents' skills folders."""

import json
from importlib.resources import files
from pathlib import Path

import pytest
from conftest import Kb

SKILL = (files("agent_fabric") / "skills" / "kb-note" / "SKILL.md").read_text()


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
    install_skill(kb, ".codex")
    claude_settings(kb, {"sandbox": {"filesystem": {"allowWrite": ["~/.local/share/agent-fabric/inbox"]}}})
    codex_config(kb, f'[sandbox_workspace_write]\nwritable_roots = ["{kb.home}/.local/share/agent-fabric/inbox"]\n')

    result = kb("doctor")

    assert (result.exit_code, result.stdout) == (
        0,
        "ok       inbox   ~/.local/share/agent-fabric/inbox is writable\n"
        "ok       skill   ~/.claude/skills/kb-note/SKILL.md (Claude Code, Copilot CLI, Cursor CLI)\n"
        "ok       skill   ~/.codex/skills/kb-note/SKILL.md (Codex, Cursor CLI)\n"
        "ok       sandbox ~/.claude/settings.json lets Claude Code write the inbox\n"
        "ok       sandbox ~/.codex/config.toml lets Codex write the inbox\n",
    )


def test_doctor_reports_a_missing_skill_copy_and_how_to_add_it(kb: Kb) -> None:
    uses_claude_code(kb)
    make_inbox(kb)
    install_skill(kb, ".claude")
    (kb.home / ".codex").mkdir()

    result = kb("doctor")

    missing = [line for line in result.stdout.splitlines() if line.startswith("missing  skill")]
    assert (result.exit_code, missing) == (
        1,
        ["missing  skill   ~/.codex/skills/kb-note/SKILL.md (Codex, Cursor CLI): `kb doctor --fix` adds it"],
    )


def test_doctor_reports_an_outdated_skill_copy(kb: Kb) -> None:
    uses_claude_code(kb)
    install_skill(kb, ".claude", content=SKILL.replace("Most sessions", "Every session"))

    result = kb("doctor")

    assert (result.exit_code, "outdated skill   ~/.claude/skills/kb-note/SKILL.md" in result.stdout) == (1, True)


def test_doctor_skips_agents_that_are_not_installed(kb: Kb) -> None:
    uses_claude_code(kb)
    install_skill(kb, ".claude")

    assert ".codex" not in kb("doctor").stdout


def test_doctor_reports_an_inbox_it_cannot_write(kb: Kb) -> None:
    install_skill(kb, ".claude")
    share = kb.home / ".local" / "share"
    share.mkdir(parents=True)
    share.chmod(0o555)
    try:
        result = kb("doctor")
    finally:
        share.chmod(0o755)

    assert (
        result.exit_code,
        "problem  inbox   ~/.local/share/agent-fabric/inbox is not writable" in result.stdout,
    ) == (
        1,
        True,
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

    assert (result.exit_code, result.stdout.splitlines()[0]) == (
        1,
        "missing  inbox   ~/.local/share/agent-fabric/inbox: `kb doctor --fix` creates it "
        "(a sandbox can only grant writes to a folder that exists)",
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

    assert (result.exit_code, result.stdout.splitlines()[1]) == (
        1,
        "problem  skill   ~/.claude/skills/kb-note/SKILL.md (Claude Code, Copilot CLI, Cursor CLI): "
        "can't write it (Permission denied)",
    )


@pytest.mark.parametrize("agent_home", [".copilot", ".cursor"])
def test_doctor_checks_the_claude_skills_folder_for_copilot_or_cursor_alone(kb: Kb, agent_home: str) -> None:
    make_inbox(kb)
    (kb.home / agent_home).mkdir()

    result = kb("doctor")

    assert (result.exit_code, result.stdout.splitlines()[1:]) == (
        1,
        [
            "missing  skill   ~/.claude/skills/kb-note/SKILL.md (Claude Code, Copilot CLI, Cursor CLI): "
            "`kb doctor --fix` adds it"
        ],
    )


def test_doctor_reports_that_claude_codes_sandbox_cannot_write_the_inbox(kb: Kb) -> None:
    make_inbox(kb)
    install_skill(kb, ".claude")
    claude_settings(kb, {"sandbox": {"filesystem": {"allowWrite": ["~/.local/share/tuicr"]}}})

    result = kb("doctor")

    assert (result.exit_code, result.stdout.splitlines()[2]) == (
        1,
        "missing  sandbox ~/.claude/settings.json: the inbox is not in sandbox.filesystem.allowWrite; "
        "`kb doctor --fix` adds it",
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

    fixed = kb("doctor", "--fix").stdout.splitlines()[-1]

    assert (fixed, json.loads(path.read_text())) == (
        "fixed    sandbox ~/.claude/settings.json: added ~/.local/share/agent-fabric/inbox to "
        "sandbox.filesystem.allowWrite",
        {
            "permissions": {"allow": ["Bash(tuicr review list *)"]},
            "sandbox": {
                "enabled": True,
                "filesystem": {"allowWrite": ["~/.local/share/tuicr", "~/.local/share/agent-fabric/inbox"]},
            },
        },
    )


def test_doctor_reports_that_codexs_sandbox_cannot_write_the_inbox(kb: Kb) -> None:
    make_inbox(kb)
    install_skill(kb, ".codex")
    codex_config(kb, "[tui]\nscreen_reader_detection_done = true\n")

    result = kb("doctor")

    assert (result.exit_code, result.stdout.splitlines()[2]) == (
        1,
        "missing  sandbox ~/.codex/config.toml: the inbox is not in sandbox_workspace_write.writable_roots; "
        "`kb doctor --fix` adds it",
    )


def test_fix_adds_a_write_table_for_the_inbox_to_codexs_config_and_keeps_the_rest(kb: Kb) -> None:
    make_inbox(kb)
    path = codex_config(kb, '# my settings\n[tui]\nscreen_reader_detection_done = true\n\n[tui.nux]\n"m" = 1\n')
    inbox = kb.home / ".local" / "share" / "agent-fabric" / "inbox"

    fixed = kb("doctor", "--fix").stdout.splitlines()[-1]

    assert (fixed, path.read_text()) == (
        f"fixed    sandbox ~/.codex/config.toml: added {inbox} to sandbox_workspace_write.writable_roots",
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
    inbox = kb.home / ".local" / "share" / "agent-fabric" / "inbox"

    result = kb("doctor", "--fix")

    assert (result.exit_code, result.stdout.splitlines()[-1], path.read_text()) == (
        1,
        f'problem  sandbox ~/.codex/config.toml: add "{inbox}" to sandbox_workspace_write.writable_roots by hand',
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

    assert (result.exit_code, result.stdout.splitlines()[-1].endswith("by hand"), path.read_text()) == (
        1,
        True,
        config,
    )


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

    assert (
        result.exit_code,
        result.stdout.splitlines()[-1].startswith(f"problem  sandbox ~/{file}: can't read it ("),
        path.read_text(),
    ) == (
        1,
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

    assert kb("doctor").stdout.splitlines()[-1] == (
        "missing  sandbox ~/.claude/settings.json: the inbox is not in sandbox.filesystem.allowWrite; "
        "`kb doctor --fix` adds it"
    )


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

    assert (result.exit_code, result.stdout.splitlines()[1]) == (
        1,
        "problem  skill   ~/.claude/skills/kb-note/SKILL.md (Claude Code, Copilot CLI, Cursor CLI): "
        "can't read it (Permission denied)",
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

    assert [line for line in kb("doctor").stdout.splitlines() if line[9:16] == "sandbox"] == [
        "ok       sandbox ~/.claude/settings.json lets Claude Code write the inbox",
        "ok       sandbox ~/.codex/config.toml lets Codex write the inbox",
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

    assert (result.stdout.splitlines()[1:], (kb.home / ".claude").exists()) == (
        [
            "fixed    skill   ~/.claude-work/skills/kb-note/SKILL.md (Claude Code): added",
            "fixed    sandbox ~/.claude-work/settings.json: added ~/.local/share/agent-fabric/inbox to "
            "sandbox.filesystem.allowWrite",
        ],
        False,
    )


def test_with_claude_config_dir_copilot_and_cursor_keep_their_own_copy(kb: Kb) -> None:
    make_inbox(kb)
    kb.env["CLAUDE_CONFIG_DIR"] = str(kb.home / ".claude-work")
    (kb.home / ".copilot").mkdir()

    assert kb("doctor").stdout.splitlines()[1:] == [
        "missing  skill   ~/.claude/skills/kb-note/SKILL.md (Copilot CLI, Cursor CLI): `kb doctor --fix` adds it"
    ]


def test_fix_follows_codex_home(kb: Kb) -> None:
    make_inbox(kb)
    codex_home = kb.home / ".config" / "codex"
    codex_home.mkdir(parents=True)
    kb.env["CODEX_HOME"] = str(codex_home)

    result = kb("doctor", "--fix")

    assert (result.stdout.splitlines()[1:], (kb.home / ".codex").exists()) == (
        [
            "fixed    skill   ~/.config/codex/skills/kb-note/SKILL.md (Codex): added",
            f"fixed    sandbox ~/.config/codex/config.toml: added {kb.home}/.local/share/agent-fabric/inbox to "
            "sandbox_workspace_write.writable_roots",
        ],
        False,
    )
