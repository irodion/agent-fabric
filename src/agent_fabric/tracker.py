"""The tracker: which sessions are going on, and when each ends a turn or finishes (docs/design.md §4.3).

It lists the sessions active in the last 24 hours from each source, reads a session's whole transcript when its
listing or its watch says it changed, and keeps one row per session in `state.db`. What it notices it returns as
`Change`s and appends to the same database, for the digester and the panel to follow:

- `started`: first seen;
- `turn_end`: the last message is the agent's answer, and nothing changed for 5 s;
- `finalized`: no new activity for the idle timeout (30 min), since no agent says reliably that a session ended;
- `reopened`: activity in a finalized session, which finalizes again later.

A listing shows every change to a session: agentsview raises its transcript revision on every write to its messages,
a tool's result folding into the call's message included, and Copilot CLI's reader moves its last time with every
event. So the listing alone says when to read; a watch only says it sooner.

Only `Tracker.poll` and `Tracker.tick` read the sources; `run` drives them from a clock and the sessions' watches.
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, NoReturn

from agent_fabric.capture import CaptureError, CaptureSource, Message, SessionMeta
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
    status TEXT NOT NULL,         -- ok | unread: its last read failed | gone: its source no longer has it
    listing TEXT NOT NULL,        -- what the listing said when it was last read: a change means read it again
    content TEXT NOT NULL,        -- a hash of the transcript as last read, without ordinals
    last_ordinal INTEGER NOT NULL,
    answer TEXT,                  -- names the agent's answer that ends the last turn; NULL while it hasn't
    turn_ended TEXT,              -- the answer the last turn_end was given for
    changed_at TEXT NOT NULL,     -- when the tracker last saw the transcript change
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
    status: Literal["ok", "unread", "gone"]
    listing: str
    content: str
    last_ordinal: int
    answer: str | None
    turn_ended: str | None
    changed_at: datetime
    active_at: datetime

    @property
    def ends_turn(self) -> bool:
        return self.answer is not None


def open_state(env: Mapping[str, str]) -> sqlite3.Connection:
    return connect(state_db(env))


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # `run` uses the tracker in a worker thread, one call at a time, and the database goes with it. The digester
    # writes it on a connection of its own: WAL lets each read while the other writes, and a write waits its turn.
    db = sqlite3.connect(path, check_same_thread=False, timeout=30)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
    except BaseException:
        db.close()
        raise
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
        self._missing: dict[str, str] = {}  # listed, but gone when first read: asked again if the listing changes

    def poll(self, now: datetime) -> list[Change]:
        """List each feed's sessions, read the new and the changed, and settle every active one."""
        changes: list[Change] = []
        listed: set[str] = set()
        missing: dict[str, str] = {}
        for feed in self._feeds.values():
            changes += self._read_listed(feed, now, listed, missing)
        self._missing = missing
        for row in self.sessions("active"):
            owner = self._feeds.get(row.feed)  # None: no longer configured, so it settles on what was read
            if owner is not None and owner.name in self.offline:
                continue  # a feed that can't be asked has said nothing of its sessions: unheard, not quiet
            if owner is not None and row.id not in listed and row.listing != UNLISTED and row.status != "gone":
                # Active when the tracker last looked, and no longer listed: read once what it did before going quiet.
                changes += self._read_or_skip(owner, row.id, None, now, listing=UNLISTED)
            changes += self.settle(row.id, now)
        return changes

    def _read_listed(self, feed: Feed, now: datetime, listed: set[str], missing: dict[str, str]) -> list[Change]:
        """Read the feed's listed sessions that are new or changed; note those it lists, and those it lacks."""
        try:
            sessions = feed.source.active_sessions(now - self._window)
        except CaptureError as error:
            if feed.name not in self.offline:
                log.warning("capture offline: %s: %s", feed.name, error)
            self.offline[feed.name] = str(error)
            return []
        if self.offline.pop(feed.name, None) is not None:
            log.info("capture back: %s", feed.name)
        changes: list[Change] = []
        for meta in sessions:
            # agentsview's own runs and others it recognises as automated make no digest (§4.2, §8).
            if meta.agent not in feed.agents or meta.is_automated:
                continue
            listed.add(meta.id)
            row = self.session(meta.id)
            # A failed read is tried again every poll; a gone session, like any other, when its listing changes.
            if _listing(meta) != (row.listing if row else self._missing.get(meta.id)) or (
                row and row.status == "unread"
            ):
                changes += self._read_or_skip(feed, meta.id, meta, now, listing=_listing(meta))
            if meta.id in self._missing:
                missing[meta.id] = self._missing[meta.id]
        return changes

    def tick(self, session_id: str, now: datetime) -> list[Change]:
        """The session's watch says it changed: read it, and settle it."""
        row = self.session(session_id)
        feed = self._feeds.get(row.feed) if row else None
        if row is None or feed is None or row.status == "gone":
            return []
        return self._read_or_skip(feed, session_id, None, now, listing=None) + self.settle(session_id, now)

    def settle(self, session_id: str, now: datetime) -> list[Change]:
        """A turn end or finalization due by now, without reading: for `run`'s check once a change has gone quiet."""
        row = self.session(session_id)
        if row is None or row.state != "active" or row.feed in self.offline:
            return []
        if row.status == "unread" and now - row.active_at < self._window:
            # Not settled on what was read before it changed, unless it has been unreadable for longer than a
            # listing looks back: then whatever it did is out of reach.
            return []
        # From the session's last activity, or when the tracker last saw a change if that's earlier: a source whose
        # clock is ahead of ours (or a zone-less local time) mustn't hold a session open until its time comes.
        idle = now - min(row.active_at, row.changed_at) >= self._idle
        changes = []
        with self._db:
            # A session idle long enough to finalize is quiet too: its last turn ends first, as after a restart.
            if row.answer is not None and row.answer != row.turn_ended and (idle or self._quiet(row, now)):
                self._db.execute("UPDATE sessions SET turn_ended = answer WHERE id = ?", (row.id,))
                changes.append(self._record(row, "turn_end", now))
            if idle:
                self._db.execute(
                    "UPDATE sessions SET state = 'finalized', finalizations = finalizations + 1 WHERE id = ?",
                    (row.id,),
                )
                changes.append(self._record(row, "finalized", now))
        return changes

    def watched(self) -> set[str]:
        """The sessions worth a watch: active, on a feed the tracker has, and still in their source."""
        return {row.id for row in self.sessions("active") if row.feed in self._feeds and row.status != "gone"}

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

    def changes(self, after: int = 0, limit: int = -1) -> list[tuple[int, Change]]:
        """The changes recorded after the one numbered `after`, for a consumer that keeps its own place; at most
        `limit` of them, where it's set."""
        rows = self._db.execute("SELECT * FROM changes WHERE id > ? ORDER BY id LIMIT ?", (after, limit))
        return [
            (row["id"], Change(row["kind"], row["session_id"], row["ordinal"], datetime.fromisoformat(row["at"])))
            for row in rows
        ]

    def _quiet(self, row: Tracked, now: datetime) -> bool:
        return now - row.changed_at >= self._turn_quiet

    def _read_or_skip(
        self, feed: Feed, session_id: str, meta: SessionMeta | None, now: datetime, *, listing: str | None
    ) -> list[Change]:
        """Read the session, and store `listing` with it (None: keep the stored one). A failure is stored too."""
        try:
            return self._read(feed, session_id, meta, now, listing)
        except CaptureError as error:
            if error.gone:
                log.info("%s session %s is gone: %s", feed.name, session_id, error)
                if self.session(session_id) is None:
                    self._missing[session_id] = listing or ""
                else:
                    # Nothing more will come: it settles on what was read, and is read again only if listed anew.
                    self._mark(session_id, "gone", listing)
                return []
            log.warning("can't read %s session %s: %s", feed.name, session_id, error)
        except Exception:
            # Whatever one session holds mustn't stop the others.
            log.exception("can't read %s session %s", feed.name, session_id)
        # Read again next poll, and not settled on what came before. The listing's time is the session's own, so
        # an active one isn't settled until a listing would no longer show that either.
        self._mark(session_id, "unread", None, meta.ended_at if meta else None)
        return []

    def _mark(self, session_id: str, status: str, listing: str | None, listed_at: datetime | None = None) -> None:
        row = self.session(session_id)
        if row is None:
            return
        active_at = row.active_at
        if listed_at is not None and row.state == "active":
            # Not for a finalized one: a read that succeeds later reopens it by its newer time.
            active_at = max(active_at, _utc(listed_at))
        with self._db:
            self._db.execute(
                "UPDATE sessions SET status = ?, listing = COALESCE(?, listing), active_at = ? WHERE id = ?",
                (status, listing, active_at.isoformat(), session_id),
            )

    def _read(
        self, feed: Feed, session_id: str, meta: SessionMeta | None, now: datetime, listing: str | None
    ) -> list[Change]:
        row = self.session(session_id)
        messages = feed.source.messages(session_id, 0).messages
        content = _hash(messages)
        changed = row is None or content != row.content
        latest = _latest(messages)
        # The session's own times, as they are: capped at now, the same times read again would look newer.
        if row is None:
            listed_at = _first(meta).ended_at
            active_at = latest or (_utc(listed_at) if listed_at else now)
        elif latest is None:
            # A session with no times at all, such as Cursor CLI's in agentsview: a change happened about now.
            active_at = now if changed else row.active_at
        else:
            # Only a newer time is activity: a re-parse, or a new field in how a message reads, isn't.
            active_at = max(row.active_at, latest)
        # A newer time is activity; in a session with no times, any change is, as it's the only sign there is.
        active = row is not None and (active_at > row.active_at or (latest is None and changed))
        new = Tracked(
            id=session_id,
            feed=feed.name,
            agent=row.agent if row else _first(meta).agent,
            project=row.project if row else _project(_first(meta)),
            cwd=row.cwd if row else _first(meta).cwd,
            headless=row.headless if row else _headless(_first(meta)),
            state="active" if active or row is None else row.state,
            finalizations=row.finalizations if row else 0,
            status="ok",
            listing=listing if listing is not None else row.listing if row else "",
            content=content,
            last_ordinal=messages[-1].ordinal if messages else -1,
            answer=_answer(messages),
            turn_ended=row.turn_ended if row else None,
            changed_at=now if changed or row is None else row.changed_at,
            active_at=active_at,
        )
        with self._db:
            self._db.execute(STORE, _columns(new))
            if row is None:
                return [self._record(new, "started", now)]
            if row.state == "finalized" and new.state == "active":
                return [self._record(new, "reopened", now)]
        return []

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
                    watched = await self._locked(self._tracker.watched)
                except Exception:
                    log.exception("tracker poll failed")
                    watched = set(self._watches)
                for session_id in watched:
                    if session_id not in self._watches or self._watches[session_id].done():
                        self._watches[session_id] = asyncio.create_task(self._follow(session_id))
                for session_id in set(self._watches) - watched:
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
                # pass on what it did, which is recorded already. Cancelled again meanwhile, it still waits.
                while not job.done():
                    try:
                        await asyncio.wait([job])
                    except asyncio.CancelledError:
                        continue
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


STORE = "INSERT OR REPLACE INTO sessions ({}) VALUES ({})".format(
    ", ".join(field.name for field in fields(Tracked)), ", ".join(f":{field.name}" for field in fields(Tracked))
)


def _columns(row: Tracked) -> dict[str, Any]:
    values = asdict(row)
    return values | {"changed_at": row.changed_at.isoformat(), "active_at": row.active_at.isoformat()}


def _tracked(row: sqlite3.Row) -> Tracked:
    values = {field.name: row[field.name] for field in fields(Tracked)}
    return Tracked(
        **values
        | {
            "headless": bool(row["headless"]),
            "changed_at": datetime.fromisoformat(row["changed_at"]),
            "active_at": datetime.fromisoformat(row["active_at"]),
        }
    )


def _first(meta: SessionMeta | None) -> SessionMeta:
    assert meta is not None, "a session is first read from its listing"
    return meta


UNLISTED = "unlisted"  # the listing stored with an active session read after it dropped out of the listing


def _listing(meta: SessionMeta) -> str:
    return f"{meta.message_count}|{meta.ended_at}|{meta.transcript_revision}"


def _headless(meta: SessionMeta) -> bool:
    """§4.3's markers. Copilot CLI and Cursor CLI show none yet, so their `-p` runs count as interactive."""
    return meta.entrypoint == "sdk-cli" or meta.session_kind == "non-interactive"


def _project(meta: SessionMeta) -> str:
    """§4.4's project id: the git project of the session's folder, else agentsview's project."""
    if meta.cwd and Path(meta.cwd).is_dir() and (project := git_project(Path(meta.cwd))):
        return project
    return meta.project


def _answer(messages: Sequence[Message]) -> str | None:
    """§4.3's turn end, but for the quiet: the last message not a system one is the agent's text, calling no tool.
    It's named by how many prompts came before it and its text: a re-parse can renumber it or change how a message
    reads, and an agent can answer two prompts alike."""
    said = [message for message in messages if not message.is_system]
    if not said or said[-1].role != "assistant" or said[-1].tool_calls or not said[-1].content:
        return None
    prompts = sum(message.role == "user" for message in said)
    return f"{prompts}:{hashlib.sha256(said[-1].content.encode(errors='surrogatepass')).hexdigest()}"


def _latest(messages: Iterable[Message]) -> datetime | None:
    """The latest time in the messages, theirs and their tool results'; one without a zone counts as UTC."""
    stamps = [
        _utc(stamp)
        for message in messages
        for stamp in (
            message.timestamp,
            *(event.timestamp for call in message.tool_calls for event in call.result_events),
        )
        if stamp is not None
    ]
    return max(stamps) if stamps else None


def _utc(stamp: datetime) -> datetime:
    """A time without a zone counts as UTC, so it compares with the clock's."""
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _hash(messages: Iterable[Message]) -> str:
    """The messages as they read, without their ordinals, which a re-parse can change. As escaped JSON, so half an
    emoji (a lone surrogate, which pydantic won't write as UTF-8) hashes too."""
    digest = hashlib.sha256()
    for message in messages:
        digest.update(json.dumps(message.model_dump(mode="json", exclude={"ordinal"}), sort_keys=True).encode())
    return digest.hexdigest()
