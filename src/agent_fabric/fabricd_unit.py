"""fabricd.service: fabricd as a systemd user unit, which `kb doctor --fix` writes and starts once some workspace
allows a project, as it does ingest's timers (docs/design.md §5). The allow list is the user's say-so to publish,
and a machine that only takes notes gets no daemon. A unit the user turned off stays off, and a unit kb installed is
still checked once no project is allowed, but only written or started while one is. A `fabricd.service` that
doesn't start with kb's header is the user's own, and kb leaves it as it is."""

import contextlib
import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path

from agent_fabric.config import BAD_CONFIG, LOCKED, Config, fabricd_settings
from agent_fabric.paths import config_file, expand, tilde, units_dir, uv_tool_dir
from agent_fabric.report import Row
from agent_fabric.timer import (
    BASE_PATH,
    CARRIED,
    HEADER,
    OFF,
    RELOAD,
    SAVED_PATH,
    _masked,
    _quoted,
    _read,
    _systemctl,
    saved_path,
)

NAME = "fabricd.service"
TOOLS = ("gitleaks", "git")  # what a digest's write runs (§4.5), in the order PATH searches them
HOMES = ("COPILOT_HOME",)  # where Copilot CLI's sessions are, when moved
# How long a stop waits before it kills: past a digest write's gitleaks scan (60 s) and wiki commit (120 s, for its
# hooks), with room for the step's git reads; systemd's default, 90 s, would kill a commit halfway.
STOP_WAIT = 300
RUNNING = "running: it follows the agents' sessions and writes their digests"


def installed_fabricd(env: Mapping[str, str]) -> Path:
    """The fabricd scripts/install.sh installs beside kb, which the unit runs: not a virtualenv's."""
    return uv_tool_dir(env) / "agent-fabric" / "bin" / "fabricd"


def _path(env: Mapping[str, str]) -> str:
    """The folders of the tools fabricd runs, from the PATH kb runs with: a unit gets none of a shell's PATH."""
    found = (shutil.which(tool, path=env.get("PATH", "")) for tool in TOOLS)
    folders = [str(Path(tool).absolute().parent) for tool in found if tool]
    return ":".join(dict.fromkeys([*folders, *BASE_PATH]))


def unit(env: Mapping[str, str]) -> str:
    """The unit as kb would write it now. A stop signals fabricd alone (`KillMode=mixed`): it waits for the step under
    way, and the git or gitleaks run of that step mustn't be killed with it (§5). A config it can't read stops it
    until the user fixes it and `kb doctor --fix` starts it again, and so does another fabricd on its data folder; any
    other failure restarts it."""
    named = [(name, env.get(name) or "") for name in CARRIED] + [
        (name, str(expand(env.get(name) or "", env)))
        for name in HOMES  # `~` expanded, as Copilot's reader does
    ]
    carried = [(name, value) for name, value in named if Path(value).is_absolute()]
    environment = "".join(
        f"Environment={_quoted(f'{name}={value}')}\n" for name, value in [("PATH", _path(env)), *carried]
    )
    return (
        f"{HEADER}[Unit]\nDescription=fabricd: digests of the agents' sessions\n\n"
        f"[Service]\nType=exec\n{environment}"
        f"ExecStart={_quoted(str(installed_fabricd(env)), command=True)}\n"
        f"Restart=on-failure\nRestartSec=30\nRestartPreventExitStatus={BAD_CONFIG} {LOCKED}\n"
        f"KillMode=mixed\nTimeoutStopSec={STOP_WAIT}\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def check(env: Mapping[str, str], config: Config | Exception, *, wanted: bool, fix: bool) -> list[Row]:
    """fabricd's row: none where no project is allowed and kb never installed it. `wanted`: some workspace allows a
    project. `config`: kb's config, or why it couldn't be read, which fabricd would stop on too."""
    folder = units_dir(env)
    path = folder / NAME
    shown = tilde(path, env)
    try:
        current = _read(path)
    except (OSError, UnicodeDecodeError) as e:
        return [_unreadable(e, env, path)] if wanted else []  # a notes-only machine has no unit to read
    if not wanted and current is None and not path.is_symlink():
        return []
    if (left := _left(env, path, current, config)) is not None:
        return [left]
    command = shutil.which("systemctl", path=env.get("PATH", ""))
    if command is None:
        detail = "no systemd here, so fabricd runs only when you start it"
        return [Row("ok", "fabricd", shown, detail, step="Run fabricd in a terminal of its own")]
    if not wanted:  # checked, never written
        return [_state(command, env, path, fix=False, wanted=False)]
    return [_wanted(command, env, path, current, fix=fix)]


def _unreadable(e: Exception, env: Mapping[str, str], path: Path) -> Row:
    shown = tilde(path, env)
    reason = e.strerror if isinstance(e, OSError) and e.strerror else e
    step = f"Look at {shown}: kb checks and rewrites it once it can read it"
    return Row("problem", "fabricd", shown, f"can't read the unit ({reason})", step=step)


def _left(env: Mapping[str, str], path: Path, current: str | None, config: Config | Exception) -> Row | None:
    """The row of a unit kb leaves as it is: masked, or not kb's, or one fabricd would stop at the start of."""
    shown = tilde(path, env)
    if _masked(path):
        return Row("ok", "fabricd", shown, _turned_off("masked"))
    if current is not None and not current.startswith(HEADER):
        step = f"Move {shown} away and run kb doctor --fix, for kb's own unit"
        return Row("ok", "fabricd", shown, "yours: kb leaves a unit it didn't write as it is", step=step)
    if (e := _settings_error(config)) is not None:
        step = f"Fix it in {tilde(config_file(env), env)}, then run kb doctor --fix"
        return Row("problem", "fabricd", shown, f"can't start: {e}", step=step)
    return None


def _settings_error(config: Config | Exception) -> Exception | None:
    """Why fabricd would stop at start, exiting 2, on this config; None where it reads."""
    if isinstance(config, Exception):
        return config
    try:
        fabricd_settings(config)
    except ValueError as e:
        return e
    return None


def _wanted(command: str, env: Mapping[str, str], path: Path, current: str | None, *, fix: bool) -> Row:
    """The unit while a project is allowed: written once fabricd can run, and checked. An installed unit is judged by
    the PATH it saved; kb's own PATH matters only to the unit it writes."""
    shown = tilde(path, env)
    if not installed_fabricd(env).is_file():
        detail = f"the unit would run {tilde(installed_fabricd(env), env)}, which isn't there"
        return Row("problem", "fabricd", shown, detail, step="Run scripts/install.sh, which installs that fabricd")
    if (stale := _stale(current, env)) is None:
        return _state(command, env, path, fix=fix, wanted=True)
    if missing := [tool for tool in TOOLS if not shutil.which(tool, path=env.get("PATH", ""))]:
        step = f"Install {missing[0]}, then run kb doctor --fix"
        if current is None:
            return Row(
                "missing", "fabricd", shown, f"not installed: it needs {' and '.join(missing)} on PATH", step=step
            )
        detail = f"{stale}, which kb rewrites once {' and '.join(missing)} is on PATH"
        return Row("outdated", "fabricd", shown, detail, step=step)
    if not fix:
        return Row("outdated" if current is not None else "missing", "fabricd", shown, stale)
    return _write(command, env, path, installed=current is not None)


def _stale(current: str | None, env: Mapping[str, str]) -> str | None:
    """Why the unit isn't kb's as it would write it now, or None. The PATH line is left out, as it depends on the
    shell kb runs in: the PATH the unit saved only has to still find the tools."""
    if current is None:
        return "fabricd doesn't run on its own"
    if SAVED_PATH.sub("", current) != SAVED_PATH.sub("", unit(env)):
        return "fabricd runs from an old unit"
    lost = [tool for tool in TOOLS if not shutil.which(tool, path=saved_path(current))]
    return f"its PATH no longer finds {' or '.join(lost)}, so no digest can be written" if lost else None


def _write(command: str, env: Mapping[str, str], path: Path, *, installed: bool) -> Row:
    """Through a temporary file and a rename, so systemd never reads half of it; then reloaded and (re)started, so
    the new unit is the one that runs, unless the user turned it off."""
    shown = tilde(path, env)
    off = _off(asked) if installed and (asked := _ask(command, env)) else None
    temporary = path.with_name(f".{NAME}.kb-new")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(unit(env), encoding="utf-8")
        temporary.replace(path)
    except OSError as e:
        with contextlib.suppress(OSError):  # best effort: in a read-only folder, unlink fails even on no file
            temporary.unlink(missing_ok=True)
        step = f"Make {tilde(path.parent, env)} writable, then run kb doctor --fix again"
        return Row("problem", "fabricd", shown, f"can't write it ({e.strerror or e})", step=step)
    if off:
        steps: list[tuple[str, ...]] = [("daemon-reload",)]
    elif installed:
        steps = [("daemon-reload",), ("enable", NAME), ("restart", NAME)]
    else:  # in one step, so that a failure leaves it neither enabled nor running
        steps = [("daemon-reload",), ("enable", "--now", NAME)]
    for args in steps:
        done = _systemctl(command, env, *args)
        if done is None or done.returncode:
            said = done.stderr.strip() if done else "no answer"
            step = RELOAD if off else _START
            if not installed:
                # A unit left behind would read as one the user turned off, and the next --fix would keep it off.
                path.unlink(missing_ok=True)
                _systemctl(command, env, "daemon-reload")
                said += "; removed the unit, so --fix tries again"
            return Row("problem", "fabricd", shown, f"systemctl {args[0]} failed: {said}", step=step)
    return Row("fixed", "fabricd", shown, f"rewrote the unit; {off}" if off else RUNNING)


_START = f"Run systemctl --user daemon-reload && systemctl --user enable --now {NAME}, and fix what it says"


def _ask(command: str, env: Mapping[str, str]) -> dict[str, str] | None:
    """The unit's state and its last result; None where systemd can't be asked, as from an agent's sandbox."""
    done = _systemctl(
        command,
        env,
        "show",
        "--property=UnitFileState,ActiveState,Result,ExecMainCode,ExecMainStatus,NeedDaemonReload",
        NAME,
    )
    if done is None or done.returncode:
        return None
    return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)


def _off(asked: dict[str, str]) -> str | None:
    state = asked.get("UnitFileState", "")
    return _turned_off(state) if state in OFF else None


def _turned_off(state: str) -> str:
    """How the user turned it off, which --fix keeps, and how to turn it back on. A masked unit of kb's has no file
    (systemctl can't mask a unit whose file is in the same folder), so --fix writes it again after the unmask."""
    if state == "masked":
        return f"turned off (masked); systemctl --user unmask {NAME} && kb doctor --fix turns it back on"
    return f"turned off ({state}); systemctl --user enable --now {NAME} turns it back on"


def _state(command: str, env: Mapping[str, str], path: Path, *, fix: bool, wanted: bool) -> Row:
    """Whether systemd has fabricd on and running, and with `fix` starts it if it should run. One that failed is left
    for the user to read why, since started again it would fail again, unless it stopped on a config that now reads.
    While no project is allowed, kb doesn't start it. A unit systemd hasn't reloaded since kb wrote it, as when the
    reload failed, is written and reloaded again."""
    shown = tilde(path, env)
    asked = _ask(command, env)
    if asked is None:
        return Row("ok", "fabricd", shown, "installed; can't ask systemd from here whether it runs")
    settled = _settled(asked, shown, wanted=wanted)
    # Only a unit kb would run or start: one turned off or failed keeps its row, and a restart wouldn't help it.
    if wanted and asked.get("NeedDaemonReload") == "yes" and (settled is None or settled.detail == RUNNING):
        return _reloaded(command, env, path, fix=fix)
    if settled is not None:
        return settled
    detail = (
        "stopped on a config it couldn't read, which reads now"
        if _config_failed(asked)
        else "installed but not running"
    )
    if not fix:
        return Row("missing", "fabricd", shown, detail)
    done = _systemctl(command, env, "enable", "--now", NAME)
    if done is None or done.returncode:
        said = done.stderr.strip() if done else "no answer"
        return Row("problem", "fabricd", shown, f"systemctl enable failed: {said}", step=_START)
    return Row("fixed", "fabricd", shown, RUNNING)


def _reloaded(command: str, env: Mapping[str, str], path: Path, *, fix: bool) -> Row:
    if not fix:
        return Row(
            "outdated", "fabricd", tilde(path, env), "systemd runs it from the unit as it was before kb rewrote it"
        )
    return _write(command, env, path, installed=True)


def _config_failed(asked: dict[str, str]) -> bool:
    """Whether it stopped on a config it couldn't read (exit 2), which systemd doesn't restart."""
    return asked.get("Result", "success") != "success" and _exited(asked, BAD_CONFIG)


def _exited(asked: dict[str, str], code: int) -> bool:
    """Whether fabricd exited with `code`: ExecMainCode 1 (CLD_EXITED), since after a signal ExecMainStatus is the
    signal's number."""
    return asked.get("ExecMainCode") == "1" and asked.get("ExecMainStatus") == str(code)


def _settled(asked: dict[str, str], shown: str, *, wanted: bool) -> Row | None:
    """The row of a unit --fix leaves as it is: turned off, starting, failed, running, or stopped while no project is
    allowed; None for one it starts."""
    active, result = asked.get("ActiveState", ""), asked.get("Result", "success")
    if off := _off(asked):
        return _kept_off(off, active, shown)
    if active == "activating":  # starting, or waiting out RestartSec after a failure
        return _activating(result, shown)
    if (asked.get("UnitFileState"), active) == ("enabled", "active"):
        return Row("ok", "fabricd", shown, RUNNING)
    if not wanted:  # stopped, failed or not, as kb would leave it
        return Row("ok", "fabricd", shown, "installed, not running: no project is allowed, so --fix leaves it off")
    if (active == "failed" or result != "success") and not _config_failed(asked):
        return _failed(asked, shown)
    return None


_JOURNAL = f"Read journalctl --user -u {shlex.quote(NAME)} and fix the cause"


def _kept_off(off: str, active: str, shown: str) -> Row:
    if active == "active":  # disabled without --now: it runs until stopped or a reboot
        step = f"Run systemctl --user stop {NAME}, or it runs until the next reboot"
        return Row("ok", "fabricd", shown, f"{off}, but it still runs", step=step)
    return Row("ok", "fabricd", shown, off)


def _failed(asked: dict[str, str], shown: str) -> Row:
    restart = f"systemctl --user restart {NAME}"
    if _exited(asked, LOCKED):
        detail = "stopped: another fabricd runs on its data folder"
        return Row("problem", "fabricd", shown, detail, step=f"Stop the other fabricd, then {restart}")
    result = asked.get("Result", "success")
    detail = f"stopped: it failed ({result if result != 'success' else asked.get('ActiveState', '')})"
    return Row("problem", "fabricd", shown, detail, step=f"{_JOURNAL}; {restart}")


def _activating(result: str, shown: str) -> Row:
    if result == "success":
        return Row("ok", "fabricd", shown, "starting")
    detail = f"restarting: it failed ({result}), and systemd starts it again shortly"
    return Row("problem", "fabricd", shown, detail, step=f"{_JOURNAL}, if it keeps failing")
