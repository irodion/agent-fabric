"""Copilot CLI's sessions, read from its own session folders (docs/design.md §4.1).

agentsview fails every Copilot session under a read-only home while Copilot's session store has no `-shm` file, so
Copilot CLI gets its own `CaptureSource`, as Cursor CLI does. A session is `<COPILOT_HOME>/session-state/<id>/`, and
`events.jsonl` in it holds the whole conversation; the store adds only token usage, which kb doesn't need. A folder
without `events.jsonl` is a session that never got a message.

Messages follow agentsview's reading of the same file: a message per prompt and per assistant reply, and each tool
call's start and end folded into the call, so that both sources give the normalizer the same shapes. Unlike
agentsview, this keeps a failed call's error text and a command's exit code, which Copilot records apart, and its
ordinals have no gaps: agentsview also numbers a hidden message per tool result.
"""

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agent_fabric.capture import CaptureError, Message, SessionMeta, Transcript
from agent_fabric.paths import expand

log = logging.getLogger(__name__)

AGENT = "copilot"
PREFIX = "copilot:"  # agentsview's prefix, so a session has one id whichever source reads it
POLL = 1.0  # seconds between looks at a watched session's file
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def copilot_home(env: Mapping[str, str]) -> Path:
    """`COPILOT_HOME`, `~` expanded as `kb doctor` expands it, else `~/.copilot`."""
    home = expand(env.get("COPILOT_HOME") or "", env)
    return home if home.is_absolute() else Path(env["HOME"]) / ".copilot"


@dataclass(frozen=True)
class _Read:
    session: SessionMeta
    messages: tuple[Message, ...]


class CopilotSource:
    """`CaptureSource` over Copilot CLI's session folders, read only."""

    def __init__(self, env: Mapping[str, str], *, poll: float = POLL) -> None:
        self._root = copilot_home(env) / "session-state"
        self._poll = poll

    def active_sessions(self, since: datetime) -> list[SessionMeta]:
        if since.tzinfo is None:
            raise ValueError("a naive datetime has no zone to compare")
        try:
            folders = sorted(self._root.iterdir())
        except FileNotFoundError:
            return []
        except OSError as error:
            raise CaptureError(f"Copilot CLI's sessions in {self._root}: {error}") from error
        found = []
        for folder in folders:
            events = folder / "events.jsonl"
            try:
                if not SESSION_ID.fullmatch(folder.name) or events.stat().st_mtime < since.timestamp():
                    continue
                found.append(self._read(folder.name).session)
            except FileNotFoundError:
                continue  # never got a message, or removed since the listing
            except CaptureError as error:
                if isinstance(error.__cause__, FileNotFoundError):
                    continue  # removed between the listing and the read
                log.warning("skipping Copilot CLI session %s: %s", folder.name, error)
            except OSError as error:
                # One session that can't be read mustn't stop capture of every other (§4.1).
                log.warning("skipping Copilot CLI session %s: %s", folder.name, error)
        return found

    def session(self, session_id: str) -> SessionMeta:
        return self._read(self._folder(session_id)).session

    def messages(self, session_id: str, from_ordinal: int) -> Transcript:
        read = self._read(self._folder(session_id))
        later = tuple(message for message in read.messages if message.ordinal >= from_ordinal)
        return Transcript(read.session.transcript_revision if later else None, later)

    async def watch(self, session_id: str) -> AsyncIterator[None]:
        events = self._root / self._folder(session_id) / "events.jsonl"
        seen = _stamp(events)
        if seen is None:
            raise CaptureError(f"Copilot CLI session {session_id}: no {events}")
        yield None
        while True:
            await asyncio.sleep(self._poll)
            now = _stamp(events)
            if now is None:
                return  # the session was removed: nothing left to watch
            if now != seen:
                seen = now
                yield None

    def _folder(self, session_id: str) -> str:
        folder = session_id.removeprefix(PREFIX)
        if not session_id.startswith(PREFIX) or not SESSION_ID.fullmatch(folder):
            raise CaptureError(f"not a Copilot CLI session id: {session_id!r}")
        return folder

    def _read(self, folder: str) -> _Read:
        events = self._root / folder / "events.jsonl"
        try:
            with events.open("rb") as file:
                inode = os.fstat(file.fileno()).st_ino
                raw = file.read()
        except FileNotFoundError as error:
            raise CaptureError(f"Copilot CLI session {PREFIX}{folder}: no {events}") from error
        except OSError as error:
            raise CaptureError(f"Copilot CLI session {PREFIX}{folder}: {error}") from error
        # Copilot appends while it runs: a last line without its newline is still being written.
        complete = raw[: raw.rfind(b"\n") + 1]
        builder = _Builder(PREFIX + folder)
        # One event per "\n", as notes.py splits: bytes.splitlines() would also cut on "\r", which valid JSON never
        # leaves raw, so this reads no file differently; it only says what a JSON Lines file is.
        for number, line in enumerate(complete.split(b"\n")[:-1], 1):
            try:
                event = json.loads(line)
            except ValueError:
                log.warning("Copilot CLI session %s: line %d isn't JSON", folder, number)
                continue
            if not isinstance(event, dict):
                continue
            try:
                builder.add(event)
            except (TypeError, ValueError, AttributeError, KeyError) as error:
                # An event of a shape this reader doesn't know is skipped like a line that isn't JSON.
                log.warning("Copilot CLI session %s: line %d: %r", folder, number, error)
        try:
            # Copilot only appends to a session's file, so the file itself names the one transcript it holds.
            return builder.build(revision=str(inode))
        except ValidationError as error:
            raise CaptureError(f"Copilot CLI session {PREFIX}{folder}: {error}") from error


def _stamp(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


class _Builder:
    """Folds a session's events into its messages, as agentsview's Copilot parser does."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.context: dict[str, Any] = {}
        self.model: str | None = None
        self.first: str | None = None
        self.last: str | None = None
        self.messages: list[dict[str, Any]] = []
        self.calls: dict[str, dict[str, Any]] = {}  # by toolCallId, into the message that made the call

    def add(self, event: dict[str, Any]) -> None:
        stamp = _stamp_of(event.get("timestamp"))  # one event's bad timestamp spoils nothing else
        if stamp:
            self.first = self.first or stamp
            self.last = stamp
        data = event.get("data")
        if not isinstance(data, dict):
            return
        match event.get("type"):
            case "session.start":
                if isinstance(data.get("context"), dict):
                    self.context = data["context"]
            case "session.model_change":
                self.model = _text(data.get("newModel")) or self.model
            case "user.message":
                self._prompt(data, stamp)
            case "assistant.message":
                self._reply(data, stamp)
            case "tool.execution_start":
                self._result(data, stamp, "started")
            case "tool.execution_complete":
                # Failed: said so, or an error with no result, whatever `success` says or leaves out.
                failed = data.get("success") is False or (
                    data.get("error") is not None and data.get("result") is None
                )
                self._result(data, stamp, "errored" if failed else "completed")

    def _prompt(self, data: dict[str, Any], stamp: str | None) -> None:
        content = (_text(data.get("content")) or "").strip()
        # A skill's text, which Copilot sends as a user message, isn't a prompt.
        if not content or (_text(data.get("source")) or "").startswith("skill-") or content.startswith("<skill"):
            return
        self._append({"role": "user", "content": content, "timestamp": stamp})

    def _reply(self, data: dict[str, Any], stamp: str | None) -> None:
        calls = []
        requests = data.get("toolRequests")
        for request in requests if isinstance(requests, list) else []:
            if not isinstance(request, dict) or not _text(request.get("name")):
                continue
            arguments = request.get("arguments")
            call = {
                "tool_use_id": _text(request.get("toolCallId")),
                "tool_name": request["name"],
                "category": _category(request["name"]),
                # Compact, as Copilot wrote it and agentsview keeps it.
                "input_json": arguments if isinstance(arguments, str) else _compact(arguments),
                "result_events": [],
            }
            calls.append(call)
            if call["tool_use_id"]:
                self.calls[call["tool_use_id"]] = call
        content = (_text(data.get("content")) or "").strip()
        thinking = (_text(data.get("reasoningText")) or "").strip()
        if not content and not calls and not thinking:
            return
        model = _text(data.get("model")) or self.model
        self._append(
            {"role": "assistant", "content": content, "thinking_text": thinking, "model": model, "timestamp": stamp}
            | {"tool_calls": calls}
        )

    def _result(self, data: dict[str, Any], stamp: str | None, status: str) -> None:
        call = self.calls.get(_text(data.get("toolCallId")) or "")
        if call is None:
            return
        content = None
        if status != "started":
            result, error = data.get("result"), data.get("error")
            # A failed call has no result, only its error: keep that text, which agentsview drops.
            source = result if status == "completed" else error
            content = _text(source.get("content") if isinstance(source, dict) else source)
            if status == "errored" and isinstance(error, dict):
                content = _text(error.get("message")) or content
            call["result_content"] = content
            shell = data.get("shellExecution")
            if isinstance(shell, dict) and isinstance(shell.get("exitCode"), int):
                call["exit_code"] = shell["exitCode"]
        call["result_events"].append({"status": status, "content": content, "timestamp": stamp})

    def _append(self, message: dict[str, Any]) -> None:
        defaults = {"session_id": self.session_id, "ordinal": len(self.messages), "is_system": False}
        defaults |= {"thinking_text": None, "model": None, "tool_calls": []}
        self.messages.append(defaults | message)

    def build(self, revision: str) -> _Read:
        messages = tuple(Message.model_validate(message) for message in self.messages)
        cwd = _text(self.context.get("cwd"))
        root = _text(self.context.get("gitRoot")) or cwd
        session = SessionMeta.model_validate(
            {
                "id": self.session_id,
                "agent": AGENT,
                "project": Path(root).name if root else "",  # a display name; the tracker works out the id (§4.4)
                "cwd": cwd,
                "git_branch": _text(self.context.get("branch")),
                "is_automated": False,
                "started_at": self.first,
                "ended_at": self.last,
                "message_count": len(messages),
                "transcript_revision": revision,
            }
        )
        return _Read(session, messages)


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _stamp_of(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


# agentsview's categories (NormalizeToolCategory in v0.46.1) for the names Copilot's tools use, so both sources agree.
CATEGORIES = {
    "bash": "Bash",
    "shell": "Bash",
    "apply_patch": "Edit",
    "edit": "Edit",
    "edit_file": "Edit",
    "write": "Write",
    "view": "Read",
    "read": "Read",
    "glob": "Glob",
    "grep": "Grep",
    "report_intent": "Tool",
}


def _category(tool: str) -> str:
    return "Task" if "subagent" in tool else CATEGORIES.get(tool, "Other")
