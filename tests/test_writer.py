"""Seam: the digest writer, over a real wiki repo and a stand-in gitleaks (docs/design.md §4.5)."""

import os
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import GITLEAKS, LEAK, a_stub

from agent_fabric.secretgate import GateUnavailable
from agent_fabric.wikirepo import GitError
from agent_fabric.workspaces import Workspace
from agent_fabric.writer import Digest, Written, write

NOW = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
PROJECT = "github.com/me/proj"
RELATIVE = "raw/sessions/github.com/me/proj/2026/11/2026-11-20-claude-a1b2c3d4.md"
DIGEST = "---\nschema: fabric.digest/1\ncwd: /home/alice/code/proj\n---\n## Prompts\n> ask someone@example.test\n"
REDACTED = DIGEST.replace("/home/alice", "~").replace("someone@example.test", "[redacted]")


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    a_stub(tmp_path / "stubs", "gitleaks", GITLEAKS)
    return {
        "HOME": str(home),
        "PATH": f"{tmp_path / 'stubs'}:{os.environ['PATH']}",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.test",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.test",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def configure(env: dict[str, str], text: str) -> None:
    config = Path(env["HOME"]) / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text)


def allow(env: dict[str, str], headless: str = "") -> None:
    configure(env, f'[publish]\nallow = ["{PROJECT}"]\n{headless}')


def put(
    env: dict[str, str], text: str = DIGEST, *, headless: bool = False, workspace: Workspace | None = None
) -> Written:
    return write(workspace or Workspace(None), env, NOW, Digest(text, RELATIVE, PROJECT, headless))


def wiki(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / "wiki"


def git(env: dict[str, str], *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=wiki(env), env=env, capture_output=True, text=True, check=True)
    return done.stdout


def local(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / ".local" / "share" / "agent-fabric" / "local" / RELATIVE


def quarantined(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / ".local" / "share" / "agent-fabric" / "quarantine" / RELATIVE


def test_a_project_not_allowed_stays_local_redacted(env: dict[str, str]) -> None:
    written = put(env)

    assert written == Written("local", local(env))
    assert local(env).read_text() == REDACTED
    assert not wiki(env).exists()


def test_an_allowed_project_is_committed_redacted_with_a_log_line(env: dict[str, str]) -> None:
    allow(env)

    written = put(env)

    assert written == Written("wiki", wiki(env) / RELATIVE, committed=True)
    assert git(env, "show", f"HEAD:{RELATIVE}") == REDACTED
    assert git(env, "log", "-1", "--format=%s") == f"digest: {PROJECT} claude a1b2c3d4\n"
    assert git(env, "show", "HEAD:log.md") == f"# Log\n2026-11-22 digest {PROJECT} claude a1b2c3d4\n"
    assert git(env, "status", "--porcelain") == ""


def test_the_same_digest_again_makes_no_commit(env: dict[str, str]) -> None:
    allow(env)
    put(env)
    head = git(env, "rev-parse", "HEAD")

    assert put(env).committed is False
    assert git(env, "rev-parse", "HEAD") == head


def test_a_rewritten_digest_is_a_new_commit_of_the_same_file(env: dict[str, str]) -> None:
    allow(env)
    put(env)

    put(env, DIGEST + "## Final message\n> done\n")

    assert git(env, "show", f"HEAD:{RELATIVE}").endswith("> done\n")
    assert git(env, "show", "HEAD:log.md").count("digest") == 2


@pytest.mark.parametrize(("listed", "where"), [("", "local"), (f'headless = ["{PROJECT}"]\n', "wiki")])
def test_a_headless_run_is_published_only_where_its_project_is_on_the_headless_list(
    env: dict[str, str], listed: str, where: str
) -> None:
    allow(env, listed)

    assert put(env, headless=True).where == where


def test_a_digest_gitleaks_stops_is_quarantined_and_not_committed(env: dict[str, str]) -> None:
    allow(env)

    written = put(env, DIGEST + LEAK + "\n")

    assert written == Written("quarantine", quarantined(env), ("fake-rule",))
    assert quarantined(env).read_text().endswith(LEAK + "\n")
    assert not (wiki(env) / RELATIVE).exists()


def test_a_digest_lives_in_one_place(env: dict[str, str]) -> None:
    put(env)
    allow(env)
    put(env, DIGEST + LEAK + "\n")
    assert not local(env).exists()

    put(env)

    assert not quarantined(env).exists()
    assert (wiki(env) / RELATIVE).read_text() == REDACTED


def test_without_gitleaks_nothing_goes_to_the_wiki(env: dict[str, str]) -> None:
    allow(env)
    env["PATH"] = "/nonexistent"

    with pytest.raises(GateUnavailable):
        put(env)
    assert not wiki(env).exists()


def test_a_wiki_on_another_branch_takes_no_digest(env: dict[str, str]) -> None:
    allow(env)
    put(env)
    git(env, "switch", "-q", "-c", "ingest/2026-11-22")

    with pytest.raises(GitError, match="ingest/2026-11-22"):
        put(env, DIGEST + "more\n")
    assert git(env, "status", "--porcelain") == ""


def status(env: dict[str, str]) -> str:
    return git(env, "status", "--porcelain", "--untracked-files=all")


@pytest.mark.parametrize(
    ("mine", "files"),
    [
        (lambda w: (w / "log.md").write_text("# Log\nmy own line\n"), " M log.md\n"),
        (lambda w: (w / "mine.md").write_text("my own\n"), "?? mine.md\n"),
    ],
    ids=["an edit", "a new file"],
)
def test_a_digest_waits_while_the_wiki_has_my_changes_and_leaves_them(
    env: dict[str, str], mine: Callable[[Path], object], files: str
) -> None:
    allow(env)
    put(env, DIGEST + "first\n")
    head = git(env, "rev-parse", "HEAD")
    mine(wiki(env))

    with pytest.raises(GitError, match="not committed"):
        put(env)
    assert status(env) == files
    assert git(env, "rev-parse", "HEAD") == head


def test_a_digest_waits_out_a_merge_and_leaves_it_as_it_was(env: dict[str, str]) -> None:
    allow(env)
    put(env, DIGEST + "first\n")
    git(env, "switch", "-q", "-c", "ingest/2026-11-22")
    (wiki(env) / "index.md").write_text("# Index\n- theirs\n")
    (wiki(env) / "log.md").write_text(git(env, "show", "HEAD:log.md") + "2026-11-22 ingest n1 add\n")
    git(env, "commit", "-qam", "ingest")
    git(env, "switch", "-q", "main")
    (wiki(env) / "index.md").write_text("# Index\n- ours\n")
    git(env, "commit", "-qam", "mine")
    subprocess.run(
        ["git", "merge", "-q", "ingest/2026-11-22"], cwd=wiki(env), env=env, capture_output=True, check=False
    )
    during = status(env), (wiki(env) / "log.md").read_text()

    with pytest.raises(GitError, match="merge is under way"):
        put(env)
    assert (status(env), (wiki(env) / "log.md").read_text()) == during
    git(env, "merge", "--abort")
    put(env)
    assert git(env, "show", "HEAD:log.md").count("digest") == 2


def test_a_commit_that_fails_leaves_the_wiki_clean(env: dict[str, str]) -> None:
    allow(env)
    put(env, DIGEST + "first\n")
    a_stub(wiki(env) / ".git" / "hooks", "pre-commit", "#!/bin/sh\nexit 1\n")
    head = git(env, "rev-parse", "HEAD")

    with pytest.raises(GitError):
        put(env)
    assert status(env) == ""
    assert git(env, "rev-parse", "HEAD") == head


def test_a_new_digest_whose_commit_fails_is_not_left_behind(env: dict[str, str]) -> None:
    allow(env)
    put(env)
    git(env, "rm", "-q", RELATIVE)
    git(env, "commit", "-qm", "drop it")
    a_stub(wiki(env) / ".git" / "hooks", "pre-commit", "#!/bin/sh\nexit 1\n")

    with pytest.raises(GitError):
        put(env)
    assert status(env) == ""
    assert not (wiki(env) / RELATIVE).parent.exists()


def test_a_digest_with_carriage_returns_is_committed_once(env: dict[str, str]) -> None:
    allow(env)
    put(env, DIGEST + "```\nok\r\nprogress\r100%\n```\n")
    head = git(env, "rev-parse", "HEAD")

    assert put(env, DIGEST + "```\nok\r\nprogress\r100%\n```\n").committed is False
    assert git(env, "rev-parse", "HEAD") == head
    assert status(env) == ""


def test_the_log_is_dated_in_utc(env: dict[str, str]) -> None:
    allow(env)
    late = datetime(2026, 11, 23, 0, 30, tzinfo=timezone(timedelta(hours=2)))

    write(Workspace(None), env, late, Digest(DIGEST, RELATIVE, PROJECT, headless=False))

    assert git(env, "show", "HEAD:log.md").endswith(f"2026-11-22 digest {PROJECT} claude a1b2c3d4\n")


def test_writing_a_digest_makes_no_folders_where_it_isnt(env: dict[str, str]) -> None:
    allow(env)

    put(env)

    data = Path(env["HOME"]) / ".local" / "share" / "agent-fabric"
    assert not (data / "local").exists()
    assert not (data / "quarantine").exists()


def test_a_workspace_root_keeps_its_digests_out_of_its_fabric_folder(env: dict[str, str], tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    configure(env, f'[[workspace]]\nroot = "{root}"\n')

    written = put(env, workspace=Workspace(root.resolve()))

    data = Path(env["HOME"]) / ".local" / "share" / "agent-fabric" / "workspaces"
    assert written.path.is_relative_to(data)
    assert written.path.read_text() == REDACTED
    assert not (root / ".fabric").exists()


@pytest.mark.parametrize("link", [".fabric", ".fabric/wiki"])
def test_a_link_out_of_a_workspace_root_is_refused_for_the_wiki(
    env: dict[str, str], tmp_path: Path, link: str
) -> None:
    root = tmp_path / "work"
    (root / link).parent.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / link).symlink_to(elsewhere)
    configure(env, f'[[workspace]]\nroot = "{root}"\nallow = ["{PROJECT}"]\n')

    with pytest.raises(OSError, match="is a link"):
        put(env, workspace=Workspace(root.resolve()))
    assert not any(elsewhere.iterdir())


@pytest.mark.parametrize("state", ["rebase-apply", "sequencer", "BISECT_LOG"])
def test_a_digest_waits_out_whatever_git_has_under_way(env: dict[str, str], state: str) -> None:
    allow(env)
    put(env, DIGEST + "first\n")
    path = wiki(env) / ".git" / state
    path.mkdir() if state != "BISECT_LOG" else path.write_text("")

    with pytest.raises(GitError, match="under way"):
        put(env)
    assert status(env) == ""


def test_an_undo_that_fails_too_is_named(env: dict[str, str]) -> None:
    allow(env)
    put(env, DIGEST + "first\n")
    git(env, "rm", "-q", RELATIVE)
    git(env, "commit", "-qm", "drop it")
    folder = (wiki(env) / RELATIVE).parent
    a_stub(wiki(env) / ".git" / "hooks", "pre-commit", f"#!/bin/sh\nchmod a-w '{folder}'\nexit 1\n")

    try:
        with pytest.raises(GitError, match=f"couldn't undo its change to {RELATIVE}"):
            put(env)
    finally:
        folder.chmod(0o755)


def test_a_log_with_no_final_newline_keeps_its_last_line(env: dict[str, str]) -> None:
    allow(env)
    put(env, DIGEST + "first\n")
    (wiki(env) / "log.md").write_text("# Log\nmy line")
    git(env, "commit", "-qam", "trim")

    put(env)

    assert git(env, "show", "HEAD:log.md").splitlines()[1:] == [
        "my line",
        f"2026-11-22 digest {PROJECT} claude a1b2c3d4",
    ]


def test_a_project_named_by_its_folder_is_logged_without_the_user_name(env: dict[str, str]) -> None:
    folder = "/home/alice/code/proj"
    configure(env, f'[publish]\nallow = ["{folder}"]\n')

    write(Workspace(None), env, NOW, Digest(DIGEST, RELATIVE, folder, headless=False))

    assert git(env, "show", "HEAD:log.md").endswith("2026-11-22 digest ~/code/proj claude a1b2c3d4\n")
    assert git(env, "log", "-1", "--format=%s") == "digest: ~/code/proj claude a1b2c3d4\n"


def test_a_project_id_gitleaks_stops_quarantines_the_digest(env: dict[str, str]) -> None:
    project = f"github.com/me/{LEAK}"
    configure(env, f'[publish]\nallow = ["{project}"]\n')

    written = write(Workspace(None), env, NOW, Digest(DIGEST, RELATIVE, project, headless=False))

    assert written.where == "quarantine"
    assert not wiki(env).exists() or LEAK not in git(env, "log", "--format=%s")


def test_a_branch_switched_to_during_the_commit_is_named(env: dict[str, str]) -> None:
    allow(env)
    put(env, DIGEST + "first\n")
    main = git(env, "rev-parse", "main")
    git(env, "branch", "review")
    a_stub(wiki(env) / ".git" / "hooks", "pre-commit", "#!/bin/sh\ngit symbolic-ref HEAD refs/heads/review\n")

    with pytest.raises(GitError, match="on refs/heads/review"):
        put(env)
    assert git(env, "rev-parse", "main") == main


def test_a_log_with_a_byte_not_in_utf8_takes_digests_and_keeps_it(env: dict[str, str]) -> None:
    allow(env)
    put(env, DIGEST + "first\n")
    (wiki(env) / "log.md").write_bytes(b"# Log\ncaf\xe9\n")
    git(env, "commit", "-qam", "latin-1")

    assert put(env).committed is True
    assert (wiki(env) / "log.md").read_bytes().startswith(b"# Log\ncaf\xe9\n2026-11-22 digest")


def test_a_wiki_left_with_a_half_written_digest_names_it(env: dict[str, str]) -> None:
    allow(env)
    put(env, DIGEST + "first\n")
    (wiki(env) / RELATIVE).write_text(DIGEST + "cut short")  # as a kb killed before its commit leaves it

    with pytest.raises(GitError, match=re.escape(f"not committed ({RELATIVE})")):
        put(env)


def test_a_digest_whose_path_is_ignored_waits_and_says_why(env: dict[str, str]) -> None:
    allow(env)
    put(env, DIGEST + "first\n")
    git(env, "rm", "-q", RELATIVE)
    (wiki(env) / ".gitignore").write_text("raw/\n")
    git(env, "add", ".gitignore")
    git(env, "commit", "-qm", "ignore raw")

    with pytest.raises(GitError, match=re.escape(".gitignore:1:raw/")):
        put(env)
    assert status(env) == ""
    assert not (wiki(env) / "raw").exists()


def test_a_root_above_kbs_data_follows_a_link_the_user_made_there(env: dict[str, str], tmp_path: Path) -> None:
    home = Path(env["HOME"])
    (tmp_path / "dotfiles").mkdir()
    (home / ".local").symlink_to(tmp_path / "dotfiles")
    configure(env, f'[[workspace]]\nroot = "{home}"\n')

    written = put(env, workspace=Workspace(home.resolve()))

    assert written.where == "local"
    assert written.path.read_text() == REDACTED


def test_a_tag_named_main_doesnt_fail_a_commit_on_the_branch(env: dict[str, str]) -> None:
    allow(env)
    put(env, DIGEST + "first\n")
    git(env, "tag", "main")

    assert put(env).committed is True


def test_files_get_the_mode_the_umask_gives(env: dict[str, str]) -> None:
    allow(env)
    old = os.umask(0o022)
    try:
        put(env)
        put(env, DIGEST + "again\n")
        configure(env, "")
        written = put(env)
    finally:
        os.umask(old)

    for path in (wiki(env) / RELATIVE, wiki(env) / "log.md", written.path):
        assert path.stat().st_mode & 0o777 == 0o644, path
