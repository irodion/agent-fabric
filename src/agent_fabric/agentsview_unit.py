"""agentsview.service: agentsview's server, sandboxed, which fabricd reads Claude Code's and Codex's sessions through
(docs/design.md §4.1, §5). `kb doctor --fix` sets it up once some workspace allows a project, as it does fabricd.

The sandbox is enforced only where the service manager can apply it: a user unit needs unprivileged user namespaces
and cgroup BPF, which a distribution may deny it (§4.1). So, before any unit is written, a transient user unit with
the same settings probes whether they hold. Where they do, kb writes and starts a user unit. Where they don't, the
unit has to be a system unit with `User=`, which needs root once: kb stages it in its data folder with a script that
installs it, for the user to run with sudo, then checks it like any other. A unit the user turned off stays off, and
a unit that doesn't start with kb's header is the user's own, left as it is."""

import contextlib
import hashlib
import os
import pwd
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_fabric.config import Config, agentsview_hidden, agentsview_port
from agent_fabric.paths import config_file, expand, state_db, tilde, units_dir
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
# Above this, the kernel reclaims its memory and slows it rather than kill it: above the devbox's first full sync,
# cache included, so a sync like that one isn't slowed, while a bigger one gives way to the agents (§4.1, Cost).
MEMORY_HIGH = "MemoryHigh=2G"
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


@dataclass(frozen=True)
class Settings:
    """What the config says of agentsview's unit: its port, and the paths it hides beyond kb's own."""

    port: int
    hidden: tuple[Path, ...] = ()


def data_dir(env: Mapping[str, str]) -> Path:
    """agentsview's own folder, the one place the sandbox lets it write."""
    return Path(env["HOME"]) / ".agentsview"


def staged(env: Mapping[str, str]) -> Path:
    """Where kb leaves the system unit for the user to install."""
    return state_db(env).parent / NAME


def script(env: Mapping[str, str]) -> Path:
    """Beside the staged unit: what installs it, for the user to run with sudo."""
    return staged(env).with_name("install-agentsview.sh")


def installer(env: Mapping[str, str], settings: Settings) -> str:
    """The script that installs the staged unit, in an administrator's shell: absolute paths, and the unit's state
    read when it runs, since the user may turn it off after kb wrote this. A first install is enabled and started;
    a unit already there is restarted only while it's enabled. It refuses a masked unit, one kb didn't write, and a
    staged unit that isn't the one kb wrote with it: the unit is copied where only root can write, then checked by
    its hash, since anything running as the user can write where kb staged it."""
    staged_hash = hashlib.sha256(unit(env, settings, system=True).encode()).hexdigest()
    first = shlex.quote(HEADER.splitlines()[0])
    return (
        "#!/bin/sh\n"
        f"{HEADER}"
        "# Installs the agentsview unit staged beside it. Run a copy, checked by its hash, as kb doctor says.\n"
        "set -eu\n"
        f"unit={shlex.quote(str(SYSTEM_UNITS / NAME))}\n"
        f"state=$(systemctl is-enabled {NAME} 2>/dev/null) || true\n"
        'case "$(readlink -f "$unit") $state" in\n'
        '  "/dev/null "* | *" masked" | *" masked-runtime")\n'
        '    echo "$unit is masked: unmask it first, if you want it on" >&2; exit 1 ;;\n'
        "esac\n"
        f'if [ -e "$unit" ] && [ "$(head -n 1 "$unit")" != {first} ]; then\n'
        "  echo \"$unit is not kb's: move it away first, for kb's own unit\" >&2; exit 1\n"
        "fi\n"
        "copy=$(mktemp)\n"
        "trap 'rm -f \"$copy\"' EXIT\n"
        f'cat {shlex.quote(str(staged(env)))} > "$copy"\n'
        f'if [ "$(sha256sum < "$copy" | cut -d " " -f 1)" != {staged_hash} ]; then\n'
        '  echo "the staged unit changed since kb wrote this script: run kb doctor --fix again" >&2; exit 1\n'
        "fi\n"
        '[ -e "$unit" ] && new=no || new=yes\n'
        'install -m 0644 "$copy" "$unit"\n'
        "systemctl daemon-reload\n"
        'if [ "$new" = yes ]; then\n'
        f"  systemctl enable {NAME}\n"
        f"  systemctl restart {NAME}\n"
        f"elif systemctl is-enabled --quiet {NAME}; then\n"
        f"  systemctl restart {NAME}\n"
        "fi\n"
    )


def _user_name() -> str:
    """The user kb runs as, whom the system unit runs agentsview as; the uid where it has no name."""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


def _hidden(env: Mapping[str, str], settings: Settings) -> list[Path]:
    """The credentials it never reads: the defaults, Codex's, Claude Code's and gh's where their folders moved, and
    the config's own."""
    home = Path(env["HOME"])
    paths = [home / path for path in HIDDEN]
    for variable, name in MOVED:
        if (moved := expand(env.get(variable) or "", env)).is_absolute():
            paths.append(moved / name)
    if Path(gh := env.get("GH_CONFIG_DIR") or "").is_absolute():  # gh's own setting, else XDG's
        paths.append(Path(gh))
    elif Path(config := env.get("XDG_CONFIG_HOME") or "").is_absolute():
        paths.append(Path(config) / "gh")
    return list(dict.fromkeys([*paths, *settings.hidden]))


def unit(env: Mapping[str, str], settings: Settings, *, system: bool) -> str:
    """The unit as kb would write it now: as a system unit, run as the user. Absolute paths, as a system unit's %h
    would be root's home."""
    user = f"User={_user_name()}\n" if system else ""
    hidden = "".join(f"InaccessiblePaths=-{_quoted(str(path))}\n" for path in _hidden(env, settings))
    moved = [(variable, expand(env.get(variable) or "", env)) for variable, _ in MOVED]
    # Where the agents keep their sessions, if moved: a unit gets none of a shell's variables. Whether agentsview
    # reads these is unconfirmed (§11); passed anyway, they cost nothing.
    environment = "".join(
        f"Environment={_quoted(f'{variable}={path}')}\n" for variable, path in moved if path.is_absolute()
    )
    sandbox = "".join(f"{setting}\n" for setting in SANDBOX)
    command = (
        f"{_quoted(str(binary(env)), command=True)} serve --no-browser --no-update-check"
        f" --port {settings.port} --replace"
    )
    return (
        f"{HEADER}[Unit]\nDescription=agentsview: the agents' sessions, for fabricd (sandboxed)\n\n"
        f"[Service]\n{user}ExecStart={command}\nEnvironment=AGENTSVIEW_TELEMETRY_ENABLED=0\n{environment}"
        f"Restart=on-failure\nRestartSec=30\n{MEMORY_HIGH}\n"
        f"{sandbox}ReadWritePaths={_quoted(str(data_dir(env)))}\n{hidden}\n"
        f"[Install]\nWantedBy={'multi-user.target' if system else 'default.target'}\n"
    )


def check(env: Mapping[str, str], config: Config | Exception, *, wanted: bool, fix: bool) -> list[Row]:
    """agentsview's rows: none where no project is allowed and kb never set it up. `wanted`: some workspace allows
    a project; while none does, an installed unit is checked but never written or started. A port or hidden
    list kb can't read gives no new unit: fabricd's row says what's wrong with a port, agentsview's with a list."""
    user_path, system_path = units_dir(env) / NAME, SYSTEM_UNITS / NAME
    rows, unreadable = [], False
    try:
        user_unit = _read(user_path)
    except (OSError, UnicodeDecodeError) as e:  # still checked: the system unit
        rows += [_unreadable(e, env)] if wanted else []
        user_unit, unreadable = None, True
    try:
        system_unit = _read(system_path)
    except (OSError, UnicodeDecodeError):  # kb's own is 0644: one only root reads is the user's
        system_unit = "unreadable"
    settings = None
    try:  # a port kb can't read: fabricd's row says what's wrong with it, or with an [agentsview] that isn't a table
        port = agentsview_port(config) if isinstance(config, Config) else None
    except ValueError:
        port = None
    if isinstance(config, Config) and port is not None:
        try:
            settings = Settings(port, _usable(agentsview_hidden(config, env), env))
        except ValueError as e:
            rows += [_bad_hidden(e, env)] if wanted else []
    if system_unit is not None or system_path.is_symlink():
        rows.append(_system(env, system_unit, settings, fix=fix, wanted=wanted))
        kbs = system_unit is not None and system_unit.startswith(HEADER) and not _masked(system_path)
        if kbs and user_unit is not None and user_unit.startswith(HEADER):  # two of kb's, on one port
            rows.append(_leftover(env))
        return rows
    if unreadable:
        return rows
    if user_unit is not None or user_path.is_symlink():
        return [*rows, _user(env, user_unit, settings, fix=fix, wanted=wanted)]
    if not wanted or settings is None:
        return rows
    return [_new(env, settings, fix=fix)]


def _usable(hidden: tuple[Path, ...], env: Mapping[str, str]) -> tuple[Path, ...]:
    """The config's hidden paths, unless one hides what agentsview needs: the home, its binary, its data folder, or
    the folders Claude Code and Codex keep their sessions in. ValueError naming the first that does."""
    home = Path(env["HOME"])
    needed = [home, binary(env), data_dir(env), home / ".claude", home / ".codex"]
    needed += [moved for variable, _ in MOVED if (moved := expand(env.get(variable) or "", env)).is_absolute()]
    for path in hidden:
        if any(need == path or need.is_relative_to(path) for need in needed):
            raise ValueError(f"[agentsview] hidden would hide what agentsview needs: {tilde(path, env)}")
    return hidden


def _bad_hidden(e: ValueError, env: Mapping[str, str]) -> Row:
    shown = tilde(config_file(env), env)
    return Row("problem", "agentsview", shown, str(e), step=f"Fix [agentsview] hidden in {shown}")


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


def _new(env: Mapping[str, str], settings: Settings, *, fix: bool) -> Row:
    """No unit yet. With --fix, the probe decides: a user unit where the sandbox holds, else the system unit, staged.
    Without it, kb doesn't probe, as a probe starts a unit."""
    shown = tilde(units_dir(env) / NAME, env)
    if (unprobed := _unprobed(env, settings, fix=fix)) is not None:
        return unprobed
    enforced = _probe(env)
    if enforced is True:
        return _write_user(env, settings, off=None)
    if enforced is False:
        return _stage(env, settings, ("missing", NEEDS_SYSTEM), fix=True)
    if enforced == NO_BUS:
        return Row("ok", "agentsview", shown, "can't ask systemd from here whether it can sandbox agentsview")
    step = "Run kb doctor --fix again once that's settled"
    return Row(
        "problem", "agentsview", shown, f"can't tell whether a user unit can sandbox it: {enforced}", step=step
    )


def _unprobed(env: Mapping[str, str], settings: Settings, *, fix: bool) -> Row | None:
    """The row of a new unit kb doesn't probe for: agentsview or systemd missing, or no --fix."""
    shown = tilde(units_dir(env) / NAME, env)
    if not binary(env).is_file():
        detail = f"not installed: the unit would run {tilde(binary(env), env)}"
        return Row("missing", "agentsview", shown, detail, step="Run scripts/install-agentsview.sh")
    if shutil.which("systemctl", path=env.get("PATH", "")) is None:
        detail = "no systemd here, so agentsview runs only when you start it, unsandboxed"
        serve = f"{tilde(binary(env), env)} serve --no-browser --no-update-check --port {settings.port}"
        step = f"Run {serve} in a terminal"
        return Row("ok", "agentsview", shown, detail, step=step)
    if fix:
        return None
    if _staged_current(env, settings):  # an earlier --fix's verdict, waiting for the user's sudo
        return _stage(env, settings, ("missing", NEEDS_SYSTEM), fix=False)
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


def _staged_current(env: Mapping[str, str], settings: Settings) -> bool:
    try:
        current = staged(env).read_text(encoding="utf-8") == unit(env, settings, system=True)
        return current and script(env).read_text(encoding="utf-8") == installer(env, settings)
    except (OSError, UnicodeDecodeError):
        return False


def _stage(env: Mapping[str, str], settings: Settings, said: tuple[str, str], *, fix: bool) -> Row:
    """The system unit, written where the user can install it from, with the script that does. `said`: the row's
    status and detail."""
    status, why = said
    shown = str(SYSTEM_UNITS / NAME)
    if fix:
        try:
            data_dir(env).mkdir(exist_ok=True)
            staged(env).parent.mkdir(parents=True, exist_ok=True)
            staged(env).write_text(unit(env, settings, system=True), encoding="utf-8")
            script(env).write_text(installer(env, settings), encoding="utf-8")
        except OSError as e:
            step = f"Make {tilde(staged(env).parent, env)} writable, then run kb doctor --fix again"
            return Row("problem", "agentsview", shown, f"can't stage the unit ({e.strerror or e})", step=step)
    elif not _staged_current(env, settings):
        return Row(status, "agentsview", shown, why, step="Run kb doctor --fix, which stages the unit to install")
    step = _install_step(env, settings)
    return Row(status, "agentsview", shown, f"{why}; staged at {tilde(staged(env), env)}", step=step)


def _install_step(env: Mapping[str, str], settings: Settings) -> str:
    """One command for an administrator's shell: copy the script where only root can write, check it against the
    hash of the one kb wrote, show it, and run it only on a y, so what the administrator reads is what runs, whatever
    writes in the user's home meanwhile. A failure stops it and sets its exit status, and the copy is removed after,
    unless the administrator interrupts it. No double quotes or backslashes, since the report would escape them and
    the command must paste as printed; that holds while the home's path has no single quote, which shlex would
    escape with double quotes. The prose names the absolute path too: in an administrator's shell, ~ is theirs."""
    digest = hashlib.sha256(installer(env, settings).encode()).hexdigest()
    path = shlex.quote(str(script(env)))
    return (
        f"As an administrator, copy {path} out of the home, check it against the one kb wrote, read it and run it: "
        f"f=$(sudo mktemp) && sudo cp {path} $f && echo '{digest}  '$f | sudo sha256sum -c && sudo cat $f "
        "&& printf 'Run it? [y/N] ' && read a && case $a in y|Y) sudo sh $f ;; *) false ;; esac; "
        "s=$?; sudo rm -f $f; [ $s = 0 ]"
    )


def _system(
    env: Mapping[str, str], current: str | None, settings: Settings | None, *, fix: bool, wanted: bool
) -> Row:
    """The system unit, which only the user can change: checked against kb's, and its state asked."""
    path = SYSTEM_UNITS / NAME
    shown = str(path)
    if _masked(path):
        return Row("ok", "agentsview", shown, f"turned off (masked); sudo systemctl unmask {NAME} turns it back on")
    if current is None or not current.startswith(HEADER):
        step = f"Move {shown} away with sudo, then run kb doctor --fix, for kb's own sandboxed unit"
        return Row("ok", "agentsview", shown, "yours: kb leaves a unit it didn't write as it is", step=step)
    asked = _ask(env, system=True)
    state = _state(asked, shown, SYSTEM_STEPS, wanted=wanted)
    if wanted and settings is not None and current != unit(env, settings, system=True):
        why = f"outdated: it runs from an old unit; {state.detail}"
        return _stage(env, settings, ("outdated", why), fix=fix)
    if (asked or {}).get("NeedDaemonReload") == "yes":
        step = f"Run sudo systemctl daemon-reload && sudo systemctl restart {NAME}"
        if asked is None or _off(asked):  # turned off: reloaded, not started
            step = "Run sudo systemctl daemon-reload"
        detail = "systemd runs it from the unit as it was before it was installed"
        return Row("outdated", "agentsview", shown, detail, step=step)
    return state


def _user(env: Mapping[str, str], current: str | None, settings: Settings | None, *, fix: bool, wanted: bool) -> Row:
    """The user unit, which kb keeps as it would write it now, and starts where it's stopped: while a project is
    allowed, and unless the user turned it off."""
    path = units_dir(env) / NAME
    shown = tilde(path, env)
    if (left := _left(env, path, current)) is not None:
        return left
    asked = _ask(env, system=False)
    if wanted and settings is not None and (stale := _stale(current, asked, env, settings)):
        if not fix:
            return Row("outdated", "agentsview", shown, stale)
        kept = (
            _off(asked)
            if asked is not None
            else "can't ask systemd from here whether it's on, so kb only reloaded it"
        )
        return _write_user(env, settings, off=kept)
    row = _state(asked, shown, USER_STEPS, wanted=wanted)
    if row.status != "missing" or not fix:
        return row
    done = _run(env, "--user", "enable", "--now", NAME)
    if done is None or done.returncode:
        said = done.stderr.strip() if done else "no answer"
        return Row("problem", "agentsview", shown, f"systemctl enable failed: {said}", step=row.step)
    return Row("fixed", "agentsview", shown, RUNNING)


def _stale(
    current: str | None, asked: dict[str, str] | None, env: Mapping[str, str], settings: Settings
) -> str | None:
    """Why the user unit isn't kb's as it runs now: an old file, or a new one systemd hasn't reloaded, as after a
    reload that failed."""
    if current != unit(env, settings, system=False):
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
        step = f"Move {shown} away and run kb doctor --fix, for kb's own sandboxed unit"
        return Row("ok", "agentsview", shown, "yours: kb leaves a unit it didn't write as it is", step=step)
    if shutil.which("systemctl", path=env.get("PATH", "")) is None:
        return Row("ok", "agentsview", shown, "installed; no systemd here to run it")
    return None


def _off(asked: dict[str, str] | None) -> str | None:
    state = (asked or {}).get("UnitFileState", "")
    return f"turned off ({state}), so fabricd captures no Claude Code or Codex session" if state in OFF else None


def _write_user(env: Mapping[str, str], settings: Settings, *, off: str | None) -> Row:
    """Through a temporary file and a rename, then reloaded and (re)started, unless the user turned it off: then
    only reloaded. A first install that systemd won't start is removed, so the next --fix probes and tries again."""
    path = units_dir(env) / NAME
    shown = tilde(path, env)
    installed = path.exists()
    temporary = path.with_name(f".{NAME}.kb-new")
    try:
        data_dir(env).mkdir(exist_ok=True)  # the sandbox makes the rest of the home read-only, this folder included
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(unit(env, settings, system=False), encoding="utf-8")
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
