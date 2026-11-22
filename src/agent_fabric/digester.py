"""The digester: a digest for each session the tracker finalizes (docs/design.md §4.3-§4.5).

It reads the tracker's changes on from its own place in `state.db`. A session's folder is snapshotted through git when
it's first seen and at each of its turn ends, while the session is active, and the first and latest snapshots are kept
with it; its workspace is found once, when it's first seen. At each finalization its digest is rendered from the whole
transcript, read again, and those snapshots, and written where §4.5 says.

A digest that can't be written now stays due: the wiki not clean, gitleaks missing, its source not answering. It's
tried again a minute later, then two, doubling up to an hour, and each new reason is logged once. One whose session
reopened meanwhile waits for the next finalization; one whose source no longer has the session, or is no longer
configured, is dropped and logged.
"""

import asyncio
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

from agent_fabric import digest, snapshot, writer
from agent_fabric.capture import CaptureError
from agent_fabric.config import read_config
from agent_fabric.secretgate import GateUnavailable
from agent_fabric.snapshot import Snapshot
from agent_fabric.tracker import IDLE, POLL, Change, Feed, Tracked, Tracker, connect
from agent_fabric.wikirepo import GitError
from agent_fabric.workspaces import Workspace, workspace_of

log = logging.getLogger(__name__)

RETRY = timedelta(minutes=1)  # after a failed write, doubling with each failure since
MAX_RETRY = timedelta(hours=1)
BATCH = 100  # changes read, and their place saved, at a time: a backlog after an outage is worked through in parts
DEFAULT = ""  # the default workspace, in the `workspace` column

SCHEMA = """
CREATE TABLE IF NOT EXISTS digester (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    place INTEGER NOT NULL        -- the last of the tracker's changes it has read
);
CREATE TABLE IF NOT EXISTS digests (
    session_id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,     -- in UTC; the session's commits are those made since
    workspace TEXT,               -- its root, '' for the default; NULL until found
    first TEXT,                   -- its first snapshot, as JSON; NULL until one is taken
    last TEXT,                    -- its latest
    path TEXT,                    -- its digest's, fixed at the first write, so a reopened session keeps its file
    due INTEGER NOT NULL,         -- finalized, and its digest not written since
    retry_at TEXT,                -- in UTC: when to try a write that failed again
    failures INTEGER NOT NULL DEFAULT 0,  -- in a row
    error TEXT                    -- why the last failed, logged once
);
"""


class Digester:
    def __init__(self, feeds: Sequence[Feed], db: Path, env: Mapping[str, str], *, idle: timedelta = IDLE) -> None:
        """`db` is the tracker's `state.db`, which the digester opens on a connection of its own; `idle` is the
        tracker's."""
        self._idle = idle
        self._db = connect(db)
        self._db.executescript(SCHEMA)
        self._tracker = Tracker(feeds, self._db)  # only read: the sessions and their changes
        self._feeds = {feed.name: feed for feed in feeds}
        self._env = env

    def step(self, now: datetime) -> list[writer.Written]:
        """Follow the changes recorded since the last step, then write what's due."""
        now = now.astimezone(UTC)
        while changes := self._tracker.changes(self._place(), limit=BATCH):
            self._follow(changes, now)
        found = []
        for row in self._due(now):
            try:
                if (written := self._write(row, now)) is not None:
                    found.append(written)
            except Exception:  # its bookkeeping failed, as on a database locked too long: the others go on
                log.exception("digest of %s failed", row["session_id"])
        return found

    def _place(self) -> int:
        found = self._db.execute("SELECT place FROM digester").fetchone()
        return int(found["place"]) if found else 0

    def _follow(self, changes: list[tuple[int, Change]], now: datetime) -> None:
        wanted: dict[str, None] = {}  # snapshotted once a batch, however many turns ended since the last
        with self._db:
            for _, change in changes:
                # First seen by its first change, `started`, which is read before the session's others.
                self._db.execute(
                    "INSERT OR IGNORE INTO digests (session_id, first_seen, due) VALUES (?, ?, 0)",
                    (change.session_id, change.at.astimezone(UTC).isoformat()),
                )
                if change.kind in ("started", "turn_end"):
                    wanted[change.session_id] = None
        for session_id in wanted:
            try:
                self._snapshot(session_id, now)
            except Exception:  # one session's mustn't hold the place, and every digest after it, back
                log.exception("snapshot of %s failed", session_id)
        with self._db:
            for _, change in changes:
                if change.kind == "finalized":
                    self._db.execute(
                        "UPDATE digests SET due = 1, retry_at = NULL, failures = 0, error = NULL"
                        " WHERE session_id = ?",
                        (change.session_id,),
                    )
            self._db.execute("INSERT OR REPLACE INTO digester (id, place) VALUES (1, ?)", (changes[-1][0],))

    def _snapshot(self, session_id: str, now: datetime) -> None:
        session = self._tracker.session(session_id)
        row = self._row(session_id)
        if session is None or row is None:
            return
        with suppress(Exception):  # a config that can't be read now: found at the write, which reports it
            self._workspace(session, row)
        # Finalized before this step read its turn end, as after an outage, or idle long enough to be, as one first
        # seen on fabricd's first start is while the tracker reads the others before settling it: the folder is no
        # longer its alone, as a later session may have changed it. The test is the tracker's own.
        idle = now - min(session.active_at, session.changed_at) >= self._idle
        if session.state != "active" or idle or session.cwd is None or not Path(session.cwd).is_dir():
            return
        taken = snapshot.take(Path(session.cwd), now, datetime.fromisoformat(row["first_seen"]))
        if taken is None:
            return  # outside git, or git couldn't read it: the next turn end tries again
        with self._db:
            self._db.execute(
                "UPDATE digests SET first = COALESCE(first, ?), last = ? WHERE session_id = ?",
                (_dump(taken), _dump(taken), session_id),
            )

    def _workspace(self, session: Tracked, row: sqlite3.Row) -> Workspace:
        """Found once, while its folder is likeliest still there: a worktree removed when its session ends would
        place it in the default workspace."""
        if row["workspace"] is not None:
            root = Path(row["workspace"]) if row["workspace"] != DEFAULT else None
            if root is None or root in read_config(self._env).folders:
                return Workspace(root)
            # A root no longer registered: found again, as kb no longer keeps that root's wiki.
        found = workspace_of(Path(session.cwd), self._env) if session.cwd else Workspace(None)
        with self._db:
            self._db.execute(
                "UPDATE digests SET workspace = ? WHERE session_id = ?",
                (str(found.root) if found.root else DEFAULT, session.id),
            )
        return found

    def _due(self, now: datetime) -> list[sqlite3.Row]:
        return list(
            self._db.execute(
                "SELECT * FROM digests WHERE due = 1 AND (retry_at IS NULL OR retry_at <= ?) ORDER BY session_id",
                (now.isoformat(),),
            )
        )

    def _write(self, row: sqlite3.Row, now: datetime) -> writer.Written | None:
        session_id = row["session_id"]
        session = self._tracker.session(session_id)
        if session is not None and session.state == "active":
            self._done(session_id)  # reopened: it finalizes again
            return None
        feed = self._feeds.get(session.feed) if session else None
        if session is None or feed is None:
            why = "the tracker has no such session" if session is None else "no feed configured reads it"
            log.warning("no digest for %s: %s", session_id, why)
            self._done(session_id)
            return None
        try:
            meta = feed.source.session(session_id)
            seen = datetime.fromisoformat(row["first_seen"])
            if meta.started_at is None:
                # No times at all, as Cursor CLI's in agentsview: it began when kb first saw it, and ended at its
                # last change the tracker saw.
                meta = meta.model_copy(update={"started_at": seen, "ended_at": meta.ended_at or session.active_at})
            messages = feed.source.messages(session_id, 0).messages
            text = digest.render(
                meta, messages, _snapshots(row), project=session.project, revision=session.finalizations
            )
            workspace = self._workspace(session, row)
            if row["path"] is None:
                start = digest.started(meta, messages) or seen
                with self._db:
                    self._db.execute(
                        "UPDATE digests SET path = ? WHERE session_id = ?",
                        (digest.path(meta, session.project, start), session_id),
                    )
                row = self._row(session_id) or row
            written = writer.write(
                workspace, self._env, now, writer.Digest(text, row["path"], session.project, session.headless)
            )
        except Exception as error:  # whatever stops one digest mustn't stop the others
            if isinstance(error, CaptureError) and error.gone:
                log.warning("no digest for %s: its source no longer has it: %s", session_id, error)
                self._done(session_id)
            else:
                self._failed(row, now, error)
            return None
        self._done(session_id)
        log.info("digest of %s: %s %s", session_id, written.where, written.path)
        return written

    def _failed(self, row: sqlite3.Row, now: datetime, error: Exception) -> None:
        reason = f"{type(error).__name__}: {error}"
        if _gist(reason) != _gist(row["error"] or ""):
            log.warning("digest of %s waits: %s", row["session_id"], reason, exc_info=_unexpected(error))
        wait = min(RETRY * 2 ** min(row["failures"], 10), MAX_RETRY)
        with self._db:
            self._db.execute(
                "UPDATE digests SET retry_at = ?, failures = failures + 1, error = ? WHERE session_id = ?",
                ((now + wait).isoformat(), reason, row["session_id"]),
            )

    def _done(self, session_id: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE digests SET due = 0, retry_at = NULL, failures = 0, error = NULL WHERE session_id = ?",
                (session_id,),
            )

    def _row(self, session_id: str) -> sqlite3.Row | None:
        found: sqlite3.Row | None = self._db.execute(
            "SELECT * FROM digests WHERE session_id = ?", (session_id,)
        ).fetchone()
        return found


async def run(
    digester: Digester,
    *,
    wake: asyncio.Event | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    every: float = POLL,
) -> NoReturn:
    """A step every `every` seconds, or sooner when `wake` is set, as the tracker's `emit` can, so a turn end's
    snapshot is taken close to it. Steps run in a worker thread, one at a time, which the database goes with.
    Cancelled during a step, it returns once the step is done: a worker thread can't be stopped, and one still
    writing the database or committing to a wiki mustn't overlap a step started after."""
    wake = wake or asyncio.Event()
    while True:
        wake.clear()
        job = asyncio.ensure_future(asyncio.to_thread(lambda: digester.step(clock())))
        try:
            await asyncio.shield(job)
        except asyncio.CancelledError:
            while not job.done():  # cancelled again meanwhile, it still waits
                with suppress(asyncio.CancelledError):
                    await asyncio.wait([job])
            raise
        except Exception:
            log.exception("digester step failed")
        with suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), every)


def _gist(reason: str) -> str:
    """The reason without what's in brackets, such as the files a wiki has changed, which change between tries."""
    return re.sub(r"\([^)]*\)", "()", reason)


def _unexpected(error: Exception) -> bool:
    """Not one of the reasons `writer.write` and the sources give for a digest to wait: worth its traceback."""
    return not isinstance(error, GitError | GateUnavailable | OSError | ValueError | CaptureError)


def _snapshots(row: sqlite3.Row) -> list[Snapshot]:
    """The stored snapshots that still load: one stored by a version whose `Snapshot` differs is left out."""
    found = []
    for column in ("first", "last"):
        if row[column] is not None:
            with suppress(KeyError, TypeError, ValueError):
                found.append(_load(row[column]))
    return found


def _dump(taken: Snapshot) -> str:
    values: dict[str, Any] = asdict(taken)
    values |= {
        "at": taken.at.isoformat(),
        "worktree": str(taken.worktree) if taken.worktree else None,
        "main": str(taken.main) if taken.main else None,
    }
    return json.dumps(values)


def _load(text: str) -> Snapshot:
    values = json.loads(text)
    for field in fields(Snapshot):
        if isinstance(values[field.name], list):
            values[field.name] = tuple(values[field.name])
    values |= {
        "at": datetime.fromisoformat(values["at"]),
        "worktree": Path(values["worktree"]) if values["worktree"] else None,
        "main": Path(values["main"]) if values["main"] else None,
    }
    return Snapshot(**values)
