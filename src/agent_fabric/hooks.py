"""`kb hook start|stop`: the agents' hooks remind them of `kb note` (docs/design.md §4.10).

The reminder carries a count and the project id, never a note's text: the agent reads notes with `kb inbox`."""

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from agent_fabric.context import Context
from agent_fabric.gitinfo import project_id
from agent_fabric.inbox import Inbox
from agent_fabric.paths import hooks_dir, inbox_dir
from agent_fabric.sandboxes import replace_file

HOOKS = ("start", "stop")
EVENTS = {  # each agent's events for kb's hooks
    "claude-code": {"start": "SessionStart", "stop": "Stop"},
    "codex": {"start": "SessionStart", "stop": "Stop"},
    "copilot": {"start": "sessionStart", "stop": "agentStop"},
    "cursor": {"start": "sessionStart", "stop": "stop"},
}
AGENTS = tuple(EVENTS)
ASK_AFTER = timedelta(minutes=10)
ASK_AGAIN_AFTER = timedelta(hours=8)  # since the last question: a long session has moved on to other work
ASK_AFTER_COMPACTION = timedelta(hours=1)  # since the last question: a small context can compact every few minutes
KEEP_MARKERS = timedelta(days=7)  # since a hook last ran for the session
PLAIN_ID = re.compile(r"[A-Za-z0-9._/~:@+-]+")  # a remote or a path, with no room for a sentence
MAX_ID = 200
AT_WORK = timedelta(hours=8)  # since a hook last ran: past this, `kb note` no longer takes a marker's word
QUESTION = (
    "kb: before you finish: did this session find something the next agent must know (a broken or flaky state, "
    "a trap, an error and its fix, a decision and its reason)? If so, leave it with `kb note`. If not, just stop. "
    "kb asks this once per session, and again later in a long one."
)


class _Session(NamedTuple):
    """What a session's marker holds. The file's own time says when a hook last ran for the session, so that a hook
    that changes nothing else doesn't rewrite it, and can't put back an older copy over one that says `asked_at`.
    A compaction sets only `compacted`, from the marker as it is then."""

    started: datetime
    asked_at: datetime | None  # the last question; None until the first
    compacted: bool  # since the last question (Claude Code's start hook says so; the other agents don't)
    project: str | None  # None until a hook has named it
    from_before: bool = False  # asked, by a kb that didn't record when: `_seen` takes it as asked now


def run(event: str, agent: str, ctx: Context) -> str:
    """What the hook prints: the agent's JSON, or nothing. It never fails: a hook must not break a session."""
    try:
        payload = json.loads(ctx.stdin.read())
        if not isinstance(payload, dict):
            return ""
        output = (start if event == "start" else stop)(agent, payload, ctx)
    except Exception:  # any input, any state of the files: say nothing rather than fail
        return ""
    return f"{json.dumps(output)}\n" if output else ""


def start(agent: str, payload: Mapping[str, Any], ctx: Context) -> dict[str, Any] | None:
    """The session-start reminder, in the agent's hook output format; None where the hook should say nothing."""
    inbox = Inbox(inbox_dir(ctx.env))
    if _not_for_us(agent, payload, inbox.path):
        return None
    marker = _marker(agent, payload, ctx.env)
    if marker:
        with contextlib.suppress(OSError):  # the reminder matters more than the marker
            _seen(marker, ctx.now)  # the stop hook counts its 10 minutes from here; before git can hang
            if payload.get("source") == "compact":
                _compacted(marker, ctx.now)
    project = _project(payload, ctx)
    if marker:
        with contextlib.suppress(OSError):
            _name_project(marker, project, ctx.now)
    count = sum(1 for _, note in inbox.read().open() if note.scope == project)
    named = _named(project)
    found = (
        f"kb: {count} open note{'s' if count > 1 else ''} from earlier sessions for {named}, unchecked. "
        "Run `kb inbox` before relying on "
        "the repo's state."
        if count
        else f"kb: no open notes from earlier sessions for {named}."
    )
    advice = (
        "Most sessions need no note. Leave one with `kb note` (skill kb-note) only when you find something the next "
        "agent must know."
    )
    return _context(agent, f"{found} {advice}")


def stop(agent: str, payload: Mapping[str, Any], ctx: Context) -> dict[str, Any] | None:
    """At the first end of a turn that `_due` allows, make the agent continue with the question. Every other end of a
    turn keeps the marker's project current, as nothing else tells it of `git init` or a move."""
    marker = _marker(agent, payload, ctx.env)
    if marker is None or _not_for_us(agent, payload, inbox_dir(ctx.env)):
        return None
    session = _seen(marker, ctx.now)
    finished = _turn_completed(payload) and not payload.get("stop_hook_active")
    if finished and _due(session, ctx.now):
        # first: a marker it can't update means no question
        _record(marker, session._replace(asked_at=ctx.now, compacted=False), ctx.now)
        return _continue(agent, QUESTION)  # no git before this: it can outlast the hook's 10 seconds
    _name_project(marker, _project(payload, ctx), ctx.now)
    return None


def at_work(project: str, env: Mapping[str, str], now: datetime) -> set[str]:
    """The agents whose hooks ran for this project lately, by their markers: how `kb note` knows an agent that sets
    no variable of its own (docs/design.md §4.10). A marker it can't read says nothing."""
    agents = set()
    for marker in hooks_dir(env).glob("*.json"):
        with contextlib.suppress(OSError):
            session = _state(marker)
            if session and session.project == project and _since_a_hook_ran(marker, now) < AT_WORK.total_seconds():
                agents.add(marker.stem.rpartition("-")[0])  # {agent}-{digest}.json
    return agents


def last_ran(agent: str, env: Mapping[str, str]) -> datetime | None:
    """When a hook last ran for this agent, by its markers: how `kb doctor` knows its hooks work. None: not since the
    last marker was pruned, or a time that's no date."""
    times = []
    for marker in hooks_dir(env).glob(f"{agent}-*.json"):
        with contextlib.suppress(OSError):
            times.append(marker.stat().st_mtime)
    try:
        return datetime.fromtimestamp(max(times), UTC) if times else None
    except (OverflowError, OSError, ValueError):
        return None


def _since_a_hook_ran(marker: Path, now: datetime) -> float:
    """In seconds: a marker's file time can be anything, even ahead of the clock, which counts as that long ago; and
    a float can't overflow as a date can."""
    return abs(now.timestamp() - marker.stat().st_mtime)


def _turn_completed(payload: Mapping[str, Any]) -> bool:
    """Not stopped by the user or an error: Cursor says so in `status`, Copilot CLI in `stopReason` (Claude Code
    doesn't run Stop hooks on an interrupt)."""
    return bool(
        payload.get("status", "completed") == "completed" and payload.get("stopReason", "end_turn") == "end_turn"
    )


def _not_for_us(agent: str, payload: Mapping[str, Any], inbox: Path) -> bool:
    """Cursor CLI runs Claude Code's user hooks too, and its own entry already reminds it; and without an inbox
    kb isn't set up (`kb doctor` says so)."""
    return (agent == "claude-code" and "cursor_version" in payload) or not inbox.is_dir()


def _marker(agent: str, payload: Mapping[str, Any], env: Mapping[str, str]) -> Path | None:
    """The session's marker file; None without a session id to key it on."""
    session = next((payload[key] for key in ("session_id", "sessionId", "conversation_id") if payload.get(key)), None)
    if not isinstance(session, str):
        return None
    digest = hashlib.sha256(f"{agent}\0{session}".encode()).hexdigest()[:32]
    return hooks_dir(env) / f"{agent}-{digest}.json"


def _due(session: _Session, now: datetime) -> bool:
    """Whether the question is due: 10 minutes into the session, then 8 hours after the last question, or an hour
    after it where a compaction has dropped what the session saw since."""
    if session.asked_at is None:
        return now - session.started >= ASK_AFTER
    since = now - session.asked_at
    return since >= ASK_AGAIN_AFTER or (session.compacted and since >= ASK_AFTER_COMPACTION)


def _compacted(marker: Path, now: datetime) -> None:
    """A compaction since the last question. From the marker as it is now, so a stop hook that asked meanwhile keeps
    its `asked_at`; before the first question there's nothing to record, as the 10 minutes still apply."""
    session = _state(marker)
    if session and session.asked_at is not None and not session.compacted:
        _record(marker, session._replace(compacted=True), now)


def _seen(marker: Path, now: datetime) -> _Session:
    """The session, with a hook recorded as having run now. New the first time, and again where the marker can't be
    read (the session starts over rather than never being asked)."""
    session = _state(marker)
    if session is None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        _prune(marker.parent, now)
        session = _Session(started=now, asked_at=None, compacted=False, project=None)
        _record(marker, session, now)
    elif session.from_before:
        session = session._replace(asked_at=now, from_before=False)
        _record(marker, session, now)
    else:
        os.utime(marker, (now.timestamp(), now.timestamp()))
    return session


def _name_project(marker: Path, project: str, now: datetime) -> None:
    """The session is in this project: the first hook names it, and later ones follow a session that moves. From the
    marker as it is now, not as it was before git ran: another hook may have asked since."""
    session = _state(marker)
    if session and session.project != project:
        _record(marker, session._replace(project=project), now)


def _state(marker: Path) -> _Session | None:
    """What the marker holds; None for no marker, or one kb can't read."""
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
        project = state.get("project") if isinstance(state, dict) else None
        started = _time(state["started"])
        if started is None:
            return None
        asked_at = _time(state["asked_at"]) if "asked_at" in state else None
        from_before = asked_at is None and state.get("asked") is True
        compacted = state.get("compacted") is True
        return _Session(started, asked_at, compacted, project if isinstance(project, str) else None, from_before)
    except (FileNotFoundError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None


def _time(value: Any) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value).astimezone(UTC)  # comparable with `now`


def _record(marker: Path, session: _Session, now: datetime) -> None:
    fields = {
        "started": session.started.isoformat(),
        "asked": session.asked_at is not None,  # for a kb from before `asked_at`, still run by another agent's hooks
        "asked_at": session.asked_at and session.asked_at.isoformat(),
        "compacted": session.compacted,
        "project": session.project,
    }
    replace_file(marker, json.dumps(fields))  # whole, or not at all
    os.utime(marker, (now.timestamp(), now.timestamp()))


def _prune(folder: Path, now: datetime) -> None:
    """Remove the markers of sessions no hook has run for in over 7 days, and any marker kb can't read."""
    for old in folder.glob("*.json"):
        try:
            remove = _state(old) is None or _since_a_hook_ran(old, now) > KEEP_MARKERS.total_seconds()
        except OSError:
            remove = True
        if remove:
            old.unlink(missing_ok=True)


def _continue(agent: str, text: str) -> dict[str, Any]:
    """Make the agent take another turn with this text, as each agent's end-of-turn hook takes it."""
    if agent == "cursor":
        return {"followup_message": text}
    return {"decision": "block", "reason": text}


def _project(payload: Mapping[str, Any], ctx: Context) -> str:
    return project_id(_folder(payload, ctx.cwd), ctx.env)


def _folder(payload: Mapping[str, Any], cwd: Path) -> Path:
    """The session's folder: `cwd` in the input, or Cursor's first workspace root (Cursor runs user hooks from
    ~/.cursor); else the folder the hook runs in."""
    roots = payload.get("workspace_roots") or []
    return Path(payload.get("cwd") or (roots[0] if roots else cwd))


def _named(project: str) -> str:
    """The project as the reminder names it. Its id is the one text there that `kb` doesn't write: it comes from a
    remote or a folder name, which can carry an instruction. So only a plain id goes in, cut to `MAX_ID`."""
    if not PLAIN_ID.fullmatch(project):
        return "this project"
    return project if len(project) <= MAX_ID else project[: MAX_ID - 1] + "…"


def _context(agent: str, text: str) -> dict[str, Any]:
    """Text for the model at session start, as each agent's hooks take it (docs/design.md §4.10)."""
    if agent == "copilot":
        return {"additionalContext": text}
    if agent == "cursor":
        return {"additional_context": text}
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
