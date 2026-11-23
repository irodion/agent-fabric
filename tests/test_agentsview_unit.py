"""agentsview.service, which `kb doctor --fix` sets up once a project is allowed (docs/design.md §4.1, §5): a user
unit where a probe shows the sandbox holds, else a system unit kb stages for the user to install."""

import os
import pwd
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import toon_format
from conftest import Kb, Lab, a_stub

from agent_fabric import agentsview_unit

UNITS = Path(".config") / "systemd" / "user"
RUNNING = "running sandboxed: fabricd reads Claude Code's and Codex's sessions through it"


@pytest.fixture(autouse=True)
def agentsview_installed(lab: Lab) -> None:
    a_stub(lab.kb.home / ".local" / "bin", "agentsview", "#!/bin/sh\n")


def doctor(kb: Kb, *argv: str) -> dict[str, Any]:
    decoded = toon_format.decode(kb("doctor", *argv).stdout)
    assert isinstance(decoded, dict)
    return decoded


def rows(kb: Kb, *argv: str) -> list[tuple[str, str]]:
    return [(row["status"], row["detail"]) for row in doctor(kb, *argv)["checks"] if row["check"] == "agentsview"]


def steps(kb: Kb, *argv: str) -> list[str]:
    return [step["step"] for step in doctor(kb, *argv).get("next", [])]


def the_unit(kb: Kb) -> Path:
    return kb.home / UNITS / "agentsview.service"


def calls(kb: Kb) -> list[str]:
    log = kb.home / "systemctl.log"
    lines = log.read_text().splitlines() if log.exists() else []
    return [line for line in lines if line.endswith("agentsview.service")]


def probes(kb: Kb) -> int:
    log = kb.home / "systemd-run.log"
    return log.read_text().count("--user --wait") if log.exists() else 0


def unenforced(kb: Kb) -> None:
    (kb.home / "systemd-run").mkdir(exist_ok=True)
    (kb.home / "systemd-run" / "exit").write_text("10")  # the probe wrote to the home


def state(kb: Kb, manager: str, name: str, text: str = "") -> None:
    folder = kb.home / "systemctl" / f"agentsview-{manager}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text)


def test_no_unit_until_a_project_is_allowed(lab: Lab) -> None:
    (lab.kb.home / ".config" / "agent-fabric" / "config.toml").write_text("")

    assert rows(lab.kb, "--fix") == []
    assert probes(lab.kb) == 0
    assert not the_unit(lab.kb).exists()


def test_where_the_sandbox_holds_fix_installs_and_starts_a_user_unit(lab: Lab) -> None:
    assert rows(lab.kb) == [("missing", "not running, so fabricd captures no Claude Code or Codex session")]

    assert rows(lab.kb, "--fix") == [("fixed", RUNNING)]
    assert calls(lab.kb)[-1] == "--user enable --now agentsview.service"
    assert (lab.kb.home / ".agentsview").is_dir()  # the one folder it may write, made before the sandbox hides it

    before = probes(lab.kb)
    assert rows(lab.kb) == [("ok", RUNNING)]
    assert probes(lab.kb) == before  # probed only before a unit is written


def test_the_probe_runs_with_the_units_sandbox(lab: Lab) -> None:
    lab.kb("doctor", "--fix")

    probe = (lab.kb.home / "systemd-run.log").read_text()
    for setting in agentsview_unit.SANDBOX:
        assert f"-p {setting} " in probe
    assert probe.startswith("--user --wait")
    assert f"-p RuntimeMaxSec={agentsview_unit.PROBE_TIMEOUT} " in probe  # a probe that hangs stops itself


def test_the_user_unit_runs_the_vetted_binary_sandboxed_on_the_configured_port(lab: Lab) -> None:
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(config.read_text() + "\n[agentsview]\nport = 8123\n")

    lab.kb("doctor", "--fix")

    text = the_unit(lab.kb).read_text()
    home = lab.kb.home
    assert (
        f'ExecStart="{home}/.local/bin/agentsview" serve --no-browser --no-update-check --port 8123 --replace\n'
        in text
    )
    for line in (*agentsview_unit.SANDBOX, f'ReadWritePaths="{home}/.agentsview"', "AGENTSVIEW_TELEMETRY_ENABLED=0"):
        assert line in text
    assert f'InaccessiblePaths=-"{home}/.ssh"\n' in text
    assert "User=" not in text
    assert text.endswith("WantedBy=default.target\n")


def test_where_the_sandbox_does_not_hold_fix_stages_a_system_unit_and_says_how_to_install_it(lab: Lab) -> None:
    unenforced(lab.kb)
    why = "not installed: a user unit can't sandbox it here, so it needs a system unit"

    [(status, detail)] = rows(lab.kb, "--fix")

    staged = lab.kb.home / ".local" / "share" / "agent-fabric" / "agentsview.service"
    assert (status, detail) == ("missing", f"{why}; staged at ~/.local/share/agent-fabric/agentsview.service")
    text = staged.read_text()
    assert f"User={lab.kb.home.owner()}\n" in text
    assert text.endswith("WantedBy=multi-user.target\n")
    assert not the_unit(lab.kb).exists()
    system = agentsview_unit.SYSTEM_UNITS / "agentsview.service"
    assert (
        f"Run sudo install -m 0644 {staged} {system} && sudo systemctl daemon-reload && "
        "sudo systemctl enable agentsview.service && sudo systemctl restart agentsview.service"
    ) in steps(lab.kb, "--fix")


def test_an_installed_system_unit_is_checked_and_its_state_asked(lab: Lab) -> None:
    unenforced(lab.kb)
    lab.kb("doctor", "--fix")
    system = agentsview_unit.SYSTEM_UNITS / "agentsview.service"
    system.parent.mkdir(parents=True)
    system.write_text((lab.kb.home / ".local" / "share" / "agent-fabric" / "agentsview.service").read_text())
    shown = str(system)

    assert rows(lab.kb) == [("missing", "installed but not running")]
    assert "Run sudo systemctl enable --now agentsview.service" in steps(lab.kb)
    assert calls(lab.kb)[-1] == "show --property=UnitFileState,ActiveState,Result,NeedDaemonReload agentsview.service"

    state(lab.kb, "system", "enabled", "enabled")
    before = len(calls(lab.kb))
    assert rows(lab.kb, "--fix") == [("ok", RUNNING)]
    assert all(call.startswith("show ") for call in calls(lab.kb)[before:])  # kb never changes the system's units
    assert any(row["path"] == shown for row in doctor(lab.kb)["checks"])


def test_an_old_system_unit_is_outdated_and_fix_stages_the_new_one(lab: Lab) -> None:
    unenforced(lab.kb)
    lab.kb("doctor", "--fix")
    staged = lab.kb.home / ".local" / "share" / "agent-fabric" / "agentsview.service"
    system = agentsview_unit.SYSTEM_UNITS / "agentsview.service"
    system.parent.mkdir(parents=True)
    system.write_text(staged.read_text().replace("--replace", ""))
    staged.unlink()

    [(status, detail)] = rows(lab.kb)
    assert (status, detail) == ("outdated", "outdated: it runs from an old unit; installed but not running")
    lab.kb("doctor", "--fix")
    assert "--replace" in staged.read_text()


def test_a_failed_system_unit_points_at_its_journal(lab: Lab) -> None:
    unenforced(lab.kb)
    lab.kb("doctor", "--fix")
    system = agentsview_unit.SYSTEM_UNITS / "agentsview.service"
    system.parent.mkdir(parents=True)
    system.write_text((lab.kb.home / ".local" / "share" / "agent-fabric" / "agentsview.service").read_text())
    state(lab.kb, "system", "enabled", "enabled")
    state(lab.kb, "system", "active", "failed")
    state(lab.kb, "system", "result", "exit-code")

    assert rows(lab.kb) == [("problem", "stopped: it failed (exit-code)")]
    assert (
        "Read journalctl -u agentsview.service and fix the cause; sudo systemctl restart agentsview.service"
        in steps(lab.kb)
    )


def test_a_stopped_user_unit_is_started_and_an_old_one_rewritten(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    state(lab.kb, "user", "stopped")

    assert rows(lab.kb) == [("missing", "installed but not running")]
    assert rows(lab.kb, "--fix") == [("fixed", RUNNING)]

    the_unit(lab.kb).write_text(the_unit(lab.kb).read_text().replace("--replace", ""))
    assert rows(lab.kb) == [("outdated", "it runs from an old unit")]
    assert rows(lab.kb, "--fix") == [("fixed", RUNNING)]
    assert calls(lab.kb)[-2:] == ["--user enable agentsview.service", "--user restart agentsview.service"]
    assert "--replace" in the_unit(lab.kb).read_text()


def test_a_user_unit_turned_off_stays_off(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    state(lab.kb, "user", "enabled", "disabled")
    before = len(calls(lab.kb))

    assert rows(lab.kb, "--fix") == [
        ("ok", "turned off (disabled), so fabricd captures no Claude Code or Codex session")
    ]
    assert all("show" in call for call in calls(lab.kb)[before:])


def test_a_unit_kb_did_not_write_is_left_as_it_is(lab: Lab) -> None:
    folder = lab.kb.home / UNITS
    folder.mkdir(parents=True)
    the_unit(lab.kb).write_text(mine := "[Service]\nExecStart=/opt/agentsview serve\n")

    assert rows(lab.kb, "--fix") == [("ok", "yours: kb leaves a unit it didn't write as it is")]
    assert the_unit(lab.kb).read_text() == mine
    assert probes(lab.kb) == 0


def test_no_unit_runs_an_agentsview_that_is_not_installed(lab: Lab) -> None:
    (lab.kb.home / ".local" / "bin" / "agentsview").unlink()

    assert rows(lab.kb, "--fix") == [("missing", "not installed: the unit would run ~/.local/bin/agentsview")]
    assert "Run scripts/install-agentsview.sh" in steps(lab.kb)
    assert probes(lab.kb) == 0


def test_where_systemd_cannot_be_asked_nothing_is_decided(lab: Lab) -> None:
    (lab.kb.home / "systemctl").mkdir(exist_ok=True)
    (lab.kb.home / "systemctl" / "no-bus").write_text("")

    assert rows(lab.kb, "--fix") == [("ok", "can't ask systemd from here whether it can sandbox agentsview")]
    assert not the_unit(lab.kb).exists()
    assert not (lab.kb.home / ".local" / "share" / "agent-fabric" / "agentsview.service").exists()


def test_a_first_install_systemd_would_not_start_is_removed_so_fix_probes_again(lab: Lab) -> None:
    state(lab.kb, "user", "cannot-enable")

    [(status, detail)] = rows(lab.kb, "--fix")

    assert (status, detail) == (
        "problem",
        "systemctl enable failed: Access denied; removed the unit, so --fix tries again",
    )
    assert not the_unit(lab.kb).exists()


def system_unit_installed(kb: Kb) -> Path:
    """The staged system unit, installed as the user's sudo line would."""
    system = agentsview_unit.SYSTEM_UNITS / "agentsview.service"
    system.parent.mkdir(parents=True, exist_ok=True)
    system.write_text((kb.home / ".local" / "share" / "agent-fabric" / "agentsview.service").read_text())
    return system


def test_a_plain_doctor_does_not_probe_and_points_at_fix(lab: Lab) -> None:
    assert rows(lab.kb) == [("missing", "not running, so fabricd captures no Claude Code or Codex session")]
    assert "Run kb doctor --fix, which sets it up" in steps(lab.kb)
    assert probes(lab.kb) == 0


def test_a_staged_system_unit_keeps_its_sudo_line_without_probing_again(lab: Lab) -> None:
    unenforced(lab.kb)
    lab.kb("doctor", "--fix")
    before = probes(lab.kb)

    [(status, detail)] = rows(lab.kb)

    assert status == "missing"
    assert detail.endswith("; staged at ~/.local/share/agent-fabric/agentsview.service")
    assert any(step.startswith("Run sudo install -m 0644 ") for step in steps(lab.kb))
    assert probes(lab.kb) == before


@pytest.mark.parametrize(
    ("exit", "why"),
    [
        ("1", "systemd-run failed (exit 1)"),  # say, a systemd that doesn't know a setting
        ("12", "loopback is blocked too, which a system unit's filter would block as well"),
        ("14", "its write in the home failed for another reason than a read-only home"),
    ],
)
def test_a_probe_that_cannot_tell_stages_nothing(lab: Lab, exit: str, why: str) -> None:
    (lab.kb.home / "systemd-run").mkdir()
    (lab.kb.home / "systemd-run" / "exit").write_text(exit)

    assert rows(lab.kb, "--fix") == [("problem", f"can't tell whether a user unit can sandbox it: {why}")]
    assert not (lab.kb.home / ".local" / "share" / "agent-fabric" / "agentsview.service").exists()
    assert not the_unit(lab.kb).exists()


def test_an_outdated_user_unit_the_user_disabled_is_rewritten_but_stays_off(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    state(lab.kb, "user", "enabled", "disabled")
    state(lab.kb, "user", "stopped")
    the_unit(lab.kb).write_text(the_unit(lab.kb).read_text().replace("--replace", ""))
    before = len(calls(lab.kb))

    [(status, detail)] = rows(lab.kb, "--fix")

    assert (status, detail) == (
        "fixed",
        "rewrote the unit; turned off (disabled), so fabricd captures no Claude Code or Codex session",
    )
    assert not any("enable" in call or "restart" in call for call in calls(lab.kb)[before:])
    assert "--replace" in the_unit(lab.kb).read_text()


def test_with_no_project_allowed_an_installed_unit_is_checked_but_not_written_or_started(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    (lab.kb.home / ".config" / "agent-fabric" / "config.toml").write_text("")
    the_unit(lab.kb).write_text(old := the_unit(lab.kb).read_text().replace("--replace", ""))
    state(lab.kb, "user", "stopped")
    before = len(calls(lab.kb))

    assert rows(lab.kb, "--fix") == [("ok", "installed, not running: no project is allowed, so --fix leaves it off")]
    assert the_unit(lab.kb).read_text() == old
    assert all(" show " in f" {call} " for call in calls(lab.kb)[before:])


def test_a_user_unit_left_beside_the_system_unit_is_a_problem(lab: Lab) -> None:
    lab.kb("doctor", "--fix")  # a user unit, while the sandbox held
    unenforced(lab.kb)
    the_unit(lab.kb).rename(the_unit(lab.kb).with_suffix(".kept"))
    lab.kb("doctor", "--fix")  # stages the system unit
    the_unit(lab.kb).with_suffix(".kept").rename(the_unit(lab.kb))
    system_unit_installed(lab.kb)

    assert rows(lab.kb)[1] == (
        "problem",
        "a user unit of kb's beside the system unit: the two take the port from each other",
    )
    assert any(
        step.startswith("Run systemctl --user disable --now agentsview.service && rm ") for step in steps(lab.kb)
    )


def test_a_bad_idle_setting_does_not_stop_agentsview_which_needs_only_the_port(lab: Lab) -> None:
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(config.read_text() + "\n[sessions]\nidle_minutes = 0\n")

    assert rows(lab.kb, "--fix") == [("fixed", RUNNING)]


def test_a_masked_system_unit_is_turned_off_not_yours(lab: Lab) -> None:
    system = agentsview_unit.SYSTEM_UNITS / "agentsview.service"
    system.parent.mkdir(parents=True)
    system.symlink_to("/dev/null")

    [(status, detail)] = rows(lab.kb, "--fix")

    assert (status, detail.split(";")[0]) == ("ok", "turned off (masked)")


def test_a_rewrite_systemd_has_not_reloaded_is_outdated_and_fix_reloads_it(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    state(lab.kb, "user", "need-reload")  # the file is current, but a reload failed after kb wrote it

    assert rows(lab.kb) == [("outdated", "systemd runs it from the unit as it was before kb rewrote it")]
    assert rows(lab.kb, "--fix") == [("fixed", RUNNING)]
    assert calls(lab.kb)[-2:] == ["--user enable agentsview.service", "--user restart agentsview.service"]
    assert rows(lab.kb) == [("ok", RUNNING)]


@pytest.mark.parametrize(
    ("answer", "exit"),
    [("exit", "226"), ("exit-filter", "11")],  # no namespaces for a user manager; a filter that isn't applied
)
def test_a_probe_that_says_no_needs_a_system_unit(lab: Lab, answer: str, exit: str) -> None:
    (lab.kb.home / "systemd-run").mkdir()
    (lab.kb.home / "systemd-run" / answer).write_text(exit)

    [(status, detail)] = rows(lab.kb, "--fix")

    assert status == "missing"
    assert detail.startswith("not installed: a user unit can't sandbox it here, so it needs a system unit")


def test_moved_codex_and_claude_homes_have_their_credentials_hidden(lab: Lab, tmp_path: Path) -> None:
    lab.kb.env["CODEX_HOME"] = str(tmp_path / "codex")
    lab.kb.env["CLAUDE_CONFIG_DIR"] = "~/claude-moved"

    lab.kb("doctor", "--fix")

    text = the_unit(lab.kb).read_text()
    assert f'InaccessiblePaths=-"{tmp_path}/codex/auth.json"\n' in text
    assert f'InaccessiblePaths=-"{lab.kb.home}/claude-moved/.credentials.json"\n' in text
    assert f'InaccessiblePaths=-"{lab.kb.home}/.codex/auth.json"\n' in text  # the default stays hidden too


def test_a_unit_disabled_without_now_still_runs_and_doctor_says_how_to_stop_it(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    state(lab.kb, "user", "enabled", "disabled")
    state(lab.kb, "user", "active", "active")

    [(status, detail)] = rows(lab.kb)

    assert (status, detail) == (
        "ok",
        "turned off (disabled), so fabricd captures no Claude Code or Codex session, but it still runs",
    )
    assert "Run systemctl --user stop agentsview.service, or it runs until the next reboot" in steps(lab.kb)


def test_the_system_unit_runs_as_the_user_kb_runs_as_not_the_homes_owner(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_name(self: Path) -> str:
        raise KeyError("a uid with no passwd entry")

    monkeypatch.setattr(Path, "owner", no_name)
    unenforced(lab.kb)
    lab.kb("doctor", "--fix")

    staged = (lab.kb.home / ".local" / "share" / "agent-fabric" / "agentsview.service").read_text()
    assert f"User={pwd.getpwuid(os.getuid()).pw_name}\n" in staged


def test_a_rewrite_where_systemd_cannot_say_whether_it_is_on_only_reloads(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    the_unit(lab.kb).write_text(the_unit(lab.kb).read_text().replace("--replace", ""))
    state(lab.kb, "user", "cannot-show")
    before = len(calls(lab.kb))

    assert rows(lab.kb, "--fix") == [
        ("fixed", "rewrote the unit; can't ask systemd from here whether it's on, so kb only reloaded it")
    ]
    assert not any("enable" in call or "restart" in call for call in calls(lab.kb)[before:])


def test_the_probe_reads_only_a_read_only_home_as_protected(tmp_path: Path) -> None:
    def probe(path: Path) -> int:
        command = [sys.executable, "-I", "-c", agentsview_unit.PROBE, "sandbox", str(path)]
        return subprocess.run(command, check=False).returncode

    assert probe(tmp_path / "new") == 10  # writable
    assert not (tmp_path / "new").exists()
    (tmp_path / "left").write_text("")
    assert probe(tmp_path / "left") == 14  # a file a killed run left: it says nothing of the home


def test_another_setup_failure_cannot_tell(lab: Lab) -> None:
    (lab.kb.home / "systemd-run").mkdir()
    (lab.kb.home / "systemd-run" / "exit").write_text("203")  # 203/EXEC: the probe's Python, not the sandbox

    assert rows(lab.kb, "--fix") == [
        ("problem", "can't tell whether a user unit can sandbox it: systemd-run failed (exit 203)")
    ]


def test_without_systemd_run_nothing_is_decided_and_doctor_says_so(lab: Lab, tmp_path: Path) -> None:
    (tmp_path / "stubs" / "systemd-run").unlink()
    lab.kb.env["PATH"] = str(tmp_path / "stubs")  # no system systemd-run either

    assert rows(lab.kb, "--fix") == [
        ("problem", "can't tell whether a user unit can sandbox it: systemd-run isn't on PATH")
    ]


def test_the_probes_run_the_sandbox_then_the_filter_alone(lab: Lab) -> None:
    lab.kb("doctor", "--fix")

    # Each run's arguments, the probe's own lines included.
    sandbox, network = (lab.kb.home / "systemd-run.log").read_text().split("--user --wait")[1:]
    assert "-p ProtectHome=read-only " in sandbox
    assert " sandbox " in sandbox
    assert network.rstrip().endswith(" filter")
    assert "-p IPAddressDeny=any " in network
    assert "IPAddressAllow" not in network
    assert "ProtectHome" not in network


def test_a_moved_gh_config_is_hidden(lab: Lab, tmp_path: Path) -> None:
    lab.kb.env["XDG_CONFIG_HOME"] = str(tmp_path / "cfg")
    (tmp_path / "cfg" / "agent-fabric").mkdir(parents=True)
    (tmp_path / "cfg" / "agent-fabric" / "config.toml").write_text(
        (lab.kb.home / ".config" / "agent-fabric" / "config.toml").read_text()
    )
    lab.kb("doctor", "--fix")
    assert f'InaccessiblePaths=-"{tmp_path}/cfg/gh"\n' in the_unit(lab.kb).read_text()

    lab.kb.env["GH_CONFIG_DIR"] = str(tmp_path / "gh-own")
    lab.kb("doctor", "--fix")
    text = the_unit(lab.kb).read_text()
    assert f'InaccessiblePaths=-"{tmp_path}/gh-own"\n' in text
    assert f'InaccessiblePaths=-"{lab.kb.home}/.config/gh"\n' in text


def test_moved_agent_homes_are_passed_to_agentsview(lab: Lab, tmp_path: Path) -> None:
    lab.kb.env["CODEX_HOME"] = "~/work/codex"

    lab.kb("doctor", "--fix")

    assert f'Environment="CODEX_HOME={lab.kb.home}/work/codex"\n' in the_unit(lab.kb).read_text()
    assert "CLAUDE_CONFIG_DIR" not in the_unit(lab.kb).read_text()


def test_a_failed_first_install_says_to_run_fix_again_not_to_start_a_removed_unit(lab: Lab) -> None:
    state(lab.kb, "user", "cannot-enable")

    lab.kb("doctor", "--fix")

    assert "Fix what systemctl says, then run kb doctor --fix again" in steps(lab.kb, "--fix")


def test_a_system_unit_only_root_can_read_is_the_users(lab: Lab) -> None:
    system = agentsview_unit.SYSTEM_UNITS / "agentsview.service"
    system.parent.mkdir(parents=True)
    system.write_text("[Service]\n")
    system.chmod(0o000)
    try:
        assert rows(lab.kb, "--fix") == [("ok", "yours: kb leaves a unit it didn't write as it is")]
    finally:
        system.chmod(0o600)


def test_a_system_unit_systemd_has_not_reloaded_is_outdated(lab: Lab) -> None:
    unenforced(lab.kb)
    lab.kb("doctor", "--fix")
    system_unit_installed(lab.kb)
    state(lab.kb, "system", "enabled", "enabled")
    state(lab.kb, "system", "need-reload")

    assert rows(lab.kb) == [("outdated", "systemd runs it from the unit as it was before it was installed")]
    assert "Run sudo systemctl daemon-reload && sudo systemctl restart agentsview.service" in steps(lab.kb)


def test_an_outdated_system_unit_the_user_disabled_is_only_installed_and_reloaded(lab: Lab) -> None:
    unenforced(lab.kb)
    lab.kb("doctor", "--fix")
    system = system_unit_installed(lab.kb)
    system.write_text(system.read_text().replace("--replace", ""))
    state(lab.kb, "system", "enabled", "disabled")

    [step] = [step for step in steps(lab.kb, "--fix") if step.startswith("Run sudo install")]

    assert step.endswith("&& sudo systemctl daemon-reload")


def unlink_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """unlink as in a read-only home, such as an agent's sandbox: it fails even where there's no file."""

    def read_only(self: Path, missing_ok: bool = False) -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "unlink", read_only)


def test_a_read_only_home_without_a_leftover_probe_file_still_probes(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlink_fails(monkeypatch)

    assert rows(lab.kb, "--fix") == [("fixed", RUNNING)]


def test_a_leftover_probe_file_kb_cannot_remove_cannot_tell(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    (lab.kb.home / f".kb-agentsview-probe-{os.getpid()}").write_text("")
    unlink_fails(monkeypatch)

    assert rows(lab.kb, "--fix") == [
        (
            "problem",
            "can't tell whether a user unit can sandbox it: it can't remove "
            f"~/.kb-agentsview-probe-{os.getpid()} (Read-only file system)",
        )
    ]
    assert probes(lab.kb) == 0


def test_a_disabled_system_unit_systemd_has_not_reloaded_is_only_reloaded(lab: Lab) -> None:
    unenforced(lab.kb)
    lab.kb("doctor", "--fix")
    system_unit_installed(lab.kb)
    state(lab.kb, "system", "enabled", "disabled")
    state(lab.kb, "system", "need-reload")

    assert "Run sudo systemctl daemon-reload" in steps(lab.kb)


def test_a_unit_folder_that_turns_out_read_only_is_a_problem_not_a_crash(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each unit kb writes, as from a sandbox whose units folder is read-only: unlink fails even where the temporary
    file was never made, which mustn't hide the write's own failure."""
    tools = lab.kb.home / ".local" / "share" / "uv" / "tools" / "agent-fabric" / "bin"
    a_stub(tools, "kb", "#!/bin/sh\n")
    a_stub(tools, "fabricd", "#!/bin/sh\n")
    write_text = Path.write_text

    def read_only_units(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.name.endswith(".kb-new"):
            raise OSError(30, "Read-only file system")
        return write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", read_only_units)
    unlink_fails(monkeypatch)

    checks = doctor(lab.kb, "--fix")["checks"]

    written = {row["check"]: row["detail"] for row in checks if row["check"] in ("timer", "fabricd", "agentsview")}
    assert set(written) == {"timer", "fabricd", "agentsview"}
    assert all(detail == "can't write it (Read-only file system)" for detail in written.values())
