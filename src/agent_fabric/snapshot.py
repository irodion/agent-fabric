"""A git snapshot of a session's folder, for its digest's changes, commits and markers (docs/design.md §4.4).

Read-only: git runs with `--no-optional-locks`, so not even a refreshed index is written to the user's repo. Only
plumbing commands run (`diff-index`, `rev-list`, `ls-files`), which leave out the settings meant for a person at a
terminal: colours, prefixes, external diffs, text converters, signature checks, quoting. The filesystem monitor and
submodules' status are off too. That keeps a snapshot quick and free of side effects; it isn't a boundary, since the
agent already runs code in the same folder, so a repo's clean filters (git-lfs's) still run as git runs them.

The session's commits are those committed since it was first seen, and its changes are measured from the parent of
the earliest of them, or from HEAD: so a rebase, pull or amend during the session needs no base carried between
snapshots. A snapshot is taken when a session is first seen and at each of its turn ends.
"""

import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

TIMEOUT = 20.0
MAX_DIFF = 2_000_000  # characters kept of a diff; a generated file can make one of any size
MAX_COMMITS = 500
SAFE = ("-c", "core.quotepath=off", "-c", "core.fsmonitor=false")
DIFF = ("--no-color", "--no-renames", "--ignore-submodules=dirty", "-p", "--unified=0")
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
CHANGE = re.compile(r"(?:@@ |Binary files |new file mode |deleted file mode |old mode |Submodule )")
ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


@dataclass(frozen=True)
class FileDiff:
    path: str
    added: int
    removed: int
    lines: tuple[str, ...]  # its hunks: `@@` lines, and lines added (`+`) and removed (`-`)


@dataclass(frozen=True)
class Snapshot:
    at: datetime
    base: str  # what the changes are measured from: the parent of the session's first commit, or HEAD
    head: str
    branch: str | None  # None on a detached HEAD
    worktree: Path | None  # the folder, when it's a linked worktree
    main: Path | None  # then, the checkout that owns it
    files: tuple[str, ...]  # changed from base to the working tree, and untracked
    stat: str  # one line per changed file, with its lines added and removed, then the totals
    diff: str  # the same, `--unified=0`, cut at MAX_DIFF
    diff_cut: bool
    untracked: tuple[str, ...]
    commits: tuple[str, ...]  # `<short sha> <subject>` committed since the session was first seen, oldest first
    commits_unlisted: int  # made since then too, but older than the MAX_COMMITS listed
    uncommitted: bool  # the working tree differs from HEAD, or has untracked files


def take(cwd: Path, at: datetime, since: datetime) -> Snapshot | None:
    """The folder's git state, with the commits made since `since` (the session's first sight); None outside a repo
    with a commit.

    Git runs from the repo's top folder, so a session in a subfolder gets the whole repo's changes, by their paths
    from the top."""
    found = _git(cwd, "rev-parse", "--show-toplevel", "HEAD")
    if found is None or len(lines := found.split("\n")) < 2:
        return None
    top, head = Path(lines[0]), lines[1]
    since = since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    made = _git(
        top,
        "rev-list",
        "--no-commit-header",
        f"--since={stamp}",
        "--format=%P%x00%h %s",
        "HEAD",
    )
    # All of them, oldest first, for the base; the digest names the newest MAX_COMMITS.
    commits = [line.split("\0", 1) for line in (made or "").split("\n") if "\0" in line][::-1]
    base = head
    if commits:
        parents = commits[0][0].split()
        base = parents[0] if parents else (_git(top, "hash-object", "-t", "tree", "/dev/null") or "").strip()
    dirs = (_git(top, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir") or "").split("\n")
    linked = len(dirs) >= 2 and dirs[0] != dirs[1]
    branch = _git(top, "symbolic-ref", "--quiet", "--short", "HEAD")
    diff = _git(top, "diff-index", *DIFF, base) or ""
    changed = parse(diff)
    since_head = changed if base == head else parse(_git(top, "diff-index", *DIFF, "HEAD") or "")
    untracked = tuple(_split(_git(top, "ls-files", "-z", "--others", "--exclude-standard")))
    return Snapshot(
        at=at,
        base=base,
        head=head,
        branch=branch.strip() if branch else None,
        worktree=top if linked else None,
        main=_main(top) if linked else None,
        files=tuple(sorted({*(f.path for f in changed), *untracked})),
        stat=_stat(changed),
        diff=diff[:MAX_DIFF],
        diff_cut=len(diff) > MAX_DIFF,
        untracked=untracked,
        commits=tuple(subject for _, subject in commits[-MAX_COMMITS:]),
        commits_unlisted=max(len(commits) - MAX_COMMITS, 0),
        uncommitted=bool(untracked or since_head),
    )


def _main(top: Path) -> Path | None:
    """The main checkout, the first `git worktree list` gives. None when that's no checkout: a bare repo, or the store
    of one cloned with `--separate-git-dir`, which git lists as it has no record of the checkout."""
    listed = (_git(top, "worktree", "list", "--porcelain", "-z") or "").split("\0")
    if not listed[0].startswith("worktree ") or "bare" in listed[1:2]:
        return None
    main = Path(listed[0].removeprefix("worktree "))
    return main if (main / ".git").exists() else None


def parse(diff: str) -> list[FileDiff]:
    """A `diff-index -p` diff by file, leaving out files that only look changed: their timestamps moved, and the
    index wasn't refreshed, since a snapshot writes nothing."""
    return [found for found in _files(diff) if found.changed]


@dataclass(frozen=True)
class _Found(FileDiff):
    changed: bool


def _files(diff: str) -> Iterator[_Found]:
    path, lines, changed = "", list[str](), False
    header = False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            if path:
                yield _found(path, lines, changed)
            path, lines, changed, header = _git_header_path(line), [], False, True
        elif header and line.startswith("+++ ") and line != "+++ /dev/null":
            path = _unquote(line[4:].removesuffix("\t")).removeprefix("b/")  # a tab ends a name with a space
        elif header and line.startswith("--- ") and line != "--- /dev/null":
            path = _unquote(line[4:].removesuffix("\t")).removeprefix("a/")
        else:
            if CHANGE.match(line):
                changed = True
            if line.startswith("@@ "):
                header = False
            if not header:
                lines.append(line)
    if path:
        yield _found(path, lines, changed)


def _found(path: str, lines: list[str], changed: bool) -> _Found:
    added = sum(line.startswith("+") for line in lines)
    removed = sum(line.startswith("-") for line in lines)
    return _Found(path, added, removed, tuple(lines), changed)


def _git_header_path(line: str) -> str:
    """The new path of `diff --git a/<old> b/<new>`, for a file whose diff has no `---`/`+++` lines (binary, mode)."""
    rest = line.removeprefix("diff --git ")
    if rest.endswith('"'):
        return _unquote(rest[rest.rindex(' "') + 1 :]).removeprefix("b/")
    return rest.rsplit(" b/", 1)[-1]


def _unquote(name: str) -> str:
    """A path as git quotes one with `"`, `\\` or a control character in it: C escapes, octal for raw bytes."""
    if not (name.startswith('"') and name.endswith('"')):
        return name
    out = bytearray()
    text, index = name[1:-1], 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            if following in ESCAPES:
                out.append(ESCAPES[following])
                index += 2
                continue
            if text[index + 1 : index + 4].isdigit():
                out.append(int(text[index + 1 : index + 4], 8))
                index += 4
                continue
        out += char.encode()
        index += 1
    return out.decode(errors="replace")


def _stat(changed: list[FileDiff]) -> str:
    if not changed:
        return ""
    width = min(max(len(found.path) for found in changed), 80)
    rows = [f" {found.path:<{width}} | +{found.added} -{found.removed}" for found in changed]
    added, removed = sum(f.added for f in changed), sum(f.removed for f in changed)
    return "\n".join([*rows, f" {len(changed)} files changed, {added} insertions(+), {removed} deletions(-)"])


def _split(out: str | None) -> list[str]:
    return [name for name in (out or "").split("\0") if name]


def _git(cwd: Path, *args: str) -> str | None:
    """Its output as it is, or None when git fails or can't run. Read as bytes, so a carriage return in a line or a
    name stays in it rather than ending the line."""
    try:
        done = subprocess.run(
            ["git", "--no-optional-locks", *SAFE, *args],
            cwd=cwd,
            env={key: value for key, value in os.environ.items() if key not in REPO_VARIABLES},
            capture_output=True,
            check=False,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.decode(errors="replace") if done.returncode == 0 else None
