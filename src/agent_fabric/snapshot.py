"""A git snapshot of a session's folder, for its digest's changes, commits and markers (docs/design.md §4.4).

Read-only: git runs with `--no-optional-locks`, so not even a refreshed index is written to the user's repo, and no
filesystem monitor, external diff, text converter or clean filter the repo names runs. A
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
# A diff as git makes it, whatever the user's config says: no colour, converter or external tool, and every path from
# the top folder.
DIFF = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-relative")
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
    """The folder's git state, measured from `base` (the first snapshot's HEAD); None outside a repo with a commit.

    Git runs from the repo's top folder, so a session in a subfolder gets the whole repo's changes, by their paths
    from the top."""
    found = _git(cwd, (), "rev-parse", "--show-toplevel", "HEAD")
    if found is None or len(lines := _lines(found)) < 2:
        return None
    top, head = Path(lines[0]), lines[1]
    # No filter's clean command runs on the working tree's files: set each filter the config names to nothing.
    filters = {
        name.split(".", 1)[1].rsplit(".", 1)[0]
        for name in _lines(_git(top, (), "config", "--name-only", "--get-regexp", r"^filter\."))
    }
    off = tuple(
        arg
        for name in sorted(filters)
        for arg in (
            "-c",
            f"filter.{name}.clean=",
            "-c",
            f"filter.{name}.process=",
            "-c",
            f"filter.{name}.required=false",
        )
    )
    base = base or head
    if _git(top, off, "cat-file", "-e", f"{base}^{{commit}}") is None:
        base = head  # rewritten away since the first snapshot: measure from where the folder is now
    dirs = _lines(_git(top, off, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"))
    linked = len(dirs) >= 2 and dirs[0] != dirs[1]
    branch = _git(top, off, "symbolic-ref", "--quiet", "--short", "HEAD")
    diff = _git(top, off, "diff", *DIFF, "--unified=0", "--src-prefix=a/", "--dst-prefix=b/", base) or ""
    log = _git(top, off, "log", "--reverse", "--no-color", "--format=%h %s", f"{base}..HEAD") or ""
    untracked = tuple(_lines(_git(top, off, "ls-files", "--others", "--exclude-standard")))
    changed = _lines(_git(top, off, "diff", *DIFF, "--name-only", base))
    return Snapshot(
        at=at,
        base=base,
        head=head,
        branch=branch.strip() if branch else None,
        worktree=top if linked else None,
        main=Path(dirs[1]).parent if linked else None,
        files=tuple(sorted({*changed, *untracked})),
        stat=(_git(top, off, "diff", *DIFF, "--stat=120", base) or "").rstrip("\n"),
        diff=diff[:MAX_DIFF],
        diff_cut=len(diff) > MAX_DIFF,
        untracked=untracked,
        commits=tuple(_lines(log)),
        uncommitted=bool(untracked) or bool(_lines(_git(top, off, "diff", *DIFF, "--name-only", "HEAD"))),
    )


def _lines(out: str | None) -> list[str]:
    return [line for line in (out or "").split("\n") if line]


def _git(cwd: Path, config: tuple[str, ...], *args: str) -> str | None:
    """Its output as it is, or None when git fails or can't run."""
    try:
        done = subprocess.run(
            ["git", "--no-optional-locks", *SAFE, *config, *args],
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
