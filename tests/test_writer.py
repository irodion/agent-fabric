"""Seam: the digest writer, over a real wiki repo and a stand-in gitleaks (docs/design.md §4.5)."""

import os
import subprocess
from datetime import UTC, datetime
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


def test_what_i_have_staged_in_the_wiki_stays_staged(env: dict[str, str]) -> None:
    allow(env)
    put(env, DIGEST + "first\n")
    (wiki(env) / "mine.md").write_text("my own\n")
    git(env, "add", "mine.md")

    put(env)

    assert git(env, "status", "--porcelain") == "A  mine.md\n"


def test_a_digest_waits_out_a_merge_and_then_logs_once(env: dict[str, str]) -> None:
    allow(env)
    put(env, DIGEST + "first\n")
    git(env, "switch", "-q", "-c", "ingest/2026-11-22")
    (wiki(env) / "index.md").write_text("# Index\n- theirs\n")
    git(env, "commit", "-qam", "ingest")
    git(env, "switch", "-q", "main")
    (wiki(env) / "index.md").write_text("# Index\n- ours\n")
    git(env, "commit", "-qam", "mine")
    subprocess.run(
        ["git", "merge", "-q", "ingest/2026-11-22"], cwd=wiki(env), env=env, capture_output=True, check=False
    )

    with pytest.raises(GitError):
        put(env)
    git(env, "merge", "--abort")
    put(env)

    assert git(env, "show", "HEAD:log.md").count("digest") == 2


def test_a_workspace_root_keeps_its_digests_in_its_own_folder(env: dict[str, str], tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    configure(env, f'[[workspace]]\nroot = "{root}"\n')

    written = put(env, workspace=Workspace(root.resolve()))

    assert written.path == root.resolve() / ".fabric" / "local" / RELATIVE


def test_a_link_out_of_a_workspace_root_is_refused(env: dict[str, str], tmp_path: Path) -> None:
    root = tmp_path / "work"
    (root / ".fabric" / "local").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / ".fabric" / "local" / "raw").symlink_to(elsewhere)
    configure(env, f'[[workspace]]\nroot = "{root}"\n')

    with pytest.raises(OSError, match="leads out of"):
        put(env, workspace=Workspace(root.resolve()))
    assert not any(elsewhere.iterdir())
