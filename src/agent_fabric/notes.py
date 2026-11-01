"""Agent notes: the `fabric.note/1` file format (docs/design.md §4.10).

Front-matter values are JSON, which is also valid YAML, so any YAML reader can load a note and `kb` needs no YAML
library to read it back.
"""

import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "fabric.note/1"
KINDS = ("status", "gotcha", "fix", "decision")
MAX_TEXT = 500
ID = re.compile(r"n[0-9a-f]{6}")


@dataclass(frozen=True)
class Note:
    id: str
    kind: str
    agent: dict[str, str]
    project: str | None
    cwd: str
    branch: str | None
    commit: str | None
    time: datetime
    refs: list[str]
    resolves: str | None
    text: str

    @property
    def scope(self) -> str:
        """The project a note belongs to: its project id, or outside git the folder it was written in."""
        return self.project or self.cwd

    @property
    def filename(self) -> str:
        return f"{self.time:%Y-%m-%d}-{self.id}.md"


# Extensions that make a bare name (no folder) a file name; `json.loads`, `example.com` and `v1.2` are not.
FILE_EXTENSIONS = frozenset(
    [
        "bash",
        "c",
        "cc",
        "cfg",
        "cjs",
        "conf",
        "cpp",
        "cs",
        "css",
        "csv",
        "diff",
        "dockerfile",
        "env",
        "ex",
        "exs",
        "go",
        "gradle",
        "graphql",
        "h",
        "hpp",
        "html",
        "ini",
        "ipynb",
        "java",
        "js",
        "json",
        "jsonl",
        "jsx",
        "kt",
        "lock",
        "log",
        "lua",
        "md",
        "mjs",
        "mk",
        "nix",
        "patch",
        "php",
        "proto",
        "py",
        "pyi",
        "rb",
        "rs",
        "rst",
        "scss",
        "sh",
        "sql",
        "svelte",
        "swift",
        "tf",
        "toml",
        "ts",
        "tsx",
        "txt",
        "vue",
        "xml",
        "yaml",
        "yml",
        "zsh",
    ]
)


def ref_path(ref: str) -> str | None:
    """The file a ref points at, if it looks like one: `src/x.py`, `src/x.rs:12:5`, `tests/t.py::test_a`,
    `README.md`. None for commands, URLs, commits and dotted names such as `httpx.ConnectError` or `json.loads`."""
    if re.search(r"\s", ref) or "://" in ref or re.fullmatch(r"[0-9a-f]{7,40}", ref):
        return None
    path = re.sub(r"(:\d+)+(-\d+)?$", "", ref.split("::", 1)[0])
    stem, dot, extension = path.rpartition(".")
    if "/" in path or (stem and dot and extension.lower() in FILE_EXTENSIONS):
        return path
    return None


def missing_ref(ref: str, bases: list[Path]) -> str | None:
    """The path to warn about when a ref names a file that isn't there, looked up from each base folder (the
    current folder and the repo root). A slashed ref only counts when its top folder exists, so `origin/main`
    is not mistaken for a missing file."""
    path = ref_path(ref)
    if path is None:
        return None
    target = Path(path).expanduser()
    if any((base / target).exists() for base in bases):
        return None
    if len(target.parts) > 1 and not target.is_absolute():
        top = target.parts[0]
        if not any((base / top).is_dir() for base in bases):
            return None
    return path


def new_id() -> str:
    """A random id; `Inbox.add` draws another if a note in the inbox already has it."""
    return "n" + secrets.token_hex(3)


def parse(text: str) -> Note:
    """Read a note written by `render`; raises ValueError on anything else."""
    _, head, body = text.split("---\n", 2)
    fields: dict[str, Any] = {}
    for line in head.removesuffix("\n").split("\n"):  # not splitlines(): JSON strings may hold U+2028 and friends
        key, _, value = line.partition(": ")
        fields[key] = json.loads(value)
    if fields.pop("schema", None) != SCHEMA:
        raise ValueError(f"not a {SCHEMA} note")
    fields["time"] = datetime.strptime(fields["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    note = Note(**fields, text=body.removesuffix("\n"))
    _check(note)
    return note


def _is_id(value: object) -> bool:
    return isinstance(value, str) and ID.fullmatch(value) is not None


def _check(note: Note) -> None:
    """Any agent can write the inbox, so a note read back is checked field by field before `kb` relies on it."""
    strings: dict[str, object] = {"cwd": note.cwd, "text": note.text}
    optional: dict[str, object] = {"project": note.project, "branch": note.branch, "commit": note.commit}
    agent: object = note.agent
    refs: object = note.refs
    if not (
        _is_id(note.id)
        and note.kind in KINDS
        and isinstance(agent, dict)
        and isinstance(agent.get("name"), str)
        and all(isinstance(value, str) for value in agent.values())
        and all(isinstance(value, str) for value in strings.values())
        and all(value is None or isinstance(value, str) for value in optional.values())
        and isinstance(refs, list)
        and all(isinstance(ref, str) for ref in refs)
        and (note.resolves is None or _is_id(note.resolves))
    ):
        raise ValueError("a note field has the wrong type or form")


def render(note: Note) -> str:
    fields: list[tuple[str, Any]] = [
        ("schema", SCHEMA),
        ("id", note.id),
        ("kind", note.kind),
        ("agent", note.agent),
        ("project", note.project),
        ("cwd", note.cwd),
        ("branch", note.branch),
        ("commit", note.commit),
        ("time", note.time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("refs", note.refs),
        ("resolves", note.resolves),
    ]
    head = "".join(f"{key}: {json.dumps(value, ensure_ascii=False)}\n" for key, value in fields)
    return f"---\n{head}---\n{note.text}\n"
