"""The tracker: which sessions are going on, and when each ends a turn or finishes (docs/design.md §4.3).

It lists the sessions active in the last 24 hours from each source, reads a session's messages when its listing or
its watch says it changed, and keeps one row per session in `state.db`. What it notices it returns as `Change`s and
appends to the same database, for the digester and the panel to follow:

- `started`: first seen;
- `turn_end`: the last message is the agent's, with no tool call left to finish, and nothing changed for 5 s;
- `finalized`: no new activity for the idle timeout (30 min), since no agent says reliably that a session ended;
- `reopened`: activity in a finalized session, which finalizes again later.

Only `Tracker.poll` and `Tracker.tick` read the sources; `run` drives them from a clock and the sessions' watches.
"""

import asyncio
import hashlib
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, NoReturn

import httpx

from agent_fabric.capture import CaptureError, CaptureSource, Message, SessionMeta, ToolCall
from agent_fabric.gitinfo import git_project
from agent_fabric.paths import state_db

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)  # how far back a listing asks
IDLE = timedelta(minutes=30)
TURN_QUIET = timedelta(seconds=5)
POLL = 10.0  # seconds between listings

Kind = Literal["started", "turn_end", "finalized", "reopened"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    feed TEXT NOT NULL,           -- the source that reads it
    agent TEXT NOT NULL,
    project TEXT NOT NULL,        -- resolved when first seen (§4.4)
    cwd TEXT,
    headless INTEGER NOT NULL,
    state TEXT NOT NULL,          -- active | finalized
    finalizations INTEGER NOT NULL,
    listing TEXT NOT NULL,        -- what the last listing said, to tell when to read again
    revision TEXT,                -- the transcript revision the cursor holds for
    cursor INTEGER NOT NULL,      -- where the next read starts: past the last message, or at a call not yet finished
    tail TEXT NOT NULL,           -- a hash of what the last read found from the cursor on
    last_ordinal INTEGER NOT NULL,
    ends_turn INTEGER NOT NULL,
    answer TEXT,                  -- a hash of the last message not a system one, which names the turn it ends
    turn_ended TEXT,              -- the answer the last turn_end was given for
    changed_at TEXT NOT NULL,     -- when the tracker last saw the messages change
    active_at TEXT NOT NULL       -- the session's last activity, from its own timestamps where it has them
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Feed:
    """A source and the agents the tracker reads through it. One agent has one feed, so that a session agentsview
    also lists (Copilot CLI's, when it can read them) is read from one place."""

    name: str
    source: CaptureSource
    agents: frozenset[str]


@dataclass(frozen=True)
class Change:
    kind: Kind
    session_id: str
    ordinal: int  # the session's last message when it happened; -1 for a session with none yet
    at: datetime


@dataclass(frozen=True)
class Tracked:
    id: str
    feed: str
    agent: str
    project: str
    cwd: str | None
    headless: bool
    state: Literal["active", "finalized"]
    finalizations: int
    listing: str
    revision: str | None
    cursor: int
    tail: str
    last_ordinal: int
    ends_turn: bool
    answer: str | None
    turn_ended: str | None
    changed_at: datetime
    active_at: datetime


def open_state(env: Mapping[str, str]) -> sqlite3.Connection:
    return connect(state_db(env))


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # `run` reads the sources in a worker thread, one call at a time, and the database goes with them.
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


class Tracker:
    def __init__(
        self,
        feeds: Sequence[Feed],
        db: sqlite3.Connection,
        *,
        idle: timedelta = IDLE,
        turn_quiet: timedelta = TURN_QUIET,
        window: timedelta = WINDOW,
    ) -> None:
        self._feeds = {feed.name: feed for feed in feeds}
        self._db = db
        self._idle = idle
        self._turn_quiet = turn_quiet
        self._window = window
        self.offline: dict[str, str] = {}  # a feed's last error while it fails, for the panel's "capture offline"
        self._unread: set[str] = set()  # sessions whose last read failed: what's known of them is out of date
        # Sessions read since this tracker started. One its listing shows unchanged is read again only if not: a
        # result can fold into a message while the tracker is down, or while a read fails, without changing the
        # listing, and a watch only opens on a session still active.
        self._read_here: set[str] = set()
        # Listed sessions their source then said it doesn't have, by listing: asked again when the listing changes.
        self._missing: dict[str, str] = {}
        # When to read a finalized session with a call not yet finished: the longer it has been still, the less often.
        self._next_read: dict[str, datetime] = {}

    def poll(self, now: datetime) -> list[Change]:
        """List each feed's sessions, read the new and the changed, and settle every active one."""
        changes: list[Change] = []
        tried: set[str] = set()  # read, or tried, in this poll
        for feed in self._feeds.values():
            try:
                listed = feed.source.active_sessions(now - self._window)
            except CaptureError as error:
                if feed.name not in self.offline:
                    log.warning("capture offline: %s: %s", feed.name, error)
                    # What changed meanwhile may not show in the listing: read its sessions again once it's back.
                    gone = self._db.execute("SELECT id FROM sessions WHERE feed = ?", (feed.name,))
                    self._read_here.difference_update(found["id"] for found in gone)
                self.offline[feed.name] = str(error)
                continue
            if self.offline.pop(feed.name, None) is not None:
                log.info("capture back: %s", feed.name)
            changes += self._read_listed(feed, listed, tried, now)
        for row in self.sessions("active"):
            # A feed that can't be asked has said nothing of its sessions: they aren't quiet, only unheard.
            owner = self._feeds.get(row.feed)
            if owner is None or owner.name in self.offline:
                continue
            if row.id not in self._read_here and row.id not in tried:
                # Active when the tracker stopped, and no longer listed: it may have gone on in the meantime.
                changes += self._read_or_skip(owner, None, row, now)
            if row.id in self._unread and now - row.active_at >= self._window:
                # Unreadable for longer than a listing looks back: whatever it did is out of reach, so settle it on
                # what was read, rather than keep it active and asked for ever.
                self._unread.discard(row.id)
            changes += self.settle(row.id, now)
        return changes

    def _read_listed(self, feed: Feed, listed: list[SessionMeta], tried: set[str], now: datetime) -> list[Change]:
        """Read the listed sessions that are new, or may have changed since they were last read."""
        changes: list[Change] = []
        for meta in listed:
            # agentsview's own runs and others it recognises as automated make no digest (§4.2, §8).
            if meta.agent not in feed.agents or meta.is_automated:
                continue
            row = self.session(meta.id)
            # A session with a call not yet finished is read again though its listing is the same: its result folds
            # into the call's message without changing the listing, and only an active session's watch would say.
            # An active one every poll; a finalized one less often the longer it has been still, as an agent killed
            # mid-call leaves one that never finishes.
            unfinished = row is not None and row.cursor <= row.last_ordinal
            if row is not None and row.listing == _listing(meta) and row.id in self._read_here:
                if not unfinished:
                    continue
                if row.state == "finalized" and now < self._next_read.get(row.id, now):
                    continue
            if row is None and self._missing.get(meta.id) == _listing(meta):
                continue
            tried.add(meta.id)
            changes += self._read_or_skip(feed, meta, row, now)
            if row is not None and row.state == "finalized" and (read := self.session(row.id)) is not None:
                self._read_later(read, now)
        return changes

    def tick(self, session_id: str, now: datetime) -> list[Change]:
        """The session's watch says it changed: read it, and settle it."""
        row = self.session(session_id)
        feed = self._feeds.get(row.feed) if row else None
        if row is None or feed is None:
            return []
        changes = self._read_or_skip(feed, None, row, now)
        return changes + self.settle(session_id, now)

    def settle(self, session_id: str, now: datetime) -> list[Change]:
        """A turn end or finalization due by now, without reading: for `run`'s check once a change has gone quiet."""
        row = self.session(session_id)
        if row is None or row.state != "active" or row.feed in self.offline or row.id in self._unread:
            return []
        return self._settle(row, now)

    def watch(self, session_id: str) -> AsyncIterator[None]:
        row = self.session(session_id)
        if row is None or row.feed not in self._feeds:
            raise KeyError(session_id)
        return self._feeds[row.feed].source.watch(session_id)

    def session(self, session_id: str) -> Tracked | None:
        found = self._db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return _tracked(found) if found else None

    def sessions(self, state: str | None = None) -> list[Tracked]:
        query = "SELECT * FROM sessions" + (" WHERE state = ?" if state else "") + " ORDER BY id"
        return [_tracked(row) for row in self._db.execute(query, (state,) if state else ())]

    def changes(self, after: int = 0) -> list[tuple[int, Change]]:
        """The changes recorded after the one numbered `after`, for a consumer that keeps its own place."""
        rows = self._db.execute("SELECT * FROM changes WHERE id > ? ORDER BY id", (after,))
        return [
            (row["id"], Change(row["kind"], row["session_id"], row["ordinal"], datetime.fromisoformat(row["at"])))
            for row in rows
        ]

    def _read_or_skip(self, feed: Feed, meta: SessionMeta | None, row: Tracked | None, now: datetime) -> list[Change]:
        session_id = row.id if row else _id(meta)
        try:
            changes = self._read(feed, meta, row, now)
        except CaptureError as error:
            if row is None and meta is not None and _gone(error):
                # Removed between the listing and the read: nothing to track.
                log.debug("%s session %s is listed but gone: %s", feed.name, session_id, error)
                self._missing[session_id] = _listing(meta)
                return []
            if row is not None and _gone(error):
                # Removed from its source: nothing more will come, so it settles on what was read.
                log.info("%s session %s is gone: %s", feed.name, session_id, error)
                self._unread.discard(session_id)
                self._read_here.add(session_id)
                return []
            # One session that can't be read mustn't stop the others, and isn't settled on what was read before it
            # changed: the next poll or tick tries again.
            log.warning("can't read %s session %s: %s", feed.name, session_id, error)
            self._unread.add(session_id)
            self._read_here.discard(session_id)
            return []
        self._unread.discard(session_id)
        self._missing.pop(session_id, None)
        self._read_here.add(session_id)
        return changes

    def _read(self, feed: Feed, meta: SessionMeta | None, row: Tracked | None, now: datetime) -> list[Change]:
        # A new transcript revision, in the listing or in the read, means agentsview re-parsed the session: ordinals
        # read before may not hold. A read past the end of a shorter transcript carries no revision, so the listing's
        # counts too.
        listed = meta.transcript_revision if meta else None
        reparsed = row is not None and row.revision is not None and listed not in (None, row.revision)
        cursor = row.cursor if row and not reparsed else 0
        transcript = feed.source.messages(row.id if row else _id(meta), cursor)
        if row and cursor and row.revision is not None and transcript.revision not in (None, row.revision):
            reparsed, cursor = True, 0
            transcript = feed.source.messages(row.id, cursor)
        messages = transcript.messages
        known = row if row and not reparsed else None  # what still holds of the last read
        # The next read starts at the new cursor, so what's kept to compare it with is what lies from there on.
        next_cursor = _cursor(messages, cursor)
        tail = _hash(message for message in messages if message.ordinal >= next_cursor)
        changed = row is None or cursor != row.cursor or _hash(messages) != row.tail
        if row is None:
            active_at = min(_latest(messages, after=-1) or _meta(meta).ended_at or now, now)
        elif not changed:
            active_at = row.active_at
        elif known is None:
            # Re-parsed: the session's own times say whether anything happened since; the re-parse itself isn't.
            active_at = max(row.active_at, min(_latest(messages, after=-1) or row.active_at, now))
        else:
            # A change counts from its own time: a new message's, or a result's that folded into an earlier call.
            # One that carries none, such as Claude Code's result, happened about now.
            latest = _latest(messages, after=row.last_ordinal)
            active_at = min(latest, now) if latest and latest > row.active_at else now
        state = row.state if row else "active"
        new = Tracked(
            id=row.id if row else _id(meta),
            feed=feed.name,
            agent=row.agent if row else _agent(meta),
            project=row.project if row else _project(_meta(meta)),
            cwd=row.cwd if row else _meta(meta).cwd,
            headless=row.headless if row else _headless(_meta(meta)),
            state="active" if row and row.state == "finalized" and active_at > row.active_at else state,
            finalizations=row.finalizations if row else 0,
            listing=_listing(meta) if meta else row.listing if row else "",
            revision=transcript.revision if transcript.revision is not None else row.revision if row else None,
            cursor=next_cursor,
            tail=tail,
            last_ordinal=messages[-1].ordinal if messages else known.last_ordinal if known else -1,
            ends_turn=_or(_ends_turn(messages), known.ends_turn if known else False),
            answer=_answer(messages) or (known.answer if known else None),
            # Kept through a re-parse: the answer's hash, not its ordinal, says whether it's the turn already ended.
            turn_ended=row.turn_ended if row else None,
            changed_at=now if changed else row.changed_at if row else now,
            active_at=active_at,
        )
        with self._db:
            _store(self._db, new)
            if row is None:
                return [self._record(new, "started", now)]
            if row.state == "finalized" and new.state == "active":
                return [self._record(new, "reopened", now)]
        return []

    def _settle(self, row: Tracked, now: datetime) -> list[Change]:
        idle = now - row.active_at >= self._idle
        changes = []
        with self._db:
            # A system message after the answer, such as a hook's, moves the last ordinal but not the answer. A
            # session idle long enough to finalize is quiet too: its last turn ends first, as after a restart.
            unended = row.ends_turn and row.answer != row.turn_ended
            if unended and (idle or now - row.changed_at >= self._turn_quiet):
                self._db.execute("UPDATE sessions SET turn_ended = answer WHERE id = ?", (row.id,))
                changes.append(self._record(row, "turn_end", now))
            if idle:
                self._db.execute(
                    "UPDATE sessions SET state = 'finalized', finalizations = finalizations + 1 WHERE id = ?",
                    (row.id,),
                )
                changes.append(self._record(row, "finalized", now))
                self._read_later(row, now)
        return changes

    def _read_later(self, row: Tracked, now: datetime) -> None:
        """When to read a finalized session again for its unfinished call: a tenth of the time it has been still."""
        if row.cursor <= row.last_ordinal:
            self._next_read[row.id] = now + max(timedelta(seconds=POLL), (now - row.changed_at) / 10)

    def _record(self, row: Tracked, kind: Kind, now: datetime) -> Change:
        change = Change(kind, row.id, row.last_ordinal, now)
        self._db.execute(
            "INSERT INTO changes (session_id, kind, ordinal, at) VALUES (?, ?, ?, ?)",
            (row.id, kind, row.last_ordinal, now.isoformat()),
        )
        return change


async def run(
    tracker: Tracker,
    *,
    emit: Callable[[Change], None] = lambda change: None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    poll: float = POLL,
    turn_quiet: float = TURN_QUIET.total_seconds(),
) -> NoReturn:
    """Poll every `poll` seconds, and follow each active session's watch in between, checking for a turn end
    `turn_quiet` seconds after its last tick. The tracker, and so its database, is used in a worker thread, one call
    at a time."""
    await _Runner(tracker, emit, clock, turn_quiet).run(poll)


class _Runner:
    def __init__(
        self, tracker: Tracker, emit: Callable[[Change], None], clock: Callable[[], datetime], turn_quiet: float
    ) -> None:
        self._tracker = tracker
        self._emit = emit
        self._clock = clock
        self._turn_quiet = turn_quiet
        self._lock = asyncio.Lock()
        self._watches: dict[str, asyncio.Task[None]] = {}
        self._checks: dict[str, asyncio.Task[None]] = {}

    async def run(self, poll: float) -> NoReturn:
        try:
            while True:
                try:
                    await self._call(self._tracker.poll)
                    active = await self._locked(lambda: {row.id for row in self._tracker.sessions("active")})
                except Exception:
                    log.exception("tracker poll failed")
                    active = set(self._watches)
                for session_id in active:
                    if session_id not in self._watches or self._watches[session_id].done():
                        self._watches[session_id] = asyncio.create_task(self._follow(session_id))
                for session_id in set(self._watches) - active:
                    self._watches.pop(session_id).cancel()
                    if (check := self._checks.pop(session_id, None)) is not None:
                        check.cancel()
                await asyncio.sleep(poll)
        finally:
            # Stopped: nothing it started may go on reading or writing.
            tasks = [*self._watches.values(), *self._checks.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _locked[R](self, step: Callable[[], R], then: Callable[[R], None] = lambda result: None) -> R:
        """Run `step` in a worker thread under the lock, then `then` on its result, even if cancelled meanwhile."""
        async with self._lock:
            job = asyncio.ensure_future(asyncio.to_thread(step))
            try:
                result = await asyncio.shield(job)
            except asyncio.CancelledError:
                # A worker thread can't be stopped: let it finish before the lock lets another use the database, and
                # pass on what it did, which is recorded already.
                await asyncio.wait([job])
                if not job.cancelled() and job.exception() is None:
                    then(job.result())
                raise
            then(result)
            return result

    async def _call(self, step: Callable[[datetime], list[Change]]) -> None:
        # The clock is read under the lock, so that one step's time never comes before the last one's.
        await self._locked(lambda: step(self._clock()), then=self._emit_all)

    def _emit_all(self, changes: list[Change]) -> None:
        for change in changes:
            self._emit(change)

    async def _follow(self, session_id: str) -> None:
        try:
            async for _ in await self._locked(lambda: self._tracker.watch(session_id)):
                await self._call(lambda now: self._tracker.tick(session_id, now))
                if (earlier := self._checks.get(session_id)) is not None:
                    earlier.cancel()
                self._checks[session_id] = asyncio.create_task(self._check_later(session_id))
        except (CaptureError, KeyError) as error:
            log.warning("watch on %s ended: %s", session_id, error)  # the next poll opens it again
        except Exception:
            log.exception("watch on %s failed", session_id)

    async def _check_later(self, session_id: str) -> None:
        try:
            await asyncio.sleep(self._turn_quiet)
            await self._call(lambda now: self._tracker.settle(session_id, now))
        except Exception:
            log.exception("turn end check on %s failed", session_id)  # the next poll settles it
        finally:
            if self._checks.get(session_id) is asyncio.current_task():
                del self._checks[session_id]


TEXT_FIELDS = tuple(
    field.name for field in fields(Tracked) if field.name not in ("headless", "ends_turn", "changed_at", "active_at")
)


def _store(db: sqlite3.Connection, row: Tracked) -> None:
    db.execute(
        "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row.id,
            row.feed,
            row.agent,
            row.project,
            row.cwd,
            row.headless,
            row.state,
            row.finalizations,
            row.listing,
            row.revision,
            row.cursor,
            row.tail,
            row.last_ordinal,
            row.ends_turn,
            row.answer,
            row.turn_ended,
            row.changed_at.isoformat(),
            row.active_at.isoformat(),
        ),
    )


def _tracked(row: sqlite3.Row) -> Tracked:
    return Tracked(
        **{key: row[key] for key in TEXT_FIELDS},
        headless=bool(row["headless"]),
        ends_turn=bool(row["ends_turn"]),
        changed_at=datetime.fromisoformat(row["changed_at"]),
        active_at=datetime.fromisoformat(row["active_at"]),
    )


def _gone(error: CaptureError) -> bool:
    """agentsview's 404, or a session folder Copilot CLI's reader no longer finds."""
    return error.status == httpx.codes.NOT_FOUND or isinstance(error.__cause__, FileNotFoundError)


def _meta(meta: SessionMeta | None) -> SessionMeta:
    assert meta is not None, "a session is first read from its listing"
    return meta


def _id(meta: SessionMeta | None) -> str:
    return _meta(meta).id


def _agent(meta: SessionMeta | None) -> str:
    return _meta(meta).agent


def _listing(meta: SessionMeta) -> str:
    """What in a listing says the session changed. Not every change: a tool's result folds into the message that
    called it without changing these, which the session's watch catches."""
    return f"{meta.message_count}|{meta.ended_at}|{meta.transcript_revision}"


def _headless(meta: SessionMeta) -> bool:
    """§4.3's markers. Copilot CLI and Cursor CLI show none yet, so their `-p` runs count as interactive."""
    return meta.entrypoint == "sdk-cli" or meta.session_kind == "non-interactive"


def _project(meta: SessionMeta) -> str:
    """§4.4's project id: the git project of the session's folder, else agentsview's project."""
    if meta.cwd and Path(meta.cwd).is_dir() and (project := git_project(Path(meta.cwd))):
        return project
    return meta.project


def _pending(call: ToolCall) -> bool:
    """Not finished: its last status says so, or it has no result at all. Claude Code gives a success an empty
    status, and a command that printed nothing an empty result, but still a result event."""
    statuses = [event.status for event in call.result_events if event.status]
    if statuses:
        return statuses[-1] in ("started", "running")
    return not call.result_events and call.result_content is None


def _cursor(messages: Sequence[Message], cursor: int) -> int:
    """Past the last message, or back at the first with a call not yet finished, whose result folds into it."""
    if not messages:
        return cursor
    unfinished = [message.ordinal for message in messages if any(_pending(call) for call in message.tool_calls)]
    return unfinished[0] if unfinished else messages[-1].ordinal + 1


def _ends_turn(messages: Sequence[Message]) -> bool | None:
    """§4.3's turn end, but for the quiet: the last message is the agent's text, and no call waits for its result.
    None when the messages say nothing of it: none read, or system messages alone, which follow what was read
    before."""
    if any(_pending(call) for message in messages for call in message.tool_calls):
        return False
    said = [message for message in messages if not message.is_system]
    if not said:
        return None
    return said[-1].role == "assistant" and not said[-1].tool_calls and bool(said[-1].content)


def _answer(messages: Sequence[Message]) -> str | None:
    """The last message not a system one, by what it says and when, not where: a re-parse can renumber it."""
    said = [message for message in messages if not message.is_system]
    return hashlib.sha256(said[-1].model_dump_json(exclude={"ordinal"}).encode()).hexdigest() if said else None


def _or(value: bool | None, otherwise: bool) -> bool:
    return otherwise if value is None else value


def _latest(messages: Iterable[Message], *, after: int) -> datetime | None:
    """The latest time in the messages: the times of messages past ordinal `after`, and of every tool result."""
    stamps = [
        stamp
        for message in messages
        for stamp in (
            message.timestamp if message.ordinal > after else None,
            *(event.timestamp for call in message.tool_calls for event in call.result_events),
        )
        if stamp is not None
    ]
    return max(stamps) if stamps else None


def _hash(messages: Iterable[Message]) -> str:
    digest = hashlib.sha256()
    for message in messages:
        digest.update(message.model_dump_json().encode())
    return digest.hexdigest()
