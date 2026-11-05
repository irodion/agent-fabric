"""`kb ingest`: open notes into a reviewed wiki branch (docs/design.md §4.7).

The model only answers typed questions; `kb` acts on the answers and files the note's own text, so no model text
reaches a claim."""

import fcntl
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

from agent_fabric import manifests
from agent_fabric.backends import Backend, CodexBackend, InvalidAnswer, Question
from agent_fabric.config import allowed_projects
from agent_fabric.context import Context
from agent_fabric.decisions import (
    Decision,
    agents_file,
    candidates,
    decide,
    questions_for,
    read_wording,
    valid_answers,
)
from agent_fabric.evidence import gather
from agent_fabric.inbox import Inbox, Snapshot
from agent_fabric.lint import Finding, findings, lint_tree
from agent_fabric.lint import render as render_findings
from agent_fabric.manifests import Manifest, review_list
from agent_fabric.notes import Note, render
from agent_fabric.pages import PROJECTS, Page, new_claim, page_path, raw_path
from agent_fabric.paths import config_file, inbox_dir, ingest_worktree, tilde, wiki_dir
from agent_fabric.secretgate import redact, scan
from agent_fabric.wikirepo import MAIN, Git, ensure_wiki, open_worktree, publish, ready

KINDS = ("gotcha", "fix", "decision")
MIN_NOTES = 3
MAX_WAIT = timedelta(days=7)
FILED = ("add", "extend", "supersede", "conflict")  # the actions that put a claim on a page
OUTCOMES = (*FILED, "duplicate", "skip", "failed")


class Stopped(Exception):
    """The run can't go on: nothing further is committed, and its notes wait for the next run."""


def _move_resolved(inbox: Inbox, snapshot: Snapshot) -> None:
    """A resolved note skips the model, and goes to the note's own month with its resolution (§4.10)."""
    for file, note in snapshot.notes:
        if resolution := snapshot.resolved_by.get(note.id):
            inbox.retire(file, note.time)
            inbox.retire(resolution[0], note.time)


def _flag_resolved(current: Manifest, inbox: Inbox, env: Mapping[str, str]) -> bool:
    """A note resolved while its claim waits for review: the review list says so, and I decide what becomes of the
    claim. Read with `done/`, since the note may have moved already; whether it was flagged yet is on the manifest."""
    resolved_by = inbox.read(with_done=True).resolved_by
    flagged = False
    for note_id, outcome in current.outcomes.items():
        if outcome.get("action") in FILED and "resolved" not in outcome and (found := resolved_by.get(note_id)):
            text = redact(found[1].text)
            outcome["resolved"] = "its text was withheld: gitleaks flagged it" if scan(text, env) else text
            flagged = True
    return flagged


def _acted(merged: Iterable[Manifest]) -> set[str]:
    return {note_id for manifest in merged for note_id, outcome in manifest.outcomes.items() if "action" in outcome}


def _move_merged(inbox: Inbox, snapshot: Snapshot, merged: Iterable[Manifest]) -> None:
    """A note with an action on a merged manifest is done. One that failed stays, to be tried on a later branch."""
    acted = _acted(merged)
    for file, note in snapshot.notes:
        if note.id in acted:
            inbox.retire(file, note.time)


def _write(tree: Git, path: str, text: str) -> None:
    target = tree.folder / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _page(tree: Git, project: str) -> Page:
    path = tree.folder / page_path(project)
    return Page.parse(path.read_text(encoding="utf-8")) if path.exists() else Page.blank(project)


def _file(page: Page, note: Note, decision: Decision, anchor: str, ctx: Context) -> None:
    """Change the page as the decision says. The note's own text goes on it, and nothing already there changes."""
    now = ctx.now
    if decision.action == "add":
        page.add(note.kind, new_claim(note, now, anchor=anchor))
    elif decision.action == "supersede":
        for target in decision.targets:
            page.supersede(target, note.id, now)
        page.add(note.kind, new_claim(note, now, anchor=anchor, supersedes=decision.targets))
    elif decision.action in ("extend", "conflict"):
        relation = "extends" if decision.action == "extend" else "conflicts"
        linked = f"{relation}: {', '.join(decision.targets)}"
        page.below(decision.targets[0], new_claim(note, now, anchor=anchor, relation=linked), note.kind)


def _ask(backend: Backend, state: str, questions: list[Question]) -> dict[str, str] | None:
    """Valid answers within one retry, or None. A `BackendError` stops the run: the model can't be asked at all."""
    for _ in range(2):
        try:
            answers = valid_answers(questions, backend.answer(state, questions))
        except InvalidAnswer:
            continue
        if answers is not None:
            return answers
    return None


def _readable(note: Note) -> bool:
    """Any agent can write the inbox, and `parse` can't tell what this run can't pass on: a NUL can't go to git, and a
    lone surrogate can't go in a file. Such a note fails by itself, before any file is touched."""
    fields = (note.text, note.cwd, note.project, note.branch, note.commit, *note.refs, *note.agent.values())
    if any("\0" in (field or "") for field in fields):
        return False
    try:
        render(note).encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _redacted(note: Note) -> Note:
    """Every field that reaches the wiki: the raw copy holds them all. Redacted once, field by field, because
    redacting the rendered file can swallow the JSON around a value."""

    def optional(value: str | None) -> str | None:
        return redact(value) if value else value

    return replace(
        note,
        agent={key: redact(value) for key, value in note.agent.items()},
        project=optional(note.project),
        cwd=redact(note.cwd),
        branch=optional(note.branch),
        commit=optional(note.commit),
        refs=[redact(ref) for ref in note.refs],
        text=redact(note.text),
    )


def _work_on(backend: Backend, tree: Git, note: Note, ctx: Context) -> dict[str, object]:
    """Steps 3 to 6 for one note: what became of it, for the manifest."""
    if not _readable(note):
        return {"failed": "unreadable"}
    clean = _redacted(note)
    page = _page(tree, clean.scope)
    evidence = gather(note, ctx.env)
    agents = tree.folder / "AGENTS.md"
    wording = read_wording(agents.read_text(encoding="utf-8") if agents.exists() else "")
    state, questions = questions_for(clean, evidence, candidates(clean, page.live_claims()), wording)
    state = redact(state)
    if rules := scan(f"{state}\n{render(clean)}", ctx.env):  # the raw copy holds more than the model sees
        return {"failed": "quarantined", "rules": rules}
    answers = _ask(backend, state, questions)
    if answers is None:
        return {"failed": "no valid answer"}
    decision = decide(answers)
    _file(page, clean, decision, redact(evidence.anchor), ctx)
    if decision.action not in ("skip", "duplicate"):
        _write(tree, page_path(clean.scope), page.render())
    _write(tree, raw_path(clean), render(clean))
    outcome: dict[str, object] = {
        "action": decision.action,
        "answers": answers,
        "targets": list(decision.targets),
        "text": clean.text,
    }
    if missing := [redact(ref.ref) for ref in evidence.refs if ref.dead]:
        outcome["missing"] = missing
    if withheld := [redact(ref.path) for ref in evidence.refs if ref.withheld and ref.path]:
        outcome["withheld"] = withheld
    return outcome


def _index(tree: Git) -> str:
    lines = ["# Index", ""]
    root = tree.folder / PROJECTS
    for path in sorted(root.rglob("*.md")) if root.is_dir() else []:
        relative = path.relative_to(tree.folder).as_posix()
        project = path.relative_to(root).with_suffix("").as_posix()
        claims = len(Page.parse(path.read_text(encoding="utf-8")).live_claims())
        lines.append(f"- [{project}]({relative}): {claims} claims")
    return "\n".join(lines) + "\n"


def _log_line(note: Note, outcome: dict[str, object], ctx: Context) -> str:
    what = outcome.get("action", f"failed: {outcome.get('failed')}")
    return f"{ctx.now:%Y-%m-%d} ingest {note.id} {what} {redact(note.scope)}\n"


def _output_gate(tree: Git, env: Mapping[str, str]) -> None:
    """Step 7: no secret goes on the branch, whatever put it in the file, and no page the run changed stays broken."""
    tree("add", "-A")
    changed = {name for name in tree("diff", "--cached", "--name-only", "-z").split("\0") if name}
    for name in sorted(changed):
        if rules := scan((tree.folder / name).read_text(encoding="utf-8"), env):
            raise Stopped(f"gitleaks found {', '.join(rules)} in {name}; nothing was committed")
    for finding in lint_tree(tree.folder, env):
        if finding.level == "error" and finding.path in changed:
            raise Stopped(f"{finding.check} in {finding.path}: {finding.detail}; nothing was committed")


def _told(wiki: Git, env: Mapping[str, str], manifest: Manifest, take: list[Note]) -> str:
    """What the run did, and what's next for me."""
    told = ""
    if take:
        outcomes = [manifest.outcomes[note.id].get("action", "failed") for note in take]
        counts = ", ".join(f"{outcomes.count(what)} {what}" for what in OUTCOMES if what in outcomes)
        told += f"kb ingest: {len(take)} notes on {manifest.branch}: {counts}\n"
    if resolved := sum("resolved" in outcome for outcome in manifest.outcomes.values()):
        told += f"kb ingest: {resolved} filed notes on {manifest.branch} have been resolved since\n"
    return (
        f"{told}Review {manifest.review_path} on that branch, then: "
        f"git -C {tilde(wiki.folder, env)} merge {manifest.branch}\n"
    )


def _commit(wiki: Git, tree: Git, manifest: Manifest, found: list[Finding], what: str) -> None:
    """Step 7, with the manifest and the review list as they now stand."""
    _write(tree, manifest.path, manifest.render())
    _write(tree, manifest.review_path, review_list(manifest, found))
    _output_gate(tree, wiki.env)
    publish(wiki, tree, manifest.branch, what)


def _flag_only(ctx: Context, wiki: Git, current: Manifest, found: list[Finding]) -> str:
    """Nothing to file, but the review list has news."""
    tree = open_worktree(wiki, ingest_worktree(ctx.env), current.branch)
    _commit(wiki, tree, current, found, "ingest: flag resolved notes")
    return _told(wiki, ctx.env, current, [])


def _run(ctx: Context, wiki: Git, current: Manifest | None, take: list[Note], found: list[Finding]) -> str:
    env = ctx.env
    backend = ctx.backend or CodexBackend(env)
    if current is None:
        current = Manifest(manifests.next_name(wiki, f"{ctx.now:%Y-%m-%d}"), wiki("rev-parse", MAIN).strip())
        start = MAIN
    else:
        start = current.branch
    tree = open_worktree(wiki, ingest_worktree(env), start)
    current.notes += [note.id for note in take if note.id not in current.notes]
    _write(tree, current.path, current.render())
    publish(wiki, tree, current.branch, f"ingest: take {len(take)} notes")
    log = ""
    for note in take:
        outcome = _work_on(backend, tree, note, ctx)
        current.outcomes[note.id] = outcome
        log += _log_line(note, outcome, ctx)
    _write(tree, "index.md", _index(tree))
    with (tree.folder / "log.md").open("a", encoding="utf-8") as f:
        f.write(log)
    _commit(wiki, tree, current, found, f"ingest: file {len(take)} notes")
    return _told(wiki, env, current, take)


@contextmanager
def _lock(env: Mapping[str, str]) -> Iterator[None]:
    """One run at a time: a timer can fire during a manual run, and both would use the one worktree."""
    path = inbox_dir(env).parent / "ingest.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise Stopped("another kb ingest is running") from e
        yield


def _allowed(env: Mapping[str, str]) -> tuple[str, ...]:
    try:
        return allowed_projects(env)
    except ValueError as e:
        raise Stopped(f"can't read {config_file(env)}: {e}") from e


def ingest(ctx: Context, *, project: str | None = None, dry_run: bool = False) -> tuple[int, str]:
    if dry_run:
        return _pass(ctx, project, dry_run=True)
    with _lock(ctx.env):
        return _pass(ctx, project, dry_run=False)


def _pass(ctx: Context, project: str | None, *, dry_run: bool) -> tuple[int, str]:
    env = ctx.env
    allowed = _allowed(env)
    found = findings(env, ctx.now)  # step 1: claims go stale while the inbox is quiet
    report = render_findings(found) if found else ""
    inbox = Inbox(inbox_dir(env))
    folder = wiki_dir(env)
    known = ready(folder, env)
    wiki = Git(folder, env)
    merged = manifests.merged(wiki) if known else []
    current = manifests.unmerged(wiki) if known else None
    flagged = current is not None and not dry_run and _flag_resolved(current, inbox, env)
    if not dry_run:
        _move_resolved(inbox, inbox.read())
        _move_merged(inbox, inbox.read(), merged)
    acted = _acted(merged)
    eligible = [
        note
        for _, note in inbox.read().open()
        if note.kind in KINDS and note.scope in allowed and project in (None, note.scope) and note.id not in acted
    ]
    has_outcome = {
        note_id for manifest in (*merged, *([current] if current else [])) for note_id in manifest.outcomes
    }
    counted = [note for note in eligible if note.id not in has_outcome]
    oldest = ctx.now - min((note.time for note in counted), default=ctx.now)
    if len(counted) < MIN_NOTES and oldest < MAX_WAIT:
        wait = f"kb ingest: {len(counted)} open, the oldest {oldest.days} days old: waiting for {MIN_NOTES}\n"
        said = report + (wait if counted else "")
        return 0, said + (_flag_only(ctx, wiki, current, found) if flagged and current else "")
    own = current.outcomes if current else {}
    take = [note for note in eligible if note.id not in own]  # step 2: with a note that failed on a merged branch
    if dry_run:
        listed = "".join(f"{note.id} {note.kind} {note.scope}\n" for note in take)
        return 0, f"{report}kb ingest: would take {len(take)} notes:\n{listed}"
    return 0, report + _run(ctx, ensure_wiki(folder, env, agents_file()), current, take, found)
