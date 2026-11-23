"""agentsview.service: agentsview's server, sandboxed, which fabricd reads Claude Code's and Codex's sessions through
(docs/design.md §4.1, §5). `kb doctor --fix` sets it up once some workspace allows a project, as it does fabricd.

The sandbox is enforced only where the service manager can apply it: a user unit needs unprivileged user namespaces
and cgroup BPF, which a distribution may deny it (§4.1). So, before any unit is written, a transient user unit with
the same settings probes whether they hold. Where they do, kb writes and starts a user unit. Where they don't, the
unit has to be a system unit with `User=`, which needs root once: kb stages it in its data folder and gives the
command that installs it, then checks it like any other. A unit the user turned off stays off, and a unit that
doesn't start with kb's header is the user's own, left as it is."""

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from agent_fabric.config import Config, fabricd_settings
from agent_fabric.paths import state_db, tilde, units_dir
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
RUNNING = "running sandboxed: fabricd reads Claude Code's and Codex's sessions through it"
PROBE_TIMEOUT = 30  # seconds for the probe's transient unit

# Run in a transient unit with SANDBOX. 0 where the sandbox holds; 10 where the home is writable, 11 where an address
# other than loopback is reachable, 12 where loopback isn't, 13 where there's no other address to try. It sends
# nothing off the machine: the other address is the machine's own, and the UDP connect that finds it sends no packet.
PROBE = """
import os, socket, sys
try:
    os.close(os.open(sys.argv[1], os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    os.unlink(sys.argv[1])
    sys.exit(10)
except OSError:
    pass
def reach(host):
    server = socket.socket()
    server.bind((host, 0))
    server.listen()
    try:
        socket.create_connection(server.getsockname(), timeout=2).close()
        return True
    except OSError:
        return False
if not reach("127.0.0.1"):
    sys.exit(12)
try:
    finder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    finder.connect(("192.0.2.1", 9))
    own = finder.getsockname()[0]
except OSError:
    sys.exit(13)
sys.exit(11 if reach(own) else 0)
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


def unit(env: Mapping[str, str], port: int, *, system: bool) -> str:
    """The unit as kb would write it now: as a system unit, run as the user. Absolute paths, as a system unit's %h
    would be root's home."""
    home = Path(env["HOME"])
    user = f"User={home.owner()}\n" if system else ""
    hidden = "".join(f"InaccessiblePaths=-{_quoted(str(home / path))}\n" for path in HIDDEN)
    sandbox = "".join(f"{setting}\n" for setting in SANDBOX)
    command = (
        f"{_quoted(str(binary(env)), command=True)} serve --no-browser --no-update-check --port {port} --replace"
    )
    return (
        f"{HEADER}[Unit]\nDescription=agentsview: the agents' sessions, for fabricd (sandboxed)\n\n"
        f"[Service]\n{user}ExecStart={command}\nEnvironment=AGENTSVIEW_TELEMETRY_ENABLED=0\n"
        f"Restart=on-failure\nRestartSec=30\n{sandbox}ReadWritePaths={_quoted(str(data_dir(env)))}\n{hidden}\n"
        f"[Install]\nWantedBy={'multi-user.target' if system else 'default.target'}\n"
    )


def check(env: Mapping[str, str], config: Config | Exception, *, wanted: bool, fix: bool) -> list[Row]:
    """agentsview's row: none where no project is allowed and kb never set it up. `wanted`: some workspace allows
    a project. A config fabricd can't read gives no row here: fabricd's says what's wrong with it."""
    user_path, system_path = units_dir(env) / NAME, SYSTEM_UNITS / NAME
    try:
        user_unit, system_unit = _read(user_path), _read(system_path)
    except (OSError, UnicodeDecodeError) as e:
        return [_unreadable(e, env)] if wanted else []
    try:
        port = fabricd_settings(config).port if isinstance(config, Config) else None
    except ValueError:
        port = None
    if system_unit is not None:
        return [_system(env, system_unit, port, fix=fix)]
    if user_unit is not None or user_path.is_symlink():
        return [_user(env, user_unit, port, fix=fix)]
    if not wanted or port is None:
        return []
    return [_new(env, port, fix=fix)]


def _unreadable(e: Exception, env: Mapping[str, str]) -> Row:
    reason = e.strerror if isinstance(e, OSError) and e.strerror else e
    shown = tilde(units_dir(env) / NAME, env)
    step = f"Look at {shown} and {SYSTEM_UNITS / NAME}: kb checks them once it can read them"
    return Row("problem", "agentsview", shown, f"can't read the unit ({reason})", step=step)


def _new(env: Mapping[str, str], port: int, *, fix: bool) -> Row:
    """No unit yet: a user unit where the probe says the sandbox holds, else the system unit, staged."""
    shown = tilde(units_dir(env) / NAME, env)
    if not binary(env).is_file():
        detail = f"not installed: the unit would run {tilde(binary(env), env)}"
        return Row("missing", "agentsview", shown, detail, step="Run scripts/install-agentsview.sh")
    command = shutil.which("systemctl", path=env.get("PATH", ""))
    if command is None or (enforced := _probe(env)) is None:
        detail = "can't ask systemd from here whether it can sandbox agentsview"
        if command is None:
            detail = "no systemd here, so agentsview runs only when you start it, unsandboxed"
        step = f"Run {tilde(binary(env), env)} serve --no-browser --no-update-check --port {port} in a terminal"
        return Row("ok", "agentsview", shown, detail, step=step if command is None else None)
    if not enforced:
        return _stage(
            env, port, fix=fix, why="not installed: a user unit can't sandbox it here, so it needs a system unit"
        )
    if not fix:
        return Row("missing", "agentsview", shown, "not running, so fabricd captures no Claude Code or Codex session")
    return _write_user(env, port)


def _probe(env: Mapping[str, str]) -> bool | None:
    """Whether a user unit gets the sandbox; None where systemd can't be asked."""
    folder = data_dir(env).parent
    probe = folder / f".kb-agentsview-probe-{os.getpid()}"
    command = shutil.which("systemd-run", path=env.get("PATH", ""))
    if command is None:
        return None
    settings = [arg for setting in SANDBOX for arg in ("-p", setting)]
    try:
        done = subprocess.run(
            [
                command,
                "--user",
                "--wait",
                "--quiet",
                "--collect",
                *settings,
                sys.executable,
                "-I",
                "-c",
                PROBE,
                str(probe),
            ],
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        probe.unlink(missing_ok=True)  # where the home was writable after all
    if done.returncode == 1 and "bus" in done.stderr:
        return None
    return done.returncode == 0


def _stage(env: Mapping[str, str], port: int, *, fix: bool, why: str) -> Row:
    """The system unit, written where the user can install it from, with the command that does: absolute paths, as
    it runs in an administrator's shell."""
    shown = str(SYSTEM_UNITS / NAME)
    if not fix:
        return Row("missing", "agentsview", shown, why, step="Run kb doctor --fix, which stages the unit to install")
    try:
        data_dir(env).mkdir(exist_ok=True)
        staged(env).parent.mkdir(parents=True, exist_ok=True)
        staged(env).write_text(unit(env, port, system=True), encoding="utf-8")
    except OSError as e:
        step = f"Make {tilde(staged(env).parent, env)} writable, then run kb doctor --fix again"
        return Row("problem", "agentsview", shown, f"can't stage the unit ({e.strerror or e})", step=step)
    install = (
        f"sudo install -m 0644 {shlex.quote(str(staged(env)))} {shown} && sudo systemctl daemon-reload && "
        f"sudo systemctl enable {NAME} && sudo systemctl restart {NAME}"
    )
    return Row("missing", "agentsview", shown, f"{why}; staged at {tilde(staged(env), env)}", step=f"Run {install}")


def _system(env: Mapping[str, str], current: str, port: int | None, *, fix: bool) -> Row:
    """The system unit, which only the user can change: checked against kb's, and its state asked."""
    shown = str(SYSTEM_UNITS / NAME)
    if not current.startswith(HEADER):
        return Row("ok", "agentsview", shown, "yours: kb leaves a unit it didn't write as it is")
    if port is not None and current != unit(env, port, system=True):
        return _stage(env, port, fix=fix, why="outdated: it runs from an old unit")
    asked = _ask(env, system=True)
    journal = f"Read journalctl -u {NAME} and fix the cause; sudo systemctl restart {NAME}"
    return _state(asked, shown, journal=journal, start=f"Run sudo systemctl enable --now {NAME}")


def _user(env: Mapping[str, str], current: str | None, port: int | None, *, fix: bool) -> Row:
    """The user unit, which kb keeps as it would write it now, and starts where it's stopped."""
    path = units_dir(env) / NAME
    shown = tilde(path, env)
    if (left := _left(env, path, current)) is not None:
        return left
    if port is not None and current != unit(env, port, system=False):
        return _write_user(env, port) if fix else Row("outdated", "agentsview", shown, "it runs from an old unit")
    asked = _ask(env, system=False)
    journal = f"Read journalctl --user -u {NAME} and fix the cause; systemctl --user restart {NAME}"
    row = _state(asked, shown, journal=journal, start=f"Run systemctl --user enable --now {NAME}")
    if row.status != "missing" or not fix:
        return row
    done = _run(env, "--user", "enable", "--now", NAME)
    if done is None or done.returncode:
        said = done.stderr.strip() if done else "no answer"
        return Row("problem", "agentsview", shown, f"systemctl enable failed: {said}", step=row.step)
    return Row("fixed", "agentsview", shown, RUNNING)


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


def _write_user(env: Mapping[str, str], port: int) -> Row:
    """Through a temporary file and a rename, then reloaded and (re)started. A first install that systemd won't start
    is removed, so the next --fix probes and tries again."""
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
        temporary.unlink(missing_ok=True)
        step = f"Make {tilde(path.parent, env)} writable, then run kb doctor --fix again"
        return Row("problem", "agentsview", shown, f"can't write it ({e.strerror or e})", step=step)
    steps = (
        [("daemon-reload",), ("enable", NAME), ("restart", NAME)]
        if installed
        else [
            ("daemon-reload",),
            ("enable", "--now", NAME),
        ]
    )
    for args in steps:
        done = _run(env, "--user", *args)
        if done is None or done.returncode:
            said = done.stderr.strip() if done else "no answer"
            if not installed:
                path.unlink(missing_ok=True)
                _run(env, "--user", "daemon-reload")
                said += "; removed the unit, so --fix tries again"
            step = f"Run systemctl --user daemon-reload && systemctl --user enable --now {NAME}, and fix what it says"
            return Row("problem", "agentsview", shown, f"systemctl {args[0]} failed: {said}", step=step)
    return Row("fixed", "agentsview", shown, RUNNING)


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
    done = _run(env, *manager, "show", "--property=UnitFileState,ActiveState,Result", NAME)
    if done is None or done.returncode:
        return None
    return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)


def _state(asked: dict[str, str] | None, shown: str, *, journal: str, start: str) -> Row:
    """Its row from systemd's answer: `missing` only for a unit that is on but stopped, which `start` starts."""
    if asked is None:
        return Row("ok", "agentsview", shown, "installed; can't ask systemd from here whether it runs")
    state, active, result = (
        asked.get("UnitFileState", ""),
        asked.get("ActiveState", ""),
        asked.get("Result", "success"),
    )
    if state in OFF:
        return Row(
            "ok", "agentsview", shown, f"turned off ({state}), so fabricd captures no Claude Code or Codex session"
        )
    if active == "active":
        return Row("ok", "agentsview", shown, RUNNING)
    if active == "activating" and result == "success":
        return Row("ok", "agentsview", shown, "starting")
    if active in ("failed", "activating") or result != "success":
        return Row("problem", "agentsview", shown, f"stopped: it failed ({result})", step=journal)
    return Row("missing", "agentsview", shown, "installed but not running", step=start)
