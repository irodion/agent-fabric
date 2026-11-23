"""agentsview.service, which `kb doctor --fix` sets up once a project is allowed (docs/design.md §4.1, §5): a user
unit where a probe shows the sandbox holds, else a system unit kb stages for the user to install."""

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
    return len(log.read_text().splitlines()) if log.exists() else 0


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
    lab.kb("doctor")

    probe = (lab.kb.home / "systemd-run.log").read_text()
    for setting in agentsview_unit.SANDBOX:
        assert f"-p {setting} " in probe
    assert probe.startswith("--user --wait")


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

    assert rows(lab.kb) == [("missing", why)]

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
    assert calls(lab.kb)[-1] == "show --property=UnitFileState,ActiveState,Result agentsview.service"

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

    assert rows(lab.kb)[0][1] == "outdated: it runs from an old unit"
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
