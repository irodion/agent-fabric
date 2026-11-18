"""Seam D: `kb doctor` across the workspaces (docs/design.md §4.11, §5)."""

import json
import os
import shutil
import tomllib
from collections.abc import Mapping
from pathlib import Path

import pytest
from conftest import PROJECT, Kb, Lab
from test_doctor import codex_config, install_kb, report, rows, uses_claude_code
from test_timer import calls, installed_kb, systemd_state

from agent_fabric import config


def a_root(kb: Kb, *parts: str, extra: str = "") -> Path:
    """A registered root, its folder made; `extra` adds lines to its entry."""
    root = kb.home.joinpath(*parts)
    root.mkdir(parents=True, exist_ok=True)
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    before = config.read_text() if config.exists() else ""
    config.write_text(f'{before}[[workspace]]\nroot = "{root}"\n{extra}')
    return root


def all_steps(output: str) -> list[str]:
    return [entry["step"] for entry in report(output).get("next", [])]


def doctor(kb: Kb, *args: str) -> str:
    install_kb(kb, kb.home / ".local" / "share" / "uv" / "tools")
    return kb("doctor", *args).stdout


def test_fix_creates_each_roots_inbox(kb: Kb) -> None:
    root = a_root(kb, "work", "acme")

    inboxes = rows(doctor(kb, "--fix"), "inbox")

    assert ("fixed", "~/work/acme/.fabric/inbox", "created") in inboxes
    assert (root / ".fabric" / "inbox").is_dir()


def test_a_root_that_isnt_there_is_named_and_nothing_is_made_in_it(kb: Kb) -> None:
    root = a_root(kb, "mnt", "clientdisk")
    root.rmdir()

    workspace = rows(doctor(kb, "--fix"), "workspace")

    assert workspace == [("problem", "~/mnt/clientdisk", "a registered root that isn't there")]
    assert not root.exists()


def test_fix_puts_every_inbox_on_the_sandboxes_write_lists(kb: Kb) -> None:
    root = a_root(kb, "work", "acme")
    uses_claude_code(kb)
    codex = codex_config(kb, "[tui]\nx = 1\n")
    claude = kb.home / ".claude" / "settings.json"

    doctor(kb, "--fix")

    allow = json.loads(claude.read_text())["sandbox"]["filesystem"]["allowWrite"]
    roots = tomllib.loads(codex.read_text())["sandbox_workspace_write"]["writable_roots"]
    assert allow == ["~/.local/share/agent-fabric/inbox", "~/work/acme/.fabric/inbox"]
    assert roots == [str(kb.home / ".local" / "share" / "agent-fabric" / "inbox"), str(root / ".fabric" / "inbox")]


def test_an_inbox_missing_from_codexs_own_list_is_named_for_the_user_to_add(kb: Kb) -> None:
    root = a_root(kb, "work", "acme")
    default = kb.home / ".local" / "share" / "agent-fabric" / "inbox"
    codex_config(kb, f'[sandbox_workspace_write]\nwritable_roots = ["{default}"]\n')

    steps = all_steps(doctor(kb, "--fix"))

    inbox = root / ".fabric" / "inbox"
    assert f"Add {inbox} to sandbox_workspace_write.writable_roots in ~/.codex/config.toml" in steps


def test_a_root_that_allows_a_project_needs_ingests_tools(kb: Kb, tmp_path: Path) -> None:
    a_root(kb, "work", "acme", extra='allow = ["github.com/acme/billing-api"]\n')
    nothing = tmp_path / "empty"
    nothing.mkdir()
    kb.env["PATH"] = str(nothing)

    ingest = rows(kb("doctor").stdout, "ingest")

    assert [path for _, path, _ in ingest] == ["gitleaks", "codex"]


def test_each_workspaces_wiki_gets_its_row(lab: Lab) -> None:
    root = lab.repo.parent
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(f'[[workspace]]\nroot = "{root}"\nallow = ["{PROJECT}"]\n')
    for kind, text in (("gotcha", "a"), ("fix", "b"), ("decision", "c")):
        lab.leave(kind, f"{text}: uv needs --cache-dir in the sandbox")
    assert lab.kb("ingest").exit_code == 0

    wiki = rows(lab.kb("doctor").stdout, "wiki")

    assert [(status, path) for status, path, _ in wiki] == [("ok", "~/code/.fabric/wiki")]


UNITS = Path(".config") / "systemd" / "user"


def timers(lab: Lab, root: Path | None = None) -> Path:
    """A home whose default workspace allows the lab's project, and `root` too where given, with kb installed."""
    installed_kb(lab.kb)
    if root is not None:
        config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
        config.write_text(config.read_text() + f'[[workspace]]\nroot = "{root}"\nallow = ["{PROJECT}"]\n')
    return lab.kb.home / UNITS


def test_a_root_that_allows_a_project_gets_its_own_timer(lab: Lab) -> None:
    root = lab.kb.home / "work" / "acme"
    root.mkdir(parents=True)
    folder = timers(lab, root)

    lab.kb("doctor", "--fix")

    [service] = folder.glob("fabric-ingest@*-work-acme.service")
    assert f'ingest --workspace "{root}"\n' in service.read_text()
    assert f"--user enable --now {service.stem}.timer" in calls(lab.kb)
    assert "--user enable --now fabric-ingest@default.timer" in calls(lab.kb)


def test_a_dash_in_a_root_is_escaped_in_its_units_name(lab: Lab) -> None:
    root = lab.kb.home / "work" / "acme-corp"
    root.mkdir(parents=True)
    folder = timers(lab, root)

    lab.kb("doctor", "--fix")

    assert any(path.name.endswith("acme\\x2dcorp.timer") for path in folder.iterdir())


def v06_timer(lab: Lab, state: str = "enabled") -> Path:
    """The fabric-ingest.timer and .service v0.6 wrote, in a state the user left it."""
    folder = timers(lab)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "fabric-ingest.service").write_text("[Service]\nExecStart=kb ingest\n")
    if state == "masked":
        (folder / "fabric-ingest.timer").symlink_to("/dev/null")
    else:
        (folder / "fabric-ingest.timer").write_text("[Timer]\nOnCalendar=*-*-* 03:00:00\n")
        systemd_state(lab.kb, "enabled", state)
    return folder


def test_fix_moves_v06s_timer_to_the_default_workspaces(lab: Lab) -> None:
    folder = v06_timer(lab)

    lab.kb("doctor", "--fix")

    assert not (folder / "fabric-ingest.timer").exists()
    assert not (folder / "fabric-ingest.service").exists()
    assert "--user disable --now fabric-ingest.timer" in calls(lab.kb)
    assert "--user enable --now fabric-ingest@default.timer" in calls(lab.kb)


def test_a_v06_timer_the_user_disabled_moves_and_stays_off(lab: Lab) -> None:
    folder = v06_timer(lab, "disabled")

    lab.kb("doctor", "--fix")

    assert (folder / "fabric-ingest@default.timer").is_file()
    assert not any(call.startswith("--user enable") for call in calls(lab.kb))


def test_a_masked_v06_timer_moves_and_stays_masked(lab: Lab) -> None:
    folder = v06_timer(lab, "masked")

    lab.kb("doctor", "--fix")

    assert os.path.realpath(folder / "fabric-ingest@default.timer") == os.devnull
    assert not (folder / "fabric-ingest.timer").is_symlink()


def test_the_timer_of_a_root_no_longer_registered_is_removed(lab: Lab) -> None:
    root = lab.kb.home / "work" / "acme"
    root.mkdir(parents=True)
    folder = timers(lab, root)
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    before = config.read_text()
    lab.kb("doctor", "--fix")
    config.write_text(f'[publish]\nallow = ["{PROJECT}"]\n')

    lab.kb("doctor", "--fix")

    assert sorted(path.name for path in folder.glob("fabric-ingest@*")) == [
        "fabric-ingest@default.service",
        "fabric-ingest@default.timer",
    ]
    assert before != config.read_text()


def test_a_config_kb_cant_read_removes_no_roots_timer(lab: Lab) -> None:
    root = lab.kb.home / "work" / "acme"
    root.mkdir(parents=True)
    folder = timers(lab, root)
    lab.kb("doctor", "--fix")
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(config.read_text() + "[[workspace]\n")

    lab.kb("doctor", "--fix")

    assert len(list(folder.glob("fabric-ingest@*"))) == 4


def test_a_bad_allow_list_in_any_workspace_gets_its_row(kb: Kb) -> None:
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[publish]\nallow = ["github.com/me/proj"]\n')
    a_root(kb, "work", "acme", extra='allow = "github.com/acme/billing-api"\n')

    ingest = rows(doctor(kb), "ingest")

    assert any("allow in the [[workspace]] for ~/work/acme" in detail for _, _, detail in ingest)


def test_a_config_kb_cant_read_is_a_row_not_a_crash(kb: Kb) -> None:
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("")
    config.chmod(0o000)
    try:
        output = doctor(kb)
    finally:
        config.chmod(0o600)

    assert rows(output, "workspace")[0][0] == "problem"


def test_a_v06_timer_turned_off_stays_off_when_the_default_allows_a_project_later(lab: Lab) -> None:
    root = lab.kb.home / "work" / "acme"
    root.mkdir(parents=True)
    v06_timer(lab, "disabled")
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(f'[[workspace]]\nroot = "{root}"\nallow = ["{PROJECT}"]\n')
    lab.kb("doctor", "--fix")
    config.write_text(f'[publish]\nallow = ["{PROJECT}"]\n' + config.read_text())

    lab.kb("doctor", "--fix")

    assert "--user enable --now fabric-ingest@default.timer" not in calls(lab.kb)


def test_a_units_folder_kb_cant_write_leaves_v06s_timer_and_says_so(lab: Lab) -> None:
    folder = v06_timer(lab)
    folder.chmod(0o500)
    try:
        output = lab.kb("doctor", "--fix").stdout
    finally:
        folder.chmod(0o700)

    assert ("problem", "~/.config/systemd/user/fabric-ingest.timer") in [
        (status, path) for status, path, _ in rows(output, "timer")
    ]


def test_a_step_naming_an_escaped_unit_quotes_it_for_the_shell(lab: Lab) -> None:
    root = lab.kb.home / "work" / "acme-corp"
    root.mkdir(parents=True)
    timers(lab, root)
    lab.kb("doctor", "--fix")
    systemd_state(lab.kb, "result", "exit-code")

    steps = all_steps(lab.kb("doctor").stdout)

    assert any("'fabric-ingest@" in step and "acme\\x2dcorp.service'" in step for step in steps)


def test_a_backup_file_or_a_template_of_the_users_isnt_taken_for_kbs_unit(lab: Lab) -> None:
    folder = timers(lab)
    lab.kb("doctor", "--fix")
    (folder / "fabric-ingest@default.timer~").write_text("an editor's backup\n")
    (folder / "fabric-ingest@.service").write_text("[Service]\nExecStart=/bin/true\n")

    output = lab.kb("doctor", "--fix").stdout

    assert [status for status, _, _ in rows(output, "timer")] == ["ok"]
    assert (folder / "fabric-ingest@.service").exists()


def test_v06s_timer_stays_until_the_default_workspaces_is_in_place(lab: Lab) -> None:
    folder = v06_timer(lab, "disabled")
    kb = lab.kb.home / ".local" / "share" / "uv" / "tools" / "agent-fabric" / "bin" / "kb"
    kb.unlink()  # the default's units can't be written without it
    lab.kb("doctor", "--fix")
    assert (folder / "fabric-ingest.timer").is_file()
    installed_kb(lab.kb)

    lab.kb("doctor", "--fix")

    assert not any(call.startswith("--user enable") for call in calls(lab.kb))


def test_v06s_timer_isnt_moved_while_systemd_cant_say_whether_its_on(lab: Lab) -> None:
    folder = v06_timer(lab, "disabled")
    systemd_state(lab.kb, "no-bus")

    lab.kb("doctor", "--fix")

    assert (folder / "fabric-ingest.timer").is_file()
    assert not (folder / "fabric-ingest@default.timer").exists()


def test_a_timer_systemd_wont_disable_keeps_its_files_and_says_so(lab: Lab) -> None:
    root = lab.kb.home / "work" / "acme"
    root.mkdir(parents=True)
    folder = timers(lab, root)
    lab.kb("doctor", "--fix")
    (lab.kb.home / ".config" / "agent-fabric" / "config.toml").write_text(f'[publish]\nallow = ["{PROJECT}"]\n')
    systemd_state(lab.kb, "cannot-disable")

    output = lab.kb("doctor", "--fix").stdout

    assert len(list(folder.glob("fabric-ingest@*"))) == 4
    assert "problem" in [status for status, _, _ in rows(output, "timer")]


def test_a_units_folder_kb_cant_read_is_no_crash(lab: Lab) -> None:
    folder = timers(lab)
    lab.kb("doctor", "--fix")
    folder.chmod(0o300)
    try:
        result = lab.kb("doctor")
    finally:
        folder.chmod(0o700)

    assert rows(result.stdout, "timer")


def test_a_units_description_names_the_workspace_through_systemd_not_the_raw_path(lab: Lab) -> None:
    root = lab.kb.home / "work" / "100%h"
    root.mkdir(parents=True)
    folder = timers(lab, root)

    lab.kb("doctor", "--fix")

    [service] = folder.glob("fabric-ingest@*100*.service")
    assert "Description=kb ingest: file agent notes in the wiki of %I\n" in service.read_text()


def test_v06s_timer_stays_while_its_replacement_cant_be_written(lab: Lab, tmp_path: Path) -> None:
    folder = v06_timer(lab)
    only = tmp_path / "only"
    only.mkdir()
    for tool in ("systemctl", "git"):
        (only / tool).symlink_to(shutil.which(tool, path=lab.kb.env["PATH"]) or tool)
    lab.kb.env["PATH"] = str(only)  # no codex or gitleaks, so no unit is written for the default workspace

    output = lab.kb("doctor", "--fix").stdout

    assert (folder / "fabric-ingest.timer").is_file()
    assert ("outdated", "~/.config/systemd/user/fabric-ingest.timer") in [
        (status, path) for status, path, _ in rows(output, "timer")
    ]


def test_a_masked_v06_service_moves_and_stays_masked(lab: Lab) -> None:
    folder = v06_timer(lab)
    (folder / "fabric-ingest.service").unlink()
    (folder / "fabric-ingest.service").symlink_to("/dev/null")

    lab.kb("doctor", "--fix")

    assert os.path.realpath(folder / "fabric-ingest@default.service") == os.devnull
    assert "--user enable --now fabric-ingest@default.timer" not in calls(lab.kb)


def test_a_masked_v06_timer_comes_back_through_unmask_and_fix(lab: Lab) -> None:
    folder = v06_timer(lab, "masked")
    lab.kb("doctor", "--fix")
    (folder / "fabric-ingest@default.timer").unlink()  # systemctl --user unmask

    lab.kb("doctor", "--fix")

    assert (folder / "fabric-ingest@default.service").is_file()
    assert "--user enable --now fabric-ingest@default.timer" in calls(lab.kb)


def test_a_disabled_v06_timer_with_a_masked_service_stays_off_after_the_unmask(lab: Lab) -> None:
    folder = v06_timer(lab, "disabled")
    (folder / "fabric-ingest.service").unlink()
    (folder / "fabric-ingest.service").symlink_to("/dev/null")
    lab.kb("doctor", "--fix")
    (folder / "fabric-ingest@default.service").unlink()  # systemctl --user unmask

    lab.kb("doctor", "--fix")

    assert not any(call.startswith("--user enable") for call in calls(lab.kb))


def test_a_bad_allow_list_of_a_root_that_isnt_mounted_still_gets_its_row(kb: Kb) -> None:
    root = a_root(kb, "mnt", "clientdisk", extra='allow = "github.com/acme/billing-api"\n')
    root.rmdir()

    ingest = rows(doctor(kb), "ingest")

    assert any("allow in the [[workspace]] for ~/mnt/clientdisk" in detail for _, _, detail in ingest)


@pytest.mark.parametrize("args", [(), ("--fix",)], ids=["check", "fix"])
def test_doctor_reads_the_config_once(lab: Lab, monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]) -> None:
    """One parse for the whole report, so its rows can't disagree over a file edited mid-run."""
    a_root(lab.kb, "work", "acme", extra=f'allow = ["{PROJECT}"]\nbackend = "codex"\n')
    a_root(lab.kb, "work", "beta", extra=f'allow = ["{PROJECT}"]\n')
    reads = []
    real = config._text

    def counted(env: Mapping[str, str]) -> str:
        reads.append(env)
        return real(env)

    monkeypatch.setattr(config, "_text", counted)

    lab.kb("doctor", *args)

    assert len(reads) == 1
