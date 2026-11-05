"""What a note's refs show, with no model (docs/design.md §4.7, step 3).

A note is untrusted and what's gathered here can go to a hosted model, so this only reads git objects of the note's
own project: nothing outside the repo, nothing untracked, and no `.env*` file. That also keeps a repo's own config
from running anything, since these plumbing commands apply no text conversion or diff driver.

A file is redacted and scanned whole, before it's cut to an excerpt: a cut can take away what makes a secret
recognisable, such as a private key's end marker."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agent_fabric.gitinfo import project_id
from agent_fabric.notes import Note, ref_path
from agent_fabric.paths import expand
from agent_fabric.secretgate import redact, scan
from agent_fabric.wikirepo import Git

COMMIT = re.compile(r"[0-9a-f]{4,64}")
LINE = re.compile(r"(?::|#L)(\d+)")
BEFORE, AFTER, FIRST_LINES = 5, 10, 30
MAX_EXCERPT = 1500
MAX_FILE = 1_000_000  # bytes; gitleaks reads the whole file, and an excerpt of a file this big says little
DOCS = "AGENTS.md"


@dataclass(frozen=True)
class RefEvidence:
    ref: str
    path: str | None = None  # the file in the repo, if the ref names one that may be read
    at_commit: bool | None = None  # None where the note's commit isn't known
    now: bool = False
    changed: bool | None = None
    excerpt: str | None = None
    withheld: bool = False  # gitleaks flagged the file, so no excerpt
    dead: bool = False  # looks like a file, but git has it at neither point: maybe untracked, or on another branch


@dataclass(frozen=True)
class Evidence:
    refs: tuple[RefEvidence, ...]
    head: str | None = None
    docs: tuple[str, ...] = ()

    @property
    def anchor(self) -> str:
        """The first file that's there now, at the commit it was checked against."""
        path = next((ref.path for ref in self.refs if ref.path and ref.now), None)
        return f"{path}@{self.head}" if path else "none"


def checkout(note: Note, env: Mapping[str, str]) -> Git | None:
    """The note's folder, if it's still there and really holds the note's project."""
    folder = expand(note.cwd, env)
    if not Path(folder).is_dir() or redact(project_id(folder, env)) != redact(
        note.scope
    ):  # a filed note's is redacted
        return None
    repo = Git(folder, env)
    return repo if repo.ok("rev-parse", "-q", "--verify", "HEAD") else None


def _whole_file(repo: Git, path: str) -> str | None:
    """The file's text, redacted; None if it's not a text file of a size to read."""
    if repo("cat-file", "-t", f"HEAD:{path}").strip() != "blob":
        return None
    if int(repo("cat-file", "-s", f"HEAD:{path}")) > MAX_FILE:
        return None
    text = repo("cat-file", "blob", f"HEAD:{path}")
    return None if "\0" in text else redact(text)


def _cut(text: str, ref: str) -> str:
    lines = text.split("\n")
    line = LINE.search(ref)
    if line:
        at = int(line[1]) - 1
        lines = lines[max(at - BEFORE, 0) : at + AFTER]
    else:
        lines = lines[:FIRST_LINES]
    return "\n".join(lines)[:MAX_EXCERPT]


def _in_repo(path: str, repo: Git, env: Mapping[str, str]) -> str | None:
    """An absolute path's name in the repo; None if it's outside."""
    try:
        return expand(path, env).resolve().relative_to(repo("rev-parse", "--show-toplevel").strip()).as_posix()
    except ValueError:
        return None


def _gather_ref(repo: Git, ref: str, note: Note, prefix: str, env: Mapping[str, str]) -> RefEvidence:
    path = ref_path(ref)
    if path is None:
        return RefEvidence(ref)
    commit = note.commit if note.commit and COMMIT.fullmatch(note.commit) else None
    if commit and not repo.ok("cat-file", "-e", f"{commit}^{{commit}}"):
        commit = None

    def there(rev: str, name: str) -> bool:
        return repo.ok("cat-file", "-e", f"{rev}:{name}")

    outside_any_folder = path.startswith(("/", "~"))
    if outside_any_folder:
        inside = _in_repo(path, repo, env)
        if inside is None:
            return RefEvidence(ref)
        names = [inside]
    else:
        names = [prefix + path, path] if prefix else [path]
    if any(PurePosixPath(name).name.startswith(".env") for name in names):  # after a symlink is followed
        return RefEvidence(ref)
    for name in names:
        now = there("HEAD", name)
        then = there(commit, name) if commit else None
        if now or then:
            changed = None
            if now and then:
                changed = repo("rev-parse", f"HEAD:{name}") != repo("rev-parse", f"{commit}:{name}")
            text = _whole_file(repo, name) if now else None
            withheld = text is not None and bool(scan(text, env))
            excerpt = _cut(text, ref) if text is not None and not withheld else None
            return RefEvidence(ref, name, then, now, changed, excerpt, withheld)
    parts = PurePosixPath(path).parts
    # A slashed ref only counts if its top folder is in the repo, so `origin/main` isn't taken for a missing file.
    is_a_path = outside_any_folder or len(parts) == 1 or any(there("HEAD", base + parts[0]) for base in (prefix, ""))
    return RefEvidence(ref, dead=is_a_path)


def gather(note: Note, env: Mapping[str, str]) -> Evidence:
    repo = checkout(note, env)
    if repo is None:
        return Evidence(tuple(RefEvidence(ref) for ref in note.refs))
    prefix = repo("rev-parse", "--show-prefix").strip()
    refs = tuple(_gather_ref(repo, ref, note, prefix, env) for ref in note.refs)
    docs = repo("cat-file", "blob", f"HEAD:{DOCS}") if repo.ok("cat-file", "-e", f"HEAD:{DOCS}") else ""
    return Evidence(refs, repo("rev-parse", "--short", "HEAD").strip(), tuple(docs.split("\n")))
