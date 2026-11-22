"""Where a session's digest goes, and how it gets there (docs/design.md §4.5).

Every digest is redacted first. A project on its workspace's allow list goes to the wiki, if gitleaks finds nothing,
and is committed on `main`; one gitleaks stops goes to the quarantine, and every other project's stays in the local
folder. Writing a digest there removes its copy from the other of those two folders, and a digest committed to the
wiki removes both: a session's latest digest is in one place. A copy committed before stays in the wiki.

The wiki's commit is made in its own checkout, which is mine too, and only while that checkout is clean on `main`
with nothing under way, such as a merge or a rebase: then nothing of mine is in the way, and a commit that fails
leaves it clean again. Otherwise the digest waits, and the same call later writes it.

A root's local digests and quarantine are in kb's data folder, out of the root's `.fabric/`, which an agent started
at the root can write. Its wiki is there, so every folder on the way to a file in it is checked for links."""

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
UNDER_WAY = {  # what git keeps while each is under way, in the repo's git folder
    "MERGE_HEAD": "merge",
    "CHERRY_PICK_HEAD": "cherry-pick",
    "REVERT_HEAD": "revert",
    "sequencer": "cherry-pick or revert",
    "rebase-merge": "rebase",
    "rebase-apply": "rebase or git am",
    "BISECT_LOG": "bisect",
}


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
    # The checkout is clean, so these are the last commit's bytes: what a failure puts back.
    before = {path: (wiki.folder / path).read_bytes() if (wiki.folder / path).exists() else None for path in written}
    try:
        target = wiki.folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _replace(target, text)
        if _at_head(wiki, relative) == wiki("hash-object", "--", relative).strip():
            return False  # what the last commit holds, as git would store it: the checkout is clean again
        name = PurePosixPath(relative).stem  # <yyyy-mm-dd>-<agent>-<shortid>, whose id has no dash
        agent, short = name[len("yyyy-mm-dd-") :].rsplit("-", 1)
        log = (before[LOG] or b"# Log\n").decode("utf-8", "surrogateescape")
        if not log.endswith("\n"):
            log += "\n"
        _replace(wiki.folder / LOG, log + f"{now:%Y-%m-%d} digest {project} {agent} {short}\n")
        wiki("add", "--", *written)
        wiki("commit", "-q", "-m", f"digest: {project} {agent} {short}", "--", *written)
    except BaseException as e:
        if (left := _restore(wiki, before)) and isinstance(e, Exception):
            raise GitError(f"{e}; and kb couldn't undo its change to {left} in {wiki.folder}: restore it") from e
        raise
    return True


def _at_head(wiki: Git, relative: str) -> str | None:
    """The blob id the last commit has for the file; None where it has none."""
    found = ("rev-parse", "-q", "--verify", f"HEAD:{relative}")
    return wiki(*found).strip() if wiki.ok(*found) else None


def _check_clean(wiki: Git) -> None:
    """Clean on `main`, with nothing under way; GitError otherwise, naming what's in the way."""
    branch = wiki("symbolic-ref", "-q", "HEAD").strip() if wiki.ok("symbolic-ref", "-q", "HEAD") else ""
    if branch != f"refs/heads/{MAIN}":
        raise GitError(f"{wiki.folder} has {branch or 'a detached HEAD'} checked out; digests wait for {MAIN}")
    found = wiki("rev-parse", *(arg for name in UNDER_WAY for arg in ("--git-path", name))).splitlines()
    for name, path in zip(UNDER_WAY, found, strict=True):
        if (wiki.folder / path).exists():
            raise GitError(f"a {UNDER_WAY[name]} is under way in {wiki.folder}; digests wait until it's done")
    if wiki("status", "--porcelain", "--untracked-files=all"):
        raise GitError(f"{wiki.folder} has changes not committed; digests wait until it's clean")


def _restore(wiki: Git, before: dict[str, bytes | None]) -> str:
    """Each file back as it was, and unstaged; what couldn't be undone, if anything."""
    left = []
    for path, content in before.items():
        try:
            if content is None:
                (wiki.folder / path).unlink(missing_ok=True)
            else:
                (wiki.folder / path).write_bytes(content)
        except OSError:
            left.append(path)
    if not wiki.ok("reset", "-q", "--", *before):
        left.append("what it staged")
    return ", ".join(left)


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
