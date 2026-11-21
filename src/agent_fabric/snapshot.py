"""A git snapshot of a session's folder, for its digest's changes, commits and markers (docs/design.md §4.4).

Read-only: git runs with `--no-optional-locks`, so not even a refreshed index is written to the user's repo. Only
plumbing commands run (`diff-index`, `rev-list`, `ls-files`), which leave out the settings meant for a person at a
terminal: colours, prefixes, external diffs, text converters, signature checks, quoting. What could still run a
program of the repo's, a filesystem monitor, a clean filter or a submodule's status, is turned off. That keeps a
snapshot quick and free of side effects; it isn't a boundary, since the agent already runs code in the same folder.

A snapshot is taken when a session is first seen and at each of its turn ends; the first one's HEAD is the base the
later ones are measured from, so a digest's changes are the working tree's, start to end, whoever made them.
"""

import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

TIMEOUT = 20.0
MAX_DIFF = 2_000_000  # characters kept of a diff; a generated file can make one of any size
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
    base: str  # the HEAD when the session was first seen
    head: str
    branch: str | None  # None on a detached HEAD
    worktree: Path | None  # the folder, when it's a linked worktree
    main: Path | None  # then, the checkout that owns it
    files: tuple[str, ...]  # changed from base to the working tree, and untracked
    stat: str  # one line per changed file, with its lines added and removed, then the totals
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
    if found is None or len(lines := found.splitlines()) < 2:
        return None
    top, head = Path(lines[0]), lines[1]
    # No filter's clean command runs on the working tree's files: set each filter the config names to nothing.
    names = _git(top, (), "config", "--null", "--name-only", "--get-regexp", r"^filter\.") or ""
    filters = sorted({name.split(".", 1)[1].rsplit(".", 1)[0] for name in names.split("\0") if name})
    off = tuple(
        arg
        for name in filters
        for key, value in (("clean", ""), ("process", ""), ("required", "false"))
        for arg in ("-c", f"filter.{name}.{key}={value}")
    )
    base = base or head
    if _git(top, off, "cat-file", "-e", f"{base}^{{commit}}") is None:
        base = head  # rewritten away since the first snapshot: measure from where the folder is now
    dirs = (_git(top, off, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir") or "").splitlines()
    linked = len(dirs) >= 2 and dirs[0] != dirs[1]
    bare = (_git(top, off, "config", "--bool", "core.bare") or "").strip() == "true"
    branch = _git(top, off, "symbolic-ref", "--quiet", "--short", "HEAD")
    diff = _git(top, off, "diff-index", *DIFF, base) or ""
    changed = parse(diff)
    since_head = changed if base == head else parse(_git(top, off, "diff-index", *DIFF, "HEAD") or "")
    log = _git(top, off, "rev-list", "--reverse", "--no-commit-header", "--format=%h %s", f"{base}..HEAD") or ""
    untracked = tuple(_split(_git(top, off, "ls-files", "-z", "--others", "--exclude-standard")))
    return Snapshot(
        at=at,
        base=base,
        head=head,
        branch=branch.strip() if branch else None,
        worktree=top if linked else None,
        main=Path(dirs[1]).parent if linked and not bare else None,  # a bare repo is no checkout
        files=tuple(sorted({*(f.path for f in changed), *untracked})),
        stat=_stat(changed),
        diff=diff[:MAX_DIFF],
        diff_cut=len(diff) > MAX_DIFF,
        untracked=untracked,
        commits=tuple(log.splitlines()),
        uncommitted=bool(untracked or since_head),
    )


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
            path = _unquote(line[4:]).removeprefix("b/")
        elif header and line.startswith("--- ") and line != "--- /dev/null":
            path = _unquote(line[4:]).removeprefix("a/")
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
