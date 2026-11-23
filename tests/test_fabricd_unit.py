"""fabricd.service, which `kb doctor --fix` installs and starts once a project is allowed, and `kb doctor` checks
(docs/design.md §5). The stand-in systemctl keeps fabricd's state apart from the timers'."""

from pathlib import Path
from typing import Any

import pytest
import toon_format
from conftest import Kb, Lab, a_stub

UNITS = Path(".config") / "systemd" / "user"
RUNNING = "running: it follows the agents' sessions and writes their digests"
SETUP = ["--user daemon-reload", "--user enable fabricd.service", "--user restart fabricd.service"]
FIRST = ["--user daemon-reload", "--user enable --now fabricd.service"]


def installed_fabricd(kb: Kb, tool_dir: Path | None = None) -> Path:
    """The fabricd scripts/install.sh installs beside kb, which the unit runs."""
    tools = tool_dir or kb.home / ".local" / "share" / "uv" / "tools"
    return a_stub(tools / "agent-fabric" / "bin", "fabricd", "#!/bin/sh\n")


@pytest.fixture(autouse=True)
def fabricd_installed(lab: Lab) -> None:
    installed_fabricd(lab.kb)


def doctor(kb: Kb, *argv: str) -> dict[str, Any]:
    decoded = toon_format.decode(kb("doctor", *argv).stdout)
    assert isinstance(decoded, dict)
    return decoded


def rows(kb: Kb, *argv: str) -> list[tuple[str, str]]:
    return [(row["status"], row["detail"]) for row in doctor(kb, *argv)["checks"] if row["check"] == "fabricd"]


def steps(kb: Kb, *argv: str) -> list[str]:
    return [step["step"] for step in doctor(kb, *argv).get("next", [])]


def the_unit(kb: Kb) -> Path:
    return kb.home / UNITS / "fabricd.service"


def calls(kb: Kb) -> list[str]:
    """fabricd's calls to systemctl, and the reloads, which name no unit."""
    log = kb.home / "systemctl.log"
    lines = log.read_text().splitlines() if log.exists() else []
    return [line for line in lines if line.endswith("fabricd.service") or line == "--user daemon-reload"]


def state(kb: Kb, name: str, text: str = "") -> None:
    folder = kb.home / "systemctl" / "fabricd"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text)


def only_on_path(lab: Lab, tmp_path: Path, *tools: str) -> None:
    """A PATH with only these tools, as stubs, and the stand-in systemctl."""
    for tool in tools:
        a_stub(tmp_path / "only", tool, "#!/bin/sh\n")
    a_stub(tmp_path / "only", "systemctl", (tmp_path / "stubs" / "systemctl").read_text())
    lab.kb.env["PATH"] = str(tmp_path / "only")


def test_no_unit_until_a_project_is_allowed(lab: Lab) -> None:
    (lab.kb.home / ".config" / "agent-fabric" / "config.toml").write_text("")

    assert rows(lab.kb, "--fix") == []
    assert not the_unit(lab.kb).exists()
    assert calls(lab.kb) == []


def test_doctor_reports_a_missing_unit_and_fix_installs_and_starts_it(lab: Lab) -> None:
    assert rows(lab.kb) == [("missing", "fabricd doesn't run on its own")]
    assert lab.kb("doctor").exit_code == 1

    assert rows(lab.kb, "--fix") == [("fixed", RUNNING)]
    assert calls(lab.kb)[-2:] == FIRST

    assert rows(lab.kb) == [("ok", RUNNING)]


def test_the_unit_runs_the_installed_fabricd_and_stops_it_alone(lab: Lab, tmp_path: Path) -> None:
    lab.kb("doctor", "--fix")

    text = the_unit(lab.kb).read_text()
    fabricd = lab.kb.home / ".local" / "share" / "uv" / "tools" / "agent-fabric" / "bin" / "fabricd"
    assert f'ExecStart="{fabricd}"\n' in text
    # gitleaks is the stub here, git the system's: a unit's PATH has none of a shell's additions.
    assert f'Environment="PATH={tmp_path / "stubs"}:' in text
    for line in ("Type=exec", "KillMode=mixed", "Restart=on-failure", "RestartPreventExitStatus=2 3"):
        assert f"\n{line}\n" in text
    assert text.endswith("[Install]\nWantedBy=default.target\n")


def test_the_unit_keeps_moved_folders(lab: Lab, tmp_path: Path) -> None:
    lab.kb.env["XDG_DATA_HOME"] = str(tmp_path / "data 100%")
    lab.kb.env["COPILOT_HOME"] = str(tmp_path / "copilot")
    installed_fabricd(lab.kb, tmp_path / "data 100%" / "uv" / "tools")

    lab.kb("doctor", "--fix")

    text = the_unit(lab.kb).read_text()
    assert f'Environment="XDG_DATA_HOME={tmp_path}/data 100%%"\n' in text
    assert f'Environment="COPILOT_HOME={tmp_path}/copilot"\n' in text
    assert f'ExecStart="{tmp_path}/data 100%%/uv/tools/agent-fabric/bin/fabricd"\n' in text


def test_an_old_unit_is_outdated_and_fix_rewrites_and_restarts_it(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    written = the_unit(lab.kb).read_text()
    the_unit(lab.kb).write_text(written.replace("KillMode=mixed\n", ""))

    assert rows(lab.kb) == [("outdated", "fabricd runs from an old unit")]
    before = len(calls(lab.kb))
    assert rows(lab.kb, "--fix") == [("fixed", RUNNING)]
    assert the_unit(lab.kb).read_text() == written
    assert calls(lab.kb)[before:] == [
        "--user show --property=UnitFileState,ActiveState,Result,ExecMainStatus,NeedDaemonReload fabricd.service",
        *SETUP,
    ]


def test_a_unit_whose_saved_path_lost_gitleaks_is_outdated(lab: Lab, tmp_path: Path) -> None:
    only_on_path(lab, tmp_path, "gitleaks", "git")
    lab.kb("doctor", "--fix")
    lab.kb.env["PATH"] = f"{a_stub(tmp_path / 'moved', 'gitleaks', '#!/bin/sh\n').parent}:{lab.kb.env['PATH']}"
    (tmp_path / "only" / "gitleaks").unlink()

    assert rows(lab.kb) == [("outdated", "its PATH no longer finds gitleaks, so no digest can be written")]
    assert rows(lab.kb, "--fix") == [("fixed", RUNNING)]
    assert f'Environment="PATH={tmp_path / "moved"}:' in the_unit(lab.kb).read_text()


def test_no_unit_while_gitleaks_is_not_on_path(lab: Lab, tmp_path: Path) -> None:
    only_on_path(lab, tmp_path, "git", "codex")

    assert rows(lab.kb, "--fix") == [("missing", "not installed: it needs gitleaks on PATH")]
    assert "Install gitleaks, then run kb doctor --fix" in steps(lab.kb)
    assert not the_unit(lab.kb).exists()


def test_no_unit_runs_a_fabricd_that_is_not_installed(lab: Lab) -> None:
    installed_fabricd(lab.kb).unlink()

    [(status, detail)] = rows(lab.kb, "--fix")

    assert (status, detail) == (
        "problem",
        "the unit would run ~/.local/share/uv/tools/agent-fabric/bin/fabricd, which isn't there",
    )
    assert "Run scripts/install.sh, which installs that fabricd" in steps(lab.kb)
    assert not the_unit(lab.kb).exists()


def test_a_unit_the_user_disabled_stays_off_even_when_rewritten(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    state(lab.kb, "enabled", "disabled")
    state(lab.kb, "stopped")
    off = "turned off (disabled); systemctl --user enable --now fabricd.service turns it back on"

    assert rows(lab.kb, "--fix") == [("ok", off)]

    the_unit(lab.kb).write_text(the_unit(lab.kb).read_text().replace("KillMode=mixed\n", ""))
    before = len(calls(lab.kb))
    assert rows(lab.kb, "--fix") == [("fixed", f"rewrote the unit; {off}")]
    assert calls(lab.kb)[before:][1:] == ["--user daemon-reload"]
    assert rows(lab.kb) == [("ok", off)]


def test_a_unit_disabled_without_now_still_runs_and_doctor_says_how_to_stop_it(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    state(lab.kb, "enabled", "disabled")
    state(lab.kb, "active", "active")

    [(status, detail)] = rows(lab.kb)

    assert (status, detail.split(";")[0]) == ("ok", "turned off (disabled)")
    assert detail.endswith(", but it still runs")
    assert "Run systemctl --user stop fabricd.service, or it runs until the next reboot" in steps(lab.kb)


def test_a_unit_masked_by_a_link_to_dev_null_is_left_alone(lab: Lab) -> None:
    folder = lab.kb.home / UNITS
    folder.mkdir(parents=True)
    the_unit(lab.kb).symlink_to("/dev/null")  # what systemctl mask makes

    [(status, detail)] = rows(lab.kb, "--fix")

    assert (status, detail.split(";")[0]) == ("ok", "turned off (masked)")
    assert the_unit(lab.kb).is_symlink()
    assert calls(lab.kb) == []


def test_a_failed_fabricd_is_a_problem_fix_leaves_for_the_user(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    state(lab.kb, "active", "failed")
    state(lab.kb, "result", "exit-code")
    before = len(calls(lab.kb))

    assert rows(lab.kb, "--fix") == [("problem", "stopped: it failed (exit-code)")]
    assert (
        "Read journalctl --user -u fabricd.service and fix the cause; systemctl --user restart fabricd.service"
        in steps(lab.kb)
    )
    assert not any("enable" in call or "restart" in call for call in calls(lab.kb)[before:])


def test_a_stopped_fabricd_is_not_running_and_fix_starts_it(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    state(lab.kb, "stopped")

    assert rows(lab.kb) == [("missing", "installed but not running")]
    assert rows(lab.kb, "--fix") == [("fixed", RUNNING)]
    assert calls(lab.kb)[-1] == "--user enable --now fabricd.service"
    assert rows(lab.kb) == [("ok", RUNNING)]


def test_where_systemd_cannot_be_asked_doctor_checks_only_the_file(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    (lab.kb.home / "systemctl" / "no-bus").write_text("")

    assert rows(lab.kb, "--fix") == [("ok", "installed; can't ask systemd from here whether it runs")]


def test_without_systemd_doctor_says_to_run_fabricd_by_hand(lab: Lab, tmp_path: Path) -> None:
    only_on_path(lab, tmp_path, "git", "gitleaks", "codex")
    (tmp_path / "only" / "systemctl").unlink()

    assert rows(lab.kb, "--fix") == [("ok", "no systemd here, so fabricd runs only when you start it")]
    assert "Run fabricd in a terminal of its own" in steps(lab.kb)
    assert not the_unit(lab.kb).exists()


def test_a_first_install_systemd_would_not_turn_on_is_removed_so_fix_tries_again(lab: Lab) -> None:
    state(lab.kb, "cannot-enable")

    [(status, detail)] = rows(lab.kb, "--fix")

    assert (status, detail) == (
        "problem",
        "systemctl enable failed: Access denied; removed the unit, so --fix tries again",
    )
    assert not the_unit(lab.kb).exists()

    (lab.kb.home / "systemctl" / "fabricd" / "cannot-enable").unlink()

    assert rows(lab.kb, "--fix") == [("fixed", RUNNING)]


def test_an_installed_unit_is_still_checked_once_no_project_is_allowed(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    (lab.kb.home / ".config" / "agent-fabric" / "config.toml").write_text("")
    the_unit(lab.kb).write_text(old := the_unit(lab.kb).read_text().replace("KillMode=mixed\n", ""))
    state(lab.kb, "active", "failed")
    state(lab.kb, "result", "exit-code")

    # Failed, but stopped as kb would leave it: no step to restart a daemon the user may mean to turn off.
    assert rows(lab.kb, "--fix") == [("ok", "installed, not running: no project is allowed, so --fix leaves it off")]
    assert the_unit(lab.kb).read_text() == old  # checked, never written


def test_a_unit_kb_cannot_write_is_left_as_it_was(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    folder = lab.kb.home / UNITS
    the_unit(lab.kb).write_text(old := the_unit(lab.kb).read_text().replace("KillMode=mixed\n", ""))
    folder.chmod(0o500)
    try:
        [(status, detail)] = rows(lab.kb, "--fix")
    finally:
        folder.chmod(0o700)

    assert (status, detail.split(" (")[0]) == ("problem", "can't write it")
    assert the_unit(lab.kb).read_text() == old
    assert not (folder / ".fabricd.service.kb-new").exists()


def test_a_unit_kb_cannot_read_is_a_problem_not_a_crash(lab: Lab) -> None:
    folder = lab.kb.home / UNITS
    folder.mkdir(parents=True)
    the_unit(lab.kb).write_bytes(b"\xff\xfe")

    [(status, detail)] = rows(lab.kb, "--fix")

    assert status == "problem"
    assert detail.startswith("can't read the unit")
    assert the_unit(lab.kb).read_bytes() == b"\xff\xfe"


def test_a_bad_fabricd_setting_is_a_problem_and_no_unit_is_written(lab: Lab) -> None:
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(config.read_text() + "\n[agentsview]\nport = 0\n")

    [(status, detail)] = rows(lab.kb, "--fix")

    assert (status, detail) == ("problem", "can't start: [agentsview] port is a port number, not 0")
    assert "Fix it in ~/.config/agent-fabric/config.toml, then run kb doctor --fix" in steps(lab.kb)
    assert not the_unit(lab.kb).exists()


def test_a_fabricd_stopped_by_a_bad_config_is_started_once_the_config_reads(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    good = config.read_text()
    config.write_text(good + "\n[sessions]\nidle_minutes = 0\n")
    state(lab.kb, "active", "failed")
    state(lab.kb, "result", "exit-code")
    state(lab.kb, "status", "2")

    assert rows(lab.kb, "--fix")[0][0] == "problem"

    config.write_text(good)
    assert rows(lab.kb) == [("missing", "stopped on a config it couldn't read, which reads now")]
    assert rows(lab.kb, "--fix") == [("fixed", RUNNING)]
    assert calls(lab.kb)[-1] == "--user enable --now fabricd.service"
    assert rows(lab.kb) == [("ok", RUNNING)]


def test_a_tilde_copilot_home_is_carried_expanded(lab: Lab) -> None:
    lab.kb.env["COPILOT_HOME"] = "~/moved-copilot"

    lab.kb("doctor", "--fix")

    assert f'Environment="COPILOT_HOME={lab.kb.home}/moved-copilot"\n' in the_unit(lab.kb).read_text()


def test_a_stopped_fabricd_with_no_project_allowed_is_ok_and_left_off(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    (lab.kb.home / ".config" / "agent-fabric" / "config.toml").write_text("")
    state(lab.kb, "stopped")
    before = len(calls(lab.kb))

    assert rows(lab.kb, "--fix") == [("ok", "installed, not running: no project is allowed, so --fix leaves it off")]
    assert not any("enable" in call for call in calls(lab.kb)[before:])


def test_a_fabricd_service_kb_did_not_write_is_left_as_it_is(lab: Lab) -> None:
    folder = lab.kb.home / UNITS
    folder.mkdir(parents=True)
    the_unit(lab.kb).write_text(mine := "[Service]\nExecStart=/opt/fabricd\n")

    assert rows(lab.kb, "--fix") == [("ok", "yours: kb leaves a unit it didn't write as it is")]
    assert the_unit(lab.kb).read_text() == mine
    assert calls(lab.kb) == []


def test_a_fabricd_waiting_to_restart_reads_as_restarting(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    state(lab.kb, "active", "activating")
    state(lab.kb, "result", "exit-code")

    assert rows(lab.kb, "--fix") == [
        ("problem", "restarting: it failed (exit-code), and systemd starts it again shortly")
    ]


def test_an_installed_unit_is_judged_by_its_saved_path_not_kbs(lab: Lab, tmp_path: Path) -> None:
    lab.kb("doctor", "--fix")
    only_on_path(lab, tmp_path, "git", "codex")  # kb now runs from a shell without gitleaks

    assert rows(lab.kb) == [("ok", RUNNING)]


def test_a_unit_systemd_has_not_reloaded_is_outdated_and_fix_reloads_and_restarts_it(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    the_unit(lab.kb).write_text(the_unit(lab.kb).read_text().replace("KillMode=mixed\n", ""))
    (lab.kb.home / "systemctl" / "fabricd" / "cannot-reload").write_text("")
    assert rows(lab.kb, "--fix")[0] == ("problem", "systemctl daemon-reload failed: Timeout")
    (lab.kb.home / "systemctl" / "fabricd" / "cannot-reload").unlink()
    state(lab.kb, "need-reload")  # the file is kb's as it would write it now, but systemd runs the old one

    assert rows(lab.kb) == [("outdated", "systemd runs it from the unit as it was before kb rewrote it")]
    before = len(calls(lab.kb))
    assert rows(lab.kb, "--fix") == [("fixed", RUNNING)]
    assert calls(lab.kb)[before:][-3:] == SETUP
    assert rows(lab.kb) == [("ok", RUNNING)]


def test_a_fabricd_stopped_because_another_runs_says_so(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    state(lab.kb, "active", "failed")
    state(lab.kb, "result", "exit-code")
    state(lab.kb, "status", "3")

    assert rows(lab.kb, "--fix") == [("problem", "stopped: another fabricd runs on its data folder")]
    assert "Stop the other fabricd, then systemctl --user restart fabricd.service" in steps(lab.kb)


def test_an_old_unit_that_cannot_be_rewritten_without_gitleaks_is_outdated(lab: Lab, tmp_path: Path) -> None:
    lab.kb("doctor", "--fix")
    the_unit(lab.kb).write_text(the_unit(lab.kb).read_text().replace("KillMode=mixed\n", ""))
    only_on_path(lab, tmp_path, "git", "codex")

    assert rows(lab.kb, "--fix") == [
        ("outdated", "fabricd runs from an old unit, which kb rewrites once gitleaks is on PATH")
    ]


def test_a_units_folder_kb_cannot_read_is_no_row_where_no_project_is_allowed(lab: Lab) -> None:
    (lab.kb.home / ".config" / "agent-fabric" / "config.toml").write_text("")
    folder = lab.kb.home / UNITS
    folder.mkdir(parents=True)
    folder.chmod(0o000)
    try:
        assert rows(lab.kb, "--fix") == []
    finally:
        folder.chmod(0o700)
