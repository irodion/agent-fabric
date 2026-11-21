"""A git snapshot of a session's folder, for its digest's changes, commits and markers (docs/design.md §4.4).

Read-only: git runs with `--no-optional-locks`, so not even a refreshed index is written to the user's repo. A
snapshot is taken when a session is first seen and at each of its turn ends; the first one's HEAD is the base the
later ones are measured from, so a digest's changes are the working tree's, start to end, whoever made them.
"""

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

TIMEOUT = 20.0
MAX_DIFF = 2_000_000  # characters kept of a diff; a generated file can make one of any size
# Paths as they are, not quoted; and no command a repo's config names runs (a filesystem monitor, an external diff).
SAFE = ("-c", "core.quotepath=off", "-c", "core.fsmonitor=false", "-c", "diff.external=")
# What would point git at another repo than the folder's, as a hook's caller sets (`git rev-parse --local-env-vars`).
REPO_VARIABLES = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_GRAFT_FILE",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INTERNAL_SUPER_PREFIX",
        "GIT_NO_REPLACE_OBJECTS",
    }
)


@dataclass(frozen=True)
class Snapshot:
    at: datetime
    base: str  # the HEAD when the session was first seen
    head: str
    branch: str | None  # None on a detached HEAD
    worktree: Path | None  # the folder, when it's a linked worktree
    main: Path | None  # then, the checkout that owns it
    files: tuple[str, ...]  # changed from base to the working tree, and untracked
    stat: str  # `git diff --stat` from base to the working tree
    diff: str  # the same, `--unified=0`, cut at MAX_DIFF
    diff_cut: bool
    untracked: tuple[str, ...]
    commits: tuple[str, ...]  # `<short sha> <subject>` made since base, oldest first
    uncommitted: bool  # the working tree differs from HEAD, or has untracked files


def take(cwd: Path, at: datetime, base: str | None = None) -> Snapshot | None:
    """The folder's git state, measured from `base` (the first snapshot's HEAD); None outside a repo with a commit."""
    head = _git(cwd, "rev-parse", "HEAD")
    if head is None:
        return None
    head = head.strip()
    base = base or head
    if _git(cwd, "cat-file", "-e", f"{base}^{{commit}}") is None:
        base = head  # rewritten away since the first snapshot: measure from where the folder is now
    dirs = (_git(cwd, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir") or "").split("\n")
    linked = len(dirs) >= 2 and dirs[0] != dirs[1]
    branch = _git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
    diff = _git(cwd, "diff", "--no-color", "--no-ext-diff", "--unified=0", base) or ""
    log = _git(cwd, "log", "--reverse", "--no-color", "--format=%h %s", f"{base}..HEAD") or ""
    untracked = tuple(_lines(_git(cwd, "ls-files", "--others", "--exclude-standard")))
    changed = _lines(_git(cwd, "diff", "--no-color", "--name-only", base))
    return Snapshot(
        at=at,
        base=base,
        head=head,
        branch=branch.strip() if branch else None,
        worktree=cwd if linked else None,
        main=Path(dirs[1]).parent if linked else None,
        files=tuple(sorted({*changed, *untracked})),
        stat=(_git(cwd, "diff", "--no-color", "--stat=120", base) or "").rstrip("\n"),
        diff=diff[:MAX_DIFF],
        diff_cut=len(diff) > MAX_DIFF,
        untracked=untracked,
        commits=tuple(_lines(log)),
        uncommitted=bool(untracked) or bool(_lines(_git(cwd, "diff", "--no-color", "--name-only", "HEAD"))),
    )


def _lines(out: str | None) -> list[str]:
    return [line for line in (out or "").split("\n") if line]


def _git(cwd: Path, *args: str) -> str | None:
    """Its output as it is, or None when git fails or can't run."""
    try:
        done = subprocess.run(
            ["git", "--no-optional-locks", *SAFE, *args],
            cwd=cwd,
            env={key: value for key, value in os.environ.items() if key not in REPO_VARIABLES},
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None
