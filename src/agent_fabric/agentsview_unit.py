"""agentsview.service: agentsview's server, sandboxed, which fabricd reads Claude Code's and Codex's sessions through
(docs/design.md §4.1, §5). `kb doctor --fix` sets it up once some workspace allows a project, as it does fabricd.

The sandbox is enforced only where the service manager can apply it: a user unit needs unprivileged user namespaces
and cgroup BPF, which a distribution may deny it (§4.1). So, before any unit is written, a transient user unit with
the same settings probes whether they hold. Where they do, kb writes and starts a user unit. Where they don't, the
unit has to be a system unit with `User=`, which needs root once: kb stages it in its data folder and gives the
command that installs it, then checks it like any other. A unit the user turned off stays off, and a unit that
doesn't start with kb's header is the user's own, left as it is."""

import contextlib
import os
import pwd
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_fabric.config import Config, agentsview_port
from agent_fabric.paths import expand, state_db, tilde, units_dir
from agent_fabric.report import Row
from agent_fabric.timer import HEADER, OFF, STATE_TIMEOUT, _masked, _quoted, _read

NAME = "agentsview.service"
SYSTEM_UNITS = Path("/etc/systemd/system")  # where a system unit of the user's goes; tests move it
SANDBOX = (
    "ProtectSystem=strict",
    "ProtectHome=read-only",
    "PrivateTmp=yes",
    "NoNewPrivileges=yes",
    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
    "IPAddressDeny=any",
    "IPAddressAllow=localhost",  # fabricd and the browser reach its API on loopback
)
HIDDEN = (
    ".ssh",
    ".gnupg",
    ".config/gh",
    ".codex/auth.json",
    ".claude/.credentials.json",
)  # credentials it never reads
MOVED = (("CODEX_HOME", "auth.json"), ("CLAUDE_CONFIG_DIR", ".credentials.json"))
RUNNING = "running sandboxed: fabricd reads Claude Code's and Codex's sessions through it"
PROBE_TIMEOUT = 30  # seconds for the probe's transient unit

# Run in a transient unit, in one of two ways, sending nothing off the machine. "sandbox", with SANDBOX: 0 where
# the home is read-only and loopback works; 10 where the home is writable, 12 where loopback is blocked, 14 where the
# write failed for another reason than a read-only home. "filter", with IPAddressDeny=any alone: 0 where loopback is
# blocked, so the filter is applied; 11 where it isn't.
PROBE = """
import errno, os, socket, sys
def reach():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    try:
        socket.create_connection(server.getsockname(), timeout=2).close()
        return True
    except OSError:
        return False
if sys.argv[1] == "filter":
    sys.exit(11 if reach() else 0)
try:
    os.close(os.open(sys.argv[2], os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    os.unlink(sys.argv[2])
    sys.exit(10)
except OSError as e:
    if e.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):
        sys.exit(14)
sys.exit(0 if reach() else 12)
"""


def binary(env: Mapping[str, str]) -> Path:
    """Where scripts/install-agentsview.sh installs the binary CI built and vetted."""
    return Path(env["HOME"]) / ".local" / "bin" / "agentsview"


def data_dir(env: Mapping[str, str]) -> Path:
    """agentsview's own folder, the one place the sandbox lets it write."""
    return Path(env["HOME"]) / ".agentsview"


def staged(env: Mapping[str, str]) -> Path:
    """Where kb leaves the system unit for the user to install."""
    return state_db(env).parent / NAME


def _user_name() -> str:
    """The user kb runs as, whom the system unit runs agentsview as; the uid where it has no name."""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


def _hidden(env: Mapping[str, str]) -> list[Path]:
    """The credentials it never reads: the defaults, and Codex's, Claude Code's and gh's where their folders moved."""
    home = Path(env["HOME"])
    paths = [home / path for path in HIDDEN]
    for variable, name in MOVED:
        if (moved := expand(env.get(variable) or "", env)).is_absolute():
            paths.append(moved / name)
    if Path(gh := env.get("GH_CONFIG_DIR") or "").is_absolute():  # gh's own setting, else XDG's
        paths.append(Path(gh))
    elif Path(config := env.get("XDG_CONFIG_HOME") or "").is_absolute():
        paths.append(Path(config) / "gh")
    return list(dict.fromkeys(paths))


def unit(env: Mapping[str, str], port: int, *, system: bool) -> str:
    """The unit as kb would write it now: as a system unit, run as the user. Absolute paths, as a system unit's %h
    would be root's home."""
    user = f"User={_user_name()}\n" if system else ""
    hidden = "".join(f"InaccessiblePaths=-{_quoted(str(path))}\n" for path in _hidden(env))
    moved = [(variable, expand(env.get(variable) or "", env)) for variable, _ in MOVED]
    # Where the agents keep their sessions, if moved: a unit gets none of a shell's variables. Whether agentsview
    # reads these is unconfirmed (§11); passed anyway, they cost nothing.
    environment = "".join(
        f"Environment={_quoted(f'{variable}={path}')}\n" for variable, path in moved if path.is_absolute()
    )
    sandbox = "".join(f"{setting}\n" for setting in SANDBOX)
    command = (
        f"{_quoted(str(binary(env)), command=True)} serve --no-browser --no-update-check --port {port} --replace"
    )
    return (
        f"{HEADER}[Unit]\nDescription=agentsview: the agents' sessions, for fabricd (sandboxed)\n\n"
        f"[Service]\n{user}ExecStart={command}\nEnvironment=AGENTSVIEW_TELEMETRY_ENABLED=0\n{environment}"
        f"Restart=on-failure\nRestartSec=30\n{sandbox}ReadWritePaths={_quoted(str(data_dir(env)))}\n{hidden}\n"
        f"[Install]\nWantedBy={'multi-user.target' if system else 'default.target'}\n"
    )


def check(env: Mapping[str, str], config: Config | Exception, *, wanted: bool, fix: bool) -> list[Row]:
    """agentsview's rows: none where no project is allowed and kb never set it up. `wanted`: some workspace allows
    a project; while none does, an installed unit is checked but never written or started. A port kb can't read
    gives no new unit: fabricd's row says what's wrong with it."""
    user_path, system_path = units_dir(env) / NAME, SYSTEM_UNITS / NAME
    try:
        user_unit = _read(user_path)
    except (OSError, UnicodeDecodeError) as e:
        return [_unreadable(e, env)] if wanted else []
    try:
        system_unit = _read(system_path)
    except (OSError, UnicodeDecodeError):  # kb's own is 0644: one only root reads is the user's
        system_unit = "unreadable"
    try:
        port = agentsview_port(config) if isinstance(config, Config) else None
    except ValueError:
        port = None
    if system_unit is not None or system_path.is_symlink():
        rows = [_system(env, system_unit, port, fix=fix, wanted=wanted)]
        if user_unit is not None and user_unit.startswith(HEADER):
            rows.append(_leftover(env))
        return rows
    if user_unit is not None or user_path.is_symlink():
        return [_user(env, user_unit, port, fix=fix, wanted=wanted)]
    if not wanted or port is None:
        return []
    return [_new(env, port, fix=fix)]


def _unreadable(e: Exception, env: Mapping[str, str]) -> Row:
    reason = e.strerror if isinstance(e, OSError) and e.strerror else e
    shown = tilde(units_dir(env) / NAME, env)
    step = f"Look at {shown}: kb checks it once it can read it"
    return Row("problem", "agentsview", shown, f"can't read the unit ({reason})", step=step)


def _leftover(env: Mapping[str, str]) -> Row:
    """A user unit of kb's beside the system unit: both serve on one port with --replace, each taking it from the
    other."""
    path = units_dir(env) / NAME
    step = (
        f"Run systemctl --user disable --now {NAME} && rm {shlex.quote(str(path))} && systemctl --user daemon-reload"
    )
    detail = "a user unit of kb's beside the system unit: the two take the port from each other"
    return Row("problem", "agentsview", tilde(path, env), detail, step=step)


def _new(env: Mapping[str, str], port: int, *, fix: bool) -> Row:
    """No unit yet. With --fix, the probe decides: a user unit where the sandbox holds, else the system unit, staged.
    Without it, kb doesn't probe, as a probe starts a unit."""
    shown = tilde(units_dir(env) / NAME, env)
    if (unprobed := _unprobed(env, port, fix=fix)) is not None:
        return unprobed
    enforced = _probe(env)
    if enforced is True:
        return _write_user(env, port, off=None)
    if enforced is False:
        return _stage(env, port, ("missing", NEEDS_SYSTEM), fix=True)
    if enforced == NO_BUS:
        return Row("ok", "agentsview", shown, "can't ask systemd from here whether it can sandbox agentsview")
    step = "Run kb doctor --fix again once that's settled"
    return Row(
        "problem", "agentsview", shown, f"can't tell whether a user unit can sandbox it: {enforced}", step=step
    )


def _unprobed(env: Mapping[str, str], port: int, *, fix: bool) -> Row | None:
    """The row of a new unit kb doesn't probe for: agentsview or systemd missing, or no --fix."""
    shown = tilde(units_dir(env) / NAME, env)
    if not binary(env).is_file():
        detail = f"not installed: the unit would run {tilde(binary(env), env)}"
        return Row("missing", "agentsview", shown, detail, step="Run scripts/install-agentsview.sh")
    if shutil.which("systemctl", path=env.get("PATH", "")) is None:
        detail = "no systemd here, so agentsview runs only when you start it, unsandboxed"
        step = f"Run {tilde(binary(env), env)} serve --no-browser --no-update-check --port {port} in a terminal"
        return Row("ok", "agentsview", shown, detail, step=step)
    if fix:
        return None
    if _staged_current(env, port):  # an earlier --fix's verdict, waiting for the user's sudo
        return _stage(env, port, ("missing", NEEDS_SYSTEM), fix=False)
    detail = "not running, so fabricd captures no Claude Code or Codex session"
    return Row("missing", "agentsview", shown, detail, step="Run kb doctor --fix, which sets it up")


NEEDS_SYSTEM = "not installed: a user unit can't sandbox it here, so it needs a system unit"
NO_BUS = "no bus"


def _probe(env: Mapping[str, str]) -> bool | str:
    """Whether a user unit gets the sandbox, else why kb can't tell: NO_BUS where systemd can't be asked. Two
    transient units: the unit's settings, then the network filter alone, which must block even loopback. Only their
    answers, and systemd failing to set up the unit's namespaces, say no."""
    command = shutil.which("systemd-run", path=env.get("PATH", ""))
    if command is None:
        return "systemd-run isn't on PATH"
    probe = data_dir(env).parent / f".kb-agentsview-probe-{os.getpid()}"
    try:  # one a killed run left, which would fail the write for another reason
        _remove(probe)
    except OSError as e:
        return f"it can't remove {tilde(probe, env)} ({e.strerror or e})"
    try:
        sandboxed = _transient(command, env, SANDBOX, "sandbox", str(probe))
    finally:
        with contextlib.suppress(OSError):
            _remove(probe)  # where the home was writable after all
    if sandboxed is not True:
        return sandboxed
    return _transient(command, env, ("IPAddressDeny=any",), "filter")


def _remove(path: Path) -> None:
    """Only a file that's there: in a read-only home, as an agent's sandbox has, unlink fails even on none."""
    if os.path.lexists(path):
        path.unlink()


def _transient(command: str, env: Mapping[str, str], settings: Sequence[str], *args: str) -> bool | str:
    """The probe in a transient user unit with these settings; it stops itself at RuntimeMaxSec."""
    properties = [arg for setting in (*settings, f"RuntimeMaxSec={PROBE_TIMEOUT}") for arg in ("-p", setting)]
    run = [command, "--user", "--wait", "--quiet", "--collect", *properties, sys.executable, "-I", "-c", PROBE, *args]
    try:
        done = subprocess.run(
            run, env=dict(env), capture_output=True, text=True, timeout=PROBE_TIMEOUT + 10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"systemd-run didn't finish ({e})"
    if done.returncode in ANSWERS:
        return ANSWERS[done.returncode]
    if done.returncode == 1 and "bus" in done.stderr:
        return NO_BUS
    said = done.stderr.strip().splitlines()[-1] if done.stderr.strip() else f"exit {done.returncode}"
    return f"systemd-run failed ({said})"


ANSWERS: dict[int, bool | str] = {
    0: True,
    10: False,
    11: False,
    12: "loopback is blocked too, which a system unit's filter would block as well",
    14: "its write in the home failed for another reason than a read-only home",
    226: False,  # systemd's 226/NAMESPACE: a user manager that can't set up the unit's namespaces
}


def _staged_current(env: Mapping[str, str], port: int) -> bool:
    try:
        return staged(env).read_text(encoding="utf-8") == unit(env, port, system=True)
    except (OSError, UnicodeDecodeError):
        return False


def _stage(env: Mapping[str, str], port: int, said: tuple[str, str], *, fix: bool, keep_off: bool = False) -> Row:
    """The system unit, written where the user can install it from, with the command that does: absolute paths, as
    it runs in an administrator's shell. `said`: the row's status and detail. `keep_off`: a unit the user turned
    off, or one whose state kb can't ask, which the command only installs and reloads."""
    status, why = said
    shown = str(SYSTEM_UNITS / NAME)
    if fix:
        try:
            data_dir(env).mkdir(exist_ok=True)
            staged(env).parent.mkdir(parents=True, exist_ok=True)
            staged(env).write_text(unit(env, port, system=True), encoding="utf-8")
        except OSError as e:
            step = f"Make {tilde(staged(env).parent, env)} writable, then run kb doctor --fix again"
            return Row("problem", "agentsview", shown, f"can't stage the unit ({e.strerror or e})", step=step)
    elif not _staged_current(env, port):
        return Row(status, "agentsview", shown, why, step="Run kb doctor --fix, which stages the unit to install")
    install = f"sudo install -m 0644 {shlex.quote(str(staged(env)))} {shown} && sudo systemctl daemon-reload"
    if not keep_off:
        install += f" && sudo systemctl enable {NAME} && sudo systemctl restart {NAME}"
    return Row(status, "agentsview", shown, f"{why}; staged at {tilde(staged(env), env)}", step=f"Run {install}")


def _system(env: Mapping[str, str], current: str | None, port: int | None, *, fix: bool, wanted: bool) -> Row:
    """The system unit, which only the user can change: checked against kb's, and its state asked."""
    path = SYSTEM_UNITS / NAME
    shown = str(path)
    if _masked(path):
        return Row("ok", "agentsview", shown, f"turned off (masked); sudo systemctl unmask {NAME} turns it back on")
    if current is None or not current.startswith(HEADER):
        return Row("ok", "agentsview", shown, "yours: kb leaves a unit it didn't write as it is")
    asked = _ask(env, system=True)
    state = _state(asked, shown, SYSTEM_STEPS, wanted=wanted)
    if wanted and port is not None and current != unit(env, port, system=True):
        why = f"outdated: it runs from an old unit; {state.detail}"
        return _stage(env, port, ("outdated", why), fix=fix, keep_off=asked is None or _off(asked) is not None)
    if (asked or {}).get("NeedDaemonReload") == "yes":
        step = f"Run sudo systemctl daemon-reload && sudo systemctl restart {NAME}"
        if asked is None or _off(asked):  # turned off: reloaded, not started
            step = "Run sudo systemctl daemon-reload"
        detail = "systemd runs it from the unit as it was before it was installed"
        return Row("outdated", "agentsview", shown, detail, step=step)
    return state


def _user(env: Mapping[str, str], current: str | None, port: int | None, *, fix: bool, wanted: bool) -> Row:
    """The user unit, which kb keeps as it would write it now, and starts where it's stopped: while a project is
    allowed, and unless the user turned it off."""
    path = units_dir(env) / NAME
    shown = tilde(path, env)
    if (left := _left(env, path, current)) is not None:
        return left
    asked = _ask(env, system=False)
    if wanted and port is not None and (stale := _stale(current, asked, env, port)):
        if not fix:
            return Row("outdated", "agentsview", shown, stale)
        kept = (
            _off(asked)
            if asked is not None
            else "can't ask systemd from here whether it's on, so kb only reloaded it"
        )
        return _write_user(env, port, off=kept)
    row = _state(asked, shown, USER_STEPS, wanted=wanted)
    if row.status != "missing" or not fix:
        return row
    done = _run(env, "--user", "enable", "--now", NAME)
    if done is None or done.returncode:
        said = done.stderr.strip() if done else "no answer"
        return Row("problem", "agentsview", shown, f"systemctl enable failed: {said}", step=row.step)
    return Row("fixed", "agentsview", shown, RUNNING)


def _stale(current: str | None, asked: dict[str, str] | None, env: Mapping[str, str], port: int) -> str | None:
    """Why the user unit isn't kb's as it runs now: an old file, or a new one systemd hasn't reloaded, as after a
    reload that failed."""
    if current != unit(env, port, system=False):
        return "it runs from an old unit"
    if (asked or {}).get("NeedDaemonReload") == "yes":
        return "systemd runs it from the unit as it was before kb rewrote it"
    return None


def _left(env: Mapping[str, str], path: Path, current: str | None) -> Row | None:
    """The row of a user unit kb leaves as it is: masked, not kb's, or where there's no systemd to run it."""
    shown = tilde(path, env)
    if _masked(path):
        return Row("ok", "agentsview", shown, f"turned off (masked); systemctl --user unmask {NAME} turns it back on")
    if current is None or not current.startswith(HEADER):
        return Row("ok", "agentsview", shown, "yours: kb leaves a unit it didn't write as it is")
    if shutil.which("systemctl", path=env.get("PATH", "")) is None:
        return Row("ok", "agentsview", shown, "installed; no systemd here to run it")
    return None


def _off(asked: dict[str, str] | None) -> str | None:
    state = (asked or {}).get("UnitFileState", "")
    return f"turned off ({state}), so fabricd captures no Claude Code or Codex session" if state in OFF else None


def _write_user(env: Mapping[str, str], port: int, *, off: str | None) -> Row:
    """Through a temporary file and a rename, then reloaded and (re)started, unless the user turned it off: then
    only reloaded. A first install that systemd won't start is removed, so the next --fix probes and tries again."""
    path = units_dir(env) / NAME
    shown = tilde(path, env)
    installed = path.exists()
    temporary = path.with_name(f".{NAME}.kb-new")
    try:
        data_dir(env).mkdir(exist_ok=True)  # the sandbox makes the rest of the home read-only, this folder included
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(unit(env, port, system=False), encoding="utf-8")
        temporary.replace(path)
    except OSError as e:
        with contextlib.suppress(OSError):  # best effort: in a read-only folder, unlink fails even on no file
            temporary.unlink(missing_ok=True)
        step = f"Make {tilde(path.parent, env)} writable, then run kb doctor --fix again"
        return Row("problem", "agentsview", shown, f"can't write it ({e.strerror or e})", step=step)
    if off:
        steps: list[tuple[str, ...]] = [("daemon-reload",)]
    elif installed:
        steps = [("daemon-reload",), ("enable", NAME), ("restart", NAME)]
    else:
        steps = [("daemon-reload",), ("enable", "--now", NAME)]
    for args in steps:
        done = _run(env, "--user", *args)
        if done is None or done.returncode:
            said = done.stderr.strip() if done else "no answer"
            if not installed:
                path.unlink(missing_ok=True)
                _run(env, "--user", "daemon-reload")
                said += "; removed the unit, so --fix tries again"
            step = (
                "Fix what systemctl says, then run kb doctor --fix again"
                if not installed
                else "Run systemctl --user daemon-reload, and fix what it says"
                if off
                else f"Run systemctl --user daemon-reload && systemctl --user restart {NAME}, and fix what it says"
            )
            return Row("problem", "agentsview", shown, f"systemctl {args[0]} failed: {said}", step=step)
    return Row("fixed", "agentsview", shown, f"rewrote the unit; {off}" if off else RUNNING)


def _run(env: Mapping[str, str], *args: str) -> subprocess.CompletedProcess[str] | None:
    """systemctl with these arguments: `--user` first for the user's manager, none for the system's, which anyone
    can ask. None where it can't be asked."""
    command = shutil.which("systemctl", path=env.get("PATH", ""))
    if command is None:
        return None
    try:
        return subprocess.run(
            [command, *args], env=dict(env), capture_output=True, text=True, timeout=STATE_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _ask(env: Mapping[str, str], *, system: bool) -> dict[str, str] | None:
    manager = () if system else ("--user",)
    done = _run(env, *manager, "show", "--property=UnitFileState,ActiveState,Result,NeedDaemonReload", NAME)
    if done is None or done.returncode:
        return None
    return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)


@dataclass(frozen=True)
class Steps:
    """The user's steps for one service manager: the user's, or the system's through sudo."""

    journal: str
    start: str
    stop: str


USER_STEPS = Steps(
    journal=f"Read journalctl --user -u {NAME} and fix the cause; systemctl --user restart {NAME}",
    start=f"Run systemctl --user enable --now {NAME}",
    stop=f"Run systemctl --user stop {NAME}, or it runs until the next reboot",
)
SYSTEM_STEPS = Steps(
    journal=f"Read journalctl -u {NAME} and fix the cause; sudo systemctl restart {NAME}",
    start=f"Run sudo systemctl enable --now {NAME}",
    stop=f"Run sudo systemctl stop {NAME}, or it runs until the next reboot",
)


def _state(asked: dict[str, str] | None, shown: str, steps: Steps, *, wanted: bool) -> Row:
    """Its row from systemd's answer: `missing` only for a unit on but stopped while a project is allowed, which
    `start` starts."""
    if asked is None:
        return Row("ok", "agentsview", shown, "installed; can't ask systemd from here whether it runs")
    active, result = asked.get("ActiveState", ""), asked.get("Result", "success")
    if off := _off(asked):
        detail = f"{off}, but it still runs" if active == "active" else off  # disabled without --now
        return Row("ok", "agentsview", shown, detail, step=steps.stop if active == "active" else None)
    if active == "active" or (active == "activating" and result == "success"):
        return Row("ok", "agentsview", shown, RUNNING if active == "active" else "starting")
    if not wanted:
        return Row("ok", "agentsview", shown, "installed, not running: no project is allowed, so --fix leaves it off")
    if active in ("failed", "activating") or result != "success":
        return Row("problem", "agentsview", shown, f"stopped: it failed ({result})", step=steps.journal)
    return Row("missing", "agentsview", shown, "installed but not running", step=steps.start)
