"""Seam A: `kb hook start|stop`, as the agents' hooks run it, with the agent's hook input on stdin."""

import json
import subprocess

import pytest
from conftest import Kb

REMINDER = (
    "Run `kb inbox` before relying on the repo's state. When you find something the next agent must know, "
    "leave a note with `kb note` (skill kb-note)."
)


QUESTION = (
    "kb: before you finish: did this session find something the next agent must know (a broken or flaky state, "
    "a trap, an error and its fix, a decision and its reason)? If so, leave it with `kb note`. If not, just stop. "
    "kb asks this once per session."
)


def make_inbox(kb: Kb) -> None:
    (kb.home / ".local" / "share" / "agent-fabric" / "inbox").mkdir(parents=True, exist_ok=True)


def stop_input(kb: Kb, session: str = "s1", extra: dict[str, object] | None = None) -> str:
    fields = {"session_id": session, "cwd": str(kb.cwd), "hook_event_name": "Stop", "stop_hook_active": False}
    return json.dumps(fields | (extra or {}))


def start_input(kb: Kb) -> str:
    return json.dumps(
        {"session_id": "s1", "cwd": str(kb.cwd), "source": "startup", "hook_event_name": "SessionStart"}
    )


def test_start_hook_tells_claude_code_how_many_notes_are_open_here(kb: Kb) -> None:
    kb("note", "--agent", "Codex", "--kind", "status", "the tests fail on main")
    kb("note", "--agent", "Codex", "--kind", "gotcha", "uv needs --cache-dir in the sandbox")

    result = kb("hook", "start", "--agent", "claude-code", stdin=start_input(kb))

    assert (result.exit_code, json.loads(result.stdout)) == (
        0,
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "kb: 2 open notes from earlier sessions for ~/code/scratch, unchecked. "
                + REMINDER,
            }
        },
    )


@pytest.mark.parametrize(
    ("agent", "key"),
    [("codex", "hookSpecificOutput"), ("copilot", "additionalContext"), ("cursor", "additional_context")],
)
def test_start_hook_answers_in_each_agents_format(kb: Kb, agent: str, key: str) -> None:
    kb("note", "--agent", "Codex", "--kind", "status", "the tests fail on main")
    kb("note", "--agent", "Codex", "--kind", "gotcha", "uv needs --cache-dir in the sandbox")
    text = f"kb: 2 open notes from earlier sessions for ~/code/scratch, unchecked. {REMINDER}"
    expected = {
        "hookSpecificOutput": {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}},
        "additionalContext": {"additionalContext": text},
        "additional_context": {"additional_context": text},
    }[key]

    result = kb("hook", "start", "--agent", agent, stdin=start_input(kb))

    assert json.loads(result.stdout) == expected


def test_start_hook_counts_only_this_projects_open_notes(kb: Kb) -> None:
    resolved = kb("note", "--agent", "Codex", "--kind", "status", "the tests fail on main").stdout.split()[0]
    kb("note", "--agent", "Codex", "--resolve", resolved, "they pass again")
    elsewhere = kb.home / "code" / "other"
    elsewhere.mkdir()
    kb.cwd, here = elsewhere, kb.cwd
    kb("note", "--agent", "Codex", "--kind", "gotcha", "a note about another project")
    kb.cwd = here

    result = kb("hook", "start", "--agent", "copilot", stdin=start_input(kb))

    assert json.loads(result.stdout) == {
        "additionalContext": "kb: no open notes from earlier sessions for ~/code/scratch. When you find something "
        "the next agent must know, leave a note with `kb note` (skill kb-note)."
    }


def test_start_hook_names_a_single_note_as_one(kb: Kb) -> None:
    kb("note", "--agent", "Codex", "--kind", "status", "the tests fail on main")

    result = kb("hook", "start", "--agent", "cursor", stdin=start_input(kb))

    assert json.loads(result.stdout)["additional_context"].startswith(
        "kb: 1 open note from earlier sessions for ~/code/scratch, unchecked."
    )


@pytest.mark.parametrize("remote", [True, False], ids=["with a remote", "without one"])
def test_start_hook_names_a_git_repos_project_as_kb_note_does(kb: Kb, remote: bool) -> None:
    subprocess.run(["git", "init", "-q"], cwd=kb.cwd, check=True)
    if remote:
        subprocess.run(["git", "remote", "add", "origin", "git@github.com:me/scratch.git"], cwd=kb.cwd, check=True)
    kb("note", "--agent", "Codex", "--kind", "status", "the tests fail on main")
    project = "github.com/me/scratch" if remote else str(kb.cwd.resolve())

    result = kb("hook", "start", "--agent", "copilot", stdin=start_input(kb))

    assert json.loads(result.stdout)["additionalContext"].startswith(
        f"kb: 1 open note from earlier sessions for {project}, unchecked."
    )


def test_start_hook_takes_cursors_folder_from_its_workspace_roots(kb: Kb) -> None:
    kb("note", "--agent", "Codex", "--kind", "status", "the tests fail on main")
    project = kb.cwd
    kb.cwd = kb.home / ".cursor"  # where Cursor runs user hooks
    kb.cwd.mkdir()
    cursor_input = {"conversation_id": "c1", "cursor_version": "2026.10.22", "workspace_roots": [str(project)]}

    result = kb("hook", "start", "--agent", "cursor", stdin=json.dumps(cursor_input))

    assert json.loads(result.stdout)["additional_context"].startswith(
        "kb: 1 open note from earlier sessions for ~/code/scratch, unchecked."
    )


def test_claude_codes_hook_stays_silent_when_cursor_runs_it(kb: Kb) -> None:
    cursor_input = {"conversation_id": "c1", "cursor_version": "2026.10.22", "workspace_roots": [str(kb.cwd)]}

    result = kb("hook", "start", "--agent", "claude-code", stdin=json.dumps(cursor_input))

    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize(
    ("agent", "stdin"),
    [
        ("claude-code", "not json"),
        ("claude-code", "[]"),
        ("claude-code", '{"cwd": 7}'),
        ("gemini", "{}"),
        ("claude-code", '{"session_id": "s1"}'),
        ("claude-code", '{"cwd": ' + "[" * 100_000),
    ],
    ids=[
        "unreadable input",
        "not an object",
        "a cwd of the wrong type",
        "an unknown agent",
        "no inbox yet",
        "input nested too deep",
    ],
)
def test_start_hook_says_nothing_rather_than_fail(kb: Kb, agent: str, stdin: str) -> None:
    result = kb("hook", "start", "--agent", agent, stdin=stdin)

    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")


def test_stop_hook_asks_claude_code_at_the_first_turn_end_ten_minutes_in(kb: Kb) -> None:
    make_inbox(kb)
    early = kb("hook", "stop", "--agent", "claude-code", stdin=stop_input(kb))
    kb.later(minutes=10)

    asked = kb("hook", "stop", "--agent", "claude-code", stdin=stop_input(kb))

    assert (early.stdout, asked.exit_code, json.loads(asked.stdout)) == (
        "",
        0,
        {"decision": "block", "reason": QUESTION},
    )


def test_stop_hook_asks_only_once_per_session(kb: Kb) -> None:
    make_inbox(kb)
    kb("hook", "stop", "--agent", "claude-code", stdin=stop_input(kb))
    kb.later(minutes=10)
    kb("hook", "stop", "--agent", "claude-code", stdin=stop_input(kb))
    kb.later(minutes=30)

    again = kb("hook", "stop", "--agent", "claude-code", stdin=stop_input(kb))
    other_session = kb("hook", "stop", "--agent", "claude-code", stdin=stop_input(kb, session="s2"))

    assert (again.stdout, other_session.stdout) == ("", "")


def test_stop_hook_counts_the_ten_minutes_from_the_start_hook(kb: Kb) -> None:
    make_inbox(kb)
    kb("hook", "start", "--agent", "codex", stdin=start_input(kb))
    kb.later(minutes=10)

    first_turn_end = kb("hook", "stop", "--agent", "codex", stdin=stop_input(kb))

    assert json.loads(first_turn_end.stdout) == {"decision": "block", "reason": QUESTION}


@pytest.mark.parametrize(
    ("agent", "extra", "inbox"),
    [
        ("claude-code", {"stop_hook_active": True}, True),
        ("claude-code", {"cursor_version": "2026.10.22"}, True),
        ("claude-code", {"session_id": None}, True),
        ("claude-code", {}, False),
        ("cursor", {"status": "aborted"}, True),
        ("cursor", {"status": "error"}, True),
        ("copilot", {"stopReason": "aborted"}, True),
    ],
    ids=[
        "already continuing for a stop hook",
        "Cursor running Claude Code's hook",
        "no session id",
        "no inbox yet",
        "the user stopped Cursor",
        "Cursor stopped on an error",
        "Copilot stopped other than at the turn's end",
    ],
)
def test_stop_hook_stays_silent(kb: Kb, agent: str, extra: dict[str, object], inbox: bool) -> None:
    if inbox:
        make_inbox(kb)
    kb("hook", "stop", "--agent", agent, stdin=stop_input(kb, extra=extra))
    kb.later(minutes=10)

    result = kb("hook", "stop", "--agent", agent, stdin=stop_input(kb, extra=extra))

    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize(
    ("agent", "hook_input", "expected"),
    [
        (
            "copilot",
            {"sessionId": "c1", "cwd": "/w", "stopReason": "end_turn", "stop_hook_active": False},
            {"decision": "block", "reason": QUESTION},
        ),
        (
            "cursor",
            {"conversation_id": "c1", "cursor_version": "2026.10.22", "status": "completed", "loop_count": 0},
            {"followup_message": QUESTION},
        ),
    ],
)
def test_stop_hook_asks_in_each_agents_format(
    kb: Kb, agent: str, hook_input: dict[str, object], expected: dict[str, str]
) -> None:
    make_inbox(kb)
    kb("hook", "stop", "--agent", agent, stdin=json.dumps(hook_input))
    kb.later(minutes=10)

    result = kb("hook", "stop", "--agent", agent, stdin=json.dumps(hook_input))

    assert json.loads(result.stdout) == expected


def test_start_hook_still_reminds_when_it_cannot_record_the_session(kb: Kb) -> None:
    make_inbox(kb)
    hooks = kb.home / ".local" / "share" / "agent-fabric" / "hooks"
    hooks.mkdir()
    hooks.chmod(0o555)
    try:
        start = kb("hook", "start", "--agent", "claude-code", stdin=start_input(kb))
        kb.later(minutes=10)
        stop = kb("hook", "stop", "--agent", "claude-code", stdin=stop_input(kb))
    finally:
        hooks.chmod(0o755)

    assert (bool(start.stdout), stop.stdout) == (True, "")


def break_the_marker(kb: Kb) -> None:
    """The one session's marker, left empty as an interrupted write would leave it."""
    (marker,) = (kb.home / ".local" / "share" / "agent-fabric" / "hooks").iterdir()
    marker.write_text("")


def test_start_hook_still_reminds_with_a_broken_marker(kb: Kb) -> None:
    make_inbox(kb)
    kb("hook", "start", "--agent", "claude-code", stdin=start_input(kb))
    break_the_marker(kb)

    result = kb("hook", "start", "--agent", "claude-code", stdin=start_input(kb))

    assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"].startswith("kb: no open notes")


def test_stop_hook_starts_the_session_over_from_a_broken_marker(kb: Kb) -> None:
    make_inbox(kb)
    kb("hook", "start", "--agent", "claude-code", stdin=start_input(kb))
    break_the_marker(kb)
    kb.later(minutes=10)
    restarted = kb("hook", "stop", "--agent", "claude-code", stdin=stop_input(kb))
    kb.later(minutes=10)

    asked = kb("hook", "stop", "--agent", "claude-code", stdin=stop_input(kb))

    assert (restarted.stdout, json.loads(asked.stdout)) == ("", {"decision": "block", "reason": QUESTION})


def test_a_new_session_removes_markers_older_than_seven_days(kb: Kb) -> None:
    make_inbox(kb)
    kb("hook", "stop", "--agent", "claude-code", stdin=stop_input(kb, session="old"))
    kb.later(days=2)
    kb("hook", "stop", "--agent", "codex", stdin=stop_input(kb, session="recent"))
    kb.later(days=6)

    kb("hook", "stop", "--agent", "claude-code", stdin=stop_input(kb, session="new"))

    markers = sorted(path.name.split("-")[0] for path in (kb.home / ".local/share/agent-fabric/hooks").iterdir())
    assert markers == ["claude", "codex"]
