"""Seam D: `kb doctor`, through the in-process CLI with a fake home holding the agents' skills folders."""

from importlib.resources import files

import pytest
from conftest import Kb

SKILL = (files("agent_fabric") / "skills" / "kb-note" / "SKILL.md").read_text()


def install_skill(kb: Kb, agent_home: str, content: str = SKILL) -> None:
    folder = kb.home / agent_home / "skills" / "kb-note"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(content)


def make_inbox(kb: Kb) -> None:
    (kb.home / ".local" / "share" / "agent-fabric" / "inbox").mkdir(parents=True)


def test_doctor_is_happy_with_a_writable_inbox_and_matching_skills(kb: Kb) -> None:
    make_inbox(kb)
    install_skill(kb, ".claude")
    install_skill(kb, ".codex")

    result = kb("doctor")

    assert (result.exit_code, result.stdout) == (
        0,
        "ok       inbox  ~/.local/share/agent-fabric/inbox is writable\n"
        "ok       skill  ~/.claude/skills/kb-note/SKILL.md (Claude Code, Copilot CLI, Cursor CLI)\n"
        "ok       skill  ~/.codex/skills/kb-note/SKILL.md (Codex, Cursor CLI)\n",
    )


def test_doctor_reports_a_missing_skill_copy_and_how_to_add_it(kb: Kb) -> None:
    make_inbox(kb)
    install_skill(kb, ".claude")
    (kb.home / ".codex").mkdir()

    result = kb("doctor")

    missing = [line for line in result.stdout.splitlines() if line.startswith("missing")]
    assert (result.exit_code, missing) == (
        1,
        ["missing  skill  ~/.codex/skills/kb-note/SKILL.md (Codex, Cursor CLI): `kb doctor --fix` adds it"],
    )


def test_doctor_reports_an_outdated_skill_copy(kb: Kb) -> None:
    install_skill(kb, ".claude", content=SKILL.replace("Most sessions", "Every session"))

    result = kb("doctor")

    assert (result.exit_code, "outdated skill  ~/.claude/skills/kb-note/SKILL.md" in result.stdout) == (1, True)


def test_doctor_skips_agents_that_are_not_installed(kb: Kb) -> None:
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
        "problem  inbox  ~/.local/share/agent-fabric/inbox is not writable" in result.stdout,
    ) == (
        1,
        True,
    )


def test_fix_adds_missing_and_updates_outdated_skill_copies(kb: Kb) -> None:
    install_skill(kb, ".claude", content="an old version\n")
    (kb.home / ".codex").mkdir()
    kb("doctor", "--fix")

    assert kb("doctor").exit_code == 0


def test_doctor_reports_a_missing_inbox_folder_because_sandboxes_need_it(kb: Kb) -> None:
    install_skill(kb, ".claude")

    result = kb("doctor")

    assert (result.exit_code, result.stdout.splitlines()[0]) == (
        1,
        "missing  inbox  ~/.local/share/agent-fabric/inbox: `kb doctor --fix` creates it "
        "(a sandbox can only grant writes to a folder that exists)",
    )


def test_fix_reports_a_skills_folder_it_cannot_write(kb: Kb) -> None:
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
        "problem  skill  ~/.claude/skills/kb-note/SKILL.md (Claude Code, Copilot CLI, Cursor CLI): "
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
            "missing  skill  ~/.claude/skills/kb-note/SKILL.md (Claude Code, Copilot CLI, Cursor CLI): "
            "`kb doctor --fix` adds it"
        ],
    )
