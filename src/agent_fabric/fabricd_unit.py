"""fabricd.service: fabricd as a systemd user unit, which `kb doctor --fix` writes and starts once some workspace
allows a project, as it does ingest's timers (docs/design.md §5). The allow list is the user's say-so to publish,
and a machine that only takes notes gets no daemon. A unit the user turned off stays off, and a unit kb installed is
still checked once no project is allowed, but only written while one is."""

import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path

from agent_fabric.paths import tilde, units_dir, uv_tool_dir
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
)

NAME = "fabricd.service"
TOOLS = ("gitleaks", "git")  # what a digest's write runs (§4.5), in the order PATH searches them
HOMES = ("COPILOT_HOME",)  # where Copilot CLI's sessions are, when moved
RUNNING = "running: it follows the agents' sessions and writes their digests"
CONFIG = 2  # fabricd's exit for a config it can't read, which restarting won't fix


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
    until the user fixes it and `kb doctor --fix` starts it again; any other failure restarts it."""
    carried = [(name, env[name]) for name in (*CARRIED, *HOMES) if Path(env.get(name) or "").is_absolute()]
    environment = "".join(
        f"Environment={_quoted(f'{name}={value}')}\n" for name, value in [("PATH", _path(env)), *carried]
    )
    return (
        f"{HEADER}[Unit]\nDescription=fabricd: digests of the agents' sessions\n\n"
        f"[Service]\nType=exec\n{environment}"
        f"ExecStart={_quoted(str(installed_fabricd(env)), command=True)}\n"
        f"Restart=on-failure\nRestartSec=30\nRestartPreventExitStatus={CONFIG}\nKillMode=mixed\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def check(env: Mapping[str, str], *, wanted: bool, fix: bool) -> list[Row]:
    """fabricd's row: none where no project is allowed and kb never installed it. `wanted`: some workspace allows a
    project."""
    folder = units_dir(env)
    path = folder / NAME
    shown = tilde(path, env)
    try:
        current = _read(path)
    except (OSError, UnicodeDecodeError) as e:
        reason = e.strerror if isinstance(e, OSError) and e.strerror else e
        step = f"Look at {shown}: kb checks and rewrites it once it can read it"
        return [Row("problem", "fabricd", shown, f"can't read the unit ({reason})", step=step)]
    if not wanted and current is None and not path.is_symlink():
        return []
    command = shutil.which("systemctl", path=env.get("PATH", ""))
    if command is None:
        detail = "no systemd here, so fabricd runs only when you start it"
        return [Row("ok", "fabricd", shown, detail, step="Run fabricd in a terminal of its own")]
    if _masked(path):
        return [Row("ok", "fabricd", shown, _turned_off("masked"))]
    if not wanted:  # checked, never written
        return [_state(command, env, shown, fix=False)]
    return [_wanted(command, env, path, current, fix=fix)]


def _wanted(command: str, env: Mapping[str, str], path: Path, current: str | None, *, fix: bool) -> Row:
    """The unit while a project is allowed: written once fabricd can run, and checked."""
    shown = tilde(path, env)
    if missing := [tool for tool in TOOLS if not shutil.which(tool, path=env.get("PATH", ""))]:
        detail = f"not installed: it needs {' and '.join(missing)} on PATH"
        return Row("missing", "fabricd", shown, detail, step=f"Install {missing[0]}, then run kb doctor --fix")
    if not installed_fabricd(env).is_file():
        detail = f"the unit would run {tilde(installed_fabricd(env), env)}, which isn't there"
        return Row("problem", "fabricd", shown, detail, step="Run scripts/install.sh, which installs that fabricd")
    if (stale := _stale(current, env)) is None:
        return _state(command, env, shown, fix=fix)
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
    saved = SAVED_PATH.search(current)
    path = saved[1].replace("%%", "%").replace('\\"', '"').replace("\\\\", "\\") if saved else ""
    lost = [tool for tool in TOOLS if not shutil.which(tool, path=path)]
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
        temporary.unlink(missing_ok=True)
        step = f"Make {tilde(path.parent, env)} writable, then run kb doctor --fix again"
        return Row("problem", "fabricd", shown, f"can't write it ({e.strerror or e})", step=step)
    steps = [("daemon-reload",)] if off else [("daemon-reload",), ("enable", NAME), ("restart", NAME)]
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
    done = _systemctl(command, env, "show", "--property=UnitFileState,ActiveState,Result", NAME)
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


def _off_or_failed(asked: dict[str, str], shown: str) -> Row | None:
    """The row of a unit the user turned off, or one that failed; None for any other."""
    if off := _off(asked):
        if asked.get("ActiveState") == "active":  # disabled without --now: it runs until stopped or a reboot
            step = f"Run systemctl --user stop {NAME}, or it runs until the next reboot"
            return Row("ok", "fabricd", shown, f"{off}, but it still runs", step=step)
        return Row("ok", "fabricd", shown, off)
    active = asked.get("ActiveState", "")
    if active == "failed" or asked.get("Result", "success") != "success":
        detail = f"stopped: it failed ({asked.get('Result') or active})"
        step = f"Read journalctl --user -u {shlex.quote(NAME)} and fix the cause; systemctl --user restart {NAME}"
        return Row("problem", "fabricd", shown, detail, step=step)
    return None


def _state(command: str, env: Mapping[str, str], shown: str, *, fix: bool) -> Row:
    """Whether systemd has fabricd on and running. One that failed is left for the user to read why: started again,
    it would fail again, and the row would say it was fixed."""
    asked = _ask(command, env)
    if asked is None:
        return Row("ok", "fabricd", shown, "installed; can't ask systemd from here whether it runs")
    if (settled := _off_or_failed(asked, shown)) is not None:
        return settled
    if (asked.get("UnitFileState"), asked.get("ActiveState")) == ("enabled", "active"):
        return Row("ok", "fabricd", shown, RUNNING)
    if not fix:
        return Row("missing", "fabricd", shown, "installed but not running")
    done = _systemctl(command, env, "enable", "--now", NAME)
    if done is None or done.returncode:
        said = done.stderr.strip() if done else "no answer"
        return Row("problem", "fabricd", shown, f"systemctl enable failed: {said}", step=_START)
    return Row("fixed", "fabricd", shown, RUNNING)
