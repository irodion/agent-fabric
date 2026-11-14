"""Workspaces: a registered root's projects share its own inbox, apart from the default workspace (docs/design.md
§4.11)."""

import json
import subprocess
from pathlib import Path

from conftest import Kb


def register(kb: Kb, *roots: Path) -> None:
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("".join(f'[[workspace]]\nroot = "{root}"\n\n' for root in roots))


def a_folder(kb: Kb, *parts: str) -> Path:
    folder = kb.home.joinpath(*parts)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def git(folder: Path, *args: str) -> None:
    who = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "Test"}
    env = {"PATH": "/usr/bin:/bin", "HOME": str(folder), "GIT_CONFIG_NOSYSTEM": "1", **who}
    env["GIT_COMMITTER_EMAIL"] = "t@example.test"
    done = subprocess.run(["git", *args], cwd=folder, env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def note(kb: Kb, text: str) -> Path:
    result = kb("note", "--kind", "gotcha", "--agent", "Codex", text)
    assert result.exit_code == 0, result.stderr
    return Path(result.stdout.split()[1])


def test_a_note_under_a_registered_root_goes_to_that_roots_inbox(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    kb.cwd = a_folder(kb, "work", "acme", "billing-api")

    assert note(kb, "the staging DB resets nightly").parent == root / ".fabric" / "inbox"


def test_under_nested_roots_the_inner_one_takes_the_note(kb: Kb) -> None:
    outer, inner = a_folder(kb, "work"), a_folder(kb, "work", "acme")
    register(kb, outer, inner)
    kb.cwd = a_folder(kb, "work", "acme", "billing-api")

    assert note(kb, "the staging DB resets nightly").parent == inner / ".fabric" / "inbox"


def test_a_worktree_outside_the_root_belongs_to_its_main_checkouts_workspace(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    repo = a_folder(kb, "work", "acme", "billing-api")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "start")
    worktree = kb.home / ".codex" / "worktrees" / "a1b2" / "billing-api"
    git(repo, "worktree", "add", "-q", "-b", "fix", str(worktree))
    kb.cwd = worktree

    assert note(kb, "the staging DB resets nightly").parent == root / ".fabric" / "inbox"


def test_a_clone_in_another_workspace_doesnt_count_this_ones_status_notes(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    clones = [a_folder(kb, "code", "billing-api"), a_folder(kb, "work", "acme", "billing-api")]
    for clone in clones:
        git(clone, "init", "-q", "-b", "main")
        git(clone, "remote", "add", "origin", "git@github.com:acme/billing-api.git")
    kb.cwd = clones[0]
    kb("note", "--kind", "status", "--agent", "Codex", "the staging DB is down")
    (root / ".fabric" / "inbox").mkdir(parents=True)
    start = {"session_id": "s1", "cwd": str(clones[1]), "source": "startup", "hook_event_name": "SessionStart"}

    reminder = json.loads(kb("hook", "start", "--agent", "claude-code", stdin=json.dumps(start)).stdout)

    assert "status note" not in reminder["hookSpecificOutput"]["additionalContext"]


def test_the_stop_hook_asks_in_a_root_whose_inbox_is_the_only_one(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    (root / ".fabric" / "inbox").mkdir(parents=True)
    kb.cwd = a_folder(kb, "work", "acme", "billing-api")
    stop = json.dumps({"session_id": "s1", "cwd": str(kb.cwd), "hook_event_name": "Stop", "stop_hook_active": False})
    kb("hook", "stop", "--agent", "claude-code", stdin=stop)
    kb.later(minutes=10)

    asked = kb("hook", "stop", "--agent", "claude-code", stdin=stop)

    assert json.loads(asked.stdout)["decision"] == "block"


def test_a_workspace_entry_without_a_root_stops_a_note_and_names_the_config(kb: Kb) -> None:
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[[workspace]]\nbackend = "local"\n')

    result = kb("note", "--kind", "gotcha", "--agent", "Codex", "the staging DB resets nightly")

    assert (result.exit_code, "~/.config/agent-fabric/config.toml" in result.stderr) == (1, True)


def test_a_relative_root_stops_a_note_and_names_the_config(kb: Kb) -> None:
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[[workspace]]\nroot = "work/acme"\n')

    result = kb("note", "--kind", "gotcha", "--agent", "Codex", "the staging DB resets nightly")

    assert (result.exit_code, "~/.config/agent-fabric/config.toml" in result.stderr) == (1, True)


def test_a_config_kb_cant_read_stops_a_note_and_names_it(kb: Kb) -> None:
    (kb.home / ".config" / "agent-fabric" / "config.toml").mkdir(parents=True)  # a folder: reading it fails

    result = kb("note", "--kind", "gotcha", "--agent", "Codex", "the staging DB resets nightly")

    assert (result.exit_code, "~/.config/agent-fabric/config.toml" in result.stderr) == (1, True)


def test_a_broken_workspace_entry_leaves_the_default_workspaces_reminder(kb: Kb) -> None:
    kb("note", "--kind", "status", "--agent", "Codex", "the tests fail on main")
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[[workspace]]\nroot = "work/acme"\n')
    start = {"session_id": "s1", "cwd": str(kb.cwd), "source": "startup", "hook_event_name": "SessionStart"}

    reminder = json.loads(kb("hook", "start", "--agent", "claude-code", stdin=json.dumps(start)).stdout)

    assert "kb: 1 status note" in reminder["hookSpecificOutput"]["additionalContext"]


def test_a_checkout_whose_git_dir_is_elsewhere_belongs_to_its_own_folders_workspace(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    kb.cwd = a_folder(kb, "work", "acme", "billing-api")
    git(kb.cwd, "init", "-q", "-b", "main", "--separate-git-dir", str(a_folder(kb, "gitdirs") / "billing-api"))

    assert note(kb, "the staging DB resets nightly").parent == root / ".fabric" / "inbox"


def test_the_home_folder_itself_can_be_a_root(kb: Kb) -> None:
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[[workspace]]\nroot = "~"\n')

    assert note(kb, "the staging DB resets nightly").parent == kb.home / ".fabric" / "inbox"
