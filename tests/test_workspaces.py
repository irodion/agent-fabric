"""Workspaces: a registered root's projects share its own inbox, apart from the default workspace (docs/design.md
§4.11)."""

import json
import subprocess
from pathlib import Path

import pytest
from conftest import Kb

from agent_fabric.cli import Result
from agent_fabric.config import allowed_projects, hooks_choice, ingest_backend, machine_opt_outs


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


def test_a_note_about_a_folder_in_another_workspace_is_refused(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    kb.cwd = a_folder(kb, "code", "dotfiles")
    elsewhere = a_folder(kb, "work", "acme", "billing-api")

    result = kb("note", "--kind", "gotcha", "--agent", "Codex", "--project", str(elsewhere), "the DB resets nightly")

    assert (result.exit_code, list(kb.home.rglob("*.md"))) == (2, [])


def test_listing_a_folder_in_another_workspace_says_to_list_it_from_there(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    kb.cwd = a_folder(kb, "work", "acme", "billing-api")
    note(kb, "the staging DB resets nightly")
    kb.cwd = a_folder(kb, "code", "dotfiles")

    result = kb("inbox", "--project", str(root / "billing-api"))

    assert (result.exit_code, "another workspace" in result.stderr) == (2, True)


def test_a_note_in_another_workspace_cant_be_resolved_from_here(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    kb.cwd = a_folder(kb, "work", "acme", "billing-api")
    theirs = note(kb, "the staging DB resets nightly").stem.rpartition("-")[2]
    kb.cwd = a_folder(kb, "code", "dotfiles")

    result = kb("note", "--resolve", theirs, "--agent", "Codex")

    assert (result.exit_code, len(list((root / ".fabric" / "inbox").glob("*.md")))) == (2, 1)


def test_an_agent_at_work_in_one_workspace_doesnt_sign_a_note_in_another(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    clones = [a_folder(kb, "work", "acme", "billing-api"), a_folder(kb, "code", "billing-api")]
    for clone in clones:
        git(clone, "init", "-q", "-b", "main")
        git(clone, "remote", "add", "origin", "git@github.com:acme/billing-api.git")
    (root / ".fabric" / "inbox").mkdir(parents=True)
    copilot = {"session_id": "s1", "cwd": str(clones[0])}
    kb("hook", "start", "--agent", "copilot", stdin=json.dumps(copilot))
    kb.cwd = clones[1]

    path = note(kb, "the staging DB resets nightly")

    assert 'agent: {"name": "Codex"' in path.read_text()


def test_listing_the_machines_notes_isnt_taken_for_a_folder_named_machine(kb: Kb) -> None:
    work = a_folder(kb, "work")
    register(kb, work, a_folder(kb, "work", "machine"))
    kb.cwd = work

    assert kb("inbox", "--project", "machine").exit_code == 0


def machine_note(kb: Kb, text: str = "uv needs --cache-dir in the sandbox") -> Result:
    return kb("note", "--kind", "gotcha", "--agent", "Codex", "--machine", text)


def inbox_ids(inbox: Path) -> list[str]:
    return sorted(file.stem.rpartition("-")[2] for file in inbox.glob("*.md"))


def test_a_machine_note_reaches_every_workspace_with_one_id(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    default = kb.home / ".local" / "share" / "agent-fabric" / "inbox"

    written = machine_note(kb)

    note_id = written.stdout.split()[0]
    assert (written.exit_code, inbox_ids(default), inbox_ids(root / ".fabric" / "inbox")) == (0, [note_id], [note_id])


def opt_out(kb: Kb, root: Path) -> None:
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(f'[[workspace]]\nroot = "{root}"\nshare_machine_notes = false\n')


def test_a_workspace_that_opts_out_keeps_its_machine_notes_in(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    opt_out(kb, root)
    kb.cwd = a_folder(kb, "work", "acme", "billing-api")

    machine_note(kb)

    assert list((kb.home / ".local" / "share" / "agent-fabric").rglob("*.md")) == []


def test_a_workspace_that_opts_out_gets_no_other_workspaces_machine_notes(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    opt_out(kb, root)

    machine_note(kb)

    assert inbox_ids(root / ".fabric" / "inbox") == []


def test_the_default_workspace_opts_out_under_publish(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(f'[publish]\nshare_machine_notes = false\n\n[[workspace]]\nroot = "{root}"\n')

    machine_note(kb)

    assert inbox_ids(root / ".fabric" / "inbox") == []


def test_an_inbox_kb_cant_write_doesnt_stop_the_other_copies(kb: Kb) -> None:
    broken, root = a_folder(kb, "work", "broken"), a_folder(kb, "work", "acme")
    register(kb, broken, root)
    (broken / ".fabric").write_text("")  # a file: no inbox can go under it

    written = machine_note(kb)

    missed = (written.exit_code, str(broken / ".fabric" / "inbox") in written.stderr)
    assert (missed, inbox_ids(root / ".fabric" / "inbox")) == ((1, True), [written.stdout.split()[0]])


def test_a_retry_writes_only_the_copies_that_are_missing(kb: Kb) -> None:
    broken, root = a_folder(kb, "work", "broken"), a_folder(kb, "work", "acme")
    register(kb, broken, root)
    (broken / ".fabric").write_text("")
    note_id = machine_note(kb).stdout.split()[0]
    (broken / ".fabric").unlink()
    kb.later(minutes=5)

    retried = machine_note(kb)

    inboxes = [
        kb.home / ".local" / "share" / "agent-fabric" / "inbox",
        broken / ".fabric" / "inbox",
        root / ".fabric" / "inbox",
    ]
    assert (retried.exit_code, [inbox_ids(inbox) for inbox in inboxes]) == (0, [[note_id]] * 3)


def test_resolving_a_machine_note_closes_every_copy(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    note_id = machine_note(kb).stdout.split()[0]
    kb.cwd = a_folder(kb, "work", "acme", "billing-api")

    resolved = kb("note", "--resolve", note_id, "--agent", "Codex", "fixed: the cache dir is set in the profile")
    kb.cwd = kb.home

    assert (resolved.exit_code, kb("inbox", "--project", "machine").stdout) == (0, "")


def file_away(inbox: Path, note_id: str) -> None:
    """What ingest does with a note it filed: move it to done/."""
    done = inbox / "done" / "2026" / "10"
    done.mkdir(parents=True, exist_ok=True)
    for file in inbox.glob(f"*-{note_id}.md"):
        file.rename(done / file.name)


def test_resolving_a_machine_note_names_the_workspaces_that_filed_it(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    note_id = machine_note(kb).stdout.split()[0]
    file_away(kb.home / ".local" / "share" / "agent-fabric" / "inbox", note_id)
    kb.cwd = a_folder(kb, "work", "acme", "billing-api")

    resolved = kb("note", "--resolve", note_id, "--agent", "Codex", "fixed: the cache dir is set in the profile")

    assert (resolved.exit_code, "default workspace" in resolved.stderr) == (0, True)


def test_a_machine_note_filed_here_is_still_resolved_where_its_open(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    note_id = machine_note(kb).stdout.split()[0]
    file_away(kb.home / ".local" / "share" / "agent-fabric" / "inbox", note_id)

    resolved = kb("note", "--resolve", note_id, "--agent", "Codex", "fixed: the cache dir is set in the profile")
    kb.cwd = a_folder(kb, "work", "acme", "billing-api")

    assert (resolved.exit_code, kb("inbox", "--project", "machine").stdout) == (0, "")


def test_a_machine_notes_resolution_carries_nothing_of_the_workspace_it_came_from(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    note_id = machine_note(kb).stdout.split()[0]
    kb.cwd = a_folder(kb, "work", "acme", "billing-api")

    kb("note", "--resolve", note_id, "--agent", "Codex", "fixed: the cache dir is set in the profile")

    default = kb.home / ".local" / "share" / "agent-fabric" / "inbox"
    assert [file for file in default.glob("*.md") if "acme" in file.read_text()] == []


def test_a_share_setting_that_isnt_true_or_false_stops_a_machine_note_and_names_the_config(kb: Kb) -> None:
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[publish]\nshare_machine_notes = "no"\n')

    result = machine_note(kb)

    assert (result.exit_code, "~/.config/agent-fabric/config.toml" in result.stderr) == (1, True)


def test_a_retried_resolve_closes_the_copies_the_first_one_missed(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    note_id = machine_note(kb).stdout.split()[0]
    theirs = root / ".fabric" / "inbox"
    theirs.chmod(0o555)
    try:
        kb("note", "--resolve", note_id, "--agent", "Codex", "fixed: the cache dir is set in the profile")
    finally:
        theirs.chmod(0o755)

    retried = kb("note", "--resolve", note_id, "--agent", "Codex", "fixed: the cache dir is set in the profile")
    kb.cwd = a_folder(kb, "work", "acme", "billing-api")

    assert (retried.exit_code, kb("inbox", "--project", "machine").stdout) == (0, "")


def test_a_retry_doesnt_write_again_into_an_inbox_whose_copy_was_filed(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    note_id = machine_note(kb).stdout.split()[0]
    file_away(root / ".fabric" / "inbox", note_id)
    kb.later(minutes=5)

    machine_note(kb)

    assert inbox_ids(root / ".fabric" / "inbox") == []


def test_a_root_that_isnt_there_gets_no_copy_and_isnt_made(kb: Kb) -> None:
    gone = kb.home / "mnt" / "clientdisk" / "acme"
    register(kb, gone)

    result = machine_note(kb)

    assert (result.exit_code, gone.exists()) == (0, False)


def test_a_machine_problem_that_comes_back_after_its_resolution_is_a_new_note_here(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    note_id = machine_note(kb).stdout.split()[0]
    theirs = root / ".fabric" / "inbox"
    theirs.chmod(0o555)  # the resolution misses acme, whose copy stays open
    try:
        kb("note", "--resolve", note_id, "--agent", "Codex", "fixed: the cache dir is set in the profile")
    finally:
        theirs.chmod(0o755)
    kb.later(hours=1)

    machine_note(kb)

    assert kb("inbox", "--project", "machine").stdout != ""


def test_a_bad_share_setting_doesnt_stop_a_note_about_a_project(kb: Kb) -> None:
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[publish]\nshare_machine_notes = "no"\n')

    assert kb("note", "--kind", "gotcha", "--agent", "Codex", "the staging DB resets nightly").exit_code == 0


def test_a_retried_resolve_says_it_copied_the_earlier_resolution(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    note_id = machine_note(kb).stdout.split()[0]
    theirs = root / ".fabric" / "inbox"
    theirs.chmod(0o555)
    try:
        kb("note", "--resolve", note_id, "--agent", "Codex", "fixed: the cache dir is set in the profile")
    finally:
        theirs.chmod(0o755)

    retried = kb("note", "--resolve", note_id, "--agent", "Codex", "fixed another way")

    assert "already resolved" in retried.stderr


def test_a_machine_problem_that_comes_back_is_one_new_note_in_every_workspace(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    note_id = machine_note(kb).stdout.split()[0]
    theirs = root / ".fabric" / "inbox"
    theirs.chmod(0o555)
    try:
        kb("note", "--resolve", note_id, "--agent", "Codex", "fixed: the cache dir is set in the profile")
    finally:
        theirs.chmod(0o755)
    kb.later(hours=1)

    again = machine_note(kb).stdout.split()[0]

    assert (again != note_id, again in inbox_ids(theirs)) == (True, True)


def resolve_missing(kb: Kb, theirs: Path, note_id: str) -> str:
    """Resolve a machine note while `theirs` can't be written, so its copy stays open; the resolution's id."""
    theirs.chmod(0o555)
    try:
        resolved = kb("note", "--resolve", note_id, "--agent", "Codex", "fixed: the cache dir is set in the profile")
    finally:
        theirs.chmod(0o755)
    return resolved.stdout.split()[0]


def test_a_problem_whose_resolution_was_filed_comes_back_as_a_new_note(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    default = kb.home / ".local" / "share" / "agent-fabric" / "inbox"
    note_id = machine_note(kb).stdout.split()[0]
    resolution = resolve_missing(kb, root / ".fabric" / "inbox", note_id)
    file_away(default, note_id)
    file_away(default, resolution)
    kb.later(hours=1)

    again = machine_note(kb).stdout.split()[0]

    assert (again != note_id, inbox_ids(default)) == (True, [again])


def test_a_retry_after_a_problem_came_back_gives_one_id_everywhere(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    note_id = machine_note(kb).stdout.split()[0]
    resolve_missing(kb, root / ".fabric" / "inbox", note_id)
    kb.later(hours=1)
    again = machine_note(kb).stdout.split()[0]
    kb.later(minutes=5)

    retried = machine_note(kb)

    assert {line.split()[0] for line in retried.stdout.splitlines()} == {again}


def test_a_retry_after_the_written_copy_was_filed_fills_only_the_missing_one(kb: Kb) -> None:
    broken = a_folder(kb, "work", "broken")
    register(kb, broken)
    default = kb.home / ".local" / "share" / "agent-fabric" / "inbox"
    (broken / ".fabric").write_text("")
    note_id = machine_note(kb).stdout.split()[0]
    file_away(default, note_id)
    (broken / ".fabric").unlink()
    kb.later(minutes=5)

    machine_note(kb)

    assert (inbox_ids(default), inbox_ids(broken / ".fabric" / "inbox")) == ([], [note_id])


def test_a_note_noted_again_after_ingest_filed_it_is_a_new_note(kb: Kb) -> None:
    default = kb.home / ".local" / "share" / "agent-fabric" / "inbox"
    first = kb("note", "--kind", "gotcha", "--agent", "Codex", "--machine", "the GPU driver needs a reboot")
    file_away(default, first.stdout.split()[0])
    kb.later(minutes=5)

    again = kb("note", "--kind", "gotcha", "--agent", "Codex", "--machine", "the GPU driver needs a reboot")

    assert (again.exit_code, inbox_ids(default)) == (0, [again.stdout.split()[0]])


def test_an_inbox_with_its_own_open_copy_gets_no_second_one(kb: Kb) -> None:
    root = a_folder(kb, "work", "acme")
    register(kb, root)
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    shared = config.read_text()
    opt_out(kb, root)
    kb.cwd = a_folder(kb, "work", "acme", "billing-api")
    theirs = machine_note(kb).stdout.split()[0]  # acme's own, while it opted out
    kb.cwd = kb.home
    kb.later(minutes=1)
    machine_note(kb)  # the default workspace's own
    config.write_text(shared)  # acme shares again
    kb.later(minutes=1)

    machine_note(kb)

    assert inbox_ids(root / ".fabric" / "inbox") == [theirs]


def test_a_bad_share_setting_doesnt_stop_resolving_a_note_about_a_project(kb: Kb) -> None:
    note_id = kb("note", "--kind", "gotcha", "--agent", "Codex", "the staging DB resets nightly").stdout.split()[0]
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[publish]\nshare_machine_notes = "no"\n')

    resolved = kb("note", "--resolve", note_id, "--agent", "Codex", "fixed: it resets weekly now")

    assert (resolved.exit_code, resolved.stderr) == (0, "")


def test_a_bad_root_leaves_the_hooks_and_the_default_workspaces_settings_readable(kb: Kb) -> None:
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[notes]\nhooks = ["start"]\n\n[publish]\nallow = ["a/b"]\n\n[[workspace]]\nroot = "rel/x"\n')
    env = {"HOME": str(kb.home), **kb.env}

    assert (hooks_choice(env), allowed_projects(env), ingest_backend(env)) == (("start",), ("a/b",), "codex")


@pytest.mark.parametrize("text", ['[workspace]\nroot = "/x"\n', "workspace = [1]\n"], ids=["one table", "a number"])
def test_a_misshapen_workspace_is_named_where_the_opt_outs_are_read(kb: Kb, text: str) -> None:
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(text)

    with pytest.raises(ValueError, match="needs a root"):
        machine_opt_outs({"HOME": str(kb.home), **kb.env})
