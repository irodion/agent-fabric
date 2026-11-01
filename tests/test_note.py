from pathlib import Path

import pytest
from conftest import Kb


def test_a_note_shows_up_in_the_inbox(kb: Kb) -> None:
    kb("note", "--kind", "gotcha", "--agent", "Codex", "uv needs --cache-dir in the sandbox")

    assert "uv needs --cache-dir in the sandbox" in kb("inbox").stdout


@pytest.mark.parametrize("kind_args", [[], ["--kind", "idea"]], ids=["missing", "unknown"])
def test_a_note_without_a_valid_kind_is_refused_with_the_kinds_listed(kb: Kb, kind_args: list[str]) -> None:
    result = kb("note", *kind_args, "--agent", "Codex", "uv needs --cache-dir in the sandbox")

    assert (result.exit_code, "status, gotcha, fix, decision" in result.stderr) == (2, True)


def test_a_note_over_500_characters_is_refused_with_the_limit_named(kb: Kb) -> None:
    result = kb("note", "--kind", "fix", "--agent", "Codex", "x" * 501)

    assert (result.exit_code, "500 characters" in result.stderr) == (2, True)


def test_a_note_of_exactly_500_characters_is_accepted(kb: Kb) -> None:
    assert kb("note", "--kind", "fix", "--agent", "Codex", "x" * 500).exit_code == 0


@pytest.mark.parametrize(("text", "stdin"), [("  ", ""), ("-", "\n")], ids=["blank-argument", "empty-stdin"])
def test_an_empty_note_is_refused(kb: Kb, text: str, stdin: str) -> None:
    assert kb("note", "--kind", "fix", "--agent", "Codex", text, stdin=stdin).exit_code == 2


def test_text_on_stdin_keeps_quotes_backticks_and_dollars(kb: Kb) -> None:
    text = "Run `pytest -m 'not db'` here; \"$HOME\" isn't set in CI."
    _, path = kb("note", "--kind", "fix", "--agent", "Codex", "-", stdin=text + "\n").stdout.split()

    assert Path(path).read_text().endswith(f"---\n{text}\n")


def test_refs_are_recorded_in_the_order_given(kb: Kb) -> None:
    (kb.cwd / "tests").mkdir()
    (kb.cwd / "tests" / "test_client.py").touch()
    _, path = kb(
        "note", "--kind", "status", "--agent", "Codex",
        "--ref", "pytest tests/integration", "--ref", "tests/test_client.py:12",
        "integration tests fail on main since the httpx bump",
    ).stdout.split()  # fmt: skip

    assert 'refs: ["pytest tests/integration", "tests/test_client.py:12"]\n' in Path(path).read_text()


def test_a_path_ref_that_does_not_exist_is_kept_with_a_warning(kb: Kb) -> None:
    (kb.cwd / "tests").mkdir()
    result = kb(
        "note", "--kind", "fix", "--agent", "Codex", "--ref", "tests/missing.py:3", "the fixture needs a socket"
    )

    assert (result.exit_code, "tests/missing.py" in result.stderr) == (0, True)


@pytest.mark.parametrize(
    "ref",
    [
        "pytest -m 'not db'",
        "https://example.com/issue/1",
        "9d8e7f6",
        "ModuleNotFoundError",
        "httpx.ConnectError",
        "json.loads",
        "os.path",
        "example.com",
        "v1.2",
        "origin/main",
    ],
    ids=["command", "url", "commit", "error", "dotted-name", "function", "module", "host", "version", "git-ref"],
)
def test_refs_that_are_not_paths_get_no_warning(kb: Kb, ref: str) -> None:
    assert kb("note", "--kind", "fix", "--agent", "Codex", "--ref", ref, "the fixture needs a socket").stderr == ""


CLAUDE_CODE_ENV = {
    "AI_AGENT": "claude-code_2-1-316_agent",
    "CLAUDE_CODE_SESSION_ID": "5b1e2c3d-0000-4000-8000-000000000001",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
}


def agent_line(kb: Kb, *agent_args: str) -> str:
    _, path = kb("note", "--kind", "gotcha", *agent_args, "uv needs --cache-dir in the sandbox").stdout.split()
    return next(line for line in Path(path).read_text().splitlines() if line.startswith("agent: "))


def test_claude_code_is_recognised_from_its_environment(kb: Kb) -> None:
    kb.env.update(CLAUDE_CODE_ENV)

    assert agent_line(kb) == (
        'agent: {"name": "Claude Code", "from": "env", '
        '"session": "5b1e2c3d-0000-4000-8000-000000000001", "entrypoint": "cli"}'
    )


def test_the_environment_wins_over_the_agent_flag(kb: Kb) -> None:
    kb.env.update(CLAUDE_CODE_ENV)

    assert '"name": "Claude Code", "from": "env"' in agent_line(kb, "--agent", "Claude Sonnet")


CODEX_ENV = {"CODEX_THREAD_ID": "019a3f00-0000-7000-8000-000000000002"}


def test_codex_is_recognised_from_its_environment(kb: Kb) -> None:
    kb.env.update(CODEX_ENV)

    assert (
        agent_line(kb) == 'agent: {"name": "Codex", "from": "env", "session": "019a3f00-0000-7000-8000-000000000002"}'
    )


def test_a_tool_named_in_the_flag_wins_over_an_inherited_environment(kb: Kb) -> None:
    kb.env.update(CLAUDE_CODE_ENV)  # e.g. Copilot CLI started from a Claude Code session

    assert agent_line(kb, "--agent", "copilot cli") == 'agent: {"name": "Copilot CLI", "from": "flag"}'


def test_two_agents_in_the_environment_need_the_flag_to_tell_which(kb: Kb) -> None:
    kb.env.update(CLAUDE_CODE_ENV | CODEX_ENV)  # Codex started from Claude Code, or the other way round
    unflagged = kb("note", "--kind", "gotcha", "uv needs --cache-dir in the sandbox")

    assert (unflagged.exit_code, "--agent" in unflagged.stderr, agent_line(kb, "--agent", "Codex")) == (
        2,
        True,
        'agent: {"name": "Codex", "from": "env", "session": "019a3f00-0000-7000-8000-000000000002"}',
    )


def test_an_unknown_agent_must_name_its_tool(kb: Kb) -> None:
    result = kb("note", "--kind", "gotcha", "uv needs --cache-dir in the sandbox")

    assert (result.exit_code, "--agent" in result.stderr, "Cursor CLI" in result.stderr) == (2, True, True)


def note_id(kb: Kb, text: str = "uv needs --cache-dir in the sandbox", kind: str = "gotcha") -> str:
    return kb("note", "--kind", kind, "--agent", "Codex", text).stdout.split()[0]


def test_the_same_note_within_24_hours_returns_the_first_note(kb: Kb) -> None:
    first = note_id(kb)
    kb.later(hours=23)

    assert note_id(kb) == first


def test_the_same_note_after_24_hours_is_a_new_note(kb: Kb) -> None:
    first = note_id(kb)
    kb.later(hours=24, seconds=1)

    assert note_id(kb) != first


def test_two_different_notes_in_the_same_second_are_both_kept(kb: Kb) -> None:
    note_id(kb, "uv needs --cache-dir in the sandbox")
    note_id(kb, "ruff runs from the project venv")

    assert len(list((kb.home / ".local/share/agent-fabric/inbox").glob("*.md"))) == 2


def test_a_note_outside_git_is_written_in_the_documented_format(kb: Kb) -> None:
    note_id, path = kb(
        "note", "--kind", "gotcha", "--agent", "Codex", "uv needs --cache-dir in the sandbox"
    ).stdout.split()

    assert Path(path).read_text() == (
        "---\n"
        'schema: "fabric.note/1"\n'
        f'id: "{note_id}"\n'
        'kind: "gotcha"\n'
        'agent: {"name": "Codex", "from": "flag"}\n'
        "project: null\n"
        'cwd: "~/code/scratch"\n'
        "branch: null\n"
        "commit: null\n"
        'time: "2026-10-31T17:05:12Z"\n'
        "refs: []\n"
        "resolves: null\n"
        "---\n"
        "uv needs --cache-dir in the sandbox\n"
    )


def test_a_resolved_note_and_its_resolution_drop_out_of_the_inbox(kb: Kb) -> None:
    broken = note_id(kb, "integration tests fail on main since the httpx bump", kind="status")
    kb.later(hours=2)
    kb("note", "--agent", "Codex", "--resolve", broken, "they pass again after the pool fix")

    assert kb("inbox").stdout == ""


def test_a_resolution_worded_like_its_note_still_resolves_it(kb: Kb) -> None:
    broken = note_id(kb, "integration tests fail on main", kind="status")
    kb.later(hours=2)
    resolution = kb("note", "--agent", "Codex", "--resolve", broken, "integration tests fail on main")

    assert (resolution.stdout.split()[0] != broken, kb("inbox").stdout) == (True, "")


def test_resolving_an_unknown_note_is_refused(kb: Kb) -> None:
    assert kb("note", "--agent", "Codex", "--resolve", "n000000", "fixed").exit_code == 2


def test_a_resolution_records_the_note_it_closes_and_its_kind(kb: Kb) -> None:
    broken = note_id(kb, "integration tests fail on main since the httpx bump", kind="status")
    kb.later(hours=2)
    _, path = kb("note", "--agent", "Codex", "--resolve", broken, "they pass again").stdout.split()
    lines = Path(path).read_text().splitlines()

    assert ('kind: "status"' in lines, f'resolves: "{broken}"' in lines) == (True, True)


def test_an_inbox_it_cannot_write_exits_1_and_names_the_folder(kb: Kb) -> None:
    inbox = kb.home / ".local/share/agent-fabric/inbox"
    inbox.mkdir(parents=True)
    inbox.chmod(0o555)
    try:
        result = kb("note", "--kind", "gotcha", "--agent", "Codex", "uv needs --cache-dir in the sandbox")
    finally:
        inbox.chmod(0o755)

    assert (result.exit_code, str(inbox) in result.stderr) == (1, True)


def test_a_problem_that_comes_back_after_its_resolution_is_a_new_note(kb: Kb) -> None:
    broken = note_id(kb, "integration tests fail on main", kind="status")
    kb.later(hours=1)
    kb("note", "--agent", "Codex", "--resolve", broken, "they pass again")
    kb.later(hours=1)
    note_id(kb, "integration tests fail on main", kind="status")

    assert "integration tests fail on main" in kb("inbox").stdout


def test_a_note_with_a_unicode_line_separator_in_a_ref_stays_readable(kb: Kb) -> None:
    ref = "err\N{LINE SEPARATOR}line2"
    kb("note", "--kind", "fix", "--agent", "Codex", "--ref", ref, "the fixture needs a socket")

    assert "the fixture needs a socket" in kb("inbox").stdout


def test_two_notes_resolved_in_the_same_second_are_both_resolved(kb: Kb) -> None:
    first = note_id(kb, "integration tests fail on main", kind="status")
    second = note_id(kb, "CI is flaky on the docker job", kind="status")
    kb.later(hours=1)
    kb("note", "--agent", "Codex", "--resolve", first)
    kb("note", "--agent", "Codex", "--resolve", second)

    assert kb("inbox").stdout == ""


def test_input_that_is_not_valid_utf8_is_refused_and_leaves_no_file(kb: Kb) -> None:
    # argv carries undecodable bytes as lone surrogates (surrogateescape)
    result = kb("note", "--kind", "fix", "--agent", "Codex", "--ref", "build-\udcff.log", "the build log is garbled")

    assert (result.exit_code, list(kb.home.rglob("*.md"))) == (2, [])


def test_a_write_error_that_is_not_about_permissions_does_not_blame_the_sandbox(kb: Kb) -> None:
    inbox = kb.home / ".local/share/agent-fabric/inbox"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("not a folder")

    result = kb("note", "--kind", "gotcha", "--agent", "Codex", "uv needs --cache-dir in the sandbox")

    assert (result.exit_code, str(inbox) in result.stderr, "sandbox" in result.stderr) == (1, True, False)


def test_a_permission_error_points_at_the_sandbox_write_list(kb: Kb) -> None:
    inbox = kb.home / ".local/share/agent-fabric/inbox"
    inbox.mkdir(parents=True)
    inbox.chmod(0o555)
    try:
        result = kb("note", "--kind", "gotcha", "--agent", "Codex", "uv needs --cache-dir in the sandbox")
    finally:
        inbox.chmod(0o755)

    assert "sandbox's write list" in result.stderr


def test_a_retried_resolve_returns_the_first_resolution(kb: Kb) -> None:
    broken = note_id(kb, "integration tests fail on main", kind="status")
    kb.later(hours=1)
    first = kb("note", "--agent", "Codex", "--resolve", broken, "they pass again")
    kb.later(seconds=30)
    retry = kb("note", "--agent", "Codex", "--resolve", broken, "they pass again")

    assert (retry.exit_code, retry.stdout.split()[0]) == (0, first.stdout.split()[0])


def test_a_relative_xdg_data_home_is_ignored(kb: Kb) -> None:
    kb.env["XDG_DATA_HOME"] = "rel"
    _, path = kb("note", "--kind", "gotcha", "--agent", "Codex", "uv needs --cache-dir in the sandbox").stdout.split()

    assert Path(path).parent == kb.home / ".local/share/agent-fabric/inbox"


def test_an_absolute_xdg_data_home_is_used(kb: Kb) -> None:
    kb.env["XDG_DATA_HOME"] = str(kb.home / "data")
    _, path = kb("note", "--kind", "gotcha", "--agent", "Codex", "uv needs --cache-dir in the sandbox").stdout.split()

    assert Path(path).parent == kb.home / "data/agent-fabric/inbox"


@pytest.mark.parametrize(
    "ref",
    ["tests/test_client.py::test_retry", "tests/test_client.py:12:5", "tests/test_client.py:12-20", "README.md"],
    ids=["pytest-node", "line-and-column", "line-range", "file-name"],
)
def test_refs_to_files_that_exist_get_no_warning(kb: Kb, ref: str) -> None:
    (kb.cwd / "tests").mkdir()
    (kb.cwd / "tests" / "test_client.py").touch()
    (kb.cwd / "README.md").touch()

    assert kb("note", "--kind", "fix", "--agent", "Codex", "--ref", ref, "the fixture needs a socket").stderr == ""


def test_a_missing_file_name_gets_a_warning(kb: Kb) -> None:
    result = kb("note", "--kind", "fix", "--agent", "Codex", "--ref", "CHANGELOG.md", "release notes are generated")

    assert "CHANGELOG.md" in result.stderr


def test_an_id_used_on_an_earlier_day_is_not_drawn_again(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    drawn = iter(["aaaaaa", "aaaaaa", "bbbbbb"])
    monkeypatch.setattr("agent_fabric.notes.secrets.token_hex", lambda _: next(drawn))
    first = note_id(kb, "integration tests fail on main", kind="status")
    kb.later(days=2)
    second = note_id(kb, "the build needs node 22", kind="status")

    assert (first, second) == ("naaaaaa", "nbbbbbb")


def test_a_kind_given_with_resolve_is_refused(kb: Kb) -> None:
    broken = note_id(kb, "integration tests fail on main", kind="status")
    result = kb("note", "--kind", "gotcha", "--agent", "Codex", "--resolve", broken, "they pass again")

    assert (result.exit_code, "--kind" in result.stderr) == (2, True)


def test_an_empty_resolve_id_is_refused_as_such(kb: Kb) -> None:
    result = kb("note", "--agent", "Codex", "--resolve", "", "they pass again")

    assert (result.exit_code, "--resolve" in result.stderr) == (2, True)
