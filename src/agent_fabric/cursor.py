"""Cursor CLI's sessions, read from its own chat folders (docs/design.md §4.1).

agentsview fails every Cursor CLI session under a read-only home and drops its tool results, so Cursor CLI gets its
own `CaptureSource`. A session is `~/.cursor/chats/<md5 of its folder>/<id>/`: `meta.json` holds its folder and
times, and `store.db` the whole conversation, which a chat that never got a message doesn't have. The store is
content-addressed: each blob's id is the sha256 of its bytes, and `meta` key `0` (hex-encoded JSON) names the root
blob, a protobuf message whose field 1 lists the conversation's message blobs in order. Those are JSON, shaped like
AI SDK messages (`system`, `user`, `assistant`, `tool`).
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agent_fabric.capture import CaptureError, Message, SessionMeta, Transcript

log = logging.getLogger(__name__)

AGENT = "cursor"
PREFIX = "cursor:"  # agentsview's prefix, so a session has one id whichever source reads it
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FOLDER = re.compile(r"[0-9a-f]{32}")
TURNS = 8  # the root's field that indexes the conversation's turns
POLL = 1.0  # seconds between looks at a watched session's files
READ_TRIES = 3  # reads of a store, each made again if Cursor wrote to it meanwhile
READ_PAUSE = 0.05  # seconds before the second read, twice that before the third
LOCK_WAIT = 1.0  # seconds a read waits on Cursor's lock: a busy store is read again, not waited on


class _Unfilled(ValueError):
    """A store Cursor made but hasn't named a conversation in yet: not a session yet, nor a broken one."""


@dataclass(frozen=True)
class _Turn:
    """A turn as Cursor indexes it: the request it answers and when the prompt was sent, each reply's text and when
    it was written, and each tool call's id with when it started and ended."""

    request: str | None  # the id of the prompt's request, which the prompt's own message carries
    at: datetime | None  # None where the prompt can't be read: its calls still have their times
    replies: tuple[tuple[str, datetime], ...]
    calls: tuple[tuple[str, datetime | None, datetime | None], ...] = ()


@dataclass(frozen=True)
class _Read:
    session: SessionMeta
    messages: tuple[Message, ...]


class CursorSource:
    """`CaptureSource` over Cursor CLI's chat folders, read only."""

    def __init__(self, env: Mapping[str, str], *, poll: float = POLL) -> None:
        self._root = Path(env["HOME"]) / ".cursor" / "chats"
        self._poll = poll

    def active_sessions(self, since: datetime) -> list[SessionMeta]:
        if since.tzinfo is None:
            raise ValueError("a naive datetime has no zone to compare")
        found = []
        chats, unreadable = self._chats()
        for error in unreadable:
            # One session that can't be read mustn't stop capture of every other (§4.1).
            log.warning("skipping a Cursor CLI session: %s", error)
        for folder in chats.values():
            try:
                if _written(folder) < since:
                    continue
                found.append(self._read(folder).session)
            except FileNotFoundError:
                continue  # removed since the listing
            except CaptureError as error:
                if isinstance(error.__cause__, FileNotFoundError | _Unfilled):
                    continue  # removed between the listing and the read, or not begun
                # One session that can't be read mustn't stop capture of every other (§4.1).
                log.warning("skipping Cursor CLI session %s: %s", folder.name, error)
            except OSError as error:
                log.warning("skipping Cursor CLI session %s: %s", folder.name, error)
        return found

    def session(self, session_id: str) -> SessionMeta:
        return self._read(self._folder(session_id)).session

    def messages(self, session_id: str, from_ordinal: int) -> Transcript:
        read = self._read(self._folder(session_id))
        later = tuple(message for message in read.messages if message.ordinal >= from_ordinal)
        return Transcript(read.session.transcript_revision if later else None, later)

    async def watch(self, session_id: str) -> AsyncIterator[None]:
        """A tick on opening and one each time Cursor writes the session: its store, its WAL or its meta, in the copy
        it's read from, found again each time. The -shm counts only by being there, as other readers of the store
        write to it (this one opens it read only, or not at all). Each look is made in a worker thread, as the
        tracker reads its sources."""
        seen = await asyncio.to_thread(self._watched, session_id)
        if seen is None:
            raise CaptureError(f"Cursor CLI session {session_id}: no store") from FileNotFoundError(session_id)
        yield None
        while True:
            await asyncio.sleep(self._poll)
            now = await asyncio.to_thread(self._watched, session_id)
            if now is None:
                return  # the session was removed: nothing left to watch
            if now != seen:
                seen = now
                yield None

    def _watched(self, session_id: str) -> tuple[object, ...] | None:
        """What a watch compares: the folder read and its store's and meta's stamps; None once the store is gone."""
        try:
            folder = self._folder(session_id)
            return folder, _stamps(folder / "store.db"), _stamp(folder / "meta.json")
        except CaptureError as error:
            if error.gone:
                return None
            raise
        except FileNotFoundError:
            return None
        except OSError as error:
            raise CaptureError(f"Cursor CLI session {session_id}: {error}") from None

    def _folder(self, session_id: str) -> Path:
        name = session_id.removeprefix(PREFIX)
        if not session_id.startswith(PREFIX) or not SESSION_ID.fullmatch(name):
            raise CaptureError(f"not a Cursor CLI session id: {session_id!r}")
        chats, unreadable = self._chats(name)
        if name in chats:
            return chats[name]
        if unreadable:  # there, maybe, but not to be looked at: not gone
            raise CaptureError(f"Cursor CLI session {session_id}: {unreadable[0]}")
        raise CaptureError(f"Cursor CLI session {session_id}: no chat folder") from FileNotFoundError(name)

    def _chats(self, name: str | None = None) -> tuple[dict[str, Path], list[OSError]]:
        """Each session's chat folder, or the one named, and why any session folder couldn't be looked at. A session
        in two folders, as a copy can leave it, is read from the one written to last, which Cursor is still writing
        to if either. The chats folder, or a folder of them, that can't be listed is a CaptureError, not an empty
        one, so that capture shows as down rather than every session as gone; one session folder that can't be
        looked at is that session's alone."""
        chats: dict[str, Path] = {}
        unreadable: list[OSError] = []
        for folder in self._folders(name):
            try:
                os.stat(folder / "store.db")
            except (FileNotFoundError, NotADirectoryError):
                continue  # never got a message, removed since the listing, or not a chat
            except OSError as error:
                unreadable.append(error)
                continue
            if folder.name in chats and _latest(chats[folder.name]) >= _latest(folder):
                continue
            chats[folder.name] = folder
        return chats, unreadable

    def _folders(self, name: str | None) -> list[Path]:
        try:
            projects = sorted(
                entry for entry in self._root.iterdir() if FOLDER.fullmatch(entry.name) and entry.is_dir()
            )
        except FileNotFoundError:
            return []  # no Cursor CLI here
        except OSError as error:
            raise CaptureError(f"Cursor CLI's chats in {self._root}: {error}") from None
        folders = []
        for project in projects:
            if name is not None:
                folders.append(project / name)
                continue
            try:
                folders += sorted(entry for entry in project.iterdir() if SESSION_ID.fullmatch(entry.name))
            except FileNotFoundError:
                continue  # removed since the listing
            except OSError as error:
                raise CaptureError(f"Cursor CLI's chats in {project}: {error}") from None
        return folders

    def _read(self, folder: Path) -> _Read:
        session_id = PREFIX + folder.name
        try:
            root, messages, turns = _conversation(folder / "store.db")
            written = _written(folder)  # after the read, so the session ends no sooner than what it read
            built = _messages(
                session_id, messages, turns
            )  # in here too: a message too deep to write spoils its session
        except (OSError, ValueError, RecursionError, sqlite3.Error) as error:
            # A store that isn't there is a session gone (CaptureError.gone), as is its folder.
            raise CaptureError(f"Cursor CLI session {session_id}: {error}") from error
        try:
            meta = json.loads((folder / "meta.json").read_bytes())
        except (OSError, ValueError) as error:
            # Gone only if the store went with it: a store without its meta is a session that can't be read.
            gone = isinstance(error, FileNotFoundError) and not (folder / "store.db").exists()
            raise CaptureError(f"Cursor CLI session {session_id}: meta.json: {error}") from (error if gone else None)
        if not isinstance(meta, dict):
            raise CaptureError(f"Cursor CLI session {session_id}: meta.json isn't an object")
        cwd = meta.get("cwd")
        # Only an absolute folder names one: an empty or relative one would be read from fabricd's own folder.
        cwd = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None
        try:
            session = SessionMeta.model_validate(
                {
                    "id": session_id,
                    "agent": AGENT,
                    "project": Path(cwd).name if cwd else "",  # a display name; the tracker works out the id (§4.4)
                    "cwd": cwd,
                    "is_automated": False,
                    "started_at": _ms(meta.get("createdAtMs")),
                    "ended_at": written,
                    "message_count": len(built),
                    "transcript_revision": root,
                }
            )
            return _Read(session, tuple(Message.model_validate(message) for message in built))
        except ValidationError as error:
            raise CaptureError(f"Cursor CLI session {session_id}: {error}") from error


def _latest(folder: Path) -> datetime:
    """`_written`, with a folder that can't be looked at counting as written never."""
    try:
        return _written(folder)
    except OSError:
        return datetime.min.replace(tzinfo=UTC)


def _written(folder: Path) -> datetime:
    """When the session was last written to: Cursor updates `meta.json` only now and then while a chat runs, so its
    store's files count too."""
    stamps = [(folder / "store.db").stat().st_mtime]  # none: the session is gone
    for name in ("store.db-wal", "meta.json"):
        try:
            stamps.append((folder / name).stat().st_mtime)
        except FileNotFoundError:
            continue  # SQLite removes the WAL when Cursor's last connection closes
    return datetime.fromtimestamp(max(stamps), UTC)


def _conversation(store: Path) -> tuple[str, list[dict[str, Any]], list[_Turn | None]]:
    """The store's conversation, read where it is (§4.1). Cursor keeps the store in WAL mode, and SQLite can't open
    such a store read only without a -shm beside it; where the folder can be written, as under fabricd's unit, it
    would make one. While Cursor runs, its -wal and -shm are there, and the store opens read only on Cursor's own
    -shm, its writes in the WAL included, under SQLite's locks. Once Cursor closes it, the WAL is gone or empty and
    the store holds everything: it opens immutable, which needs no -shm and takes no locks. A WAL left without its
    -shm, as a crash leaves it, isn't read, and nothing is made beside it.

    A read is made again, up to three times, when the store or its WAL changed as it was made: an immutable one
    doesn't see Cursor write, and one made read only as Cursor closed finds the WAL gone. That last open leaves an
    empty -wal in a folder it can write, which SQLite makes even read only; it reads as no WAL next time, and is
    the one thing this reader can leave beside Cursor's files (§8)."""
    unfilled: _Unfilled | None = None
    for attempt in range(READ_TRIES):
        time.sleep(READ_PAUSE * attempt)
        before = _stamps(store)  # a store gone is a session gone
        wal = before[1] is not None and before[1][1] > 0  # a WAL with something in it
        # readonly_shm: SQLite makes no -shm; mode=ro: an immutable open makes no store that isn't there.
        mode = "mode=ro&readonly_shm=1" if wal else "mode=ro&immutable=1"
        try:
            read = _open(store, mode)
        except (sqlite3.DatabaseError, ValueError) as failed:
            if isinstance(failed, _Unfilled):
                unfilled = failed
            if wal and not before[2]:
                time.sleep(READ_PAUSE)  # a close removes the -shm, then the -wal: a crash leaves the -wal alone
            if _stamps(store) == before:
                raise
            continue  # Cursor wrote or closed as it was read
        if wal or _stamps(store) == before:
            return read
    # A store Cursor is still making, throughout every read, is one not begun yet.
    raise unfilled or ValueError(f"Cursor wrote the store throughout {READ_TRIES} reads")


def _open(store: Path, mode: str) -> tuple[str, list[dict[str, Any]], list[_Turn | None]]:
    try:
        db = sqlite3.connect(f"{store.as_uri()}?{mode}", uri=True, timeout=LOCK_WAIT)
    except sqlite3.Error as error:
        if not store.exists():
            raise FileNotFoundError(store) from error  # removed as it opened
        raise
    with closing(db):
        db.execute("BEGIN")  # one snapshot for every read, though Cursor writes meanwhile
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
        if not {"blobs", "meta"} <= tables:
            raise _Unfilled("the store has no tables yet")  # as while Cursor creates it
        read = _blobs(db)
    return read


def _stamps(store: Path) -> tuple[tuple[int, int, int], tuple[int, int, int] | None, bool]:
    """The store's and its WAL's size and time, and whether the -shm is there, which a close removes first: what tells
    a read made while Cursor wrote or closed, to make again."""
    stamp = _stamp(store)
    if stamp is None:
        raise FileNotFoundError(store)
    return stamp, _stamp(store.with_name("store.db-wal")), _stamp(store.with_name("store.db-shm")) is not None


def _stamp(path: Path) -> tuple[int, int, int] | None:
    """A file's inode, size and time; None where it isn't there. Any other error is raised, on Python 3.13 as on
    3.14, where Path.exists would swallow it."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def _blobs(db: sqlite3.Connection) -> tuple[str, list[dict[str, Any]], list[_Turn | None]]:
    """The root blob's id, the conversation's messages in its order, and its turns as Cursor indexes them."""
    row = db.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
    if row is None:
        raise _Unfilled("the store names no conversation yet")
    if not isinstance(row[0], str):
        raise ValueError("the store names no root")
    head = json.loads(bytes.fromhex(row[0]))
    root = head.get("latestRootBlobId") if isinstance(head, dict) else None
    if not isinstance(root, str):
        raise ValueError("the store names no root")
    row = db.execute("SELECT data FROM blobs WHERE id = ?", (root,)).fetchone()
    if row is None or not isinstance(row[0], bytes | str):
        raise ValueError(f"the store has no root blob {root}")
    messages = []
    fields = _fields(row[0] if isinstance(row[0], bytes) else row[0].encode())
    for number, value in fields:
        if number != 1 or not isinstance(value, bytes):
            continue
        row = db.execute("SELECT data FROM blobs WHERE id = ?", (value.hex(),)).fetchone()
        if row is None or not isinstance(row[0], bytes | str):
            raise ValueError(f"the store has no message blob {value.hex()}")
        message = json.loads(row[0])
        if not isinstance(message, dict):
            raise ValueError(f"message blob {value.hex()} isn't a JSON object")
        messages.append(message)
    return root, messages, [_turn(db, value) for number, value in fields if number == TURNS]


def _turn(db: sqlite3.Connection, value: int | bytes) -> _Turn | None:
    """A turn from the root's index, which carries the times the messages' own blobs don't (as agentsview reads it,
    though it reads only the first): field 1 the turn, whose field 3 is the id of the request it answers, which the
    prompt's own message carries; whose field 1 is the prompt (its time in milliseconds in field 25, or else 26); and
    each field 2 a step: a reply's in its field 1 (text, time), the reasoning's in field 3, a tool call's in field 2
    (its id in field 57, its start and end in 59 and 60); each held inline or in a blob of its own. None for a turn
    of a shape this reader doesn't know, and a step of one is left out: it times nothing, and spoils nothing else. A
    store that can't be read as the turn is (Cursor holding it) is raised, to be read again."""
    try:
        turn = _message_of(db, _field(_message_of(db, value), 1))
    except ValueError:
        return None
    try:
        prompt = _message_of(db, _field(turn, 1))
    except ValueError:
        prompt = []  # none to read: the turn's calls still have their times
    request = _field(turn, 3, None)
    at = _turn_ms(_field(prompt, 25, None)) or _turn_ms(_field(prompt, 26, None))
    replies, calls = [], []
    for number, step in turn:
        if number != 2:
            continue
        try:
            fields = _message_of(db, step)
            reply, call = _field(fields, 1, None), _field(fields, 2, None)
            block = _message_of(db, reply) if isinstance(reply, bytes) else []  # none: reasoning, or a tool call
            done = _message_of(db, call) if isinstance(call, bytes) else []
        except ValueError:
            continue
        text, when = _field(block, 1, None), _turn_ms(_field(block, 2, None))
        if isinstance(text, bytes) and when is not None:
            replies.append((text.decode(errors="replace").strip(), when))
        if isinstance(called := _field(done, 57, None), bytes) and called:
            calls.append(
                (called.decode(errors="replace"), _turn_ms(_field(done, 59, None)), _turn_ms(_field(done, 60, None)))
            )
    asked = request.decode(errors="replace") if isinstance(request, bytes) and request else None
    return _Turn(asked, at, tuple(replies), tuple(calls))


def _turn_ms(value: object) -> datetime | None:
    """A time in a turn, in milliseconds; None for a number that can't be one, such as seconds (as agentsview
    reads them: Cursor's are near 1.8e12)."""
    if not isinstance(value, int) or not 10**12 <= value < 10**14:
        return None
    return _ms(value)


def _message_of(db: sqlite3.Connection, value: int | bytes | None) -> list[tuple[int, int | bytes]]:
    """A protobuf message held inline, or in the blob a 32-byte field names."""
    if not isinstance(value, bytes):
        raise ValueError("not a message")
    if len(value) == 32:
        row = db.execute("SELECT data FROM blobs WHERE id = ?", (value.hex(),)).fetchone()
        if row is not None and isinstance(row[0], bytes | str):
            return _fields(row[0] if isinstance(row[0], bytes) else row[0].encode())
    return _fields(value)


def _field(fields: list[tuple[int, int | bytes]], number: int, default: object = ...) -> Any:
    """A field's value: its last, as protobuf reads a field written more than once."""
    found = next((value for found, value in reversed(fields) if found == number), default)
    if found is ...:
        raise ValueError(f"no field {number}")
    return found


def _messages(session_id: str, messages: list[dict[str, Any]], turns: list[_Turn | None]) -> list[dict[str, Any]]:
    """The prompts and replies, numbered with no gaps: not Cursor's system prompt, its context or anything else it
    sends as a user message, nor the tool results, nor a prompt or reply with nothing to read, as Copilot CLI's reader
    leaves them out. A reply that names no model was made by the one before's. The ordinals are this reader's own:
    agentsview doesn't read Cursor CLI's sessions for fabricd (§4.1)."""
    reads: list[dict[str, Any]] = []
    latest: dict[str, dict[str, Any]] = {}  # the latest call read with each id, which a result after it folds into
    for message in messages:
        if message.get("role") == "assistant" or (message.get("role") == "user" and _query(message)):
            reads.append(_message(session_id, 0, message))
            latest |= {call["tool_use_id"]: call for call in reads[-1]["tool_calls"] if call["tool_use_id"]}
        elif message.get("role") == "tool":
            for call, result in _results(message):
                if call in latest:
                    _fold(latest[call], result)
    model = None
    for read in reads:
        if read["role"] == "assistant":
            model = read["model"] = read["model"] or model
    _time(reads, turns)  # before the empty are left out: an empty prompt still ends its turn's replies
    built = [read for read in reads if read["content"] or read["thinking_text"] or read["tool_calls"]]
    for ordinal, read in enumerate(built):
        read["ordinal"] = ordinal
    return built


def _time(built: list[dict[str, Any]], turns: list[_Turn | None]) -> None:
    """Give the prompts and replies their times from Cursor's turns, as the blobs carry none. A prompt takes the turns
    named for its request, which no other prompt takes: the first time they hold is its own; each reply after it, the
    time of the next of their steps that holds its text, or else of the first of its calls they started; and a call's
    result, the time the call ended. A call is found by its id in those turns only, as a model can number its calls
    afresh each turn. Nothing else is given a time, rather than a guessed one."""
    by_request: dict[str, list[_Turn]] = {}
    for turn in turns:
        if turn and turn.request:
            by_request.setdefault(turn.request, []).append(turn)
    replies: list[tuple[str, datetime]] = []  # the prompt's turns', not yet given to a reply
    called: dict[str, tuple[datetime | None, datetime | None]] = {}
    for read in built:
        if read["role"] == "user":
            own = by_request.pop(read.pop("request", None) or "", [])
            read["timestamp"] = next((turn.at for turn in own if turn.at), None)
            replies = [step for turn in own for step in turn.replies]
            called = _calls(own)
            continue
        step = next((i for i, (text, _) in enumerate(replies) if text and text == read["content"]), None)
        if step is not None:
            read["timestamp"] = replies[step][1]
            replies = replies[step + 1 :]
        for call in read["tool_calls"]:
            start, end = called.get(call["tool_use_id"], (None, None)) if call["tool_use_id"] else (None, None)
            read["timestamp"] = read["timestamp"] or start
            for event in call.get("result_events", []):
                event["timestamp"] = end


def _calls(turns: Iterable[_Turn]) -> dict[str, tuple[datetime | None, datetime | None]]:
    """Each call's start and end by its id, the first of each the turns hold: a turn stopped before its call ended
    can leave the end to another for the same request."""
    found: dict[str, tuple[datetime | None, datetime | None]] = {}
    for turn in turns:
        for call, start, end in turn.calls:
            was_start, was_end = found.get(call, (None, None))
            found[call] = (was_start or start, was_end or end)
    return found


def _query(message: dict[str, Any]) -> str | None:
    """A prompt's text, which Cursor wraps in `<user_query>`, as it doesn't wrap anything else it sends as a user
    message, such as its context (the machine, the repo, the user's rules), which it sends first. Only the text part
    that holds it: another part, such as a file attached, isn't what the user typed."""
    return next((text for text in _texts(message.get("content")) if "<user_query>" in text), None)


def _texts(content: object) -> list[str]:
    """A message's texts: the string, or its text parts."""
    if isinstance(content, str):
        return [content]
    return [
        part["text"] for part in _parts(content) if part.get("type") == "text" and isinstance(part.get("text"), str)
    ]


def _results(message: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """A tool message's results, each with the id of its call, to fold into the call as agentsview folds a result into
    its call: what the tool answered, and how the call ended. Cursor keeps its own record of the end on the tool
    message, which it names for the call (every one on this machine held one result, named so): a result in a
    message named for another call has no record, and no status."""
    results = []
    for part in _parts(message.get("content")):
        call = _text_of(part.get("toolCallId"))
        if part.get("type") != "tool-result" or call is None:
            continue
        ended = _ended(message) if message.get("id") == call else {}
        found = ended.get("output")
        output: dict[str, Any] = found if isinstance(found, dict) else {}
        content = _answer(part.get("result"))
        results.append((call, {"result_content": content, "status": _status(ended, output), "output": output}))
    return results


def _fold(call: dict[str, Any], result: dict[str, Any]) -> None:
    """A result into its call: of two results for one call, the later in the conversation is kept."""
    call["result_content"] = result["result_content"]
    call["result_events"] = [{"status": result["status"], "content": result["result_content"]}]
    call["exit_code"] = _exit(result["output"]) if call["category"] == "Bash" else None


def _status(ended: dict[str, Any], output: dict[str, Any]) -> str | None:
    """How the call ended, by Cursor's record: `completed` where its outcome is `success`, `running` for a command
    left in the background, `errored` for any other outcome (a rejection, an error, a failure whatever its exit code)
    or where it flags an error, and None where it names no outcome."""
    if ended.get("isError") is True or ended.get("rawErrorMessages"):
        return "errored"
    if _succeeded(output):
        return "running" if output.get("isBackground") is True else "completed"
    # An outcome is the record's one object beside its settings, such as the sandbox it ran in.
    return "errored" if any(isinstance(v, dict) for k, v in output.items() if k != "sandboxPolicy") else None


def _succeeded(output: dict[str, Any]) -> bool:
    return isinstance(output.get("success"), dict)


def _answer(answer: object) -> str | None:
    """What a tool answered, as text: a string, the text of content parts (none for parts with no text, such as an
    image's data), or else its JSON, NaN and all."""
    if answer is None or isinstance(answer, str):
        return answer
    if (
        isinstance(answer, list)
        and answer
        and all(isinstance(t := _type(part), str) and t in PART_TYPES for part in answer)
    ):
        return "\n".join(texts) if (texts := _texts(answer)) else None
    return json.dumps(answer, ensure_ascii=False, separators=(",", ":"))


PART_TYPES = frozenset({"text", "image", "media", "file"})  # the AI SDK's content parts


def _type(part: object) -> object:
    return part.get("type") if isinstance(part, dict) else None


def _ended(message: dict[str, Any]) -> dict[str, Any]:
    """Cursor's own record of how a call ended: its `output` (`success`, `failure` with the exit code, `rejected`,
    `error`) and whether it was an error."""
    options = message.get("providerOptions")
    cursor = options.get("cursor") if isinstance(options, dict) else None
    ended = cursor.get("highLevelToolCallResult") if isinstance(cursor, dict) else None
    return ended if isinstance(ended, dict) else {}


def _message(session_id: str, ordinal: int, message: dict[str, Any]) -> dict[str, Any]:
    """A prompt or a reply as `capture.Message` holds it. A prompt is the text Cursor wraps in `<user_query>`; a reply
    is its text parts, its reasoning and its tool calls, the model named on its reasoning."""
    said = {"session_id": session_id, "ordinal": ordinal, "role": message["role"], "timestamp": None}
    said |= {"is_system": False, "thinking_text": None, "model": None, "tool_calls": []}
    if message["role"] == "user":
        options = message.get("providerOptions")
        cursor = options.get("cursor") if isinstance(options, dict) else None
        request = cursor.get("requestId") if isinstance(cursor, dict) else None
        return said | {"content": _prompt(_query(message) or ""), "request": _text_of(request)}
    parts = [part for part in _parts(message.get("content")) if isinstance(part.get("type"), str)]
    # Joined as agentsview joins a reply's lines, and trimmed only at the ends, so a part keeps its indentation.
    texts = _texts(message.get("content"))
    reasoning = [part for part in parts if part["type"] == "reasoning"]
    thoughts = [part["text"] for part in reasoning if isinstance(part.get("text"), str)]
    models = [_model(part) for part in reasoning]
    return said | {
        "content": "\n".join(texts).strip(),
        "thinking_text": "\n".join(thoughts).strip() or None,
        "model": next((model for model in models if model), None),
        "tool_calls": [
            _call(part) for part in parts if part["type"] == "tool-call" and _text_of(part.get("toolName"))
        ],
    }


def _prompt(text: str) -> str:
    """What the user typed: Cursor wraps it in `<user_query>`, after a `<timestamp>`, as agentsview reads it."""
    start = text.find("<user_query>")
    if start >= 0:
        end = text.rfind("</user_query>")
        text = text[start + len("<user_query>") : end if end > start else len(text)]  # cut short: what's there
    return text.strip()


def _call(part: dict[str, Any]) -> dict[str, Any]:
    arguments = part.get("args", part.get("input"))  # the AI SDK's older name, which Cursor writes, and its newer
    return {
        "tool_use_id": _text_of(part.get("toolCallId")),
        "tool_name": part["toolName"],
        "category": _category(part["toolName"]),
        # Compact, as agentsview keeps a call's input.
        "input_json": _input(arguments),
    }


def _exit(output: dict[str, Any]) -> int | None:
    """A command's exit code, as Cursor records it apart: the outcome's own, and else 0 for a success, unless the
    command was left running in the background, which hasn't exited."""
    outcome = output.get("success") if _succeeded(output) else output.get("failure")
    code = outcome.get("exitCode") if isinstance(outcome, dict) else None
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return 0 if _succeeded(output) and output.get("isBackground") is not True else None


def _input(arguments: object) -> str | None:
    """A call's input as JSON, compact as agentsview keeps it; None for one JSON can't write, such as a NaN."""
    if arguments is None or isinstance(arguments, str):
        return arguments  # none, or written as JSON already, as Cursor can
    try:
        return json.dumps(arguments, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except ValueError:
        return None


def _model(part: dict[str, Any]) -> str | None:
    options = part.get("providerOptions")
    cursor = options.get("cursor") if isinstance(options, dict) else None
    return _text_of(cursor.get("modelName")) if isinstance(cursor, dict) else None


def _parts(content: object) -> list[dict[str, Any]]:
    return [part for part in content if isinstance(part, dict)] if isinstance(content, list) else []


def _text_of(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


# agentsview's categories (NormalizeToolCategory in v0.46.1) for the names Cursor's tools use, so both sources agree.
CATEGORIES = {
    "Shell": "Bash",
    "StrReplace": "Edit",
    "ApplyPatch": "Edit",
    "Edit": "Edit",
    "Write": "Write",
    "Read": "Read",
    "LS": "Read",
    "Glob": "Glob",
    "Grep": "Grep",
    "Subagent": "Task",
    "TodoWrite": "Tool",
    "WebFetch": "Tool",
    "WebSearch": "Tool",
}


def _category(tool: str) -> str:
    return "Task" if "subagent" in tool else CATEGORIES.get(tool, "Other")


def _ms(value: object) -> datetime | None:
    """A time in milliseconds, as `meta.json` keeps them; None for one that isn't a time."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (ValueError, OverflowError, OSError):  # NaN, infinite, or out of range
        return None


def _fields(data: bytes) -> list[tuple[int, int | bytes]]:
    """A protobuf message's fields, in order: a varint's value, or a length-delimited field's bytes."""
    fields: list[tuple[int, int | bytes]] = []
    at = 0
    while at < len(data):
        key, at = _varint(data, at)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, at = _varint(data, at)
            fields.append((number, value))
        elif wire == 2:
            size, at = _varint(data, at)
            if at + size > len(data):
                raise ValueError("a protobuf field runs past its message")
            fields.append((number, data[at : at + size]))
            at += size
        elif wire in (1, 5):
            at += 8 if wire == 1 else 4
            if at > len(data):
                raise ValueError("a protobuf field runs past its message")
        else:
            raise ValueError(f"protobuf wire type {wire}")
    return fields


def _varint(data: bytes, at: int) -> tuple[int, int]:
    value = shift = 0
    for _ in range(10):  # protobuf's longest, for 64 bits
        if at >= len(data):
            raise ValueError("a protobuf varint runs past its message")
        byte = data[at]
        at += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, at
    raise ValueError("a protobuf varint longer than 10 bytes")
