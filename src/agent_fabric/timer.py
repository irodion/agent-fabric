"""fabric-ingest.timer: `kb ingest` daily at 03:00, as systemd user units that `kb doctor --fix` writes
(docs/design.md §5). Each run's gate decides whether there's work, so a daily run costs no more than a weekly one."""

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from agent_fabric import config
from agent_fabric.ingest import BUSY  # another run held the lock: not a failure for the timer
from agent_fabric.paths import tilde, units_dir, uv_tool_dir
from agent_fabric.report import Row

SERVICE = "fabric-ingest.service"
TIMER = "fabric-ingest.timer"
NEEDED = ("gitleaks", "codex")  # what `kb ingest` runs; doctor's ingest row reports one that's missing
ALSO_ON_PATH = ("git", "node")  # git for the projects, node for a codex installed as a script outside node's folder
BASE_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
CARRIED = ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "CODEX_HOME")  # moved folders, which a unit doesn't get from a shell
DAILY = "runs kb ingest daily at 03:00"
HEADER = (
    "# Written by kb doctor --fix (agent-fabric, docs/design.md §5), which rewrites it: change kb, not this file.\n"
)
STATE_TIMEOUT = 10  # seconds for the systemctl call
SAVED_PATH = re.compile(r'^Environment="PATH=(.*)"$', re.MULTILINE)
OFF = ("disabled", "masked")  # states only the user sets, which kb keeps
RELOAD = "Run systemctl --user daemon-reload, and fix what it says"
RERUN = f"Read journalctl --user -u {SERVICE} and fix the cause; systemctl --user start {SERVICE} runs it again"


def _quoted(text: str, *, command: bool = False) -> str:
    """One word of a unit file, which expands % everywhere, and $ in a command line."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped.replace("$", "$$") if command else escaped}"'


def _path(env: Mapping[str, str]) -> str:
    """The folders of the tools ingest runs, from the PATH kb runs with: a unit gets none of a shell's PATH, and codex
    lives where its node does, such as under nvm."""
    found = (shutil.which(tool, path=env.get("PATH", "")) for tool in (*NEEDED, *ALSO_ON_PATH))
    # Absolute, as a unit has no working folder to resolve against; not resolved, as codex's link would lead into
    # node's package folder, which holds no node.
    folders = [os.path.dirname(os.path.abspath(tool)) for tool in found if tool]
    return ":".join(dict.fromkeys([*folders, *BASE_PATH]))


def installed_kb(env: Mapping[str, str]) -> Path:
    """The kb scripts/install.sh installs, which the service runs: not a virtualenv's."""
    return uv_tool_dir(env) / "agent-fabric" / "bin" / "kb"


def units(env: Mapping[str, str]) -> dict[str, str]:
    """The two files, as kb would write them now."""
    kb = installed_kb(env)
    carried = [(name, env[name]) for name in CARRIED if Path(env.get(name) or "").is_absolute()]
    environment = "".join(
        f"Environment={_quoted(f'{name}={value}')}\n" for name, value in [("PATH", _path(env)), *carried]
    )
    service = (
        f"{HEADER}[Unit]\nDescription=kb ingest: file agent notes in the wiki\n\n"
        f"[Service]\nType=oneshot\n{environment}ExecStart={_quoted(str(kb), command=True)} ingest\n"
        f"SuccessExitStatus={BUSY}\n"
    )
    timer = (
        f"{HEADER}[Unit]\nDescription=Run kb ingest daily\n\n"
        "[Timer]\nOnCalendar=*-*-* 03:00:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
    )
    return {SERVICE: service, TIMER: timer}


def _systemctl(command: str, env: Mapping[str, str], *args: str) -> subprocess.CompletedProcess[str] | None:
    """None where systemd can't be asked, as from an agent's sandbox."""
    try:
        return subprocess.run(
            [command, "--user", *args],
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=STATE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _turn_on(command: str, env: Mapping[str, str], shown: str) -> Row:
    for args in (("daemon-reload",), ("enable", "--now", TIMER)):
        done = _systemctl(command, env, *args)
        if done is None or done.returncode:
            said = done.stderr.strip() if done else "no answer"
            step = (
                f"Run systemctl --user daemon-reload && systemctl --user enable --now {TIMER}, and fix what it says"
            )
            return Row("problem", "timer", shown, f"systemctl {args[0]} failed: {said}", step=step)
    return Row("fixed", "timer", shown, DAILY)


def _wanted(env: Mapping[str, str]) -> bool:
    """Once there's a project for ingest to publish and the tools it runs are on PATH: a unit written without one
    would lack it, and the ingest row says what to install."""
    try:
        allowed = config.allowed_projects(env)
    except ValueError:  # the ingest row reports it
        return False
    return bool(allowed) and all(shutil.which(tool, path=env.get("PATH", "")) for tool in NEEDED)


def _read(path: Path) -> str | None:
    """None where there's no such file; OSError or UnicodeDecodeError where there's one kb can't read."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _write(wanted: dict[str, str], command: str, env: Mapping[str, str], shown: str, *, installed: bool) -> Row:
    """Each file through a temporary one and a rename, so systemd never reads half of one. An installed timer the
    user turned off stays off: only systemd's copy is reloaded."""
    folder = units_dir(env)
    off = _off(asked) if installed and (asked := _ask(command, env)) else None
    temporary = {name: folder / f".{name}.kb-new" for name in wanted}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in wanted.items():
            temporary[name].write_text(text, encoding="utf-8")
        for name in wanted:
            temporary[name].replace(folder / name)
    except OSError as e:
        for path in temporary.values():
            path.unlink(missing_ok=True)
        step = f"Make {tilde(folder, env)} writable, then run kb doctor --fix again"
        return Row("problem", "timer", shown, f"can't write it ({e.strerror or e})", step=step)
    if off:
        done = _systemctl(command, env, "daemon-reload")
        if done is None or done.returncode:
            return Row("problem", "timer", shown, "systemctl daemon-reload failed", step=RELOAD)
        return Row("fixed", "timer", shown, f"rewrote the units; {off}")
    row = _turn_on(command, env, shown)
    if row.status == "problem" and not installed:
        # Units left behind would read as a timer the user turned off, and the next --fix would keep it off.
        for name in wanted:
            (folder / name).unlink(missing_ok=True)
        _systemctl(command, env, "daemon-reload")
        return Row(
            row.status, row.check, row.path, f"{row.detail}; removed the units, so --fix tries again", row.step
        )
    return row


def _properties(text: str) -> list[dict[str, str]]:
    """`systemctl show`'s answer for several units: one block of NAME=value lines each, in the order asked."""
    return [
        dict(line.split("=", 1) for line in block.splitlines() if "=" in line) for block in text.strip().split("\n\n")
    ]


def _ask(command: str, env: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]] | None:
    """The timer's state and the service's last result, in one call, so a bus that hangs costs one timeout; None
    where systemd can't be asked."""
    done = _systemctl(command, env, "show", "--property=UnitFileState,ActiveState,Result", TIMER, SERVICE)
    blocks = _properties(done.stdout) if done and not done.returncode else []
    return (blocks[0], blocks[1]) if len(blocks) == 2 else None


def _off(asked: tuple[dict[str, str], dict[str, str]]) -> str | None:
    """How the user turned the timer off, which --fix keeps: disabled or masked."""
    state = asked[0].get("UnitFileState", "")
    return _turned_off(state) if state in OFF else None


def _turned_off(state: str) -> str:
    back = f"systemctl --user {'unmask ' + TIMER + ' && systemctl --user ' if state == 'masked' else ''}enable --now"
    return f"turned off ({state}); {back} {TIMER} turns it back on"


def _state(command: str, env: Mapping[str, str], shown: str, *, fix: bool) -> Row:
    """Whether systemd has the timer on and waiting, and how the service's last run went."""
    asked = _ask(command, env)
    if asked is None:
        return Row("ok", "timer", shown, "installed; can't ask systemd from here whether it's on or how it last ran")
    timer, service = asked
    if off := _off(asked):
        if timer.get("ActiveState") == "active":  # disabled without --now: it fires until stopped or a reboot
            step = f"Run systemctl --user stop {TIMER}, or tonight's run still happens"
            return Row("ok", "timer", shown, f"{off}, but it still waits for its next run", step=step)
        return Row("ok", "timer", shown, off)
    if (timer.get("UnitFileState"), timer.get("ActiveState")) != ("enabled", "active"):
        return _turn_on(command, env, shown) if fix else Row("missing", "timer", shown, "installed but not running")
    if (last := service.get("Result", "")) not in ("success", ""):
        return Row("problem", "timer", shown, f"the last run failed ({last})", step=RERUN)
    return Row("ok", "timer", shown, DAILY)


def check(env: Mapping[str, str], *, fix: bool) -> list[Row]:
    """The timer's row. Units already installed are still checked once ingest isn't wanted, so a timer whose runs
    fail doesn't go unseen, but only written while it is."""
    folder = units_dir(env)
    shown = tilde(folder / TIMER, env)
    try:
        current = {name: _read(folder / name) for name in (SERVICE, TIMER)}
    except (OSError, UnicodeDecodeError) as e:
        reason = e.strerror if isinstance(e, OSError) and e.strerror else e
        step = f"Look at {tilde(folder, env)}/fabric-ingest.*: kb checks and rewrites them once it can read them"
        return [Row("problem", "timer", shown, f"can't read the units ({reason})", step=step)]
    # A mask is a link to /dev/null, which kb never rewrites. A masked timer doesn't fire.
    masked = {name for name in (SERVICE, TIMER) if os.path.realpath(folder / name) == os.devnull}
    if TIMER in masked:
        return [Row("ok", "timer", shown, _turned_off("masked"))]
    installed = any(text is not None for text in current.values())
    wanted = _wanted(env)
    if not wanted and not installed:
        return []
    command = shutil.which("systemctl", path=env.get("PATH", ""))
    if command is None:
        detail = "no systemd here, so kb ingest runs only when you start it"
        return [Row("ok", "timer", shown, detail, step="Run kb ingest yourself every few days")]
    if masked or not wanted:  # checked, never written
        return [_service_masked(command, env, shown) if masked else _state(command, env, shown, fix=False)]
    return [_installed(command, env, current, shown, fix=fix)]


def _service_masked(command: str, env: Mapping[str, str], shown: str) -> Row:
    """Only the service masked: off, unless the timer still fires it, and then each run fails."""
    asked = _ask(command, env)
    if asked is None:
        return Row("ok", "timer", shown, "the service is masked; can't ask systemd from here whether the timer runs")
    if asked[0].get("ActiveState") == "active":
        step = f"Run systemctl --user disable --now {TIMER} to keep it off, or systemctl --user unmask {SERVICE}"
        return Row("problem", "timer", shown, "the service is masked, but the timer still fires it", step=step)
    return Row("ok", "timer", shown, _turned_off("masked"))


def _without_path(text: str | None) -> str | None:
    return SAVED_PATH.sub("", text) if text is not None else None


def _stale(current: dict[str, str | None], expected: dict[str, str], env: Mapping[str, str]) -> str | None:
    """Why the units aren't kb's as it would write them now, or None. The PATH line is left out of the comparison, as
    it depends on the shell kb runs in (an `nvm use`, an agent's session): the PATH a unit saved only has to still
    find the tools, and node where codex needs it."""
    if {name: _without_path(text) for name, text in current.items()} != {
        name: _without_path(text) for name, text in expected.items()
    }:
        return "kb ingest runs from an old unit"
    saved = SAVED_PATH.search(current[SERVICE] or "")
    path = saved[1].replace("%%", "%").replace('\\"', '"').replace("\\\\", "\\") if saved else ""
    node = ("node",) if shutil.which("node", path=env.get("PATH", "")) else ()
    lost = [tool for tool in (*NEEDED, *node) if not shutil.which(tool, path=path)]
    return f"its PATH no longer finds {' or '.join(lost)}, so each run fails" if lost else None


def _installed(command: str, env: Mapping[str, str], current: dict[str, str | None], shown: str, *, fix: bool) -> Row:
    if not installed_kb(env).is_file():
        detail = f"the service would run {tilde(installed_kb(env), env)}, which isn't there"
        return Row("problem", "timer", shown, detail, step="Run scripts/install.sh, which installs that kb")
    expected = units(env)
    if (stale := _stale(current, expected, env)) is None:
        return _state(command, env, shown, fix=fix)
    installed = any(text is not None for text in current.values())
    if fix:
        return _write(expected, command, env, shown, installed=installed)
    if installed:
        return Row("outdated", "timer", shown, stale)
    return Row("missing", "timer", shown, "kb ingest doesn't run on its own")
