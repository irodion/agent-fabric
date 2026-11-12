"""fabric-ingest.timer, which `kb doctor --fix` installs and `kb doctor` checks (docs/design.md §5)."""

from pathlib import Path
from typing import Any

import pytest
import toon_format
from conftest import Kb, Lab, a_stub

UNITS = Path(".config") / "systemd" / "user"
DAILY = "runs kb ingest daily at 03:00"


def installed_kb(kb: Kb, tool_dir: Path | None = None) -> Path:
    """The kb scripts/install.sh installs, which the service runs."""
    tools = tool_dir or kb.home / ".local" / "share" / "uv" / "tools"
    return a_stub(tools / "agent-fabric" / "bin", "kb", "#!/bin/sh\n")


@pytest.fixture(autouse=True)
def kb_installed(lab: Lab) -> None:
    installed_kb(lab.kb)


def doctor(kb: Kb, *argv: str) -> dict[str, Any]:
    decoded = toon_format.decode(kb("doctor", *argv).stdout)
    assert isinstance(decoded, dict)
    return decoded


def timer_rows(kb: Kb, *argv: str) -> list[tuple[str, str]]:
    return [(row["status"], row["detail"]) for row in doctor(kb, *argv)["checks"] if row["check"] == "timer"]


def steps(kb: Kb, *argv: str) -> list[str]:
    return [step["step"] for step in doctor(kb, *argv).get("next", [])]


def unit(kb: Kb, name: str) -> str:
    return (kb.home / UNITS / name).read_text()


def calls(kb: Kb) -> list[str]:
    log = kb.home / "systemctl.log"
    return log.read_text().splitlines() if log.exists() else []


def systemd_state(kb: Kb, name: str, text: str = "") -> None:
    (kb.home / "systemctl").mkdir(exist_ok=True)
    (kb.home / "systemctl" / name).write_text(text)


def test_no_timer_until_a_project_is_allowed(lab: Lab) -> None:
    (lab.kb.home / ".config" / "agent-fabric" / "config.toml").write_text("")

    assert timer_rows(lab.kb, "--fix") == []
    assert not (lab.kb.home / UNITS).exists()
    assert calls(lab.kb) == []


def test_doctor_reports_a_missing_timer_and_fix_installs_it(lab: Lab) -> None:
    assert timer_rows(lab.kb) == [("missing", "kb ingest doesn't run on its own")]
    assert calls(lab.kb) == []

    assert timer_rows(lab.kb, "--fix") == [("fixed", DAILY)]
    assert calls(lab.kb) == ["--user daemon-reload", "--user enable --now fabric-ingest.timer"]
    assert "OnCalendar=*-*-* 03:00:00\nPersistent=true\n" in unit(lab.kb, "fabric-ingest.timer")
    assert "WantedBy=timers.target" in unit(lab.kb, "fabric-ingest.timer")

    assert timer_rows(lab.kb) == [("ok", DAILY)]


def test_the_service_runs_the_installed_kb_with_the_folders_of_its_tools(lab: Lab, tmp_path: Path) -> None:
    lab.kb("doctor", "--fix")

    service = unit(lab.kb, "fabric-ingest.service")
    kb = lab.kb.home / ".local" / "share" / "uv" / "tools" / "agent-fabric" / "bin" / "kb"
    assert f'ExecStart="{kb}" ingest\n' in service
    # codex and gitleaks are the stubs here; git is the system's. A unit's PATH has none of a shell's additions.
    path = next(line for line in service.splitlines() if line.startswith("Environment="))
    assert path.startswith(f'Environment="PATH={tmp_path / "stubs"}:')
    assert "Type=oneshot" in service


def test_the_service_keeps_a_moved_data_folder(lab: Lab, tmp_path: Path) -> None:
    lab.kb.env["XDG_DATA_HOME"] = str(tmp_path / "data 100%")
    installed_kb(lab.kb, tmp_path / "data 100%" / "uv" / "tools")

    lab.kb("doctor", "--fix")

    assert f'Environment="XDG_DATA_HOME={tmp_path}/data 100%%"\n' in unit(lab.kb, "fabric-ingest.service")


def test_a_unit_kb_did_not_write_this_way_is_outdated_and_fix_rewrites_it(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    timer = lab.kb.home / UNITS / "fabric-ingest.timer"
    written = timer.read_text()
    timer.write_text(written.replace("03:00:00", "Mon 03:00:00"))

    assert timer_rows(lab.kb) == [("outdated", "kb ingest runs from an old unit")]
    assert timer_rows(lab.kb, "--fix") == [("fixed", DAILY)]
    assert timer.read_text() == written


def test_doctor_reports_a_failed_run_and_where_to_read_why(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    systemd_state(lab.kb, "result", "exit-code")

    assert timer_rows(lab.kb) == [("problem", "the last run failed (exit-code)")]
    assert any("journalctl --user -u fabric-ingest" in step for step in steps(lab.kb))
    assert lab.kb("doctor").exit_code == 1


def test_where_systemd_cannot_be_asked_doctor_checks_only_the_files(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    systemd_state(lab.kb, "no-bus")
    systemd_state(lab.kb, "result", "exit-code")

    [(status, detail)] = timer_rows(lab.kb)

    assert status == "ok"
    assert "can't ask systemd" in detail


def test_fix_reports_a_timer_systemd_would_not_turn_on(lab: Lab) -> None:
    systemd_state(lab.kb, "cannot-enable")

    [(status, detail)] = timer_rows(lab.kb, "--fix")

    assert status == "problem"
    assert "Access denied" in detail
    assert any("systemctl --user enable --now fabric-ingest.timer" in step for step in steps(lab.kb, "--fix"))


def only_on_path(lab: Lab, tmp_path: Path, *tools: str) -> None:
    """A PATH with only these tools, as stubs: no systemctl or codex this machine may have."""
    for tool in tools:
        a_stub(tmp_path / "only", tool, "#!/bin/sh\n")
    lab.kb.env["PATH"] = str(tmp_path / "only")


def test_without_systemd_doctor_says_to_run_ingest_by_hand(lab: Lab, tmp_path: Path) -> None:
    only_on_path(lab, tmp_path, "codex", "gitleaks")

    assert timer_rows(lab.kb, "--fix") == [("ok", "no systemd here, so kb ingest runs only when you start it")]
    assert "Run kb ingest yourself every few days" in steps(lab.kb)
    assert lab.kb("doctor").exit_code == 1  # kb isn't on this PATH; the timer row alone wouldn't fail it
    assert not (lab.kb.home / UNITS).exists()


def test_fix_leaves_the_timer_alone_while_codex_is_not_on_path(lab: Lab, tmp_path: Path) -> None:
    lab.kb("doctor", "--fix")
    written = unit(lab.kb, "fabric-ingest.service")
    only_on_path(lab, tmp_path, "gitleaks")
    a_stub(tmp_path / "only", "systemctl", (tmp_path / "stubs" / "systemctl").read_text())

    result = lab.kb("doctor", "--fix")

    assert timer_rows(lab.kb, "--fix") == [("ok", DAILY)]  # checked, not rewritten: the ingest row names codex
    assert "codex isn't on PATH" in result.stdout
    assert unit(lab.kb, "fabric-ingest.service") == written


def test_a_timer_that_was_stopped_is_not_running_and_fix_starts_it(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    systemd_state(lab.kb, "stopped")

    assert timer_rows(lab.kb) == [("missing", "installed but not running")]
    assert timer_rows(lab.kb, "--fix") == [("fixed", DAILY)]
    assert timer_rows(lab.kb) == [("ok", DAILY)]


def test_doctor_asks_systemd_once(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    before = len(calls(lab.kb))

    lab.kb("doctor")

    assert len(calls(lab.kb)) == before + 1


def test_a_unit_kb_cannot_read_is_a_problem_not_a_crash(lab: Lab) -> None:
    (lab.kb.home / UNITS / "fabric-ingest.service").mkdir(parents=True)

    [(status, detail)] = timer_rows(lab.kb, "--fix")

    assert status == "problem"
    assert detail.startswith("can't read the units")
    assert any(step.startswith("Look at ~/.config/systemd/user/fabric-ingest.*") for step in steps(lab.kb))


def test_the_units_go_where_systemd_looks_whatever_kbs_config_folder(lab: Lab, tmp_path: Path) -> None:
    moved = tmp_path / "cfg"
    (moved / "agent-fabric").mkdir(parents=True)
    (moved / "agent-fabric" / "config.toml").write_text('[publish]\nallow = ["github.com/me/proj"]\n')
    lab.kb.env["XDG_CONFIG_HOME"] = str(moved)

    assert timer_rows(lab.kb, "--fix") == [("fixed", DAILY)]
    assert f'Environment="XDG_CONFIG_HOME={moved}"\n' in unit(lab.kb, "fabric-ingest.service")
    assert not (moved / "systemd").exists()


def test_the_service_gets_nodes_folder_and_codexs_home(lab: Lab, tmp_path: Path) -> None:
    node = a_stub(tmp_path / "node-bin", "node", "#!/bin/sh\n")
    lab.kb.env["PATH"] = f"{node.parent}:{lab.kb.env['PATH']}"
    lab.kb.env["CODEX_HOME"] = str(tmp_path / "codex-home")

    lab.kb("doctor", "--fix")

    service = unit(lab.kb, "fabric-ingest.service")
    path = next(line for line in service.splitlines() if line.startswith('Environment="PATH='))
    assert f":{node.parent}:" in path  # wherever git is found, which differs between machines
    assert f'Environment="CODEX_HOME={tmp_path}/codex-home"\n' in service


def test_a_dollar_in_kbs_path_is_not_expanded_by_systemd(lab: Lab, tmp_path: Path) -> None:
    lab.kb.env["UV_TOOL_DIR"] = str(tmp_path / "tools$HOME")
    installed_kb(lab.kb, tmp_path / "tools$HOME")

    lab.kb("doctor", "--fix")

    assert f'ExecStart="{tmp_path}/tools$$HOME/agent-fabric/bin/kb" ingest\n' in unit(lab.kb, "fabric-ingest.service")


def test_an_installed_timer_is_still_checked_once_no_project_is_allowed(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    written = unit(lab.kb, "fabric-ingest.timer")
    (lab.kb.home / ".config" / "agent-fabric" / "config.toml").write_text("")
    systemd_state(lab.kb, "result", "exit-code")

    assert timer_rows(lab.kb, "--fix") == [("problem", "the last run failed (exit-code)")]
    assert unit(lab.kb, "fabric-ingest.timer") == written


def test_a_unit_saved_from_another_shells_path_is_current_while_it_finds_the_tools(lab: Lab, tmp_path: Path) -> None:
    lab.kb("doctor", "--fix")
    written = unit(lab.kb, "fabric-ingest.service")
    node = a_stub(tmp_path / "other-node", "node", "#!/bin/sh\n")  # as after an `nvm use`
    lab.kb.env["PATH"] = f"{node.parent}:{lab.kb.env['PATH']}"

    assert timer_rows(lab.kb, "--fix") == [("ok", DAILY)]
    assert unit(lab.kb, "fabric-ingest.service") == written


def test_a_unit_whose_saved_path_lost_codex_is_outdated(lab: Lab, tmp_path: Path) -> None:
    lab.kb.env["PATH"] = f"{tmp_path / 'stubs'}:/usr/bin:/bin"  # no codex but the stub
    lab.kb("doctor", "--fix")
    moved = a_stub(tmp_path / "moved", "codex", "#!/bin/sh\n")
    (tmp_path / "stubs" / "codex").unlink()
    lab.kb.env["PATH"] = f"{moved.parent}:{lab.kb.env['PATH']}"

    assert timer_rows(lab.kb) == [("outdated", "its PATH no longer finds codex, so each run fails")]
    assert timer_rows(lab.kb, "--fix") == [("fixed", DAILY)]
    assert str(moved.parent) in unit(lab.kb, "fabric-ingest.service")


def test_a_timer_the_user_turned_off_stays_off(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    systemd_state(lab.kb, "enabled", "disabled")
    turned_off = "turned off (disabled); systemctl --user enable --now fabric-ingest.timer turns it back on"

    assert timer_rows(lab.kb, "--fix") == [("ok", turned_off)]
    assert calls(lab.kb)[-1].startswith("--user show")


def test_no_unit_runs_a_kb_that_is_not_installed(lab: Lab) -> None:
    installed_kb(lab.kb).unlink()

    [(status, detail)] = timer_rows(lab.kb, "--fix")

    assert (status, detail) == (
        "problem",
        "the service would run ~/.local/share/uv/tools/agent-fabric/bin/kb, which isn't there",
    )
    assert "Run scripts/install.sh, which installs that kb" in steps(lab.kb)
    assert not (lab.kb.home / UNITS).exists()


def test_a_relative_folder_on_path_goes_into_the_unit_absolute(
    lab: Lab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a_stub(tmp_path / "rel" / "bin", "codex", "#!/bin/sh\n")
    monkeypatch.chdir(tmp_path / "rel")
    lab.kb.env["PATH"] = f"bin:{lab.kb.env['PATH']}"

    lab.kb("doctor", "--fix")

    service = unit(lab.kb, "fabric-ingest.service")
    assert f":{tmp_path / 'rel' / 'bin'}:" in service
    assert ":bin:" not in service


def test_a_run_that_finds_another_running_is_not_a_failure(lab: Lab) -> None:
    lab.kb("doctor", "--fix")

    assert "SuccessExitStatus=75\n" in unit(lab.kb, "fabric-ingest.service")


def test_fix_rewrites_the_units_of_a_timer_the_user_turned_off_and_leaves_it_off(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    timer = lab.kb.home / UNITS / "fabric-ingest.timer"
    written = timer.read_text()
    timer.write_text(written.replace("03:00:00", "Mon 03:00:00"))
    systemd_state(lab.kb, "enabled", "disabled")

    [(status, detail)] = timer_rows(lab.kb, "--fix")

    assert (status, detail.split(";")[0]) == ("fixed", "rewrote the units")
    assert "turned off (disabled)" in detail
    assert timer.read_text() == written
    assert calls(lab.kb)[-1] == "--user daemon-reload"
    assert not list((lab.kb.home / UNITS).glob(".*kb-new"))


def test_a_masked_timer_stays_masked(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    systemd_state(lab.kb, "enabled", "masked")
    before = len(calls(lab.kb))
    masked = (
        "turned off (masked); systemctl --user unmask fabric-ingest.timer && systemctl --user enable --now"
        " fabric-ingest.timer turns it back on"
    )

    assert timer_rows(lab.kb, "--fix") == [("ok", masked)]
    assert not any(call.startswith("--user enable") for call in calls(lab.kb)[before:])


def test_a_timer_disabled_without_now_still_fires_and_doctor_says_how_to_stop_it(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    systemd_state(lab.kb, "enabled", "disabled")
    systemd_state(lab.kb, "active", "active")

    [(status, detail)] = timer_rows(lab.kb)

    assert status == "ok"
    assert detail.endswith("but it still waits for its next run")
    assert "Run systemctl --user stop fabric-ingest.timer, or tonight's run still happens" in steps(lab.kb)


def test_a_unit_whose_saved_path_lost_node_is_outdated(lab: Lab, tmp_path: Path) -> None:
    node = a_stub(tmp_path / "old-node", "node", "#!/bin/sh\n")
    lab.kb.env["PATH"] = f"{tmp_path / 'stubs'}:{node.parent}:/usr/bin:/bin"
    lab.kb("doctor", "--fix")
    node.unlink()  # as when nvm removes a version
    lab.kb.env["PATH"] = f"{a_stub(tmp_path / 'new-node', 'node', '#!/bin/sh\n').parent}:{lab.kb.env['PATH']}"

    assert timer_rows(lab.kb) == [("outdated", "its PATH no longer finds node, so each run fails")]


def test_a_unit_kb_cannot_write_is_left_as_it_was(lab: Lab) -> None:
    lab.kb("doctor", "--fix")
    folder = lab.kb.home / UNITS
    timer = folder / "fabric-ingest.timer"
    timer.write_text(timer.read_text().replace("03:00:00", "Mon 03:00:00"))
    before = {path.name: path.read_text() for path in folder.iterdir()}
    folder.chmod(0o500)
    try:
        report = doctor(lab.kb, "--fix")
    finally:
        folder.chmod(0o700)

    [row] = [row for row in report["checks"] if row["check"] == "timer"]
    assert (row["status"], row["detail"].split(" (")[0]) == ("problem", "can't write it")
    assert {"by": "user", "step": "Make ~/.config/systemd/user writable, then run kb doctor --fix again"} in report[
        "next"
    ]
    assert {path.name: path.read_text() for path in folder.iterdir()} == before


def test_a_unit_masked_by_a_link_to_dev_null_is_left_alone(lab: Lab) -> None:
    folder = lab.kb.home / UNITS
    folder.mkdir(parents=True)
    (folder / "fabric-ingest.timer").symlink_to("/dev/null")  # what systemctl mask makes

    [(status, detail)] = timer_rows(lab.kb, "--fix")

    assert (status, detail.split(";")[0]) == ("ok", "turned off (masked)")
    assert (folder / "fabric-ingest.timer").is_symlink()
    assert not (folder / "fabric-ingest.service").exists()
    assert calls(lab.kb) == []


def test_a_first_install_systemd_would_not_turn_on_is_removed_so_fix_tries_again(lab: Lab) -> None:
    systemd_state(lab.kb, "cannot-enable")

    [(status, detail)] = timer_rows(lab.kb, "--fix")

    assert status == "problem"
    assert detail.endswith("removed the units, so --fix tries again")
    assert not (lab.kb.home / UNITS / "fabric-ingest.timer").exists()

    (lab.kb.home / "systemctl" / "cannot-enable").unlink()

    assert timer_rows(lab.kb, "--fix") == [("fixed", DAILY)]
