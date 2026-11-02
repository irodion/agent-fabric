"""`kb hook start|stop`: the agents' hooks remind them of `kb note` (docs/design.md §4.10).

The reminder carries a count and the project id, never a note's text: the agent reads notes with `kb inbox`."""

import contextlib
import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from agent_fabric.context import Context
from agent_fabric.gitinfo import git_project
from agent_fabric.inbox import Inbox
from agent_fabric.paths import hooks_dir, inbox_dir, tilde
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
KEEP_MARKERS = timedelta(days=7)
AT_WORK = timedelta(hours=8)  # since its hook last ran: past this, `kb note` no longer takes a marker's word
QUESTION = (
    "kb: before you finish: did this session find something the next agent must know (a broken or flaky state, "
    "a trap, an error and its fix, a decision and its reason)? If so, leave it with `kb note`. If not, just stop. "
    "kb asks this once per session."
)


class _Session(NamedTuple):
    """What a session's marker holds."""

    started: datetime
    asked: bool
    project: str | None
    seen: datetime


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
    project = _project(payload, ctx)
    if marker := _marker(agent, payload, ctx.env):
        with contextlib.suppress(OSError):  # the reminder matters more than the marker
            _seen(marker, ctx.now, lambda: project)  # the stop hook counts its 10 minutes from here
    count = sum(1 for _, note in inbox.read().open() if note.scope == project)
    found = (
        f"kb: {count} open note{'s' if count > 1 else ''} from earlier sessions for {project}, unchecked. "
        "Run `kb inbox` before relying on "
        "the repo's state."
        if count
        else f"kb: no open notes from earlier sessions for {project}."
    )
    advice = (
        "Most sessions need no note. Leave one with `kb note` (skill kb-note) only when you find something the next "
        "agent must know."
    )
    return _context(agent, f"{found} {advice}")


def stop(agent: str, payload: Mapping[str, Any], ctx: Context) -> dict[str, Any] | None:
    """Once per session, at the first end of a turn 10 minutes in: make the agent continue with the question."""
    marker = _marker(agent, payload, ctx.env)
    if (
        marker is None
        or payload.get("stop_hook_active")
        or not _turn_completed(payload)
        or _not_for_us(agent, payload, inbox_dir(ctx.env))
    ):
        return None
    session = _seen(marker, ctx.now, lambda: _project(payload, ctx))
    if session.asked or ctx.now - session.started < ASK_AFTER:
        return None
    _record(marker, session._replace(asked=True))  # first: a marker it can't update means no question
    return _continue(agent, QUESTION)


def at_work(project: str, env: Mapping[str, str], now: datetime) -> set[str]:
    """The agents whose hooks ran for this project lately, by their markers: how `kb note` knows an agent that sets
    no variable of its own (docs/design.md §4.10). A marker it can't read says nothing."""
    agents = set()
    for marker in hooks_dir(env).glob("*.json"):
        with contextlib.suppress(OSError):
            session = _state(marker)
            if session and session.project == project and now - session.seen < AT_WORK:
                agents.add(marker.stem.rpartition("-")[0])  # {agent}-{digest}.json
    return agents


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


def _seen(marker: Path, now: datetime, project: Callable[[], str]) -> _Session:
    """The session, recorded as seen now. New the first time, and again where the marker can't be read (the session
    starts over rather than never being asked). `project` runs git, so only where the marker doesn't name one."""
    session = _state(marker)
    if session is None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        _prune(marker.parent, now)
        session = _Session(started=now, asked=False, project=project(), seen=now)
    else:
        session = session._replace(project=session.project or project(), seen=now)
    _record(marker, session)
    return session


def _state(marker: Path) -> _Session | None:
    """What the marker holds; None for no marker, or one kb can't read."""
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
        project = state.get("project") if isinstance(state, dict) else None
        return _Session(
            started=_time(state["started"]),
            asked=state["asked"] is True,
            project=project if isinstance(project, str) else None,
            seen=_time(state.get("seen") or state["started"]),
        )
    except (FileNotFoundError, ValueError, TypeError, KeyError):
        return None


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)  # always comparable with `now`


def _record(marker: Path, session: _Session) -> None:
    fields = session._asdict() | {"started": session.started.isoformat(), "seen": session.seen.isoformat()}
    replace_file(marker, json.dumps(fields))  # whole, or not at all


def _prune(folder: Path, now: datetime) -> None:
    """Remove the markers of sessions that started over 7 days ago, and any marker kb can't read."""
    for old in folder.glob("*.json"):
        try:
            state = _state(old)
        except OSError:
            state = None
        if state is None or now - state.started > KEEP_MARKERS:
            old.unlink(missing_ok=True)


def _continue(agent: str, text: str) -> dict[str, Any]:
    """Make the agent take another turn with this text, as each agent's end-of-turn hook takes it."""
    if agent == "cursor":
        return {"followup_message": text}
    return {"decision": "block", "reason": text}


def _project(payload: Mapping[str, Any], ctx: Context) -> str:
    """The session's project, as `kb note` names it."""
    folder = _folder(payload, ctx.cwd)
    return git_project(folder) or tilde(folder, ctx.env)


def _folder(payload: Mapping[str, Any], cwd: Path) -> Path:
    """The session's folder: `cwd` in the input, or Cursor's first workspace root (Cursor runs user hooks from
    ~/.cursor); else the folder the hook runs in."""
    roots = payload.get("workspace_roots") or []
    return Path(payload.get("cwd") or (roots[0] if roots else cwd))


def _context(agent: str, text: str) -> dict[str, Any]:
    """Text for the model at session start, as each agent's hooks take it (docs/design.md §4.10)."""
    if agent == "copilot":
        return {"additionalContext": text}
    if agent == "cursor":
        return {"additional_context": text}
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
