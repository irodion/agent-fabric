"""fabric-ingest@<workspace>.timer: `kb ingest --workspace <workspace>` daily at 03:00, as systemd user units that
`kb doctor --fix` writes, a pair per workspace with a project allowed (docs/design.md §5, §4.11). Each run's gate
decides whether there's work, so a daily run costs no more than a weekly one. Each pair is a file of its own, not an
instance of a template: a template's instance that the user disabled leaves no trace, and kb keeps a timer the user
turned off."""

import contextlib
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from agent_fabric.backends import not_ours
from agent_fabric.config import Config
from agent_fabric.ingest import BUSY  # another run held the lock: not a failure for the timer
from agent_fabric.paths import tilde, units_dir, uv_tool_dir
from agent_fabric.report import Row
from agent_fabric.workspaces import Workspace

OLD = ("fabric-ingest.service", "fabric-ingest.timer")  # v0.6's, before workspaces
KBS = re.compile(r"fabric-ingest@(.+)\.(?:service|timer)")  # a unit kb writes, by its whole name
ALSO_ON_PATH = ("git", "node")  # git for the projects, node for a codex installed as a script outside node's folder
BASE_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
# Moved folders, which a unit doesn't get from a shell: kb's, and its workspace's backend's home.
CARRIED = ("XDG_DATA_HOME", "XDG_CONFIG_HOME")
HOMES = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR"}
DAILY = "runs kb ingest daily at 03:00"
HEADER = (
    "# Written by kb doctor --fix (agent-fabric, docs/design.md §5), which rewrites it: change kb, not this file.\n"
)
STATE_TIMEOUT = 10  # seconds for the systemctl call
SAVED_PATH = re.compile(r'^Environment="PATH=(.*)"$', re.MULTILINE)
OFF = ("disabled", "masked")  # states only the user sets, which kb keeps
RELOAD = "Run systemctl --user daemon-reload, and fix what it says"


def _escaped(text: str) -> str:
    """`systemd-escape`: a unit name's instance part, from any text."""
    out = ""
    for i, char in enumerate(text):
        if char == "/":
            out += "-"
        elif (char.isascii() and char.isalnum()) or char in ":_" or (char == "." and i > 0):
            out += char
        else:
            out += "".join(f"\\x{byte:02x}" for byte in char.encode("utf-8"))
    return out


def _names(instance: str) -> tuple[str, str]:
    """An instance's service and timer."""
    return f"fabric-ingest@{instance}.service", f"fabric-ingest@{instance}.timer"


@dataclass(frozen=True)
class Unit:
    """One workspace's pair of units."""

    workspace: Workspace
    allowed: bool  # whether the workspace allows a project: one whose list kb can't read allows none
    tools: tuple[str, ...] | None  # what its ingest runs; None where its backend can't run, so no run could
    moved_off: str | None = None  # how v0.6's timer was turned off, which the default workspace's takes over

    @property
    def instance(self) -> str:
        return _instance(self.workspace)

    @property
    def timer(self) -> str:
        return _names(self.instance)[1]

    @property
    def service(self) -> str:
        return _names(self.instance)[0]

    @property
    def argument(self) -> str:
        """What the service passes to `--workspace`."""
        return "default" if self.workspace.root is None else str(self.workspace.root)

    @property
    def timer_sh(self) -> str:
        """The timer's name as a shell word: an escaped root holds backslashes."""
        return shlex.quote(self.timer)

    @property
    def service_sh(self) -> str:
        return shlex.quote(self.service)

    def rerun(self) -> str:
        return (
            f"Read journalctl --user -u {self.service_sh} and fix the cause; systemctl --user start {self.service_sh}"
            " runs it again"
        )


def _quoted(text: str, *, command: bool = False) -> str:
    """One word of a unit file, which expands % everywhere, and $ in a command line."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped.replace("$", "$$") if command else escaped}"'


def _instance(workspace: Workspace) -> str:
    return "default" if workspace.root is None else _escaped(str(workspace.root))


def unit_of(workspace: Workspace, config: Config | None) -> Unit:
    """The workspace's units, with what the config says of its ingest: from the one read doctor makes, or none where
    it couldn't read the file. Doctor's ingest and workspace rows say what's wrong with a setting kb can't use."""
    if config is None:
        return Unit(workspace, allowed=False, tools=None)
    try:
        allowed = bool(config.allowed(workspace.root))
    except ValueError:
        allowed = False
    try:
        backend: str | None = config.backend(workspace.root)
    except ValueError:
        backend = None
    tools = ("gitleaks", backend) if backend is not None and not not_ours(backend) else None
    return Unit(workspace, allowed=allowed, tools=tools)


def _path(env: Mapping[str, str], unit: Unit) -> str:
    """The folders of the tools ingest runs, from the PATH kb runs with: a unit gets none of a shell's PATH, and codex
    lives where its node does, such as under nvm."""
    found = (shutil.which(tool, path=env.get("PATH", "")) for tool in (*(unit.tools or ()), *ALSO_ON_PATH))
    # Absolute, as a unit has no working folder to resolve against; not resolved, as codex's link would lead into
    # node's package folder, which holds no node.
    folders = [os.path.dirname(os.path.abspath(tool)) for tool in found if tool]
    return ":".join(dict.fromkeys([*folders, *BASE_PATH]))


def installed_kb(env: Mapping[str, str]) -> Path:
    """The kb scripts/install.sh installs, which the service runs: not a virtualenv's."""
    return uv_tool_dir(env) / "agent-fabric" / "bin" / "kb"


def units(env: Mapping[str, str], unit: Unit) -> dict[str, str]:
    """The workspace's two files, as kb would write them now."""
    kb = installed_kb(env)
    homes = (HOMES[unit.tools[1]],) if unit.tools else ()
    carried = [(name, env[name]) for name in (*CARRIED, *homes) if Path(env.get(name) or "").is_absolute()]
    environment = "".join(
        f"Environment={_quoted(f'{name}={value}')}\n" for name, value in [("PATH", _path(env, unit)), *carried]
    )
    run = f"{_quoted(str(kb), command=True)} ingest --workspace {_quoted(unit.argument, command=True)}"
    service = (
        # %I: systemd's own unescaping of the name, where a path would have its % expanded
        f"{HEADER}[Unit]\nDescription=kb ingest: file agent notes in the wiki of %I\n\n"
        f"[Service]\nType=oneshot\n{environment}"
        f"ExecStart={run}\n"  # named, not the folder it starts in
        f"SuccessExitStatus={BUSY}\n"
    )
    timer = (
        f"{HEADER}[Unit]\nDescription=Run kb ingest daily for %I\n\n"
        "[Timer]\nOnCalendar=*-*-* 03:00:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
    )
    return {unit.service: service, unit.timer: timer}


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


def _turn_on(command: str, env: Mapping[str, str], shown: str, unit: Unit) -> Row:
    for args in (("daemon-reload",), ("enable", "--now", unit.timer)):
        done = _systemctl(command, env, *args)
        if done is None or done.returncode:
            said = done.stderr.strip() if done else "no answer"
            step = (
                f"Run systemctl --user daemon-reload && systemctl --user enable --now {unit.timer_sh}, and fix what"
                " it says"
            )
            return Row("problem", "timer", shown, f"systemctl {args[0]} failed: {said}", step=step)
    return Row("fixed", "timer", shown, DAILY)


def _wanted(env: Mapping[str, str], unit: Unit) -> bool:
    """Once there's a project for its ingest to publish and the tools it runs are on PATH: a unit written without one
    would lack it, and the ingest row says what to install."""
    tools = unit.tools
    return unit.allowed and tools is not None and all(shutil.which(tool, path=env.get("PATH", "")) for tool in tools)


def _read(path: Path) -> str | None:
    """None where there's no such file; OSError or UnicodeDecodeError where there's one kb can't read."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _write(command: str, env: Mapping[str, str], unit: Unit, *, installed: bool) -> Row:
    """Each file through a temporary one and a rename, so systemd never reads half of one. An installed timer the
    user turned off stays off, and so does one moved from v0.6's that was: only systemd's copy is reloaded."""
    folder = units_dir(env)
    shown = tilde(folder / unit.timer, env)
    wanted = units(env, unit)
    off = unit.moved_off or (_off(asked, unit) if installed and (asked := _ask(command, env, unit)) else None)
    temporary = {name: folder / f".{name}.kb-new" for name in wanted}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in wanted.items():
            temporary[name].write_text(text, encoding="utf-8")
        for name in wanted:
            temporary[name].replace(folder / name)
    except OSError as e:
        for path in temporary.values():
            with contextlib.suppress(OSError):  # best effort: in a read-only folder, unlink fails even on no file
                path.unlink(missing_ok=True)
        step = f"Make {tilde(folder, env)} writable, then run kb doctor --fix again"
        return Row("problem", "timer", shown, f"can't write it ({e.strerror or e})", step=step)
    if off:
        done = _systemctl(command, env, "daemon-reload")
        if done is None or done.returncode:
            return Row("problem", "timer", shown, "systemctl daemon-reload failed", step=RELOAD)
        return Row("fixed", "timer", shown, f"rewrote the units; {off}")
    row = _turn_on(command, env, shown, unit)
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


def _ask(command: str, env: Mapping[str, str], unit: Unit | None) -> tuple[dict[str, str], dict[str, str]] | None:
    """The timer's state and the service's last result, in one call, so a bus that hangs costs one timeout; None
    where systemd can't be asked. No unit: v0.6's."""
    timer, service = (unit.timer, unit.service) if unit else (OLD[1], OLD[0])
    done = _systemctl(command, env, "show", "--property=UnitFileState,ActiveState,Result", timer, service)
    blocks = _properties(done.stdout) if done and not done.returncode else []
    return (blocks[0], blocks[1]) if len(blocks) == 2 else None


def _off(asked: tuple[dict[str, str], dict[str, str]], unit: Unit) -> str | None:
    """How the user turned the timer off, which --fix keeps: disabled or masked."""
    state = asked[0].get("UnitFileState", "")
    return _turned_off(state, unit) if state in OFF else None


def _turned_off(state: str, unit: Unit) -> str:
    """How it was turned off, and how to turn it back on. A masked unit of kb's has no file (systemctl can't mask a
    unit whose file is in the same folder), so kb doctor --fix writes the pair again after the unmask."""
    if state == "masked":
        return f"turned off (masked); systemctl --user unmask {unit.timer_sh} && kb doctor --fix turns it back on"
    return f"turned off ({state}); systemctl --user enable --now {unit.timer_sh} turns it back on"


def _on(asked: tuple[dict[str, str], dict[str, str]]) -> bool:
    """Whether systemd has the timer on and waiting."""
    return (asked[0].get("UnitFileState"), asked[0].get("ActiveState")) == ("enabled", "active")


def _state(
    command: str,
    env: Mapping[str, str],
    unit: Unit,
    asked: tuple[dict[str, str], dict[str, str]] | None,
    *,
    fix: bool,
) -> Row:
    """Whether systemd has the timer on and waiting, and how the service's last run went, from what it said."""
    shown = tilde(units_dir(env) / unit.timer, env)
    if asked is None:
        return Row("ok", "timer", shown, "installed; can't ask systemd from here whether it's on or how it last ran")
    timer, service = asked
    if off := _off(asked, unit):
        if timer.get("ActiveState") == "active":  # disabled without --now: it fires until stopped or a reboot
            step = f"Run systemctl --user stop {unit.timer_sh}, or tonight's run still happens"
            return Row("ok", "timer", shown, f"{off}, but it still waits for its next run", step=step)
        return Row("ok", "timer", shown, off)
    if not _on(asked):
        return (
            _turn_on(command, env, shown, unit)
            if fix
            else Row("missing", "timer", shown, "installed but not running")
        )
    if (last := service.get("Result", "")) not in ("success", ""):
        return Row("problem", "timer", shown, f"the last run failed ({last})", step=unit.rerun())
    return Row("ok", "timer", shown, DAILY)


def _masked(path: Path) -> bool:
    """A mask is a link to /dev/null, which kb never rewrites. A masked timer doesn't fire."""
    return os.path.realpath(path) == os.devnull


def _instances(folder: Path) -> set[str]:
    """The instances whose units kb wrote are in the folder: not a backup file, nor a template of the user's."""
    try:
        names = [path.name for path in folder.iterdir()] if folder.is_dir() else []
    except OSError:  # each unit's own row says what it can't read
        return set()
    return {found[1] for name in names if (found := KBS.fullmatch(name))}


def check(
    env: Mapping[str, str], registered: list[Workspace], present: list[Unit], *, fix: bool, known: bool
) -> tuple[list[Row], set[Workspace]]:
    """The timers' rows: one per workspace that's there and has or wants a timer, v0.6's timer moved to the default
    workspace's, and the units of a workspace that's no longer registered removed. A root that isn't there keeps its
    units, and so does every root while the config can't be read (`known` false). With them, the workspaces whose
    timer systemd runs on a PATH that finds the tools their ingest needs, which the shell's PATH may not (§5)."""
    folder = units_dir(env)
    command = shutil.which("systemctl", path=env.get("PATH", ""))
    # lexists: a link too, even a broken one, and False where the folder can't be read, which Path.exists raises on
    # before Python 3.14. Where systemd is, each workspace with a project allowed then says kb can't read its units.
    old = [name for name in OLD if os.path.lexists(folder / name)]
    gone = _instances(folder) - {_instance(workspace) for workspace in registered} if known else set()
    if command is None:
        if old or any(_wanted(env, unit) for unit in present) or _instances(folder):
            detail = "no systemd here, so kb ingest runs only when you start it"
            by_hand = Row("ok", "timer", tilde(folder, env), detail, step="Run kb ingest yourself every few days")
            return [by_hand], set()
        return [], set()
    rows = []
    running: set[Workspace] = set()
    moving, off = False, None  # whether v0.6's timer moves to the default workspace's, and how it was turned off
    default = next((unit for unit in present if unit.workspace.root is None), unit_of(Workspace(None), None))
    if old:
        row, moving, off = _old(command, env, default, fix=fix)
        rows += [row] if row else []
    for each in present:
        unit = replace(each, moved_off=off) if each.workspace.root is None else each
        one, runs = _one(command, env, unit, fix=fix)
        rows += one
        running |= {unit.workspace} if runs else set()
        if unit.workspace.root is None and moving:
            rows.append(_moved(command, env, unit, one))
    rows += [_remove(command, env, instance, fix=fix) for instance in sorted(gone)]
    return rows, running


def _old(command: str, env: Mapping[str, str], default: Unit, *, fix: bool) -> tuple[Row | None, bool, str | None]:
    """v0.6's fabric-ingest.timer, which the default workspace's replaces (docs/design.md §5): a row where it stays,
    whether it moves, and how the user had turned it off, which goes with it. While systemd can't say, it stays."""
    folder = units_dir(env)
    shown = tilde(folder / OLD[1], env)
    if not fix:
        return (
            Row("outdated", "timer", shown, f"v0.6's timer; kb doctor --fix moves it to {default.timer}"),
            False,
            None,
        )
    if not os.access(folder, os.W_OK | os.X_OK):
        return _cant_change(env, shown), False, None
    if _masked(folder / OLD[1]):
        state: str | None = "masked"
    else:
        asked = _ask(command, env, None)
        if asked is None:
            detail = "can't ask systemd from here whether it's on, so it isn't moved yet"
            return (
                Row("ok", "timer", shown, detail, step="Run kb doctor --fix where systemctl --user works"),
                False,
                None,
            )
        state = asked[0].get("UnitFileState")
    # A mask moves with whichever unit has it: a masked service stops each run its timer starts.
    masks = [new for old, new in zip(OLD, (default.service, default.timer), strict=True) if _masked(folder / old)]
    masks += [default.timer] if state == "masked" and default.timer not in masks else []
    try:
        for name in masks:
            (folder / name).unlink(missing_ok=True)
            (folder / name).symlink_to(os.devnull)  # as systemctl mask does
        if masks == [default.service] and state in OFF:
            # The timer's file, never enabled, keeps its "turned off" past an unmask of the service.
            (folder / default.timer).write_text(units(env, default)[default.timer], encoding="utf-8")
    except OSError:
        return _cant_change(env, shown), False, None
    return None, True, _turned_off(state, default) if state in OFF else None


def _moved(command: str, env: Mapping[str, str], unit: Unit, one: list[Row]) -> Row:
    """v0.6's timer retired once the default workspace's is in place, and kept, still running, until then."""
    if one and all(row.status in ("ok", "fixed") for row in one):
        return _retire(command, env, OLD, f"moved to {unit.timer}")
    shown = tilde(units_dir(env) / OLD[1], env)
    return Row("outdated", "timer", shown, f"v0.6's timer, kept until {unit.timer} is in place")


def _retire(command: str, env: Mapping[str, str], names: tuple[str, str], done: str) -> Row:
    """Disable a timer and remove its units, once systemd has let it go: a timer still loaded would keep firing."""
    folder = units_dir(env)
    service, timer = names
    shown = tilde(folder / timer, env)
    if not os.access(folder, os.W_OK | os.X_OK):
        return _cant_change(env, shown)
    stopped = _systemctl(command, env, "disable", "--now", timer)
    if stopped is None or stopped.returncode:
        said = stopped.stderr.strip() if stopped else "no answer"
        step = f"Run systemctl --user disable --now {shlex.quote(timer)}, then kb doctor --fix again"
        return Row("problem", "timer", shown, f"systemctl disable failed: {said}; its units are left", step=step)
    try:
        for name in (timer, service):
            (folder / name).unlink(missing_ok=True)
    except OSError:
        return _cant_change(env, shown)
    _systemctl(command, env, "daemon-reload")
    return Row("fixed", "timer", shown, done)


def _remove(command: str, env: Mapping[str, str], instance: str, *, fix: bool) -> Row:
    """The units of a workspace that's no longer registered."""
    names = _names(instance)
    if not fix:
        detail = "its workspace isn't registered any more; kb doctor --fix removes it"
        return Row("outdated", "timer", tilde(units_dir(env) / names[1], env), detail)
    return _retire(command, env, names, "removed: its workspace isn't registered any more")


def _cant_change(env: Mapping[str, str], shown: str) -> Row:
    folder = tilde(units_dir(env), env)
    step = f"Make {folder} writable, then run kb doctor --fix again"
    return Row("problem", "timer", shown, f"can't change {folder}, so it's left as it is", step=step)


def _one(command: str, env: Mapping[str, str], unit: Unit, *, fix: bool) -> tuple[list[Row], bool]:
    """One workspace's timer, and whether systemd runs it on a PATH that finds its tools where the shell's doesn't.
    Units already installed are still checked once ingest isn't wanted, so a timer whose runs fail doesn't go unseen,
    but only written while it is."""
    folder = units_dir(env)
    shown = tilde(folder / unit.timer, env)
    names = (unit.service, unit.timer)
    try:
        current = {name: _read(folder / name) for name in names}
    except (OSError, UnicodeDecodeError) as e:
        if not unit.allowed and unit.moved_off is None:  # as fabricd's: a notes-only machine has no units to read
            return [], False
        reason = e.strerror if isinstance(e, OSError) and e.strerror else e
        step = (
            f"Look at {tilde(folder, env)}/fabric-ingest@{unit.instance}.*: kb checks and rewrites them once it can"
        )
        return [Row("problem", "timer", shown, f"can't read the units ({reason})", step=step + " read them")], False
    masked = {name for name in names if _masked(folder / name)}
    if unit.timer in masked:
        return [Row("ok", "timer", shown, _turned_off("masked", unit))], False
    installed = any(text is not None for text in current.values())
    # v0.6's timer turned off moves here even before the workspace wants one, or its state would be lost.
    wanted = _wanted(env, unit) or unit.moved_off is not None
    if not wanted and not installed:
        return [], False
    if masked or not wanted:  # checked, never written
        row, runs = (_service_masked(command, env, unit), False) if masked else _checked(command, env, current, unit)
        return [row], runs
    return [_installed(command, env, current, unit, fix=fix)], False


def _checked(command: str, env: Mapping[str, str], current: dict[str, str | None], unit: Unit) -> tuple[Row, bool]:
    """Installed units kb won't write from this shell, and whether their runs find the tools. While systemd runs the
    timer, its runs use the unit's PATH, not the shell's, so that's where the tools are looked for: a shell without
    nvm, such as ssh's, doesn't make codex missing (§5)."""
    asked = _ask(command, env, unit)
    if asked is None or not _on(asked) or not unit.allowed or not unit.tools:
        return _state(command, env, unit, asked, fix=False), False
    shown = tilde(units_dir(env) / unit.timer, env)
    if not installed_kb(env).is_file():
        detail = f"the service would run {tilde(installed_kb(env), env)}, which isn't there"
        return Row("problem", "timer", shown, detail, step="Run scripts/install.sh, which installs that kb"), False
    if current[unit.service] is None:
        detail = f"{unit.service} isn't there, so the timer starts nothing"
        step = f"Run kb doctor --fix from a shell whose PATH finds {' and '.join(unit.tools)}"
        return Row("problem", "timer", shown, detail, step=step), False
    if lost := _lost(env, unit, saved_path(current[unit.service])):
        step = f"Run kb doctor --fix from a shell whose PATH finds {' and '.join(lost)}"
        return Row(
            "problem", "timer", shown, f"its PATH no longer finds {' or '.join(lost)}, so each run fails", step=step
        ), False
    return _state(command, env, unit, asked, fix=False), True


def _service_masked(command: str, env: Mapping[str, str], unit: Unit) -> Row:
    """Only the service masked: off, unless the timer still fires it, and then each run fails."""
    shown = tilde(units_dir(env) / unit.timer, env)
    asked = _ask(command, env, unit)
    if asked is None:
        return Row("ok", "timer", shown, "the service is masked; can't ask systemd from here whether the timer runs")
    if asked[0].get("ActiveState") == "active":
        step = (
            f"Run systemctl --user disable --now {unit.timer_sh} to keep it off, or systemctl --user unmask"
            f" {unit.service_sh}"
        )
        return Row("problem", "timer", shown, "the service is masked, but the timer still fires it", step=step)
    return Row("ok", "timer", shown, _turned_off("masked", unit))


def saved_path(text: str | None) -> str:
    """The PATH a unit file of kb's saved, as systemd reads it, or "" where it saved none."""
    saved = SAVED_PATH.search(text or "")
    return saved[1].replace("%%", "%").replace('\\"', '"').replace("\\\\", "\\") if saved else ""


def _runs_on_node(script: str | None) -> bool:
    """Whether a tool is a script that finds node on PATH, as codex's `#!/usr/bin/env node` does. A first line that
    names node by its path needs none on PATH."""
    if script is None:
        return False
    try:
        with open(script, "rb") as file:
            first = file.readline(256)
    except OSError:
        return False
    if not first.startswith(b"#!"):
        return False
    words = first[2:].split()
    return bool(words) and os.path.basename(words[0]) == b"env" and b"node" in words[1:]


def _lost(env: Mapping[str, str], unit: Unit, path: str) -> list[str]:
    """The tools a unit's PATH no longer finds: those its ingest runs, and node where the shell's has one or a tool
    the unit's PATH finds runs on it, which a shell without nvm has no node to show."""
    tools = unit.tools or ()
    needs_node = shutil.which("node", path=env.get("PATH", "")) or any(
        _runs_on_node(shutil.which(tool, path=path)) for tool in tools
    )
    return [tool for tool in (*tools, *(("node",) if needs_node else ())) if not shutil.which(tool, path=path)]


def _without_path(text: str | None) -> str | None:
    return SAVED_PATH.sub("", text) if text is not None else None


def _stale(
    current: dict[str, str | None], expected: dict[str, str], env: Mapping[str, str], unit: Unit
) -> str | None:
    """Why the units aren't kb's as it would write them now, or None. The PATH line is left out of the comparison, as
    it depends on the shell kb runs in (an `nvm use`, an agent's session): the PATH a unit saved only has to still
    find the tools, and node where codex needs it."""
    if {name: _without_path(text) for name, text in current.items()} != {
        name: _without_path(text) for name, text in expected.items()
    }:
        return "kb ingest runs from an old unit"
    lost = _lost(env, unit, saved_path(current[unit.service]))
    return f"its PATH no longer finds {' or '.join(lost)}, so each run fails" if lost else None


def _installed(command: str, env: Mapping[str, str], current: dict[str, str | None], unit: Unit, *, fix: bool) -> Row:
    shown = tilde(units_dir(env) / unit.timer, env)
    if not installed_kb(env).is_file():
        detail = f"the service would run {tilde(installed_kb(env), env)}, which isn't there"
        return Row("problem", "timer", shown, detail, step="Run scripts/install.sh, which installs that kb")
    expected = units(env, unit)
    if (stale := _stale(current, expected, env, unit)) is None:
        return _state(command, env, unit, _ask(command, env, unit), fix=fix)
    installed = any(text is not None for text in current.values())
    if fix:
        return _write(command, env, unit, installed=installed)
    if installed:
        return Row("outdated", "timer", shown, stale)
    return Row("missing", "timer", shown, "kb ingest doesn't run on its own")
