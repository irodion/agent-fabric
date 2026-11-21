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
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

TIMEOUT = 20.0
MAX_DIFF = 2_000_000  # bytes read of a diff; a generated file can make one of any size, so git stops past it
MAX_COMMITS = 500
SAFE = ("-c", "core.quotepath=off", "-c", "core.fsmonitor=false")
CHANGED = ("--no-renames", "--ignore-submodules=dirty")
DIFF = ("--no-color", *CHANGED, "-p", "--unified=0")
# Per file, lines added and removed: no hunks, so as small as the list of files, and no file that only looks
# changed (its timestamp moved, the index not refreshed), as `--quiet` would count.
NUMSTAT = (*CHANGED, "--numstat", "-z")
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
# What would change the diff's form: context lines instead of `--unified=0`, another program making it.
DIFF_VARIABLES = frozenset({"GIT_DIFF_OPTS", "GIT_EXTERNAL_DIFF"})
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
    with a commit, or when git can't read it (a clean filter that fails, a timeout): an empty read would look clean.

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
        "--first-parent",
        "--no-commit-header",
        f"--since={stamp}",
        "--format=%P%x00%h %s",
        "HEAD",
    )
    if made is None:
        return None
    # All of them, oldest first, for the base; the digest names the newest MAX_COMMITS. Along first parents only,
    # so a merge, as `git pull` makes, is one commit, and not the commits it brought in.
    commits = [line.split("\0", 1) for line in made.split("\n") if "\0" in line][::-1]
    base = head
    if commits and (parents := commits[0][0].split()):
        base = parents[0]
    elif commits and (_git(top, "rev-parse", "--is-shallow-repository") or "").strip() != "true":
        # A root commit made in the session; in a shallow clone, a commit with no parents is only where it was cut.
        base = (_git(top, "hash-object", "-t", "tree", "/dev/null") or "").strip()
    dirs = (_git(top, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir") or "").split("\n")
    linked = len(dirs) >= 2 and dirs[0] != dirs[1]
    branch = _git(top, "symbolic-ref", "--quiet", "--short", "HEAD")
    read = _read(top, MAX_DIFF, "diff-index", *DIFF, base)
    counted = _git(top, "diff-index", *NUMSTAT, base)
    counted_since_head = counted if base == head else _git(top, "diff-index", *NUMSTAT, "HEAD")
    listed = _git(top, "ls-files", "-z", "--others", "--exclude-standard")
    if not base or read is None or counted is None or counted_since_head is None or listed is None:
        return None
    diff, diff_cut = read
    changed, since_head = _numstat(counted), _numstat(counted_since_head)
    untracked = tuple(_split(listed))
    return Snapshot(
        at=at,
        base=base,
        head=head,
        branch=branch.strip() if branch else None,
        worktree=top if linked else None,
        main=_main(top) if linked else None,
        files=tuple(sorted({*(path for path, _, _ in changed), *untracked})),
        stat=_stat(changed),
        diff=diff,
        diff_cut=diff_cut,
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
    # With renames off, `a/<path> b/<path>` names one path twice: its second half, whatever ` b/` the path holds.
    return rest[(len(rest) - 1) // 2 + 1 :].removeprefix("b/")


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


def _numstat(out: str | None) -> list[tuple[str, int, int]]:
    """`--numstat -z`'s files, with their lines added and removed; a binary file's are `-`, counted as 0."""
    found = []
    for record in _split(out):
        added, removed, path = record.split("\t", 2)
        found.append((path, int(added) if added.isdigit() else 0, int(removed) if removed.isdigit() else 0))
    return found


def _stat(changed: list[tuple[str, int, int]]) -> str:
    if not changed:
        return ""
    width = min(max(len(path) for path, _, _ in changed), 80)
    rows = [f" {path:<{width}} | +{added} -{removed}" for path, added, removed in changed]
    added, removed = sum(a for _, a, _ in changed), sum(r for _, _, r in changed)
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
            env=_env(),
            capture_output=True,
            check=False,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.decode(errors="replace") if done.returncode == 0 else None


def _read(cwd: Path, limit: int, *args: str) -> tuple[str, bool] | None:
    """Its output's first `limit` bytes, and whether there was more, which git isn't left to write: a diff of a
    huge file is never held whole. None when git fails, can't run or takes longer than TIMEOUT."""
    try:
        with subprocess.Popen(
            ["git", "--no-optional-locks", *SAFE, *args],
            cwd=cwd,
            env=_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            timer = threading.Timer(TIMEOUT, process.kill)
            timer.start()
            try:
                out = process.stdout.read(limit + 1) if process.stdout else b""
                cut = len(out) > limit
                if cut:
                    process.kill()
                process.wait()
            finally:
                timer.cancel()
    except OSError:
        return None
    if not cut and process.returncode != 0:
        return None
    return out[:limit].decode(errors="replace"), cut


def _env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in REPO_VARIABLES | DIFF_VARIABLES}
