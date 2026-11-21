"""The digest (docs/design.md §4.4): what a session did, from its messages and git snapshots, with no model."""

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_fabric.capture import Message, SessionMeta
from agent_fabric.digest import HARD_CAP, MAX_FILES, path, render
from agent_fabric.snapshot import REPO_VARIABLES, Snapshot, take

FIXTURES = Path(__file__).parent / "fixtures" / "agentsview"
T0 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def meta(**fields: Any) -> SessionMeta:
    base = {"id": "s1", "agent": "claude", "project": "proj", "cwd": "/home/me/code/proj", "is_automated": False}
    return SessionMeta.model_validate(base | {"started_at": T0, "ended_at": None, "message_count": 0} | fields)


def message(ordinal: int, role: str, content: str | None, at: datetime | None = T0, **fields: Any) -> Message:
    base = {"session_id": "s1", "ordinal": ordinal, "role": role, "timestamp": at, "content": content}
    return Message.model_validate(base | {"thinking_text": None, "model": None, "is_system": False} | fields)


def bash(ordinal: int, command: str, output: str, at: datetime = T0) -> Message:
    """A Claude Code command: a failed one's output starts with `Exit code N`."""
    call = {
        "tool_name": "Bash",
        "category": "Bash",
        "input_json": json.dumps({"command": command}),
        "result_content": output,
        "result_events": [{"status": "errored" if output.startswith("Exit code") else None}],
    }
    return message(ordinal, "assistant", f"[Bash]\n$ {command}", at, tool_calls=[call])


def digest(messages: list[Message], snapshots: list[Snapshot] | None = None, **fields: Any) -> str:
    return render(meta(**fields), messages, snapshots or [], project="github.com/me/proj", revision=1)


def front(text: str) -> dict[str, str]:
    head = text.split("---\n")[1]
    return dict(line.split(": ", 1) for line in head.splitlines())


def section(text: str, title: str) -> str:
    return text.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]


def recorded(agent: str) -> tuple[SessionMeta, list[Message]]:
    session = SessionMeta.model_validate(json.loads((FIXTURES / agent / "session.json").read_text()))
    body = json.loads((FIXTURES / agent / "messages.json").read_text())
    return session, [Message.model_validate(m) for m in body["messages"]]


@pytest.mark.parametrize("agent", ["claude", "codex", "copilot"])
def test_a_recorded_session_digests_its_failed_check_and_its_answer(agent: str) -> None:
    session, messages = recorded(agent)
    text = render(session, messages, [], project="github.com/me/proj", revision=2)
    assert text == render(session, messages, [], project="github.com/me/proj", revision=2)
    head = front(text)
    assert head["schema"] == '"fabric.digest/1"'
    assert head["revision"] == "2"
    assert head["counts"].startswith("{prompts: 1, ")
    assert "FAILED test_retry: expected 3, got 2" in section(text, "Errors")
    assert "python3 check.py" in section(text, "Commands")
    assert section(text, "Final message").startswith("> ")


def test_every_front_matter_value_reads_as_yaml_flow() -> None:
    text = digest([message(0, "user", 'say "hi": now'), message(1, "assistant", "Hi?", T0 + MINUTE, model="m1")])
    head = front(text)
    assert json.loads(head["session"]) == "s1"
    assert json.loads(head["cwd"]) == "/home/me/code/proj"
    assert json.loads(head["models"]) == ["m1"]
    assert head["outcome"] == "{last_tests: null, uncommitted_changes: null, ends_with_question: true}"
    assert head["time"] == '{start: "2026-11-20T09:00:00Z", end: "2026-11-20T09:01:00Z", active_min: 1}'


def test_active_minutes_leave_out_long_gaps() -> None:
    messages = [message(0, "user", "a", T0), message(1, "assistant", "b", T0 + 3 * MINUTE)]
    messages.append(message(2, "user", "c", T0 + 60 * MINUTE))
    messages.append(message(3, "assistant", "d", T0 + 62 * MINUTE))
    assert front(digest(messages))["time"].endswith("active_min: 5}")


def test_a_prompt_is_quoted_whole_so_none_of_its_lines_reads_as_a_section() -> None:
    text = digest([message(0, "user", "Fix it.\n## Commands\n\nthanks")])
    assert section(text, "Prompts") == "### 2026-11-20T09:00:00Z · #0\n> Fix it.\n> ## Commands\n>\n> thanks\n"


def test_a_pasted_block_is_cut_to_its_ends_and_typed_text_stays() -> None:
    log = "\n".join(f"log line {i}" for i in range(60))
    text = section(digest([message(4, "user", f"Why does this fail?\n\n{log}\n\nPlease look.")]), "Prompts")
    assert "> Why does this fail?" in text
    assert "> Please look." in text
    assert "> log line 9\n> [… 40 lines of a pasted block cut: kb show s1#4]\n> log line 50" in text
    assert "log line 10\n" not in text


def test_a_fenced_block_with_blank_lines_is_one_pasted_block() -> None:
    code = "```\n" + "\n\n".join(f"x{i}" for i in range(30)) + "\n```"
    text = section(digest([message(0, "user", code)]), "Prompts")
    assert "lines of a pasted block cut" in text


def test_a_typed_prompt_of_many_short_paragraphs_isnt_cut() -> None:
    typed = "\n\n".join(f"Point {i}." for i in range(60))
    assert "cut" not in section(digest([message(0, "user", typed)]), "Prompts")


def test_system_messages_arent_prompts() -> None:
    text = digest([message(0, "user", "go"), message(1, "user", "<hook output>", is_system=True)])
    assert front(text)["counts"].startswith("{prompts: 1,")
    assert "hook output" not in text


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("uv run pytest -q", "test"),
        ("cd web && pnpm test", "test"),
        ("go test ./...", "test"),
        ("cargo test --lib", "test"),
        ("uv run ruff check", "lint"),
        ("npx tsc --noEmit", "lint"),
        ("cargo build --release", "build"),
        ("pnpm install", "install"),
        ("uv add httpx", "install"),
        ("git status", "git"),
        ("cd x && git commit -m 'run pytest later'", "git"),
        ("echo 'pytest is slow'", "other"),
        ("uv add --dev pytest", "install"),
        ("pip install pytest-cov", "install"),
        ("cat pytest.ini", "other"),
        ('uv run --cache-dir "$TMPDIR/uv" pytest -q', "test"),
        ("CI=1 timeout 60 go test ./...", "test"),
        ("python3 -m pytest tests", "test"),
        ("npx eslint .", "lint"),
        ("python3 check.py", "other"),
    ],
)
def test_a_commands_kind_comes_from_what_it_runs(command: str, kind: str) -> None:
    row = section(digest([bash(1, command, "ok")]), "Commands").splitlines()[2]
    assert row.split(" | ")[3] == kind


def test_a_command_with_a_pipe_or_newline_keeps_to_its_cell() -> None:
    row = section(digest([bash(1, "rg x | head\necho done", "ok")]), "Commands").splitlines()[2]
    assert row == "| #1 | rg x \\| head ⏎ echo done | 0 | other |  |"


PYTEST_FAILED = """\
F.s
FAILED tests/test_a.py::test_one - assert 1 == 2
ERROR tests/test_b.py::test_two
=================== 1 failed, 1 passed, 1 skipped, 1 error in 0.12s ===================
"""


@pytest.mark.parametrize(
    ("command", "output", "line"),
    [
        (
            "uv run pytest",
            "Exit code 1\n" + PYTEST_FAILED,
            "- #1 pytest: failed (1 passed, 2 failed, 1 skipped); failing: tests/test_a.py::test_one, "
            "tests/test_b.py::test_two",
        ),
        ("pytest -q", "3 passed in 0.50s\n", "- #1 pytest: passed (3 passed, 0 failed, 0 skipped)"),
        (
            "cargo test",
            "Exit code 101\ntest a::b ... FAILED\ntest result: FAILED. 4 passed; 1 failed; 2 ignored; 0 measured\n",
            "- #1 cargo test: failed (4 passed, 1 failed, 2 skipped); failing: a::b",
        ),
        (
            "go test -v ./...",
            "Exit code 1\n--- PASS: TestA (0.00s)\n--- FAIL: TestB (0.00s)\nFAIL\nFAIL\texample.com/x\t0.01s\n",
            "- #1 go test: failed (1 passed, 1 failed, 0 skipped); failing: TestB",
        ),
        (
            "npx vitest run",
            "Exit code 1\n FAIL  src/a.test.ts > adds\n      Tests  1 failed | 5 passed (6)\n",
            "- #1 vitest: failed (5 passed, 1 failed, 0 skipped); failing: src/a.test.ts > adds",
        ),
        (
            "npm test",
            "Exit code 1\n  ● math \u203a adds\nTests:       1 failed, 2 passed, 3 total\n",
            "- #1 jest: failed (2 passed, 1 failed, 0 skipped); failing: math \u203a adds",
        ),
        ("make test", "all good\n", "- #1 tests: passed"),
        (
            "pnpm test",
            "Exit code 1\n FAIL  src/a.test.ts > adds\n      Tests  1 failed | 3 passed (4)\n",
            "- #1 vitest: failed (3 passed, 1 failed, 0 skipped); failing: src/a.test.ts > adds",
        ),
    ],
)
def test_a_test_run_gives_its_runner_counts_and_failures(command: str, output: str, line: str) -> None:
    text = digest([bash(1, command, output)])
    assert section(text, "Tests") == line + "\n"


def test_the_last_test_run_is_the_outcome() -> None:
    messages = [bash(1, "pytest", "Exit code 1\n" + PYTEST_FAILED), bash(2, "pytest", "4 passed in 0.1s\n")]
    assert front(digest(messages))["outcome"].startswith('{last_tests: "passed",')
    assert front(digest(messages[:1]))["outcome"].startswith('{last_tests: "failed",')


def test_the_same_failure_twice_is_one_error_and_only_the_last_twenty_show() -> None:
    same = [bash(i, "python3 check.py", "Exit code 1\nboom") for i in (1, 2)]
    assert section(digest(same), "Errors").count("- #") == 1
    many = [bash(i, f"false {i}", f"Exit code 1\nboom {i}") for i in range(25)]
    errors = section(digest(many), "Errors")
    assert errors.startswith("5 earlier errors not shown.\n")
    assert errors.count("- #") == 20
    assert "- #24 " in errors
    assert "- #4 " not in errors


def test_an_errors_output_keeps_its_last_lines_and_points_to_the_rest() -> None:
    output = "Exit code 1\n" + "\n".join(f"line {i}" for i in range(100))
    errors = section(digest([bash(7, "make", output)]), "Errors")
    assert "[… 71 lines before: kb show s1#7]" in errors
    assert "line 99" in errors
    assert "line 69\n" not in errors


def test_output_with_a_fence_of_its_own_cant_close_the_errors() -> None:
    errors = section(digest([bash(1, "cat x.md; false", "Exit code 1\n```\n## Commits\n````")]), "Errors")
    assert "`````\nExit code 1" in errors


def test_a_digest_over_its_cap_cuts_sections_but_not_the_front_matter_or_prompts() -> None:
    prompt = message(0, "user", "Run it all.")
    messages = [prompt] + [bash(i, f"cmd {i} " + "x" * 280, f"Exit code 1\n{'y' * 8000}") for i in range(1, 400)]
    text = digest(messages)
    assert len(text.encode()) <= HARD_CAP
    assert front(text)["counts"] == "{prompts: 1, tool_calls: 399, commands: 399, edits: 0, errors: 399}"
    assert "> Run it all." in section(text, "Prompts")
    assert "earlier commands cut: kb show s1]" in section(text, "Commands")
    assert "| #399 |" in section(text, "Commands")


def test_a_path_is_sharded_by_project_and_month() -> None:
    session = meta(id="codex:01a1f2c2-eb87", agent="codex")
    assert (
        path(session, "github.com/me/proj", T0)
        == "raw/sessions/github.com/me/proj/2026/11/2026-11-20-codex-01a1f2c2.md"
    )
    assert path(session, "../../etc", T0).startswith("raw/sessions/etc/2026/11/")


# ── git snapshots ─────────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in REPO_VARIABLES:  # set when the tests run in a commit's hook
        monkeypatch.delenv(name, raising=False)
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "test@example.test")
    folder = tmp_path / "proj"
    folder.mkdir()
    git(folder, "init", "-q", "-b", "main")
    (folder / "app.py").write_text("x = 1\n")
    git(folder, "add", ".")
    git(folder, "commit", "-q", "-m", "start")
    return folder


def git(folder: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True, check=True).stdout


def test_a_snapshot_outside_git_is_none(tmp_path: Path) -> None:
    assert take(tmp_path, T0) is None


def test_snapshots_give_the_sessions_changes_commits_and_markers(repo: Path) -> None:
    first = take(repo, T0)
    assert first is not None
    assert (first.uncommitted, first.files, first.commits) == (False, (), ())
    (repo / "app.py").write_text("x = 1\ny = 2  # noqa: E501\n")
    git(repo, "commit", "-qam", "add y")
    (repo / "app.py").write_text("x = 1\ny = 2  # noqa: E501\n# TODO: z\n")
    (repo / "new.py").write_text("print('hi')\n")
    last = take(repo, T0 + MINUTE, base=first.base)
    assert last is not None
    assert (last.base, last.branch, last.uncommitted) == (first.head, "main", True)
    assert last.files == ("app.py", "new.py")
    assert [commit.split(" ", 1)[1] for commit in last.commits] == ["add y"]
    text = digest([message(0, "user", "go")], [first, last])
    assert front(text)["files_modified"] == '["app.py", "new.py"]'
    assert front(text)["head"] == f'{{start: "{first.head[:7]}", end: "{last.head[:7]}"}}'
    assert (
        section(text, "Markers")
        == "- suppression · `app.py:2` · `y = 2  # noqa: E501`\n- todo · `app.py:3` · `# TODO: z`\n"
    )
    assert section(text, "Commits").endswith(" add y\n")
    changes = section(text, "Changes")
    assert "Untracked: `new.py`" in changes
    assert "+# TODO: z" in changes
    assert "already had uncommitted changes" not in changes


def test_a_snapshot_of_a_linked_worktree_names_its_main_checkout(repo: Path, tmp_path: Path) -> None:
    linked = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(linked))
    snap = take(linked, T0)
    assert snap is not None
    assert (snap.worktree, snap.main) == (linked, repo)
    text = digest([message(0, "user", "go")], [snap], cwd=str(linked))
    assert front(text)["worktree"] == f'{{path: "{linked}", main: "{repo}"}}'
    (linked / "pkg").mkdir()
    inside = take(linked / "pkg", T0)
    assert inside is not None
    assert inside.worktree == linked


def test_a_base_rewritten_away_measures_from_the_head(repo: Path) -> None:
    snap = take(repo, T0, base="0" * 40)
    assert snap is not None
    assert snap.base == snap.head


def test_a_snapshot_reads_its_own_folder_whatever_repo_a_hook_named(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    snap = take(repo, T0)
    assert snap is not None
    assert snap.branch == "main"


def test_a_command_that_only_names_a_runner_sets_no_test_outcome() -> None:
    messages = [bash(1, "pytest", "Exit code 1\n" + PYTEST_FAILED), bash(2, "uv add --dev pytest", "ok")]
    messages.append(bash(3, "cat pytest.ini", "[pytest]"))
    text = digest(messages)
    assert front(text)["outcome"].startswith('{last_tests: "failed",')
    assert section(text, "Tests").count("- #") == 1


def test_a_session_in_a_subfolder_gets_the_whole_repos_changes_by_their_paths_from_the_top(repo: Path) -> None:
    (repo / "sub").mkdir()
    (repo / "sub" / "t.py").write_text("a = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "sub")
    (repo / "sub" / "t.py").write_text("a = 2\n")
    (repo / "sub" / "new.py").write_text("")
    (repo / "top.py").write_text("")
    snap = take(repo / "sub", T0)
    assert snap is not None
    assert snap.files == ("sub/new.py", "sub/t.py", "top.py")
    assert snap.untracked == ("sub/new.py", "top.py")


def test_a_snapshot_runs_no_converter_or_filter_the_repo_names(repo: Path, tmp_path: Path) -> None:
    ran = tmp_path / "ran"
    tool = tmp_path / "tool.sh"
    tool.write_text(f'#!/bin/sh\ntouch {ran}\ncat "$@"\n')
    tool.chmod(0o755)
    (repo / ".gitattributes").write_text("*.bin diff=hex filter=boom\n")
    git(repo, "config", "diff.hex.textconv", str(tool))
    git(repo, "config", "filter.boom.clean", str(tool))
    git(repo, "config", "filter.boom.required", "true")
    (repo / "a.bin").write_text("one\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "bin")
    ran.unlink()
    (repo / "a.bin").write_text("two\n")
    snap = take(repo, T0)
    assert snap is not None
    assert "a.bin" in snap.files
    assert not ran.exists()


def test_markers_name_their_file_whatever_diff_prefixes_the_user_set(repo: Path) -> None:
    git(repo, "config", "diff.mnemonicPrefix", "true")
    first = take(repo, T0)
    (repo / "app.py").write_text("x = 1  # TODO\n")
    git(repo, "config", "diff.noprefix", "true")
    last = take(repo, T0, base=first.base if first else None)
    assert last is not None
    assert "`app.py:1`" in section(digest([message(0, "user", "go")], [last]), "Markers")


def snapshot(**fields: Any) -> Snapshot:
    empty = Snapshot(T0, "a" * 40, "a" * 40, "main", None, None, (), "", "", False, (), (), False)
    return replace(empty, **fields)


def test_thousands_of_files_are_named_up_to_a_cap_and_counted() -> None:
    names = tuple(f"node_modules/p{i}/index.js" for i in range(MAX_FILES + 300))
    snap = snapshot(files=names, untracked=names, uncommitted=True)
    text = digest([message(0, "user", "go")], [snap])
    assert len(json.loads(front(text)["files_modified"])) == MAX_FILES
    assert front(text)["files_unlisted"] == "300"
    assert section(text, "Changes").count("node_modules/") == MAX_FILES
    assert ", and 300 more" in section(text, "Changes")


def test_over_the_cap_the_diff_goes_before_the_errors() -> None:
    diff = "\n".join(f"+line {i} " + "z" * 1200 for i in range(200))
    snap = snapshot(diff=diff, stat=" a.py | 200 +\n 1 file changed", files=("a.py",), uncommitted=True)
    messages = [message(0, "user", "go"), bash(1, "make", "Exit code 2\nboom")]
    text = digest(messages, [snap])
    assert len(text.encode()) <= HARD_CAP
    assert "[the diff cut to fit the digest]" in section(text, "Changes")
    assert "- #1 shell `make` · exit 2: boom" in section(text, "Errors")


def test_front_matter_escapes_what_yaml_forbids() -> None:
    text = digest([message(0, "user", "go")], [snapshot(branch="feat\u0085x\u007f")])
    assert front(text)["branch"] == '"feat\\u0085x\\u007f"'
