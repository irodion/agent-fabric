"""Where a session's digest goes, and how it gets there (docs/design.md §4.5).

Every digest is redacted first. A project on its workspace's allow list goes to the wiki, if gitleaks finds nothing,
and is committed on `main`; one gitleaks stops goes to the quarantine, and every other project's stays in the local
folder. Writing a digest there removes its copy from the other of those two folders, and a digest committed to the
wiki removes both: a session's latest digest is in one place. A copy committed before stays in the wiki.

The wiki's commit is made in its own checkout, which is mine too, and only while that checkout is clean on `main`
with no merge, cherry-pick or revert under way: then nothing of mine is in the way, and a commit that fails leaves it
clean again. Otherwise the digest waits, and the same call later writes it."""

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from agent_fabric.config import read_config
from agent_fabric.decisions import agents_file
from agent_fabric.secretgate import redact, scan
from agent_fabric.wikirepo import MAIN, Git, GitError, ensure_wiki
from agent_fabric.workspaces import Workspace

LOG = "log.md"
UNDER_WAY = {"MERGE_HEAD": "merge", "CHERRY_PICK_HEAD": "cherry-pick", "REVERT_HEAD": "revert"}


@dataclass(frozen=True)
class Digest:
    text: str  # as `digest.render` gives it
    path: str  # where in the folder, as `digest.path` names it
    project: str
    headless: bool


@dataclass(frozen=True)
class Written:
    where: Literal["wiki", "local", "quarantine"]
    path: Path  # the file
    rules: tuple[str, ...] = ()  # the gitleaks rules that stopped it, for the quarantine
    committed: bool = False  # in the wiki: false when its last commit already held these bytes


def write(workspace: Workspace, env: Mapping[str, str], now: datetime, digest: Digest) -> Written:
    """Write the digest in the workspace's wiki, quarantine or local folder.

    Raises `secretgate.GateUnavailable` when gitleaks can't vouch for a digest bound for the wiki, `GitError` when the
    wiki can't take the commit now, ValueError when the config can't be read, and OSError when a folder can't be
    written or a link in it leads elsewhere. The same call made again later finishes the job."""
    text, project = redact(digest.text), digest.project
    config = read_config(env)
    local = _unlinked(workspace, workspace.local(env), digest.path)
    quarantine = _unlinked(workspace, workspace.quarantine(env), digest.path)
    allowed = project in config.allowed(workspace.root)
    if not allowed or (digest.headless and project not in config.headless(workspace.root)):
        _move(text, local, away=quarantine)
        return Written("local", local)
    if rules := scan(text, env):
        _move(text, quarantine, away=local)
        return Written("quarantine", quarantine, tuple(rules))
    folder = _unlinked(workspace, workspace.wiki(env), ".")
    _unlinked(workspace, folder, digest.path)
    _unlinked(workspace, folder, LOG)
    wiki = ensure_wiki(folder, env, agents_file())
    committed = _commit(wiki, digest.path, text, now.astimezone(UTC), project)
    local.unlink(missing_ok=True)
    quarantine.unlink(missing_ok=True)
    return Written("wiki", folder / digest.path, committed=committed)


def _commit(wiki: Git, relative: str, text: str, now: datetime, project: str) -> bool:
    _check_clean(wiki)
    written = [relative, LOG]
    try:
        target = wiki.folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _replace(target, text)
        if wiki.ok("cat-file", "-e", f"HEAD:{relative}") and wiki.ok("diff", "--quiet", "HEAD", "--", relative):
            return False  # byte for byte what the last commit holds, so the checkout is clean again
        name = PurePosixPath(relative).stem  # <yyyy-mm-dd>-<agent>-<shortid>, whose id has no dash
        agent, short = name[len("yyyy-mm-dd-") :].rsplit("-", 1)
        log = wiki.folder / LOG
        before = log.read_text(encoding="utf-8", newline="") if log.exists() else "# Log\n"
        _replace(log, before + f"{now:%Y-%m-%d} digest {project} {agent} {short}\n")
        wiki("add", "--", *written)
        wiki("commit", "-q", "-m", f"digest: {project} {agent} {short}", "--", *written)
    except BaseException:
        _restore(wiki, written)
        raise
    return True


def _check_clean(wiki: Git) -> None:
    """Clean on `main`, with nothing under way; GitError otherwise, naming what's in the way."""
    branch = wiki("symbolic-ref", "-q", "HEAD").strip() if wiki.ok("symbolic-ref", "-q", "HEAD") else ""
    if branch != f"refs/heads/{MAIN}":
        raise GitError(f"{wiki.folder} has {branch or 'a detached HEAD'} checked out; digests wait for {MAIN}")
    for ref, what in UNDER_WAY.items():
        if wiki.ok("rev-parse", "-q", "--verify", ref):
            raise GitError(f"a {what} is under way in {wiki.folder}; digests wait until it's done")
    if wiki("status", "--porcelain", "--untracked-files=all"):
        raise GitError(f"{wiki.folder} has changes not committed; digests wait until it's clean")


def _restore(wiki: Git, paths: list[str]) -> None:
    """Each path as the last commit has it, staged or not, or gone where it has none: the checkout was clean."""
    wiki.ok("reset", "-q", "--", *paths)
    for path in paths:
        if wiki.ok("cat-file", "-e", f"HEAD:{path}"):
            wiki.ok("checkout", "-q", "HEAD", "--", path)
        else:
            (wiki.folder / path).unlink(missing_ok=True)


def _unlinked(workspace: Workspace, folder: Path, relative: str) -> Path:
    """`relative` in `folder`, refused where a link on the way would lead elsewhere: a root's `.fabric/` is open to
    its agents. Checked from the root down, or from the folder itself in the default workspace."""
    target = folder / relative
    base = workspace.root if workspace.root is not None and folder.is_relative_to(workspace.root) else folder
    current = base
    for part in target.relative_to(base).parts:
        current /= part
        if current.is_symlink():
            raise OSError(f"{current} is a link, which kb won't follow")
    return target


def _move(text: str, target: Path, *, away: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    _replace(target, text)
    away.unlink(missing_ok=True)


def _replace(target: Path, text: str) -> None:
    """Written whole or not at all, for a reader such as `kb` that may open it at any time."""
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
