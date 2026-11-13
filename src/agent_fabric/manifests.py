"""Ingest's manifests: which notes a branch took, and what became of each (docs/design.md §4.7, steps 2, 7 and 9).

They live in the wiki repo and merge with the branch, so notes need no ledger elsewhere. A note listed with no outcome
is one a run died on: only a commit at the output gate gives it an outcome."""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from agent_fabric.lint import Finding
from agent_fabric.notes import MACHINE
from agent_fabric.pages import safe
from agent_fabric.wikirepo import MAIN, Git, GitError

SCHEMA = "fabric.ingest/1"
FOLDER = "manifests"
BRANCHES = "ingest/"
CERTAINTY = {"low": 1, "medium": 2, "high": 3}


@dataclass
class Manifest:
    name: str
    base: str
    notes: list[str] = field(default_factory=list)
    outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def branch(self) -> str:
        return f"{BRANCHES}{self.name}"

    @property
    def path(self) -> str:
        return path_of(self.name)

    @property
    def review_path(self) -> str:
        return f"{FOLDER}/{self.name}.md"

    def render(self) -> str:
        fields = {"schema": SCHEMA, "base": self.base, "notes": self.notes, "outcomes": self.outcomes}
        return json.dumps(fields, indent=2) + "\n"


def path_of(name: str) -> str:
    return f"{FOLDER}/{name}.json"


def _read(git: Git, rev: str, name: str) -> Manifest:
    text = git("show", f"{rev}:{path_of(name)}")
    try:
        fields = json.loads(text)
        return Manifest(name, fields["base"], list(fields["notes"]), dict(fields["outcomes"]))
    except (ValueError, KeyError, TypeError) as e:
        raise GitError(f"can't read {path_of(name)} at {rev}: {e}") from e


def merged(git: Git) -> list[Manifest]:
    """The manifests on main: their branches have been reviewed and merged."""
    listed = git("ls-tree", "--name-only", f"{MAIN}:{FOLDER}") if git.ok("rev-parse", f"{MAIN}:{FOLDER}") else ""
    return [_read(git, MAIN, name.removesuffix(".json")) for name in listed.split() if name.endswith(".json")]


def unmerged(git: Git) -> Manifest | None:
    """The newest ingest branch whose manifest isn't on main as it stands on the branch, however it was merged."""
    branches = git("for-each-ref", "--format=%(refname:short)", f"refs/heads/{BRANCHES}").split()
    for branch in sorted(branches, reverse=True):
        name = branch.removeprefix(BRANCHES)
        path = path_of(name)
        if not git.ok("rev-parse", "-q", "--verify", f"{branch}:{path}"):
            continue
        ours = git("rev-parse", f"{branch}:{path}")
        on_main = git.ok("rev-parse", "-q", "--verify", f"{MAIN}:{path}") and git("rev-parse", f"{MAIN}:{path}")
        if ours != on_main:
            return _read(git, branch, name)
    return None


def next_name(git: Git, date: str) -> str:
    """`<date>`, or `<date>-2` when that day already has a manifest or a branch."""
    taken = {manifest.name for manifest in merged(git)}
    taken |= {
        branch.removeprefix(BRANCHES)
        for branch in git("for-each-ref", "--format=%(refname:short)", f"refs/heads/{BRANCHES}").split()
    }
    name, n = date, 1
    while name in taken:
        n += 1
        name = f"{date}-{n}"
    return name


CONTEXT = (
    "The model says every session needs these before it starts, least certain first. Each is filed as a claim all"
    " the same. kb never edits instructions: add the ones you agree with where it says, by hand or by asking an"
    " agent, and check that a public repo's AGENTS.md gets nothing private."
)


def context_candidates(manifest: Manifest) -> list[tuple[str, dict[str, Any]]]:
    """The notes that may belong in agent instructions, least certain first (§4.7, step 8). One resolved since filing
    or superseded by a later note on the branch no longer holds, and one a later note contradicts is in dispute, so
    they're left out."""
    replaced = {
        target
        for o in manifest.outcomes.values()
        if o.get("action") in ("supersede", "conflict")
        for target in o["targets"]
    }
    found = [
        (note_id, o)
        for note_id, o in manifest.outcomes.items()
        if "context" in o and "resolved" not in o and note_id not in replaced
    ]
    return sorted(found, key=lambda item: _certainty(item[1]))


def _where(outcome: dict[str, Any]) -> str:
    """Where a candidate goes. One that supersedes a claim replaces that claim's rule if the file has it, so that the
    file doesn't hold both."""
    project = outcome["context"]
    where = f"{project}'s AGENTS.md"
    if project == MACHINE:
        where = "your agents' global instructions, as it holds on this machine"
    if outcome["action"] == "supersede":
        where += f", replacing the rule of {', '.join(outcome['targets'])} if it's there"
    return where


def _certainty(outcome: dict[str, Any]) -> int:
    """Failed notes first, then the least certain (§4.7, step 8)."""
    if "failed" in outcome:
        return 0
    if outcome["action"] == "idea":
        return len(CERTAINTY) + 1  # nothing was decided: last
    return CERTAINTY.get(outcome["answers"].get("sure"), 1)


def _entry(note_id: str, outcome: dict[str, Any]) -> str:
    if failed := outcome.get("failed"):
        rules = f" ({', '.join(outcome['rules'])})" if outcome.get("rules") else ""
        return f"## {note_id} {failed}{rules}\n"
    quoted = "".join(f"> {line}\n" for line in safe(outcome["text"]).split("\n"))
    if outcome["action"] == "idea":
        return f"## {note_id} idea\n\n{quoted}"
    answers = outcome["answers"]
    listed = "".join(f"- {question}: {answer}\n" for question, answer in answers.items() if question != "sure")
    targets = f"- on: {', '.join(outcome['targets'])}\n" if outcome["targets"] else ""
    missing = "".join(f"- ref not found: {safe(ref)}\n" for ref in outcome.get("missing", ()))
    withheld = "".join(
        f"- excerpt withheld, gitleaks flagged it: {safe(path)}\n" for path in outcome.get("withheld", ())
    )
    unchecked = "".join(
        f"- ref not checked, its checkout isn't here: {safe(ref)}\n" for ref in outcome.get("unchecked", ())
    )
    details = f"{listed}{targets}{missing}{unchecked}{withheld}"
    return f"## {note_id} {outcome['action']} (sure: {answers.get('sure')})\n\n{quoted}\n{details}"


def review_list(manifest: Manifest, found: Iterable[Finding]) -> str:
    """What I review beside the branch's diff: each note and its answers, least certain first (§4.7, step 8)."""
    lines = [f"# Review of {manifest.branch}\n", "\nMark each action right or wrong.\n"]
    resolved = "".join(
        f"- {note_id}: {safe(outcome['resolved']).replace(chr(10), ' ')}\n"
        for note_id, outcome in manifest.outcomes.items()
        if "resolved" in outcome
    )
    if resolved:
        advice = "Take a claim off the page before merging if it no longer holds."
        lines.append(f"\n## Resolved since filing\n\n{advice}\n\n{resolved}")
    problems = "".join(f"- {f.level} {f.check} {f.path}: {safe(f.detail)}\n" for f in found)
    if problems:
        lines.append(f"\n## Lint\n\n{problems}")
    context = "".join(
        f"- {note_id} (sure: {outcome['answers'].get('sure')}): {safe(_where(outcome))}\n"
        for note_id, outcome in context_candidates(manifest)
    )
    if context:
        lines.append(f"\n## Belongs in agent instructions\n\n{CONTEXT}\n\n{context}")
    ordered = sorted(manifest.outcomes.items(), key=lambda item: _certainty(item[1]))
    lines += [f"\n{_entry(note_id, outcome)}" for note_id, outcome in ordered]
    return "".join(lines)
