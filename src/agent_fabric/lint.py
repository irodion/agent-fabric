"""`kb lint`: what's stale or broken in the wiki, with no model (docs/design.md §4.7, §8).

An error is a wiki that no longer holds together, and it stops ingest's commit if it's on a page the run changed. A
warning is for me to look at. A note's own text can't cause an error: links are only read outside claims."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent_fabric.decisions import words
from agent_fabric.evidence import checkout
from agent_fabric.inbox import Inbox
from agent_fabric.notes import STALE_AFTER, Note, parse
from agent_fabric.pages import MACHINE_PAGE, PROJECTS, Claim, Page
from agent_fabric.paths import inbox_dir, tilde, wiki_dir
from agent_fabric.report import value

FIELDS = ("level", "check", "path", "detail")
MAX_PAGE_LINES = 400
NEAR_DUPLICATE = 0.6
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


@dataclass(frozen=True)
class Finding:
    level: str  # error or warning
    check: str
    path: str
    detail: str


def _raw_notes(folder: Path) -> dict[str, Path]:
    return {path.stem.rpartition("-")[2]: path for path in (folder / "raw" / "notes").rglob("*.md")}


def _broken_links(folder: Path, path: str, text: str) -> list[Finding]:
    found = []
    for link in LINK.findall(text):
        target = link.split("#", 1)[0]
        external = "://" in link or link.startswith("mailto:")
        if target and not external and not ((folder / path).parent / target).exists():
            found.append(Finding("error", "broken-link", path, f"links to {link}, which isn't there"))
    return found


def _source_of(claim: Claim, raw: dict[str, Path]) -> Note | None:
    path = raw.get(claim.id or "")
    try:
        return parse(path.read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _anchor(claim: Claim, raw: dict[str, Path], path: str, env: Mapping[str, str]) -> tuple[Finding | None, bool]:
    """A finding if the claim's anchor file changed; and whether it couldn't be checked."""
    anchor = claim.meta["anchor"] if claim.meta else "none"
    if anchor == "none":
        return None, False
    note = _source_of(claim, raw)
    repo = checkout(note, env) if note else None
    file, _, commit = anchor.rpartition("@")
    if repo is None or not repo.ok("rev-parse", "-q", "--verify", f"{commit}:{file}"):
        return None, True
    now = repo("rev-parse", f"HEAD:{file}") if repo.ok("rev-parse", "-q", "--verify", f"HEAD:{file}") else None
    if now == repo("rev-parse", f"{commit}:{file}"):
        return None, False
    how = "changed" if now else "is gone"
    return Finding("warning", "stale-anchor", path, f"{claim.id}: {file} {how} since {commit}"), False


def _near_duplicates(claims: list[Claim], path: str) -> list[Finding]:
    """By the whole text, since a title is only the first sentence, cut at 80 characters, so notes that open alike get
    close titles. Shared words count against the shorter text, so a note repeated inside a longer one matches."""
    found = []
    texts = [(claim.id, words(claim.body)) for claim in claims]
    for at, (one, a) in enumerate(texts):
        for other, b in texts[at + 1 :]:
            if a and b and len(a & b) / min(len(a), len(b)) >= NEAR_DUPLICATE:
                found.append(Finding("warning", "near-duplicate", path, f"{one} and {other} say much the same"))
    return found


def _page_findings(folder: Path, file: Path, raw: dict[str, Path], env: Mapping[str, str]) -> list[Finding]:
    path = file.relative_to(folder).as_posix()
    text = file.read_text(encoding="utf-8")
    page = Page.parse(text)
    every = [claim for section in page.sections for claim in section.claims]
    ids = {claim.id for claim in every}
    outside_claims = "\n".join([*page.head, *(line for section in page.sections for line in section.intro)])
    found = _broken_links(folder, path, outside_claims)
    unchecked = 0
    for claim in every:
        if claim.id is None:
            found.append(Finding("error", "no-source", path, f"{claim.title!r} has no source line"))
            continue
        if claim.id not in raw:
            found.append(Finding("error", "missing-note", path, f"{claim.id} cites a note not in raw/notes"))
        found += [
            Finding("error", "dangling-supersedes", path, f"{claim.id} supersedes {old}, which isn't on the page")
            for old in claim.supersedes
            if old not in ids
        ]
    for claim in page.live_claims():
        if claim.id in raw:  # one with no note is reported above, and has nothing to check its anchor with
            finding, skipped = _anchor(claim, raw, path, env)
            found += [finding] if finding else []
            unchecked += skipped
    if unchecked:
        found.append(Finding("warning", "anchor-unchecked", path, f"{unchecked} claims: no checkout to compare with"))
    if text.count("\n") > MAX_PAGE_LINES:
        found.append(Finding("warning", "oversized-page", path, f"over {MAX_PAGE_LINES} lines"))
    return found + _near_duplicates([claim for claim in page.live_claims() if claim.id], path)


def lint_tree(folder: Path, env: Mapping[str, str]) -> list[Finding]:
    """The findings in one checkout of the wiki."""
    index = folder / "index.md"
    index_text = index.read_text(encoding="utf-8") if index.exists() else ""
    found = _broken_links(folder, "index.md", index_text)
    raw = _raw_notes(folder)
    listed = set(LINK.findall(index_text))
    root = folder / PROJECTS
    pages = sorted(root.rglob("*.md")) if root.is_dir() else []
    for file in [*pages, *[machine for machine in [folder / MACHINE_PAGE] if machine.exists()]]:
        found += _page_findings(folder, file, raw, env)
        path = file.relative_to(folder).as_posix()
        if path not in listed:
            found.append(Finding("warning", "orphan-page", path, "not in index.md"))
    return found


def _stale_status(env: Mapping[str, str], now: datetime) -> list[Finding]:
    return [
        Finding("warning", "stale-status", tilde(file, env), f"{note.id} is {(now - note.time).days} days old")
        for file, note in Inbox(inbox_dir(env)).read().open()
        if note.kind == "status" and now - note.time > STALE_AFTER
    ]


def findings(env: Mapping[str, str], now: datetime) -> list[Finding]:
    folder = wiki_dir(env)
    found = lint_tree(folder, env) if folder.is_dir() else []
    return found + _stale_status(env, now)


def _count(number: int, what: str) -> str:
    return f"{number} {what}{'' if number == 1 else 's'}"


def summary(found: list[Finding]) -> str:
    levels = [finding.level for finding in found]
    counts = [_count(levels.count(level), level) for level in ("error", "warning") if level in levels]
    return " and ".join(counts) or "nothing to report"


def render(found: list[Finding]) -> str:
    ordered = sorted(found, key=lambda finding: finding.level)  # errors first; otherwise in the order they were found
    lines = [f"summary: {value(summary(found))}", f"findings[{len(ordered)}]{{{','.join(FIELDS)}}}:"]
    lines += ["  " + ",".join(value(getattr(finding, field)) for field in FIELDS) for finding in ordered]
    return "".join(f"{line}\n" for line in lines)


def lint(env: Mapping[str, str], now: datetime) -> tuple[int, str]:
    found = findings(env, now)
    return (1 if any(finding.level == "error" for finding in found) else 0), render(found)
