"""The digest (docs/design.md §4.4): what a session did, from its messages and git snapshots, with no model."""

import json
import os
import subprocess
import time
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
        == "raw/sessions/github.com/me/proj/2026/11/2026-11-20-codex-f2c2eb87.md"
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
    before(folder, "commit", "-q", "-m", "start")
    return folder


def before(folder: Path, *args: str) -> str:
    """git, at a time before the session was first seen (T0): what was there already."""
    when = {"GIT_AUTHOR_DATE": "2026-11-19T00:00:00Z", "GIT_COMMITTER_DATE": "2026-11-19T00:00:00Z"}
    done = subprocess.run(
        ["git", *args], cwd=folder, env=os.environ | when, capture_output=True, text=True, check=True
    )
    return done.stdout


def git(folder: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True, check=True).stdout


def test_a_snapshot_outside_git_is_none(tmp_path: Path) -> None:
    assert take(tmp_path, T0, T0) is None


def test_snapshots_give_the_sessions_changes_commits_and_markers(repo: Path) -> None:
    first = take(repo, T0, T0)
    assert first is not None
    assert (first.uncommitted, first.files, first.commits) == (False, (), ())
    (repo / "app.py").write_text("x = 1\ny = 2  # noqa: E501\n")
    git(repo, "commit", "-qam", "add y")
    (repo / "app.py").write_text("x = 1\ny = 2  # noqa: E501\n# TODO: z\n")
    (repo / "new.py").write_text("print('hi')\n")
    last = take(repo, T0 + MINUTE, T0)
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
    snap = take(linked, T0, T0)
    assert snap is not None
    assert (snap.worktree, snap.main) == (linked, repo)
    text = digest([message(0, "user", "go")], [snap], cwd=str(linked))
    assert front(text)["worktree"] == f'{{path: "{linked}", main: "{repo}"}}'
    (linked / "pkg").mkdir()
    inside = take(linked / "pkg", T0, T0)
    assert inside is not None
    assert inside.worktree == linked


def test_a_rebase_during_the_session_keeps_its_commits_and_not_upstreams(repo: Path) -> None:
    before(repo, "switch", "-q", "-c", "feature")
    (repo / "feature.py").write_text("f = 1\n")
    before(repo, "add", ".")
    before(repo, "commit", "-qm", "feature")
    before(repo, "switch", "-q", "main")
    (repo / "upstream.py").write_text("u = 1\n")
    before(repo, "add", ".")
    before(repo, "commit", "-qm", "upstream")
    before(repo, "switch", "-q", "feature")
    git(repo, "rebase", "-q", "main")  # during the session
    snap = take(repo, T0, T0)
    assert snap is not None
    assert [commit.split(" ", 1)[1] for commit in snap.commits] == ["feature"]
    assert snap.files == ("feature.py",)
    assert snap.base == git(repo, "rev-parse", "main").strip()


def test_a_snapshot_reads_its_own_folder_whatever_repo_a_hook_named(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    snap = take(repo, T0, T0)
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
    snap = take(repo / "sub", T0, T0)
    assert snap is not None
    assert snap.files == ("sub/new.py", "sub/t.py", "top.py")
    assert snap.untracked == ("sub/new.py", "top.py")


def test_a_snapshot_runs_no_text_converter_the_repo_names(repo: Path, tmp_path: Path) -> None:
    ran = tmp_path / "ran"
    tool = tmp_path / "tool.sh"
    tool.write_text(f'#!/bin/sh\ntouch {ran}\ncat "$@"\n')
    tool.chmod(0o755)
    (repo / ".gitattributes").write_text("*.bin diff=hex\n")
    git(repo, "config", "diff.hex.textconv", str(tool))
    (repo / "a.bin").write_text("one\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "bin")
    (repo / "a.bin").write_text("two\n")
    snap = take(repo, T0, T0)
    assert snap is not None
    assert "a.bin" in snap.files
    assert not ran.exists()


def test_markers_name_their_file_whatever_diff_prefixes_the_user_set(repo: Path) -> None:
    git(repo, "config", "diff.mnemonicPrefix", "true")
    (repo / "app.py").write_text("x = 1  # TODO\n")
    git(repo, "config", "diff.noprefix", "true")
    last = take(repo, T0, T0)
    assert last is not None
    assert "`app.py:1`" in section(digest([message(0, "user", "go")], [last]), "Markers")


def snapshot(**fields: Any) -> Snapshot:
    empty = Snapshot(T0, "a" * 40, "a" * 40, "main", None, None, (), "", "", False, (), (), 0, False)
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


def test_front_matter_writes_a_character_past_the_bmp_as_one_escape() -> None:
    text = digest([message(0, "user", "go")], [snapshot(branch="feat/\U0001f680")])
    assert front(text)["branch"] == '"feat/\\U0001f680"'


@pytest.mark.parametrize("command", ["uv run --with pytest python x.py", "npx --package jest node a.js"])
def test_a_wrappers_option_value_isnt_the_program_it_runs(command: str) -> None:
    assert section(digest([bash(1, command, "ok")]), "Commands").splitlines()[2].split(" | ")[3] == "other"


def test_a_snapshot_checks_no_signature_whatever_the_config_says(repo: Path, tmp_path: Path) -> None:
    ran = tmp_path / "ran"
    tool = tmp_path / "gpg.sh"
    tool.write_text(f"#!/bin/sh\ntouch {ran}\n")
    tool.chmod(0o755)
    (repo / "app.py").write_text("x = 2\n")
    git(repo, "commit", "-qam", "signed")
    signature = "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQ==\n -----END PGP SIGNATURE-----\n"
    body = git(repo, "cat-file", "commit", "HEAD").replace("\n\n", "\n" + signature + "\n", 1)
    signed = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=repo,
        input=body,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    git(repo, "update-ref", "HEAD", signed)
    git(repo, "config", "log.showSignature", "true")
    git(repo, "config", "gpg.program", str(tool))
    snap = take(repo, T0, T0)
    assert snap is not None
    assert [commit.split(" ", 1)[1] for commit in snap.commits] == ["signed"]
    assert not ran.exists()


def test_a_file_only_touched_isnt_changed(repo: Path) -> None:
    os.utime(repo / "app.py", (1, 1))
    snap = take(repo, T0, T0)
    assert snap is not None
    assert (snap.files, snap.stat, snap.uncommitted) == ((), "", False)


def test_an_added_line_that_looks_like_a_file_header_is_still_a_line(repo: Path) -> None:
    (repo / "app.py").write_text("x = 1\n++ TODO fix\ny = 2  # FIXME\n")
    snap = take(repo, T0, T0)
    assert snap is not None
    markers = section(digest([message(0, "user", "go")], [snap]), "Markers")
    assert markers == "- todo · `app.py:2` · `++ TODO fix`\n- todo · `app.py:3` · `y = 2  # FIXME`\n"


def test_a_path_git_would_quote_comes_out_as_it_is(repo: Path) -> None:
    (repo / 'q"x.py').write_text("a = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "quoted")
    (repo / 'q"x.py').write_text("a = 1  # TODO\n")
    (repo / "tab\there.txt").write_text("")
    last = take(repo, T0, T0)
    assert last is not None
    assert last.files == ('q"x.py', "tab\there.txt")
    assert '`q"x.py:1`' in section(digest([message(0, "user", "go")], [last]), "Markers")


def test_a_worktree_of_a_bare_repo_has_no_main_checkout(repo: Path, tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    linked = tmp_path / "wt-bare"
    git(bare, "worktree", "add", "-q", str(linked))
    snap = take(linked, T0, T0)
    assert snap is not None
    assert (snap.worktree, snap.main) == (linked, None)


def test_a_final_message_cut_to_fit_points_to_its_message() -> None:
    answer = message(3, "assistant", "z" * (HARD_CAP + 10))
    text = digest([message(0, "user", "go"), answer])
    assert section(text, "Final message") == "[the final message cut to fit: kb show s1#3]\n"


def test_a_changed_file_with_a_space_in_its_name_keeps_its_name(repo: Path) -> None:
    (repo / "a b.txt").write_text("one\n")
    before(repo, "add", ".")
    before(repo, "commit", "-qm", "spaced")
    (repo / "a b.txt").write_text("two\n")
    snap = take(repo, T0, T0)
    assert snap is not None
    assert snap.files == ("a b.txt",)


def test_a_carriage_return_inside_an_added_line_stays_in_it(repo: Path) -> None:
    (repo / "app.py").write_bytes(b"x = 1\nfoo\rbar  # TODO\n")
    snap = take(repo, T0, T0)
    assert snap is not None
    assert snap.stat.startswith(" app.py | +1 -0")
    assert "- todo · `app.py:2`" in section(digest([message(0, "user", "go")], [snap]), "Markers")


def test_inline_code_or_an_unclosed_fence_cuts_no_typed_text() -> None:
    paragraphs = "\n\n".join(f"Point {i}." for i in range(30))
    for opening in ("```x``` is inline code", "```"):
        assert "cut" not in section(digest([message(0, "user", f"{opening}\n{paragraphs}")]), "Prompts")


def test_typed_lines_right_above_a_pasted_fence_stay() -> None:
    typed = "\n".join(f"typed {i}" for i in range(15))
    log = "\n".join(f"log {i}" for i in range(50))
    text = section(digest([message(0, "user", f"{typed}\n```log\n{log}\n```")]), "Prompts")
    assert "> typed 14\n> ```log" in text
    assert "lines of a pasted block cut" in text


@pytest.mark.parametrize(
    "command", ["time -p pytest", ".venv/bin/pytest -q", "uv run timeout 60 pytest", "python3.13 -m pytest"]
)
def test_a_runner_behind_a_path_a_version_or_two_wrappers_is_a_test_run(command: str) -> None:
    assert section(digest([bash(1, command, "Exit code 2\ncollection error")]), "Tests") == "- #1 pytest: failed\n"


def test_a_runners_summary_makes_a_test_run_however_it_was_started() -> None:
    text = digest([bash(1, "./scripts/check.sh", "3 passed in 0.50s\n"), bash(2, "cat ci.log", "9 failed in 1.0s\n")])
    assert section(text, "Tests") == "- #1 pytest: passed (3 passed, 0 failed, 0 skipped)\n"
    assert front(text)["outcome"].startswith('{last_tests: "passed",')


def test_a_worktree_of_a_repo_whose_checkout_git_cant_name_has_none(repo: Path, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", f"--separate-git-dir={tmp_path / 'store.git'}", str(repo), str(checkout))
    linked = tmp_path / "wt-separate"
    git(checkout, "worktree", "add", "-q", str(linked))
    snap = take(linked, T0, T0)
    assert snap is not None
    assert (snap.worktree, snap.main) == (linked, None)  # git lists the store, not the checkout


def test_a_start_with_no_zone_dates_the_file_as_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        naive = datetime(2026, 11, 21, 22, 0)
        assert "/2026-11-21-claude-s1.md" in path(meta(), "p", naive)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_the_final_message_is_the_last_turns_or_none() -> None:
    messages = [message(0, "user", "go"), message(1, "assistant", "Shall I proceed?"), message(2, "user", "yes")]
    text = digest([*messages, bash(3, "make", "ok")])
    assert section(text, "Final message") == ""
    assert "ends_with_question: false" in front(text)["outcome"]


def test_a_detached_head_names_no_branch() -> None:
    text = digest([message(0, "user", "go")], [snapshot(branch=None)], git_branch="main")
    assert front(text)["branch"] == "null"


def test_a_cut_note_goes_above_the_commands_table() -> None:
    messages = [message(0, "user", "go")] + [bash(i, f"cmd {i} " + "x" * 280, "ok") for i in range(1, 900)]
    commands = section(digest(messages), "Commands").splitlines()
    assert commands[0].startswith("[… ")
    assert commands[1] == "| # | command | exit | kind | first error line |"


def test_commits_past_the_listed_ones_are_counted_and_measured_from(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_fabric.snapshot.MAX_COMMITS", 1)
    start = git(repo, "rev-parse", "HEAD").strip()
    for name in ("one", "two"):
        (repo / f"{name}.py").write_text("")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", name)
    snap = take(repo, T0, T0)
    assert snap is not None
    assert (snap.base, snap.commits_unlisted, snap.files) == (start, 1, ("one.py", "two.py"))
    assert [commit.split(" ", 1)[1] for commit in snap.commits] == ["two"]
    assert section(digest([message(0, "user", "go")], [snap]), "Commits").startswith("1 earlier commits not shown.")


@pytest.mark.parametrize(
    "command",
    [
        "uv run --frozen pytest",
        "uv run -- pytest",
        "npx --yes jest",
        "timeout -k 5 60 pytest",
        "timeout -s KILL 600 pytest",
        "env -u VAR pytest",
        "uv run --frozen python -m unittest",
    ],
)
def test_a_wrappers_flag_doesnt_hide_the_runner(command: str) -> None:
    assert section(digest([bash(1, command, "Exit code 2\ncrashed")]), "Tests").startswith("- #1 ")


def test_fences_pair_as_markdown_pairs_them() -> None:
    typed = "\n\n".join(f"Point {i}." for i in range(30))
    # A `~~~` inside a ``` block doesn't open a fence, so the last `~~~` closes nothing and the typed text stays.
    mixed = "\n".join(["```markdown", "~~~", "a", "```", typed, "~~~"])
    # A fence indented 4 spaces is code, not a fence.
    indented = "\n".join(["    ```", typed, "    ```"])
    for prompt in (mixed, indented):
        assert "cut" not in section(digest([message(0, "user", prompt)]), "Prompts")


def test_half_an_emoji_doesnt_stop_the_digest() -> None:
    text = digest([message(0, "user", "cut \ud83d here"), message(1, "assistant", "ok \ud83d")])
    assert "cut �" in section(text, "Prompts")


def test_two_codex_sessions_started_together_get_their_own_files() -> None:
    first = path(meta(id="codex:01a1f2c2-eb87-7743-97f3-eba0fe2f6192", agent="codex"), "p", T0)
    second = path(meta(id="codex:01a1f2c2-eb88-7a01-8c11-0b3c5d7e9f10", agent="codex"), "p", T0)
    assert first != second


def test_diff_options_in_the_environment_dont_change_the_snapshot(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "app.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\n")
    before(repo, "commit", "-qam", "four")
    (repo / "app.py").write_text("a = 1\nb = 2\nc = 3\nd = 4  # TODO\n")
    monkeypatch.setenv("GIT_DIFF_OPTS", "--unified=3")
    snap = take(repo, T0, T0)
    assert snap is not None
    assert "\n b = 2" not in snap.diff
    assert "`app.py:4`" in section(digest([message(0, "user", "go")], [snap]), "Markers")


@pytest.mark.parametrize("command", ["uv sync && pytest", "git stash && pytest -x"])
def test_a_test_after_another_step_is_a_test_run(command: str) -> None:
    assert section(digest([bash(1, command, "Exit code 139\nSegmentation fault")]), "Tests").startswith("- #1 pytest")


def test_a_binary_change_under_a_path_with_b_slash_keeps_its_path(repo: Path) -> None:
    (repo / "x b").mkdir()
    (repo / "x b" / "y.bin").write_bytes(b"\0one")
    before(repo, "add", ".")
    before(repo, "commit", "-qm", "bin")
    (repo / "x b" / "y.bin").write_bytes(b"\0two")
    snap = take(repo, T0, T0)
    assert snap is not None
    assert snap.files == ("x b/y.bin",)


def test_a_pipe_outside_the_table_isnt_escaped(repo: Path) -> None:
    (repo / "app.py").write_text("x = a | b  # noqa\n")
    snap = take(repo, T0, T0)
    assert snap is not None
    assert "`x = a | b  # noqa`" in section(digest([message(0, "user", "go")], [snap]), "Markers")


def test_a_digest_its_cuts_cant_fit_loses_its_lists_then_says_it_is_over() -> None:
    names = tuple(f"{'d' * 300}/{i}.js" for i in range(MAX_FILES))
    snap = snapshot(files=names, untracked=names, uncommitted=True)
    text = digest([message(0, "user", "go")], [snap])
    assert "Untracked:" in section(text, "Changes")
    prompts = [message(i, "user", "y" * 1500) for i in range(70)]
    text = digest(prompts, [snap])
    assert len(text.encode()) <= HARD_CAP
    assert section(text, "Changes") == "[… cut to fit the digest]\n"
    text = digest([message(i, "user", "y" * 3000) for i in range(80)])
    assert front(text)["over_cap"] == "true"


def at(when: datetime) -> dict[str, str]:
    return {"GIT_AUTHOR_DATE": when.isoformat(), "GIT_COMMITTER_DATE": when.isoformat()}


def test_a_pull_during_the_session_brings_in_no_commits_of_its_own(repo: Path) -> None:
    start = git(repo, "rev-parse", "HEAD").strip()
    before(repo, "switch", "-q", "-c", "theirs", "HEAD")
    (repo / "theirs.py").write_text("t = 1\n")
    before(repo, "add", ".")
    # A teammate's commit, made after the session started on a branch from before it: older than the session's.
    subprocess.run(["git", "commit", "-qm", "theirs"], cwd=repo, env=os.environ | at(T0 + MINUTE), check=True)
    before(repo, "switch", "-q", "main")
    (repo / "mine.py").write_text("m = 1\n")
    git(repo, "add", ".")
    subprocess.run(["git", "commit", "-qm", "mine"], cwd=repo, env=os.environ | at(T0 + 2 * MINUTE), check=True)
    git(repo, "merge", "-q", "--no-ff", "-m", "pull", "theirs")
    snap = take(repo, T0 + 5 * MINUTE, T0)
    assert snap is not None
    assert [commit.split(" ", 1)[1] for commit in snap.commits] == ["mine", "pull"]
    assert snap.base == start


def test_a_shallow_clones_cut_isnt_measured_from_nothing(repo: Path, tmp_path: Path) -> None:
    (repo / "two.py").write_text("")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "two")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo}", str(clone))
    snap = take(clone, T0, T0)
    assert snap is not None
    assert (snap.base, snap.files) == (snap.head, ())


def test_a_huge_diff_is_read_only_to_its_cap_and_still_counted_whole(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_fabric.snapshot.MAX_DIFF", 1000)
    (repo / "dump.sql").write_text("".join(f"insert {i};\n" for i in range(5000)))
    git(repo, "add", "dump.sql")
    snap = take(repo, T0, T0)
    assert snap is not None
    assert (snap.diff_cut, len(snap.diff) <= 1000, snap.files) == (True, True, ("dump.sql",))
    assert "| +5000 -0" in snap.stat


@pytest.mark.parametrize(
    "command", ["uv run echo pytest", "uv run python scripts/check.py pytest", "env -u X ls pytest"]
)
def test_a_runners_name_as_an_argument_isnt_a_test_run(command: str) -> None:
    assert section(digest([bash(1, command, "ok")]), "Tests") == ""


def test_what_the_agent_said_before_its_last_calls_isnt_the_final_message() -> None:
    messages = [message(0, "user", "go"), message(1, "assistant", "Shall I inspect?"), bash(2, "make", "ok")]
    text = digest(messages)
    assert section(text, "Final message") == ""
    assert "ends_with_question: false" in front(text)["outcome"]


def test_a_go_package_that_didnt_build_fails_the_run() -> None:
    out = "ok  \texample.test/good\t0.01s\nFAIL\texample.test/broken [build failed]\nFAIL\n"
    text = digest([bash(1, "go test ./... | tee test.log", out)])
    assert 'last_tests: "failed"' in front(text)["outcome"]


def test_a_go_run_without_v_leaves_its_passes_unknown() -> None:
    out = "ok  \texample.test/a\t0.12s\nok  \texample.test/b\t(cached)\n"
    assert section(digest([bash(1, "go test ./...", out)]), "Tests") == "- #1 go test: passed (0 failed)\n"


def test_a_snapshot_git_cant_read_is_none_not_clean(repo: Path) -> None:
    (repo / ".gitattributes").write_text("*.py filter=broken\n")
    before(repo, "add", ".gitattributes")
    before(repo, "commit", "-qm", "attributes")
    git(repo, "config", "filter.broken.clean", "false")
    git(repo, "config", "filter.broken.required", "true")
    (repo / "app.py").write_text("x = 2\n")
    assert take(repo, T0, T0) is None


def test_a_carriage_return_doesnt_end_the_quote() -> None:
    messages = [message(0, "user", "run it\r## Commands\r\nok"), message(1, "assistant", "done\r## Errors")]
    text = digest(messages)
    assert "\n## Commands\n" not in section(text, "Prompts")
    assert "> ## Commands\n> ok" in section(text, "Prompts")
    assert section(text, "Final message") == "> done\n> ## Errors\n"


def test_a_coloured_runners_summary_and_errors_are_read() -> None:
    out = (
        "\x1b[31mFAILED\x1b[0m tests/test_a.py::test_x - assert 1 == 2\n"
        "\x1b[31m========= \x1b[31m\x1b[1m1 failed\x1b[0m, \x1b[32m2 passed\x1b[0m"
        "\x1b[31m in 0.12s\x1b[0m\x1b[31m ===\x1b[0m"
    )
    text = digest([bash(1, "scripts/run-tests", "Exit code 1\n" + out)])
    assert (
        section(text, "Tests")
        == "- #1 pytest: failed (2 passed, 1 failed, 0 skipped); failing: tests/test_a.py::test_x\n"
    )
    assert "\x1b" not in section(text, "Commands")
