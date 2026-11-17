"""`kb setup`: the choices of docs/design.md §4.11, asked in the user's own terminal, then applied through
`kb doctor --fix`. It asks through an `Asker`, so tests can answer for a person; `questionary` is loaded only by the
real one, so hooks and notes never import it."""

import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from agent_fabric.backends import BACKENDS
from agent_fabric.config import HOOKS, Config, Settings, check_editable, firsts, read_config, record_workspaces
from agent_fabric.context import Context
from agent_fabric.doctor import doctor
from agent_fabric.gitinfo import main_checkout, project_id
from agent_fabric.inbox import Inbox
from agent_fabric.notes import MACHINE
from agent_fabric.paths import config_file, expand, tilde
from agent_fabric.workspaces import Workspace, innermost

DEPTH = 3  # how far below a root to look for projects; a deeper one is named by its folder
SKIPPED = {"node_modules"}
HOOK_CHOICES = {"start and stop": HOOKS, "start": ("start",), "stop": ("stop",), "none": ()}


QUIT = 130  # kb setup's exit where the user left, as for Ctrl-C: install.sh stops too
UNCHANGED = 3  # where it can't read or edit the config: install.sh then runs kb doctor --fix, so that a new kb still
# updates the skill copies and hooks, and its report names the problem


class Quit(Exception):
    """The user left before the last step: nothing is written."""


class NothingChanged(Exception):
    """The config can't be read, or edited safely: nothing is written."""


class Asker(Protocol):
    """Each question opens on what's recorded, so Enter keeps it. Raises `Quit` where the user leaves."""

    def checkbox(self, message: str, choices: list[str], checked: list[str]) -> list[str]: ...

    def select(self, message: str, choices: list[str], default: str) -> str: ...

    def confirm(self, message: str, default: bool) -> bool: ...

    def text(self, message: str, validate: Callable[[str], str | None]) -> str:
        """Free text; `validate` returns why an answer won't do, or None."""
        ...

    def tell(self, message: str) -> None:
        """A choice made without asking, where there's none to make."""
        ...


def _not_a_folder(typed: str, env: Mapping[str, str]) -> str | None:
    """Why a typed folder won't do, or None: it's a full path, or one in the home folder, not one relative to wherever
    kb happens to run."""
    folder = expand(typed.strip(), env)
    if not folder.is_absolute():
        return "a full path, or one that starts with ~/"
    if not folder.is_dir():
        return f"{typed.strip()} isn't a folder"
    return None


def _new_root(typed: str, roots: list[str], env: Mapping[str, str]) -> str | None:
    """Why a folder can't be a new root, or None."""
    if problem := _not_a_folder(typed, env):
        return problem
    folder = expand(typed.strip(), env)
    if main_checkout(folder) is not None:
        return "it's in a git work tree: a root holds repos and is none itself"
    if any(expand(root, env).resolve() == folder.resolve() for root in roots):
        return "it's a root already"
    return None


def _roots(asker: Asker, config: Config) -> list[str]:
    """Step 1: the registered roots to keep, and folders to add. A folder with two entries, written two ways, is asked
    about once, by its first entry, which is the one kb reads; keeping it keeps both."""
    env = config.env
    spelled = config.firsts
    registered = list(dict.fromkeys(spelled))
    kept = asker.checkbox("Workspaces: keep which roots?", registered, registered) if registered else []
    roots = [root for root, first in zip(config.roots, spelled, strict=True) if first in kept]

    def check(typed: str) -> str | None:
        return _new_root(typed, roots, env) if typed.strip() else None

    while typed := asker.text("A folder to add as a new root (Enter for none)", check).strip():
        roots.append(tilde(expand(typed, env).resolve(), env))
    return roots


def _name(root: str | None) -> str:
    return "default workspace" if root is None else root


def _projects_below(root: Path, roots: list[Path], env: Mapping[str, str]) -> list[str]:
    """The git projects a root owns: below it, not in a hidden folder, not under a nested root, and with their main
    checkout in this workspace."""
    found = []
    for folder, dirs, _ in os.walk(root):
        here = Path(folder)
        if here != root and (here / ".git").exists():
            # a linked worktree, or a repo whose git dir is elsewhere, goes where its main checkout is
            if (here / ".git").is_dir() or innermost(here, roots) == root:
                found.append(project_id(here, env))
            dirs.clear()
            continue
        depth = len(here.relative_to(root).parts)
        dirs[:] = [
            d
            for d in sorted(dirs)
            if depth < DEPTH and not d.startswith(".") and d not in SKIPPED and (here / d).resolve() not in roots
        ]
    return found


def _with_notes(roots: list[Path], env: Mapping[str, str]) -> list[str]:
    """The projects with notes in the default workspace's inbox, but those written under one of these roots, which
    are in that root's workspace now. By the folder the note was written in, without running git for each."""
    notes = Inbox(Workspace(None).inbox(env)).read(with_done=True).notes
    return [note.scope for _, note in notes if note.scope != MACHINE and not _under(expand(note.cwd, env), roots)]


def _under(folder: Path, roots: list[Path]) -> bool:
    """Whether a note's folder is under one of these roots: a note is untrusted, so a folder that isn't a full path
    is under none, not resolved against wherever kb runs."""
    return folder.is_absolute() and any(folder.resolve().is_relative_to(root) for root in roots)


def _allow(
    asker: Asker, root: str | None, roots: list[str], recorded: Settings, env: Mapping[str, str]
) -> tuple[str, ...]:
    """Step 2: a workspace's allow list."""
    paths = [expand(r, env).resolve() for r in roots]
    here = None if root is None else expand(root, env).resolve()
    found = _with_notes(paths, env) if here is None else _projects_below(here, paths, env)
    offered = [*sorted((set(found) | set(recorded.allow)) - {MACHINE}), MACHINE]
    picked = asker.checkbox(f"{_name(root)}: projects to allow", offered, list(recorded.allow))
    # in the recorded order, then the new ones: the list may come back in the order it was shown
    allow = [p for p in recorded.allow if p in picked] + [p for p in picked if p not in recorded.allow]

    def check(typed: str) -> str | None:
        if not typed.strip():
            return None
        if problem := _not_a_folder(typed, env):
            return problem
        return None if innermost(expand(typed.strip(), env), paths) == here else "it's in another workspace"

    while typed := asker.text(f"{_name(root)}: another project folder to allow (Enter for none)", check).strip():
        if (project := project_id(expand(typed, env), env)) not in allow:
            allow.append(project)
    return tuple(allow)


def _backend(asker: Asker, root: str | None, recorded: Settings, env: Mapping[str, str], *, written: bool) -> str:
    """Step 3: a workspace's backend, among those installed, `codex` first, and the one recorded; named, not asked,
    where there's no choice, and named as missing where none is installed."""
    installed = [backend for backend in BACKENDS if shutil.which(backend, path=env.get("PATH"))]
    if not installed:
        asker.tell(
            f"{_name(root)}: kb ingest uses {recorded.backend}, but no backend kb has is installed"
            f" ({', '.join(BACKENDS)})"
        )
        return recorded.backend
    if installed == [recorded.backend]:
        asker.tell(f"{_name(root)}: kb ingest uses {recorded.backend}, the one backend kb has that's installed")
        return recorded.backend
    offered = list(dict.fromkeys([*installed, recorded.backend]))
    # Enter keeps a backend that's written; codex, which stands where none is, gives way to one that's installed.
    default = recorded.backend if written or recorded.backend in installed else installed[0]
    return asker.select(f"{_name(root)}: backend for kb ingest", offered, default)


def _workspace(asker: Asker, root: str | None, roots: list[str], config: Config) -> Settings:
    """Steps 2 and 3 for one workspace, each opening on what's recorded: for a root, by its folder, however the entry
    writes it, so that a root removed and added again under another spelling keeps its settings."""
    env = config.env
    folder = None if root is None else expand(root, env).resolve()
    known = folder is None or folder in config.folders
    recorded = config.settings(folder) if known else Settings()
    return Settings(
        _allow(asker, root, roots, recorded, env),
        _backend(asker, root, recorded, env, written=known and config.backend_written(folder)),
        asker.confirm(f"{_name(root)}: share machine notes with the other workspaces?", recorded.share_machine_notes),
    )


def setup(ctx: Context, asker: Asker) -> tuple[int, str]:
    """Ask every choice, then write them and run `kb doctor --fix`, returning its exit code and report. Raises `Quit`
    where the user leaves, and `NothingChanged` where the config can't be read or edited safely."""
    env = ctx.env
    try:
        config = read_config(env)
        check_editable(config)
    except (OSError, ValueError) as e:  # TOMLDecodeError too
        reason = getattr(e, "strerror", None) or e
        raise NothingChanged(f"can't read {tilde(config_file(env), env)}: {reason}") from e
    roots = _roots(asker, config)
    chosen = {root: _workspace(asker, root, roots, config) for root in (None, *dict.fromkeys(firsts(roots, env)))}
    recorded_hooks = config.hooks()
    label = next(label for label, hooks in HOOK_CHOICES.items() if hooks == recorded_hooks)
    hooks = HOOK_CHOICES[asker.select("Hooks: which reminders should agents get?", list(HOOK_CHOICES), label)]
    if not asker.confirm("Write these choices and run kb doctor --fix?", True):
        raise Quit
    changed_hooks = hooks if hooks != recorded_hooks else None  # kept: nothing to record
    try:
        record_workspaces(env, roots, chosen)
    except (OSError, ValueError) as e:  # TOMLDecodeError too
        raise NothingChanged(getattr(e, "strerror", None) or e) from e
    return doctor(env, ctx.now, fix=True, hooks=changed_hooks)
