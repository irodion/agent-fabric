"""Which workspace a folder belongs to, and where that workspace keeps its notes (docs/design.md §4.11)."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_fabric.config import machine_opt_outs, workspace_roots
from agent_fabric.gitinfo import main_checkout
from agent_fabric.paths import expand, inbox_dir


@dataclass(frozen=True)
class Workspace:
    root: Path | None  # None: the default workspace

    def inbox(self, env: Mapping[str, str]) -> Path:
        return inbox_dir(env) if self.root is None else self.root / ".fabric" / "inbox"


def workspace_of(folder: Path, env: Mapping[str, str]) -> Workspace:
    """The registered root that holds the folder, the inner one where roots nest; the default workspace under none. A
    git checkout is placed by its main checkout, since some agents put worktrees outside the project."""
    physical = main_checkout(folder) or folder.resolve()
    holding = [path for root in workspace_roots(env) if physical.is_relative_to(path := expand(root, env).resolve())]
    return Workspace(max(holding, key=lambda path: len(path.parts), default=None))


def every_workspace(env: Mapping[str, str]) -> list[Workspace]:
    """The default workspace and each registered root, once."""
    roots = dict.fromkeys(expand(root, env).resolve() for root in workspace_roots(env))
    return [Workspace(None), *(Workspace(root) for root in roots)]


def machine_workspaces(workspace: Workspace, env: Mapping[str, str]) -> list[Workspace]:
    """Where a machine note written in this workspace goes, this one first: every workspace that shares machine
    notes, or this one alone where it opts out (docs/design.md §4.11). A root that isn't there, such as one on a disk
    that isn't mounted, gets no copy: kb would make the folder, and no session can run in it."""
    out = {None if root is None else expand(root, env).resolve() for root in machine_opt_outs(env)}
    if workspace.root in out:
        return [workspace]
    others = [other for other in every_workspace(env) if other != workspace and other.root not in out]
    return [workspace, *(other for other in others if other.root is None or other.root.is_dir())]


def any_inbox(env: Mapping[str, str]) -> bool:
    """Whether some workspace has its inbox: kb is set up on this machine. Reads only the config, never git, and a
    config it can't read leaves the default inbox."""
    try:
        roots = [Workspace(expand(root, env)) for root in workspace_roots(env)]
    except (OSError, ValueError):
        roots = []
    return any(workspace.inbox(env).is_dir() for workspace in (Workspace(None), *roots))


def hook_workspace(folder: Path, env: Mapping[str, str]) -> Workspace:
    """`workspace_of` for the hooks, which must not stop over a broken `[[workspace]]` entry: the default workspace,
    which needs no entry, keeps its reminder (`kb note` names the error)."""
    try:
        return workspace_of(folder, env)
    except (OSError, ValueError):
        return Workspace(None)
