"""Seam A: `kb ingest` and `kb lint` on one workspace's inbox and wiki (docs/design.md §4.7, §4.11)."""

import fcntl
import shutil
from pathlib import Path

from conftest import BRANCH, PAGE, PROJECT, Lab

from agent_fabric.wikirepo import Git


def in_a_root(lab: Lab, allow: str = f'["{PROJECT}"]') -> Path:
    """Register the folder that holds the lab's project as a root, its allow list given; the default allows none."""
    root = lab.repo.parent
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(f'[[workspace]]\nroot = "{root}"\nallow = {allow}\n')
    return root


def three_notes(lab: Lab) -> None:
    for kind, text in (
        ("gotcha", "uv needs --cache-dir in the sandbox"),
        ("fix", "the DB fixture needs a socket; run -m 'not db'"),
        ("decision", "the wiki stays local, since the machines have no link"),
    ):
        lab.leave(kind, text)
        lab.kb.later(minutes=1)


def test_ingest_in_a_root_files_its_notes_into_that_roots_wiki(lab: Lab) -> None:
    root = in_a_root(lab)
    three_notes(lab)

    result = lab.kb("ingest")

    assert result.exit_code == 0, result.stderr
    page = Git(root / ".fabric" / "wiki", lab.kb.env | {"HOME": str(lab.kb.home)})("show", f"{BRANCH}:{PAGE}")
    assert ("uv needs --cache-dir in the sandbox\n" in page, lab.wiki.exists()) == (True, False)


def test_ingest_of_the_default_workspace_from_a_root_leaves_the_roots_inbox(lab: Lab) -> None:
    root = in_a_root(lab)
    three_notes(lab)

    result = lab.kb("ingest", "--workspace", "default")

    assert (result.exit_code, len(list((root / ".fabric" / "inbox").glob("*.md")))) == (0, 3)
    assert "no project is allowed yet: add its id under [publish] allow" in result.stdout


def test_ingest_of_a_root_named_from_elsewhere_files_that_roots_notes(lab: Lab) -> None:
    root = in_a_root(lab)
    three_notes(lab)
    lab.kb.cwd = lab.kb.home

    result = lab.kb("ingest", "--workspace", str(root))

    assert result.exit_code == 0, result.stderr
    assert (root / ".fabric" / "wiki" / ".git").exists()


def test_a_folder_that_isnt_a_registered_root_is_refused(lab: Lab) -> None:
    in_a_root(lab)

    result = lab.kb("ingest", "--workspace", str(lab.repo))

    assert (result.exit_code, "isn't a registered workspace root" in result.stderr) == (2, True)


def test_a_root_allows_only_what_its_own_entry_lists(lab: Lab) -> None:
    root = in_a_root(lab, allow="[]")
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(f'[publish]\nallow = ["{PROJECT}"]\n' + config.read_text())
    three_notes(lab)

    result = lab.kb("ingest")

    assert (result.exit_code, (root / ".fabric" / "wiki").exists()) == (0, False)
    assert "add its id under allow in the [[workspace]] for ~/code" in result.stdout


def test_a_run_in_one_workspace_doesnt_hold_off_another(lab: Lab) -> None:
    root = in_a_root(lab)
    three_notes(lab)
    lock = lab.kb.home / ".local" / "share" / "agent-fabric" / "ingest.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)

        result = lab.kb("ingest")

    assert (result.exit_code, (root / ".fabric" / "wiki" / ".git").exists()) == (0, True)


def test_lint_in_a_root_reads_that_roots_inbox(lab: Lab) -> None:
    in_a_root(lab)
    lab.leave("status", "integration tests fail on main")
    lab.kb.later(days=15)

    in_root = lab.kb("lint")
    lab.kb.cwd = lab.kb.home

    assert ("stale-status" in in_root.stdout, "stale-status" in lab.kb("lint").stdout) == (True, False)


def test_ingest_of_a_project_folder_in_another_workspace_is_refused(lab: Lab) -> None:
    in_a_root(lab)
    lab.kb.cwd = lab.kb.home

    result = lab.kb("ingest", "--project", str(lab.repo))

    assert (result.exit_code, "another workspace" in result.stderr) == (2, True)


def test_a_backend_kb_doesnt_have_stops_the_run_before_any_model_is_asked(lab: Lab) -> None:
    in_a_root(lab)
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(config.read_text() + 'backend = "local"\n')
    three_notes(lab)

    result = lab.kb("ingest")

    assert (result.exit_code, "local" in result.stderr, lab.fake.calls) == (1, True, [])


def test_the_default_workspace_takes_its_backend_from_ingest(lab: Lab) -> None:
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(config.read_text() + '[ingest]\nbackend = "codex"\n')
    lab.leave("gotcha", "uv needs --cache-dir in the sandbox")
    lab.kb.later(days=7)

    assert lab.kb("ingest").exit_code == 0


def test_a_config_kb_cant_read_with_a_project_folder_is_named_not_a_crash(lab: Lab) -> None:
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text("[[workspace]\n")

    result = lab.kb("ingest", "--workspace", "default", "--project", str(lab.repo))

    assert (result.exit_code, "can't read ~/.config/agent-fabric/config.toml" in result.stderr) == (1, True)


def test_a_root_that_isnt_there_is_refused_and_isnt_made(lab: Lab) -> None:
    gone = lab.kb.home / "mnt" / "clientdisk"
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(f'[[workspace]]\nroot = "{gone}"\nallow = ["{PROJECT}"]\n')

    result = lab.kb("ingest", "--workspace", str(gone))

    assert (result.exit_code, gone.exists()) == (2, False)


def test_a_dry_run_lists_its_notes_whatever_the_backend(lab: Lab) -> None:
    in_a_root(lab)
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(config.read_text() + 'backend = "local"\n')
    three_notes(lab)

    result = lab.kb("ingest", "--dry-run")

    assert (result.exit_code, "would take 3 notes" in result.stdout) == (0, True)


def test_a_bad_allow_list_names_the_workspace_its_in(lab: Lab) -> None:
    in_a_root(lab, allow='"everything"')

    result = lab.kb("ingest")

    assert (result.exit_code, "the [[workspace]] for ~/code" in result.stderr) == (1, True)


def test_a_worktree_swapped_for_a_link_to_another_repo_leaves_that_repo_alone(lab: Lab) -> None:
    root = in_a_root(lab)
    three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    Git(root / ".fabric" / "wiki", lab.kb.env | {"HOME": str(lab.kb.home)})("merge", "-q", "--no-edit", BRANCH)
    worktree = root / ".fabric" / "ingest-wt"
    shutil.rmtree(worktree)
    worktree.symlink_to(lab.repo)  # what an agent in the root could do: its sandbox can write .fabric/
    (lab.repo / ".env").write_text("TOKEN=synthetic\n")
    lab.kb.later(days=1)
    three_notes(lab)

    result = lab.kb("ingest")

    assert (result.exit_code, (lab.repo / ".env").exists(), worktree.is_symlink()) == (0, True, False)


def test_a_backend_that_isnt_a_name_names_the_workspace_its_in(lab: Lab) -> None:
    in_a_root(lab)
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(config.read_text() + "backend = 1\n")
    three_notes(lab)

    result = lab.kb("ingest")

    assert (result.exit_code, "backend in the [[workspace]] for ~/code" in result.stderr) == (1, True)


def test_a_file_in_the_worktrees_place_is_replaced_by_the_worktree(lab: Lab) -> None:
    root = in_a_root(lab)
    worktree = root / ".fabric" / "ingest-wt"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    worktree.write_text("not a worktree\n")
    three_notes(lab)

    result = lab.kb("ingest")

    assert (result.exit_code, worktree.is_dir()) == (0, True), result.stderr
