"""Seam: the tracker over capture sources, with a fixed clock (docs/design.md §4.3).

Most tests drive a made-up source whose sessions they change between polls, raising its transcript revision on every
change as agentsview does; the last ones run the live check's recorded sessions (2026-11-19) through agentsview's
adapter and Copilot CLI's own reader."""

import asyncio
import json
import shutil
import sqlite3
import tempfile
import threading
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, precondition, rule

from agent_fabric.capture import AgentsviewSource, CaptureError, Message, SessionMeta, Transcript
from agent_fabric.copilot import CopilotSource
from agent_fabric.tracker import IDLE, SCHEMA, TURN_QUIET, Change, Feed, Tracker, connect, run

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)


class Source:
    """A capture source whose sessions and messages a test sets, and which fails while `down` says why."""

    def __init__(self) -> None:
        self.metas: dict[str, SessionMeta] = {}
        self.texts: dict[str, list[Message]] = {}
        self.down: str | None = None
        self.unreadable: set[str] = set()
        self.broken: set[str] = set()  # fail with what isn't a capture error
        self.reads: list[str] = []
        self.attempts = 0
        self.ticks: dict[str, asyncio.Queue[None]] = {}

    def add(self, sid: str, agent: str = "claude", **fields: Any) -> None:
        self.metas[sid] = meta(sid, agent, **({"transcript_revision": "0"} | fields))
        self.texts[sid] = []

    def say(self, sid: str, role: str, content: str | None, at: datetime | None, **fields: Any) -> None:
        ordinal = self.texts[sid][-1].ordinal + 1 if self.texts[sid] else 0
        self.texts[sid].append(message(sid, ordinal, role, content, at, **fields))
        self.changed(sid, ended_at=at)

    def fold(self, sid: str, ordinal: int, **fields: Any) -> None:
        """Change a message already there, as a tool's result folding into its call does."""
        at = next(i for i, m in enumerate(self.texts[sid]) if m.ordinal == ordinal)
        self.texts[sid][at] = Message.model_validate(self.texts[sid][at].model_dump() | fields)
        self.changed(sid)

    def reparse(self, sid: str, messages: list[Message]) -> None:
        self.texts[sid] = messages
        self.changed(sid)

    def changed(self, sid: str, **fields: Any) -> None:
        old = self.metas[sid]
        revision = str(int(old.transcript_revision or 0) + 1)
        update = {"message_count": len(self.texts[sid]), "transcript_revision": revision} | fields
        self.metas[sid] = old.model_copy(update=update)

    def active_sessions(self, since: datetime) -> list[SessionMeta]:
        if self.down:
            raise CaptureError(self.down)
        # agentsview filters on its side, whatever zone a time was written in.
        return [m for m in self.metas.values() if m.ended_at is None or m.ended_at.replace(tzinfo=UTC) >= since]

    def session(self, session_id: str) -> SessionMeta:
        return self.metas[session_id]

    def messages(self, session_id: str, from_ordinal: int) -> Transcript:
        self.attempts += 1
        if session_id not in self.texts:
            raise CaptureError(f"agentsview: HTTP 404: no session {session_id}", 404)
        if session_id in self.broken:
            raise TypeError("can't compare offset-naive and offset-aware datetimes")
        if self.down or session_id in self.unreadable:
            raise CaptureError(self.down or "unreadable")
        self.reads.append(session_id)
        later = tuple(m for m in self.texts[session_id] if m.ordinal >= from_ordinal)
        return Transcript(self.metas[session_id].transcript_revision if later else None, later)

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


def turn(source: Source, sid: str, at: datetime, prompt: str = "run make", answer: str = "Done.") -> None:
    """A prompt, a command, and the agent's answer."""
    source.say(sid, "user", prompt, at)
    source.say(sid, "assistant", None, at + SECOND, tool_calls=[shell("ok", "completed")])
    source.say(sid, "assistant", answer, at + 2 * SECOND)


ENDS = [("turn_end", "s1"), ("finalized", "s1")]


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
    # Never quiet for 5 s between polls: each turn ends as the session finalizes.
    assert kinds(t.poll(T0 + 40 * MINUTE)) == ENDS
    turn(source, "s1", T0 + 50 * MINUTE)
    assert kinds(t.poll(T0 + 51 * MINUTE)) == [("reopened", "s1")]
    assert kinds(t.poll(T0 + 90 * MINUTE)) == ENDS
    row = t.session("s1")
    assert row is not None
    assert (row.finalizations, row.last_ordinal) == (2, 5)


def test_a_session_first_seen_long_quiet_finalizes_at_once(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    assert kinds(tracker(db, feed(source)).poll(T0 + 33 * MINUTE)) == [("started", "s1"), *ENDS]


def test_a_turn_isnt_over_while_the_agent_calls_a_tool(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    source.say("s1", "assistant", "Building.", T0, tool_calls=[shell(None, "running")])
    t = tracker(db, feed(source))
    t.poll(T0)
    assert t.poll(T0 + MINUTE) == []


def test_a_result_that_folds_in_is_read_from_the_listing_alone(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    source.say("s1", "assistant", None, T0, tool_calls=[shell(None, "running")])
    t = tracker(db, feed(source))
    t.poll(T0)
    t.poll(T0 + 10 * SECOND)
    source.fold("s1", 1, tool_calls=[shell("ok", "completed")])
    t.poll(T0 + 20 * SECOND)
    assert source.reads == ["s1", "s1"]
    row = t.session("s1")
    assert row is not None
    assert row.changed_at == T0 + 20 * SECOND


def test_a_call_that_never_finished_doesnt_hold_later_turns(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    source.say("s1", "assistant", None, T0, tool_calls=[shell(None)])  # interrupted: no result, ever
    turn(source, "s1", T0 + MINUTE)
    t = tracker(db, feed(source))
    t.poll(T0 + MINUTE)
    assert kinds(t.poll(T0 + 2 * MINUTE)) == [("turn_end", "s1")]


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


def test_a_reparse_that_renumbers_the_answer_doesnt_end_its_turn_again(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    assert kinds(t.poll(T0 + 10 * SECOND)) == [("turn_end", "s1")]
    # Re-parsed with the tool result numbered apart: the same answer is now ordinal 3.
    first, call, answer = source.texts["s1"]
    hidden = message("s1", 2, "user", None, T0 + SECOND, is_system=True)
    source.reparse("s1", [first, call, hidden, answer.model_copy(update={"ordinal": 3})])
    t.poll(T0 + MINUTE)
    assert t.poll(T0 + 2 * MINUTE) == []


def test_a_reparse_that_numbers_a_new_turn_as_an_old_one_still_ends_it(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    t.poll(T0 + 10 * SECOND)  # turn end at ordinal 2
    source.reparse("s1", [])
    turn(source, "s1", T0 + MINUTE, prompt="again", answer="Done again.")
    t.poll(T0 + MINUTE)
    assert kinds(t.poll(T0 + MINUTE + 10 * SECOND)) == [("turn_end", "s1")]


def test_two_turns_answered_alike_with_no_times_each_end(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1", "cursor")
    t = tracker(db, feed(source, "cursor"))
    source.say("s1", "user", "fix it", None)
    source.say("s1", "assistant", "Done.", None)
    t.poll(T0)
    assert kinds(t.poll(T0 + 10 * SECOND)) == [("turn_end", "s1")]
    source.say("s1", "user", "and the other", None)
    source.say("s1", "assistant", "Done.", None)
    t.poll(T0 + MINUTE)
    assert kinds(t.poll(T0 + MINUTE + 10 * SECOND)) == [("turn_end", "s1")]


def test_a_session_unchanged_in_its_listing_isnt_read_again(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    t.poll(T0 + 10 * SECOND)
    t.poll(T0 + 20 * SECOND)
    assert source.reads == ["s1"]
    source.say("s1", "user", "more", T0 + 30 * SECOND)
    t.poll(T0 + 30 * SECOND)
    assert source.reads == ["s1", "s1"]


def test_a_reparse_that_changes_nothing_is_no_activity(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    t.poll(T0 + 40 * MINUTE)
    source.reparse("s1", list(source.texts["s1"]))
    assert t.poll(T0 + 41 * MINUTE) == []
    row = t.session("s1")
    assert row is not None
    assert row.state == "finalized"


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


def test_a_result_that_folds_in_counts_from_its_own_time(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    source.say("s1", "assistant", None, T0, tool_calls=[shell(None, "running")])
    t = tracker(db, feed(source))
    t.poll(T0)
    # A 40-minute build: its result folds into the call's message, which keeps the call's time.
    done = shell("ok") | {"result_events": [{"status": "completed", "timestamp": (T0 + 40 * MINUTE).isoformat()}]}
    source.fold("s1", 1, tool_calls=[done])
    assert t.tick("s1", T0 + 40 * MINUTE) == []
    row = t.session("s1")
    assert row is not None
    assert (row.state, row.active_at) == ("active", T0 + 40 * MINUTE)


def test_a_change_in_a_session_with_no_times_counts_from_when_it_was_seen(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1", "cursor")
    source.say("s1", "user", "fix it", None)
    t = tracker(db, feed(source, "cursor"))
    t.poll(T0)
    source.say("s1", "assistant", "Fixed.", None)
    t.poll(T0 + 20 * MINUTE)
    assert kinds(t.poll(T0 + 45 * MINUTE)) == [("turn_end", "s1")]
    assert kinds(t.poll(T0 + 50 * MINUTE)) == [("finalized", "s1")]


def test_a_new_field_in_how_messages_read_reopens_nothing(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0 + 3 * SECOND)
    t.poll(T0 + 40 * MINUTE)
    # As after an agentsview upgrade, or a field added to the capture models: every message reads differently.
    source.reparse("s1", [m.model_copy(update={"model": "claude-x"}) for m in source.texts["s1"]])
    assert t.poll(T0 + 41 * MINUTE) == []
    assert t.poll(T0 + 42 * MINUTE) == []


def test_a_time_without_a_zone_counts_as_utc(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "hi", datetime(2026, 11, 20, 9, 0))  # a source can give a time without a zone
    source.metas["s1"] = source.metas["s1"].model_copy(update={"ended_at": T0})
    t = tracker(db, feed(source))
    t.poll(T0 + MINUTE)
    row = t.session("s1")
    assert row is not None
    assert row.active_at == T0


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
    assert agentsview.reads == ["s1"]


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
    assert kinds(t.poll(T0 + 42 * MINUTE)) == ENDS
    assert t.offline == {}


def test_a_session_that_cant_be_read_doesnt_stop_the_others(db: sqlite3.Connection, source: Source) -> None:
    source.add("bad")
    source.add("good")
    source.unreadable.add("bad")
    t = tracker(db, feed(source))
    assert kinds(t.poll(T0)) == [("started", "good")]
    source.unreadable.clear()
    assert kinds(t.poll(T0 + SECOND)) == [("started", "bad")]


def test_any_error_in_one_session_leaves_the_others_and_the_next_poll(
    db: sqlite3.Connection, source: Source, caplog: pytest.LogCaptureFixture
) -> None:
    source.add("bad")
    source.add("good")
    source.broken.add("bad")
    t = tracker(db, feed(source))
    assert kinds(t.poll(T0)) == [("started", "good")]
    assert "TypeError" in caplog.text
    source.broken.clear()
    assert kinds(t.poll(T0 + SECOND)) == [("started", "bad")]


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
    t.poll(T0 + 33 * MINUTE)  # its listing changed while it couldn't be read: read now
    row = t.session("s1")
    assert row is not None
    assert (row.state, row.active_at) == ("active", T0 + 29 * MINUTE)


def test_a_session_whose_read_fails_is_asked_once_a_poll(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    source.unreadable.add("s1")
    source.say("s1", "user", "more", T0 + MINUTE)
    t.poll(T0 + MINUTE)
    t.poll(T0 + 2 * MINUTE)
    assert source.attempts == 3


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
    assert kinds(t.poll(T0 + 25 * 60 * MINUTE)) == ENDS


def test_a_session_listed_as_active_isnt_settled_while_it_cant_be_read(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)  # then the tracker stops for a day
    later = T0 + 25 * 60 * MINUTE
    source.say("s1", "user", "more", later)
    source.unreadable.add("s1")
    assert t.poll(later + MINUTE) == []  # its listing says it worked a minute ago
    assert t.poll(later + 60 * MINUTE) == []
    assert kinds(t.poll(later + 25 * 60 * MINUTE)) == ENDS


def test_a_listing_time_without_a_zone_counts_as_utc(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1", ended_at=T0.replace(tzinfo=None))  # its messages have no times of their own
    source.say("s1", "user", "hi", None)
    source.changed("s1", ended_at=T0.replace(tzinfo=None))
    t = tracker(db, feed(source))
    assert kinds(t.poll(T0)) == [("started", "s1")]
    assert kinds(t.poll(T0 + 31 * MINUTE)) == [("finalized", "s1")]


def test_a_session_removed_from_its_source_finalizes_once_quiet_unwatched(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    del source.metas["s1"], source.texts["s1"]
    assert t.poll(T0 + MINUTE) == [Change("turn_end", "s1", 2, T0 + MINUTE)]
    assert t.watched() == set()
    assert t.tick("s1", T0 + 2 * MINUTE) == []
    assert kinds(t.poll(T0 + 31 * MINUTE)) == [("finalized", "s1")]
    assert source.attempts == 2  # asked once after it went: a gone session isn't asked again


def test_a_listed_session_its_source_doesnt_have_is_left_until_listed_again(
    db: sqlite3.Connection, source: Source, caplog: pytest.LogCaptureFixture
) -> None:
    source.metas["s1"] = meta("s1", message_count=1)  # listed, its messages gone
    t = tracker(db, feed(source))
    assert t.poll(T0) == []
    assert t.poll(T0 + 10 * SECOND) == []
    assert source.attempts == 1
    assert "can't read" not in caplog.text
    source.add("s1")
    source.say("s1", "user", "hi", T0 + MINUTE)
    assert kinds(t.poll(T0 + MINUTE)) == [("started", "s1")]


def test_a_watch_read_that_failed_is_tried_again_by_the_next_poll(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    source.say("s1", "assistant", "Built.", T0 + MINUTE)
    t.poll(T0 + MINUTE)  # the listing is read, and stored
    source.unreadable.add("s1")
    t.tick("s1", T0 + MINUTE + SECOND)  # agentsview restarted: the watch's opening tick fails
    source.unreadable.clear()
    t.poll(T0 + 2 * MINUTE)  # the listing is the same
    assert source.reads == ["s1", "s1", "s1"]
    assert kinds(t.poll(T0 + 40 * MINUTE)) == [("finalized", "s1")]


def test_a_tracked_session_still_listed_but_gone_is_read_only_when_listed_anew(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    source.changed("s1")
    del source.texts["s1"]  # its transcript gone, the listing not yet
    t.poll(T0 + MINUTE)
    t.poll(T0 + 2 * MINUTE)
    t.poll(T0 + 3 * MINUTE)
    assert source.attempts == 2
    assert t.watched() == set()


def test_an_active_session_no_longer_listed_is_read_once(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    t = tracker(db, feed(source))
    t.poll(T0)
    source.metas["s1"] = source.metas["s1"].model_copy(update={"is_automated": True})  # no longer listed
    t.poll(T0 + MINUTE)
    t.poll(T0 + 2 * MINUTE)
    t.poll(T0 + 3 * MINUTE)
    assert source.reads == ["s1", "s1"]


def test_a_gone_session_stays_gone_after_a_restart(tmp_path: Path, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    first = tracker(connect(tmp_path / "state.db"), feed(source))
    first.poll(T0)
    del source.metas["s1"], source.texts["s1"]
    first.poll(T0 + MINUTE)
    again = tracker(connect(tmp_path / "state.db"), feed(source))
    again.poll(T0 + 2 * MINUTE)
    assert source.attempts == 2
    assert again.watched() == set()


def test_a_session_whose_feed_was_removed_finalizes_unwatched(tmp_path: Path, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    tracker(connect(tmp_path / "state.db"), feed(source, name="old")).poll(T0)
    t = tracker(connect(tmp_path / "state.db"), feed(source, "codex", name="new"))
    assert t.watched() == set()
    assert kinds(t.poll(T0 + 40 * MINUTE)) == ENDS


def test_a_restart_reads_what_changed_while_it_was_down(tmp_path: Path, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    tracker(connect(tmp_path / "state.db"), feed(source)).poll(T0)
    again = tracker(connect(tmp_path / "state.db"), feed(source))
    assert kinds(again.poll(T0 + MINUTE)) == [("turn_end", "s1")]
    assert source.reads == ["s1"]
    source.say("s1", "user", "more", T0 + 2 * MINUTE)
    again.poll(T0 + 2 * MINUTE)
    assert source.reads == ["s1", "s1"]
    assert [(n, c.kind) for n, c in again.changes()] == [(1, "started"), (2, "turn_end")]


def test_a_restart_reads_an_active_session_no_longer_listed_before_finalizing_it(
    tmp_path: Path, source: Source
) -> None:
    source.add("s1")
    turn(source, "s1", T0)
    tracker(connect(tmp_path / "state.db"), feed(source)).poll(T0)
    turn(source, "s1", T0 + 60 * MINUTE)  # went on while the tracker was down, and then quiet for over a day
    again = tracker(connect(tmp_path / "state.db"), feed(source))
    # Its last turn ended while the tracker was down, and gets its turn end with the finalization.
    assert kinds(again.poll(T0 + 30 * 60 * MINUTE)) == ENDS
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


def test_a_stopped_run_still_passes_on_what_the_step_under_way_did(db: sqlite3.Connection) -> None:
    source = SlowSource()
    source.add("s1")
    source.say("s1", "user", "build", T0)
    t = tracker(db, feed(source))
    seen: list[Change] = []

    async def scenario() -> None:
        task = asyncio.create_task(run(t, emit=seen.append, clock=lambda: T0, poll=3600))
        while not source.reading.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        source.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert kinds(seen) == [("started", "s1")]


def test_run_reads_the_clock_under_the_lock(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    source.say("s1", "user", "build", T0)
    t = tracker(db, feed(source))
    threads: set[int] = set()

    def clock() -> datetime:
        threads.add(threading.get_ident())
        return T0

    async def scenario() -> None:
        task = asyncio.create_task(run(t, clock=clock, poll=0.01))
        while len(threads) < 1 or len(source.reads) < 2:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert threading.get_ident() not in threads


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


def test_half_an_emoji_in_a_transcript_doesnt_stop_its_turn_end(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0, prompt="cut \ud83d here", answer="ok \ud83d")
    t = tracker(db, feed(source))
    assert kinds(t.poll(T0 + 3 * SECOND)) == [("started", "s1")]
    assert kinds(t.poll(T0 + 8 * SECOND)) == [("turn_end", "s1")]


def test_a_time_ahead_of_the_clock_doesnt_hold_a_session_open(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0 + timedelta(days=1))  # a clock a day ahead of ours
    t = tracker(db, feed(source))
    t.poll(T0)
    assert kinds(t.poll(T0 + 31 * MINUTE)) == ENDS


def test_a_listing_ahead_of_the_clock_doesnt_hold_a_session_with_no_times_open(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1", ended_at=T0 + timedelta(days=1))
    source.say("s1", "user", "hi", None)
    source.changed("s1", ended_at=T0 + timedelta(days=1))
    t = tracker(db, feed(source))
    t.poll(T0)
    assert kinds(t.poll(T0 + 31 * MINUTE)) == [("finalized", "s1")]


def test_a_reparse_doesnt_reopen_a_session_ahead_of_the_clock(db: sqlite3.Connection, source: Source) -> None:
    source.add("s1")
    turn(source, "s1", T0 + timedelta(hours=3))  # a zone-less local time on a machine 3 h ahead of UTC
    t = tracker(db, feed(source))
    t.poll(T0)
    assert kinds(t.poll(T0 + 31 * MINUTE)) == ENDS
    first, call, answer = source.texts["s1"]
    source.reparse("s1", [first, call, answer.model_copy(update={"content": "Done!"})])
    assert t.poll(T0 + 32 * MINUTE) == []
    turn(source, "s1", T0 + timedelta(hours=3, minutes=40), prompt="and now?")  # the same offset, later
    assert kinds(t.poll(T0 + 41 * MINUTE)) == [("reopened", "s1")]


def test_a_session_with_no_times_listed_ahead_of_the_clock_reopens_on_a_change(
    db: sqlite3.Connection, source: Source
) -> None:
    source.add("s1", "cursor")
    source.say("s1", "user", "fix it", None)
    source.changed("s1", ended_at=T0 + timedelta(hours=3))  # a zone-less local time, 3 h ahead of UTC
    t = tracker(db, feed(source, "cursor"))
    t.poll(T0)
    assert kinds(t.poll(T0 + 31 * MINUTE)) == [("finalized", "s1")]
    source.say("s1", "user", "and this", None)
    source.changed("s1", ended_at=T0 + timedelta(hours=3, minutes=40))
    assert kinds(t.poll(T0 + 40 * MINUTE)) == [("reopened", "s1")]


# ── the lifecycle, as a state machine ─────────────────────────────────────


class Lifecycle(RuleBasedStateMachine):
    """§4.3's lifecycle against any order of turns, re-parses and polls, on a source whose clock may run ahead of
    ours (a zone-less local time read as UTC) and whose messages may carry no times (Cursor CLI's in agentsview).

    After every poll: a new message has started or reopened the session, a re-parse of a timed session reopens
    nothing, an answer has ended its turn once quiet, a session is finalized once unchanged for the idle timeout and
    not before its last new message is that old, and the changes come in their lifecycle's order."""

    def __init__(self) -> None:
        super().__init__()
        self.source = Source()
        self.source.add("s1")
        self.folder: Path | None = None
        self.db: sqlite3.Connection | None = None
        self.now = T0
        self.ahead = timedelta(0)
        self.timed = True
        self.seen: datetime | None = None  # the last poll that read a change
        self.active = T0  # no later than the last new activity, by our clock
        self.answer = (0, "")  # the last answer, by its turn and wording
        self.ended = (0, "")  # the answer the last turn_end was for
        self.read: datetime | None = None  # the poll that read the last answer
        self.pending: str | None = None  # what changed since the last poll: "turn" or "reparse"
        self.state: str | None = None  # the lifecycle as the changes tell it

    @initialize(ahead=st.sampled_from([timedelta(0), timedelta(hours=3)]), timed=st.booleans())
    def source_clock(self, ahead: timedelta, timed: bool) -> None:
        self.ahead, self.timed = ahead, timed
        # Here, not in __init__, so that teardown removes the folder even if opening the database fails.
        self.folder = Path(tempfile.mkdtemp())
        self.db = connect(self.folder / "state.db")
        self.tracker = tracker(self.db, feed(source=self.source))

    @rule()
    def new_turn(self) -> None:
        at = self.now + self.ahead
        if self.timed:
            turn(self.source, "s1", at)  # lists the session as ending at its answer's time
        else:
            self.source.say("s1", "user", "run make", None)
            self.source.say("s1", "assistant", "Done.", None)
            self.source.changed("s1", ended_at=at)  # only the listing has a time
        self.pending = "turn"
        self.active = self.now
        self.answer = (self.answer[0] + 1, "Done.")

    @precondition(lambda self: self.source.texts["s1"] and self.pending is None)
    @rule()
    def reparse(self) -> None:
        """The answer read anew, its wording changed and its time not."""
        *rest, last = self.source.texts["s1"]
        wording = "Done!" if last.content == "Done." else "Done."
        self.source.reparse("s1", [*rest, last.model_copy(update={"content": wording})])
        self.pending = "reparse"
        self.answer = (self.answer[0], wording)
        if not self.timed:
            self.active = self.now  # with no times to go by, any change is activity

    # Gaps by what they reach: not yet a turn's quiet, past it, past the idle timeout.
    @rule(gap=st.sampled_from([2 * SECOND, 10 * SECOND, 5 * MINUTE, IDLE + MINUTE]))
    def poll(self, gap: timedelta) -> None:
        self.now += gap
        before = self.state
        changes = self.tracker.poll(self.now)
        kinds = [change.kind for change in changes]
        for change in changes:
            expected = {
                "started": (None,),
                "turn_end": ("active",),
                "finalized": ("active",),
                "reopened": ("finalized",),
            }[change.kind]
            assert self.state in expected, f"{change.kind} while {self.state}"
            self.state = {"started": "active", "reopened": "active", "finalized": "finalized"}.get(
                change.kind, self.state
            )
        row = self.tracker.session("s1")
        if self.pending == "turn" and before != "active":
            # It may finalize in the same poll, when its own time is already the idle timeout ago.
            assert {"started", "reopened"} & set(kinds), "a new message didn't reopen the session"
        if self.pending == "reparse" and self.timed:
            assert "reopened" not in kinds, "a re-parse reopened the session"
        if self.pending == "reparse" and not self.timed and before == "finalized":
            assert "reopened" in kinds, "a change with no times didn't reopen the session"
        if self.pending is None:
            assert "reopened" not in kinds, "a poll that read no change reopened the session"
        if "finalized" in kinds:
            assert self.now - self.active >= IDLE, "a session finalized before its idle timeout"
        if self.pending:
            self.read = self.now
        if "turn_end" in kinds:
            assert self.answer != self.ended, "an answer ended its turn twice"
            self.ended = self.answer
        if self.state == "active" and self.answer != self.ended and self.read is not None:
            assert self.now - self.read < TURN_QUIET, "an answer quiet for 5 s didn't end its turn"
        if self.pending or "started" in kinds:  # a session first read with no messages has to finalize too
            self.seen = self.now
        self.pending = None
        if row is not None and self.seen is not None and self.now - self.seen >= IDLE:
            assert row.state == "finalized", "a session unchanged for the idle timeout wasn't finalized"

    def teardown(self) -> None:
        if self.db is not None:
            self.db.close()
        if self.folder is not None:
            shutil.rmtree(self.folder)


TestLifecycle = Lifecycle.TestCase
# About 9 s. Fresh runs find each clock bug a review caught here (a re-parse that reopened a session ahead of the
# clock, an untimed one that couldn't reopen) about 4 times in 5; the two tests above pin them. A failure prints a
# blob for @reproduce_failure, since CI keeps no .hypothesis folder.
TestLifecycle.settings = settings(max_examples=1000, stateful_step_count=20, deadline=None, print_blob=True)
