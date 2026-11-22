"""Where a session's digest goes, and how it gets there (docs/design.md §4.5).

Every digest is redacted first. A project on its workspace's allow list goes to the wiki, if gitleaks finds nothing,
and is committed on `main`; one gitleaks stops goes to the quarantine, and every other project's stays in the local
folder. A digest lives in one place: writing it removes its copy from the local folder or the quarantine.

The wiki's commit is made in its own checkout, which is mine too: kb writes the digest and `log.md`, and commits those
two paths alone, so anything else I have staged or changed there stays as it was. It commits only while `main` is
checked out, and git refuses a commit of some paths mid-merge, so it waits out a merge of an ingest branch."""

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from agent_fabric.config import read_config
from agent_fabric.decisions import agents_file
from agent_fabric.secretgate import redact, scan
from agent_fabric.wikirepo import MAIN, Git, GitError, ensure_wiki
from agent_fabric.workspaces import Workspace

LOG = "log.md"


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
    wiki can't take the commit now, and ValueError when the config can't be read: nothing is written then, and the
    same call can be made again later."""
    text, relative, project = redact(digest.text), digest.path, digest.project
    config = read_config(env)
    allowed = project in config.allowed(workspace.root)
    if not allowed or (digest.headless and project not in config.headless(workspace.root)):
        return Written("local", _put(workspace.local(env), relative, text, also=workspace.quarantine(env)))
    if rules := scan(text, env):
        where = _put(workspace.quarantine(env), relative, text, also=workspace.local(env))
        return Written("quarantine", where, tuple(rules))
    wiki = ensure_wiki(workspace.wiki(env), env, agents_file())
    committed = _commit(wiki, relative, text, now, project)
    for folder in (workspace.local(env), workspace.quarantine(env)):
        _inside(folder, relative).unlink(missing_ok=True)
    return Written("wiki", wiki.folder / relative, committed=committed)


def _commit(wiki: Git, relative: str, text: str, now: datetime, project: str) -> bool:
    branch = wiki("symbolic-ref", "-q", "HEAD").strip() if wiki.ok("symbolic-ref", "-q", "HEAD") else ""
    if branch != f"refs/heads/{MAIN}":
        raise GitError(f"{wiki.folder} has {branch or 'a detached HEAD'} checked out; digests wait for {MAIN}")
    if _at_head(wiki, relative) == text:
        return False
    name = PurePosixPath(relative).stem  # <yyyy-mm-dd>-<agent>-<shortid>, whose id has no dash
    agent, short = name[len("yyyy-mm-dd-") :].rsplit("-", 1)
    # From the last commit's log, so a commit that failed after writing it doesn't log the digest twice.
    log = (_at_head(wiki, LOG) or "# Log\n") + f"{now:%Y-%m-%d} digest {project} {agent} {short}\n"
    _replace(_inside(wiki.folder, relative), text)
    _replace(_inside(wiki.folder, LOG), log)
    wiki("add", "--", relative, LOG)
    wiki("commit", "-q", "-m", f"digest: {project} {agent} {short}", "--", relative, LOG)
    return True


def _at_head(wiki: Git, relative: str) -> str | None:
    """The file as the last commit has it; None where it has none."""
    if not wiki.ok("cat-file", "-e", f"HEAD:{relative}"):
        return None
    return wiki("cat-file", "blob", f"HEAD:{relative}")


def _put(folder: Path, relative: str, text: str, *, also: Path) -> Path:
    """The digest in `folder`, and no longer in `also`."""
    target = _inside(folder, relative)
    _replace(target, text)
    _inside(also, relative).unlink(missing_ok=True)
    return target


def _inside(folder: Path, relative: str) -> Path:
    """`relative` in `folder`, refused where a link would take it out: a root's `.fabric/` is open to its agents."""
    target = folder / relative
    if folder.is_symlink() or not target.parent.resolve().is_relative_to(folder.resolve()):
        raise OSError(f"{target} leads out of {folder}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise OSError(f"{target} leads out of {folder}")
    return target


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
