"""Seam: the tracker over capture sources, with a fixed clock (docs/design.md §4.3).

Most tests drive a made-up source whose sessions they change between polls; the last ones run the live check's
recorded sessions (2026-11-19) through agentsview's adapter and Copilot CLI's own reader."""

import asyncio
import json
import shutil
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_fabric.capture import AgentsviewSource, CaptureError, Message, SessionMeta, Transcript
from agent_fabric.copilot import CopilotSource
from agent_fabric.tracker import SCHEMA, Change, Feed, Tracker, connect, run

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)


class Source:
    """A capture source whose sessions and messages a test sets, and which fails while `down` says why."""

    def __init__(self) -> None:
        self.metas: dict[str, SessionMeta] = {}
        self.texts: dict[str, list[Message]] = {}
        self.revisions: dict[str, str] = {}
        self.down: str | None = None
        self.unreadable: set[str] = set()
        self.reads: list[tuple[str, int]] = []
        self.attempts = 0
        self.ticks: dict[str, asyncio.Queue[None]] = {}

    def add(self, sid: str, agent: str = "claude", **fields: Any) -> None:
        self.metas[sid] = meta(sid, agent, **fields)
        self.texts[sid] = []
        self.revisions[sid] = "1"

    def say(self, sid: str, role: str, content: str | None, at: datetime | None, **fields: Any) -> None:
        ordinal = self.texts[sid][-1].ordinal + 1 if self.texts[sid] else 0
        self.texts[sid].append(message(sid, ordinal, role, content, at, **fields))
        self.metas[sid] = self.metas[sid].model_copy(update={"message_count": len(self.texts[sid]), "ended_at": at})

    def active_sessions(self, since: datetime) -> list[SessionMeta]:
        if self.down:
            raise CaptureError(self.down)
        return [m for m in self.metas.values() if m.ended_at is None or m.ended_at >= since]

    def session(self, session_id: str) -> SessionMeta:
        return self.metas[session_id]

    def messages(self, session_id: str, from_ordinal: int) -> Transcript:
        self.attempts += 1
        if session_id not in self.texts:
            raise CaptureError(f"agentsview: HTTP 404: no session {session_id}", 404)
        if self.down or session_id in self.unreadable:
            raise CaptureError(self.down or "unreadable")
        self.reads.append((session_id, from_ordinal))
        later = tuple(m for m in self.texts[session_id] if m.ordinal >= from_ordinal)
        return Transcript(self.revisions[session_id] if later else None, later)

    async def watch(self, session_id: str) -> AsyncIterator[None]:
        queue = self.ticks.setdefault(session_id, asyncio.Queue())
        yield None
        while True:
            await queue.get()
            yield None


def meta(sid: str, agent: str = "claude", **fields: Any) -> SessionMeta:
    base = {"id": sid, "agent": agent, "project": "proj", "is_automated": False, "started_at": None}
    return SessionMeta.model_validate(base | {"ended_at": None, "message_count": 0} | fields)


def message(sid: str, ordinal: int, role: str, content: str | None, at: datetime | None, **fields: Any) -> Message:
    base = {"session_id": sid, "ordinal": ordinal, "role": role, "timestamp": at, "content": content}
    return Message.model_validate(base | {"thinking_text": None, "model": None, "is_system": False} | fields)


def shell(result: str | None, *statuses: str) -> dict[str, Any]:
    return {
        "tool_name": "Bash",
        "category": "Bash",
        "input_json": '{"command": "make"}',
        "result_content": result,
        "result_events": [{"status": status} for status in statuses],
    }


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "state.db")


@pytest.fixture
def source() -> Source:
    return Source()


def tracker(db: sqlite3.Connection, *feeds: Feed) -> Tracker:
    return Tracker(feeds, db)


def feed(source: Any, *agents: str, name: str = "fake") -> Feed:
    return Feed(name, source, frozenset(agents or ("claude", "codex")))


def kinds(changes: list[Change]) -> list[tuple[str, str]]:
    return [(change.kind, change.session_id) for change in changes]


def turn(source: Source, sid: str, at: datetime) -> None:
    """A prompt, a command, and the agent's answer."""
    source.say(sid, "user", "run make", at)
    source.say(sid, "assistant", None, at + SECOND, tool_calls=[shell("ok", "completed")])
    source.say(sid, "assistant", "Done.", at + 2 * SECOND)


# ── the lifecycle ─────────────────────────────────────────────────────────


def test_a_new_session_starts_ends_its_turn_and_finalizes_when_quiet(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    assert kinds(t.poll(T0 + 3 * SECOND)) == [("started", "s1")]
    assert t.poll(T0 + 6 * SECOND) == []  # changed 3 s ago: not quiet yet
    assert kinds(t.poll(T0 + 8 * SECOND)) == [("turn_end", "s1")]
    assert t.poll(T0 + 10 * MINUTE) == []  # one turn end per turn
    assert t.poll(T0 + 30 * MINUTE) == []
    assert kinds(t.poll(T0 + 2 * SECOND + 30 * MINUTE)) == [("finalized", "s1")]
    assert t.poll(T0 + 60 * MINUTE) == []
    row = t.session("s1")
    assert row is not None
    assert (row.state, row.finalizations, row.last_ordinal) == ("finalized", 1, 2)


def test_new_activity_reopens_a_finalized_session_which_finalizes_again(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0 + 3 * SECOND)
    assert kinds(t.poll(T0 + 40 * MINUTE)) == [("finalized", "s1")]
    turn(source, "s1", T0 + 50 * MINUTE)
    assert kinds(t.poll(T0 + 51 * MINUTE)) == [("reopened", "s1")]
    assert kinds(t.poll(T0 + 90 * MINUTE)) == [("finalized", "s1")]
    row = t.session("s1")
    assert row is not None
    assert (row.finalizations, row.last_ordinal) == (2, 5)


def test_a_session_first_seen_long_quiet_finalizes_at_once(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    assert kinds(tracker(db, feed(source)).poll(T0 + 3 * MINUTE + 30 * MINUTE)) == [
        ("started", "s1"),
        ("finalized", "s1"),
    ]


def test_a_turn_isnt_over_while_a_call_waits_for_its_result(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    source.say("s1", "assistant", "Building.", T0, tool_calls=[shell(None, "running")])
    t = tracker(db, feed(source))
    t.poll(T0)
    assert t.poll(T0 + MINUTE) == []


def test_a_call_still_running_is_read_again_until_its_result_folds_in(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    source.say("s1", "assistant", None, T0, tool_calls=[shell(None, "running")])
    t = tracker(db, feed(source))
    t.poll(T0)
    # The result folds into the message that made the call, which only its watch would tell.
    source.texts["s1"][1] = message("s1", 1, "assistant", None, T0, tool_calls=[shell("ok", "completed")])
    source.say("s1", "assistant", "Built.", T0 + MINUTE)
    t.tick("s1", T0 + MINUTE)
    assert source.reads == [("s1", 0), ("s1", 1)]
    assert kinds(t.poll(T0 + MINUTE + 5 * SECOND)) == [("turn_end", "s1")]
    row = t.session("s1")
    assert row is not None
    assert row.cursor == 3


def test_a_command_that_printed_nothing_has_finished(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "touch it", T0)
    quiet = shell(None) | {"result_events": [{"status": "", "content": "", "timestamp": T0.isoformat()}]}
    source.say("s1", "assistant", None, T0, tool_calls=[quiet])
    source.say("s1", "assistant", "Touched.", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    assert kinds(t.poll(T0 + 10 * SECOND)) == [("turn_end", "s1")]
    row = t.session("s1")
    assert row is not None
    assert row.cursor == 3


def test_a_system_message_after_the_answer_keeps_the_turn_end(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    source.say("s1", "user", "<hook output>", T0 + SECOND, is_system=True)
    t.poll(T0 + SECOND)
    assert kinds(t.poll(T0 + 10 * SECOND)) == [("turn_end", "s1")]


def test_a_system_message_after_a_turns_end_doesnt_end_it_again(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    assert kinds(t.poll(T0 + 10 * SECOND)) == [("turn_end", "s1")]
    source.say("s1", "user", "<hook output>", T0 + 20 * SECOND, is_system=True)
    t.poll(T0 + 20 * SECOND)
    assert t.poll(T0 + 30 * SECOND) == []


def test_a_reparse_of_the_same_messages_doesnt_end_the_turn_again(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    assert kinds(t.poll(T0 + 10 * SECOND)) == [("turn_end", "s1")]
    source.revisions["s1"] = "2"
    source.metas["s1"] = source.metas["s1"].model_copy(update={"transcript_revision": "2"})
    t.poll(T0 + MINUTE)
    assert t.poll(T0 + 2 * MINUTE) == []
    assert source.reads[-1] == ("s1", 0)


def test_a_session_unchanged_in_its_listing_isnt_read_again(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    t.poll(T0 + 10 * SECOND)
    t.poll(T0 + 20 * SECOND)
    assert source.reads == [("s1", 0)]
    source.say("s1", "user", "more", T0 + 30 * SECOND)
    t.poll(T0 + 30 * SECOND)
    assert source.reads == [("s1", 0), ("s1", 3)]


def test_a_reparsed_session_is_read_again_from_the_start(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    source.say("s1", "user", "more", T0 + MINUTE)
    source.revisions["s1"] = "2"
    t.poll(T0 + MINUTE)
    assert source.reads == [("s1", 0), ("s1", 3), ("s1", 0)]


def test_a_reparse_into_fewer_messages_starts_over_from_the_listings_revision(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    # A read from the old cursor finds nothing, and so no revision: only the listing says it changed.
    del source.texts["s1"][1:]
    source.revisions["s1"] = "2"
    source.metas["s1"] = source.metas["s1"].model_copy(update={"message_count": 1, "transcript_revision": "2"})
    t.poll(T0 + MINUTE)
    assert source.reads == [("s1", 0), ("s1", 0)]
    row = t.session("s1")
    assert row is not None
    assert (row.last_ordinal, row.ends_turn, row.cursor, row.revision) == (0, False, 1, "2")
    assert t.poll(T0 + 2 * MINUTE) == []  # no turn end for messages that are gone


def test_a_reparse_that_numbers_a_new_turn_as_an_old_one_still_ends_it(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    t.poll(T0 + 10 * SECOND)  # turn end at ordinal 2
    # Re-parsed into one prompt and a new answer, numbered 0 to 2 again.
    source.texts["s1"] = []
    source.say("s1", "user", "again", T0 + MINUTE)
    source.say("s1", "assistant", "Thinking.", T0 + MINUTE)
    source.say("s1", "assistant", "Done again.", T0 + MINUTE)
    source.revisions["s1"] = "2"
    source.metas["s1"] = source.metas["s1"].model_copy(update={"transcript_revision": "2"})
    t.poll(T0 + MINUTE)
    assert kinds(t.poll(T0 + MINUTE + 10 * SECOND)) == [("turn_end", "s1")]


def test_a_session_that_couldnt_be_read_isnt_settled_on_what_was_read_before(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    source.say("s1", "user", "more", T0 + 29 * MINUTE)
    source.unreadable.add("s1")
    assert t.poll(T0 + 31 * MINUTE) == []
    assert t.tick("s1", T0 + 32 * MINUTE) == []
    source.unreadable.clear()
    t.tick("s1", T0 + 33 * MINUTE)
    row = t.session("s1")
    assert row is not None
    assert (row.state, row.active_at) == ("active", T0 + 29 * MINUTE)


def test_a_result_with_no_time_of_its_own_counts_from_when_it_was_seen(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    source.say("s1", "assistant", None, T0, tool_calls=[shell(None, "running")])
    t = tracker(db, feed(source))
    t.poll(T0)
    # A 40-minute build: its result folds into the call's message, which keeps the call's time.
    source.texts["s1"][1] = message("s1", 1, "assistant", None, T0, tool_calls=[shell("ok", "completed")])
    assert t.tick("s1", T0 + 40 * MINUTE) == []
    row = t.session("s1")
    assert row is not None
    assert (row.state, row.active_at) == ("active", T0 + 40 * MINUTE)


def test_activity_comes_from_the_sessions_own_timestamps(db: sqlite3.Connection, source: Source) -> None:
    # The machine was off: the session went on for 20 minutes after the tracker last read it.
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    turn(source, "s1", T0 + 20 * MINUTE)
    t.poll(T0 + 3 * 60 * MINUTE)
    row = t.session("s1")
    assert row is not None
    assert row.state == "finalized"
    assert row.active_at == T0 + 20 * MINUTE + 2 * SECOND


def test_a_change_with_no_timestamp_counts_from_when_it_was_seen(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "hi", None)
    t = tracker(db, feed(source))
    t.poll(T0)
    source.say("s1", "assistant", "Hello.", None)
    t.poll(T0 + 20 * MINUTE)
    assert kinds(t.poll(T0 + 45 * MINUTE)) == [("turn_end", "s1")]
    assert kinds(t.poll(T0 + 50 * MINUTE)) == [("finalized", "s1")]


# ── which sessions ────────────────────────────────────────────────────────


def test_headless_runs_are_tracked_and_marked(db: sqlite3.Connection, source: Source) -> None:
    source.add("c", entrypoint="sdk-cli", relationship_type="subagent")
    source.add("codex:x", "codex", session_kind="non-interactive", relationship_type="subagent")
    source.add("i")
    t = tracker(db, feed(source))
    t.poll(T0)
    assert {row.id: row.headless for row in t.sessions()} == {"c": True, "codex:x": True, "i": False}


def test_automated_sessions_are_left_out(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1", is_automated=True)
    t = tracker(db, feed(source))
    assert t.poll(T0) == []
    assert t.sessions() == []


def test_each_agent_is_read_through_its_own_feed(db: sqlite3.Connection) -> None:
    agentsview, copilot = Source(), Source()
    for each in (agentsview, copilot):
        each.add("copilot:1", "copilot")
    agentsview.add("s1")
    t = tracker(db, feed(agentsview, "claude", name="agentsview"), feed(copilot, "copilot", name="copilot"))
    t.poll(T0)
    assert {row.id: row.feed for row in t.sessions()} == {"copilot:1": "copilot", "s1": "agentsview"}
    assert agentsview.reads == [("s1", 0)]


def test_the_project_is_the_git_project_of_the_sessions_folder(
    db: sqlite3.Connection, source: Source, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / ".git" / "config").write_text('[remote "origin"]\n\turl = git@github.com:me/billing-api.git\n')
    (repo / ".git" / "objects").mkdir()
    (repo / ".git" / "refs").mkdir()
    source.add("in-git", cwd=str(repo))
    source.add("gone", cwd=str(tmp_path / "removed"), project="kept")
    t = tracker(db, feed(source))
    t.poll(T0)
    assert {row.id: row.project for row in t.sessions()} == {"in-git": "github.com/me/billing-api", "gone": "kept"}


# ── when things go wrong ──────────────────────────────────────────────────


def test_a_feed_that_fails_is_offline_and_its_sessions_dont_finalize(
    db: sqlite3.Connection, source: Source, caplog: pytest.LogCaptureFixture
) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    source.down = "agentsview: connection refused"
    assert t.poll(T0 + 40 * MINUTE) == []
    assert t.poll(T0 + 41 * MINUTE) == []
    assert t.offline == {"fake": "agentsview: connection refused"}
    assert caplog.text.count("capture offline") == 1
    source.down = None
    assert kinds(t.poll(T0 + 42 * MINUTE)) == [("finalized", "s1")]
    assert t.offline == {}


def test_a_session_that_cant_be_read_doesnt_stop_the_others(db: sqlite3.Connection, source: Source) -> None:
    source.add("bad")
    source.add("good")
    source.unreadable.add("bad")
    t = tracker(db, feed(source))
    assert kinds(t.poll(T0)) == [("started", "good")]
    source.unreadable.clear()
    source.say("bad", "user", "hi", T0)
    assert kinds(t.poll(T0 + SECOND)) == [("started", "bad")]


def test_a_session_whose_watch_read_failed_is_read_again_by_the_next_poll(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    source.say("s1", "assistant", None, T0, tool_calls=[shell(None, "running")])
    t = tracker(db, feed(source))
    t.poll(T0)
    source.texts["s1"][1] = message("s1", 1, "assistant", None, T0, tool_calls=[shell("ok", "completed")])
    source.unreadable.add("s1")
    t.tick("s1", T0 + MINUTE)
    source.unreadable.clear()
    t.poll(T0 + 2 * MINUTE)  # the listing hasn't changed: a result folded in
    assert source.reads == [("s1", 0), ("s1", 1)]
    row = t.session("s1")
    assert row is not None
    assert (row.cursor, row.active_at) == (2, T0 + 2 * MINUTE)


def test_a_feed_back_from_an_outage_has_its_sessions_read_again(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    source.say("s1", "assistant", "Building.", T0, tool_calls=[shell("started", "completed")])
    t = tracker(db, feed(source))
    t.poll(T0)
    source.down = "connection refused"
    t.poll(T0 + MINUTE)
    source.texts["s1"][1] = message("s1", 1, "assistant", "Built.", T0)  # re-written, the listing the same
    source.down = None
    t.poll(T0 + 2 * MINUTE)
    assert source.reads == [("s1", 0), ("s1", 2)]


def test_a_finalized_session_with_a_call_unfinished_is_read_until_its_result_comes(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    source.say("s1", "assistant", None, T0, tool_calls=[shell(None, "running")])
    t = tracker(db, feed(source))
    t.poll(T0)
    assert kinds(t.poll(T0 + 40 * MINUTE)) == [("finalized", "s1")]
    source.texts["s1"][1] = message("s1", 1, "assistant", None, T0, tool_calls=[shell("ok", "completed")])
    assert kinds(t.poll(T0 + 50 * MINUTE)) == [("reopened", "s1")]
    t.poll(T0 + 51 * MINUTE)
    assert source.reads == [("s1", 0), ("s1", 1), ("s1", 1)]  # finished: read again only when it changes


def test_a_restart_reads_each_listed_session_again_before_settling_it(tmp_path: Path, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    source.say("s1", "assistant", None, T0, tool_calls=[shell(None, "running")])
    tracker(connect(tmp_path / "state.db"), feed(source)).poll(T0)
    # While the tracker was down, the build's result folded in, which the listing doesn't show.
    source.texts["s1"][1] = message("s1", 1, "assistant", None, T0, tool_calls=[shell("ok", "completed")])
    again = tracker(connect(tmp_path / "state.db"), feed(source))
    assert again.poll(T0 + 40 * MINUTE) == []
    assert source.reads == [("s1", 0), ("s1", 1)]


def test_a_session_removed_from_its_source_finalizes_once_quiet(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    t.tick("s1", T0 + MINUTE)
    del source.metas["s1"], source.texts["s1"]
    t.tick("s1", T0 + 2 * MINUTE)
    assert t.poll(T0 + 3 * MINUTE) == []
    assert kinds(t.poll(T0 + 31 * MINUTE)) == [("finalized", "s1")]
    assert source.attempts == 3  # asked once: a gone session isn't asked again


def test_a_session_unreadable_for_longer_than_a_listing_looks_back_finalizes(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    source.unreadable.add("s1")
    t.tick("s1", T0 + MINUTE)
    assert t.poll(T0 + 60 * MINUTE) == []
    assert kinds(t.poll(T0 + 25 * 60 * MINUTE)) == [("finalized", "s1")]


def test_a_session_whose_read_fails_is_asked_once_a_poll(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    source.unreadable.add("s1")
    source.say("s1", "user", "more", T0 + MINUTE)
    t.poll(T0 + MINUTE)
    assert source.attempts == 2


def test_a_restart_resumes_from_the_saved_cursor(tmp_path: Path, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    first = tracker(connect(tmp_path / "state.db"), feed(source))
    first.poll(T0)
    source.say("s1", "user", "more", T0 + MINUTE)
    again = tracker(connect(tmp_path / "state.db"), feed(source))
    assert again.poll(T0 + MINUTE) == []
    assert source.reads == [("s1", 0), ("s1", 3)]
    assert [(n, c.kind) for n, c in again.changes()] == [(1, "started")]


def test_a_restart_reads_an_active_session_no_longer_listed_before_finalizing_it(
    tmp_path: Path, source: Source
) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    tracker(connect(tmp_path / "state.db"), feed(source)).poll(T0)
    turn(source, "s1", T0 + 60 * MINUTE)  # went on while the tracker was down, and then quiet for over a day
    again = tracker(connect(tmp_path / "state.db"), feed(source))
    assert kinds(again.poll(T0 + 30 * 60 * MINUTE)) == [("finalized", "s1")]
    row = again.session("s1")
    assert row is not None
    assert (row.last_ordinal, row.active_at) == (5, T0 + 60 * MINUTE + 2 * SECOND)


def test_changes_are_kept_for_a_consumer_to_read_from_its_place(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    t.poll(T0 + 10 * SECOND)
    t.poll(T0 + 40 * MINUTE)
    assert [(n, c.kind, c.ordinal) for n, c in t.changes(1)] == [(2, "turn_end", 2), (3, "finalized", 2)]


# ── run ───────────────────────────────────────────────────────────────────


def test_run_follows_a_sessions_watch_and_finds_its_turn_end(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    t = tracker(db, feed(source))
    seen: list[Change] = []
    clock = [T0]

    async def until(done: Callable[[], bool]) -> None:
        for _ in range(500):
            if done():
                return
            await asyncio.sleep(0.01)

    async def scenario() -> None:
        task = asyncio.create_task(run(t, emit=seen.append, clock=lambda: clock[0], poll=3600, turn_quiet=0.3))
        await until(lambda: len(source.reads) == 2)  # the poll's read, and the watch's opening tick's
        clock[0] = T0 + MINUTE
        source.say("s1", "assistant", "Built.", T0 + MINUTE)
        source.ticks["s1"].put_nowait(None)
        await until(lambda: len(source.reads) == 3)
        clock[0] = T0 + MINUTE + 5 * SECOND  # before the check, 0.3 s after the tick
        await until(lambda: len(seen) == 2)
        task.cancel()

    asyncio.run(scenario())
    assert kinds(seen) == [("started", "s1"), ("turn_end", "s1")]


def test_a_stopped_run_leaves_nothing_running(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    t = tracker(db, feed(source))

    async def scenario() -> None:
        task = asyncio.create_task(run(t, clock=lambda: T0, poll=3600, turn_quiet=3600))
        for _ in range(500):
            if len(source.reads) == 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


class OneThread(sqlite3.Connection):
    """A connection that records the threads that use it, and fails a use that overlaps another."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.threads: set[int] = set()
        self.busy = threading.Lock()

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        if not self.busy.acquire(blocking=False):
            raise AssertionError("two threads use the database at once")
        try:
            self.threads.add(threading.get_ident())
            return super().execute(*args, **kwargs)
        finally:
            self.busy.release()


def test_run_uses_the_database_only_from_its_worker_threads(tmp_path: Path, source: Source) -> None:
    db = sqlite3.connect(tmp_path / "state.db", factory=OneThread, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    source.add("s1")
    source.say("s1", "user", "build", T0)
    t = tracker(db, feed(source))

    async def scenario() -> None:
        task = asyncio.create_task(run(t, clock=lambda: T0, poll=0.01, turn_quiet=0.01))
        while len(source.reads) < 2:
            await asyncio.sleep(0.01)
        for _ in range(5):
            source.ticks["s1"].put_nowait(None)
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert db.threads
    assert threading.get_ident() not in db.threads


class BrokenWatch(Source):
    """A source whose watch fails with what isn't a capture error."""

    def __init__(self) -> None:
        super().__init__()
        self.opened: list[str] = []

    async def watch(self, session_id: str) -> AsyncIterator[None]:
        self.opened.append(session_id)
        raise RuntimeError("stream broke")
        yield None


def test_a_watch_that_fails_is_logged_and_opened_again(
    db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    source = BrokenWatch()
    source.add("s1")
    source.say("s1", "user", "build", T0)
    t = tracker(db, feed(source))

    async def scenario() -> None:
        task = asyncio.create_task(run(t, clock=lambda: T0, poll=0.01))
        while len(source.opened) < 2:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert "watch on s1 failed" in caplog.text


class SlowSource(Source):
    """Reads messages only once a test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.reading, self.release = threading.Event(), threading.Event()

    def messages(self, session_id: str, from_ordinal: int) -> Transcript:
        self.reading.set()
        self.release.wait(5)
        return super().messages(session_id, from_ordinal)


def test_a_stopped_run_waits_for_the_read_under_way(db: sqlite3.Connection) -> None:
    source = SlowSource()
    source.add("s1")
    source.say("s1", "user", "build", T0)
    t = tracker(db, feed(source))
    reading, release = source.reading, source.release

    async def scenario() -> None:
        task = asyncio.create_task(run(t, clock=lambda: T0, poll=3600))
        while not reading.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.1)
        assert not task.done()  # the read goes on in its thread, and holds the database till it ends
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


# ── the recorded sessions ─────────────────────────────────────────────────


def agentsview_over_fixtures() -> AgentsviewSource:
    sessions = json.loads((FIXTURES / "agentsview" / "sessions.json").read_text())["sessions"]
    by_id = {
        json.loads((FIXTURES / "agentsview" / agent / "session.json").read_text())["id"]: agent
        for agent in ("claude", "codex", "copilot")
    }

    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/sessions":
            return httpx.Response(200, json={"sessions": sessions, "total": len(sessions)})
        sid = request.url.path.split("/")[4]
        if sid not in by_id:
            return httpx.Response(404, json={"status": 404, "detail": "session not found"})
        body = json.loads((FIXTURES / "agentsview" / by_id[sid] / "messages.json").read_text())
        later = [m for m in body["messages"] if m["ordinal"] >= int(request.url.params["from"])]
        return httpx.Response(
            200, json={"messages": later or None, "transcript_revision": body.get("transcript_revision")}
        )

    return AgentsviewSource("http://av", transport=httpx.MockTransport(serve))


def test_the_recorded_sessions_end_their_turn_and_finalize(db: sqlite3.Connection, tmp_path: Path) -> None:
    shutil.copytree(FIXTURES / "copilot" / "session-state", tmp_path / ".copilot" / "session-state")
    copilot = CopilotSource({"HOME": str(tmp_path)})
    with agentsview_over_fixtures() as agentsview:
        t = tracker(
            db, feed(agentsview, "claude", "codex", name="agentsview"), feed(copilot, "copilot", name="copilot")
        )
        recorded = datetime(2026, 11, 19, 7, 50, tzinfo=UTC)
        t.poll(recorded)
        t.poll(recorded + 10 * SECOND)
        later = t.poll(recorded + 2 * 60 * MINUTE)
    rows = {row.id: row for row in t.sessions()}
    assert {sid: (row.feed, row.headless, row.ends_turn) for sid, row in rows.items()} == {
        sid: ("copilot" if sid.startswith("copilot:") else "agentsview", not sid.startswith("copilot:"), True)
        for sid in rows
    }
    assert len(rows) == 3  # the listing's fourth session has no recorded messages, so isn't read
    assert sorted(kinds(later)) == sorted(("finalized", sid) for sid in rows)
    assert [c.kind for _, c in t.changes()].count("turn_end") == 3
