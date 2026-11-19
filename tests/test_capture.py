"""Seam: the agentsview adapter against responses recorded from agentsview (docs/design.md §4.1).

The fixtures come from the live check of 2026-11-19: one scripted headless session each of Claude Code, Codex and
Copilot CLI, recorded from agentsview at the pin and scrubbed of the machine's paths. The contract tests hold them
to the pinned spec; the rest serve them from a fake agentsview that pages them the way agentsview does."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from agent_fabric.capture import AgentsviewSource, CaptureError

FIXTURES = Path(__file__).parent / "fixtures" / "agentsview"
PIN = Path(__file__).parent.parent / "vendor" / "agentsview.pin"
AGENTS = ("claude", "codex", "copilot")
SINCE = datetime(2026, 11, 19, 7, 0, tzinfo=UTC)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


SPEC = load("openapi.json")


def ids(agent: str) -> str:
    return str(load(f"{agent}/session.json")["id"])


# ── contract ──────────────────────────────────────────────────────────────


def test_the_spec_is_the_pinned_versions() -> None:
    pin = dict(line.split("=", 1) for line in PIN.read_text().splitlines() if line and not line.startswith("#"))
    assert (SPEC["tag"], SPEC["commit"]) == (pin["tag"], pin["commit"]), "run scripts/agentsview-openapi.py"


@pytest.mark.parametrize(
    ("path", "fixture"),
    [
        ("/api/v1/sessions", "sessions.json"),
        *[("/api/v1/sessions/{id}", f"{agent}/session.json") for agent in AGENTS],
        *[("/api/v1/sessions/{id}/messages", f"{agent}/messages.json") for agent in AGENTS],
    ],
)
def test_a_recorded_response_matches_the_pinned_spec(path: str, fixture: str) -> None:
    schema = {**SPEC["paths"][path]["response"], "components": SPEC["components"]}
    errors = sorted(Draft202012Validator(schema).iter_errors(load(fixture)), key=lambda e: list(e.absolute_path))
    assert not errors, "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors[:5])


def test_the_adapter_sends_only_parameters_the_spec_has() -> None:
    sent: dict[str, set[str]] = {}

    def record(request: httpx.Request) -> httpx.Response:
        sent.setdefault(spec_path(request.url.path), set()).update(request.url.params.keys())
        return Agentsview().handle(request)

    with AgentsviewSource("http://av", transport=httpx.MockTransport(record), page=2) as source:
        source.active_sessions(SINCE)
        source.session(ids("codex"))
        source.messages(ids("claude"), 0)
    for path, params in sent.items():
        assert params <= set(SPEC["paths"][path]["parameters"]), path


# ── a fake agentsview over the fixtures ───────────────────────────────────


def spec_path(path: str) -> str:
    parts = path.split("/")
    if len(parts) > 4:
        parts[4] = "{id}"
    return "/".join(parts)


class Agentsview:
    """Serves the fixtures, paging the session list by cursor and messages by ordinal, as agentsview does, and
    clamping `limit` to `cap` without saying so, as agentsview does to 500 sessions and 1000 messages."""

    def __init__(self, cap: int = 10_000) -> None:
        self.cap = cap
        self.sessions = {ids(agent): load(f"{agent}/session.json") for agent in AGENTS}
        self.messages = {ids(agent): load(f"{agent}/messages.json")["messages"] for agent in AGENTS}
        self.requests: list[httpx.Request] = []

    def revision(self, sid: str) -> str:
        return str(self.sessions[sid]["transcript_revision"])

    def page(self, sid: str, start: int, limit: int) -> dict[str, Any]:
        later = [m for m in self.messages[sid] if m["ordinal"] >= start][:limit]
        if not later:
            # Past the last message agentsview's Go slice was never filled: null, and no revision to look up.
            return {"messages": None, "count": 0}
        return {
            "messages": later,
            "count": len(later),
            "transcript_revision": self.revision(sid),
            "first_ordinal": later[0]["ordinal"],
            "last_ordinal": later[-1]["ordinal"],
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, params = request.url.path, request.url.params
        limit = min(int(params.get("limit", "0")) or 100, self.cap)
        if path == "/api/v1/sessions":
            listed = load("sessions.json")["sessions"]
            start = int(params.get("cursor", "0"))
            body: dict[str, Any] = {"sessions": listed[start : start + limit], "total": len(listed)}
            if start + limit < len(listed):
                body["next_cursor"] = str(start + limit)
            return httpx.Response(200, json=body)
        parts = path.split("/")
        sid = parts[4]
        if sid not in self.sessions:
            return httpx.Response(404, json={"title": "Not Found", "status": 404, "detail": "session not found"})
        if len(parts) == 5:
            return httpx.Response(200, json=self.sessions[sid])
        if parts[5] == "messages":
            return httpx.Response(200, json=self.page(sid, int(params.get("from", "0")), limit))
        if parts[5] == "watch":
            agent = self.sessions[sid]["agent"]
            stream = (FIXTURES / agent / "watch.sse").read_bytes()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)
        return httpx.Response(404)


def source_over(fake: Agentsview | Callable[[httpx.Request], httpx.Response], page: int = 2) -> AgentsviewSource:
    handler = fake.handle if isinstance(fake, Agentsview) else fake
    transport = httpx.MockTransport(handler)
    return AgentsviewSource("http://av", transport=transport, async_transport=transport, page=page)


# ── sessions ──────────────────────────────────────────────────────────────


def test_active_sessions_takes_every_page_with_headless_runs_and_one_shots() -> None:
    fake = Agentsview()
    with source_over(fake) as source:
        found = source.active_sessions(SINCE)
    assert [s.id for s in found] == [s["id"] for s in load("sessions.json")["sessions"]]
    assert len(found) == 4
    assert len(fake.requests) == 2
    first = fake.requests[0].url.params
    assert first["active_since"] == "2026-11-19T07:00:00Z"
    assert (first["include_children"], first["include_one_shot"]) == ("true", "true")
    assert fake.requests[1].url.params["cursor"] == "2"


def test_active_sessions_follow_the_cursor_past_a_page_agentsview_cut_short() -> None:
    fake = Agentsview(cap=1)
    with source_over(fake, page=3) as source:
        assert len(source.active_sessions(SINCE)) == 4
    assert len(fake.requests) == 4


@pytest.mark.parametrize("agent", AGENTS)
def test_messages_read_on_past_a_page_agentsview_cut_short(agent: str) -> None:
    with source_over(Agentsview(cap=2), page=10) as source:
        got = source.messages(ids(agent), 0).messages
    assert [m.ordinal for m in got] == [m["ordinal"] for m in load(f"{agent}/messages.json")["messages"]]


def test_active_sessions_sends_its_time_in_utc() -> None:
    fake = Agentsview()
    with source_over(fake, page=10) as source:
        source.active_sessions(datetime.fromisoformat("2026-11-19T09:00:00+02:00"))
    assert fake.requests[0].url.params["active_since"] == "2026-11-19T07:00:00Z"


def test_a_naive_time_is_refused() -> None:
    with source_over(Agentsview()) as source, pytest.raises(ValueError, match="naive"):
        source.active_sessions(datetime(2026, 11, 19, 7, 0))


def test_each_agents_session_reads_as_agentsview_files_it() -> None:
    with source_over(Agentsview()) as source:
        claude, codex, copilot = (source.session(ids(agent)) for agent in AGENTS)
    assert not claude.id.startswith("claude:")
    assert (claude.entrypoint, claude.relationship_type) == ("sdk-cli", "subagent")
    assert codex.id.startswith("codex:")
    assert (codex.session_kind, codex.termination_status) == ("non-interactive", "awaiting_user")
    assert copilot.cwd is None
    assert copilot.project == "spikeproj"
    assert claude.cwd == "/home/user/code/spikeproj"
    assert claude.started_at is not None
    assert claude.started_at.tzinfo is not None


def test_a_session_id_with_a_colon_reaches_its_session() -> None:
    fake = Agentsview()
    with source_over(fake) as source:
        assert source.session(ids("codex")).agent == "codex"
    assert fake.requests[0].url.raw_path.decode().startswith("/api/v1/sessions/codex%3A")


def listing(*sessions: dict[str, Any], next_cursor: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {"sessions": list(sessions), "total": len(sessions)}
    if next_cursor:
        body["next_cursor"] = next_cursor
    return httpx.Response(200, json=body)


def test_a_session_without_a_project_keeps_an_empty_one() -> None:
    nameless = load("codex/session.json") | {"project": ""}

    with source_over(lambda request: listing(nameless)) as source:
        (found,) = source.active_sessions(SINCE)
    assert found.project == ""


def test_a_session_the_adapter_cant_read_is_skipped_alone(caplog: pytest.LogCaptureFixture) -> None:
    broken = load("codex/session.json") | {"started_at": "yesterday"}
    fine = load("claude/session.json")

    with source_over(lambda request: listing(broken, fine)) as source:
        found = source.active_sessions(SINCE)
    assert [s.id for s in found] == [fine["id"]]
    assert broken["id"] in caplog.text


def test_no_active_session_is_an_empty_list() -> None:
    with source_over(lambda request: httpx.Response(200, json={"sessions": None, "total": 0})) as source:
        assert source.active_sessions(SINCE) == []


def test_a_session_on_two_pages_is_listed_once() -> None:
    codex, claude = load("codex/session.json"), load("claude/session.json")

    def moved(request: httpx.Request) -> httpx.Response:
        if "cursor" not in request.url.params:
            return listing(codex, claude, next_cursor="1")
        return listing(codex)  # active again while the list was read, so it moved up a page

    with source_over(moved) as source:
        assert [s.id for s in source.active_sessions(SINCE)] == [codex["id"], claude["id"]]


def test_a_cursor_that_comes_back_is_a_capture_error() -> None:
    session = load("codex/session.json")
    cycle = iter(["a", "b"] * 25)

    def cycling(request: httpx.Request) -> httpx.Response:
        return listing(session, next_cursor=next(cycle))  # runs out, and fails, if the adapter never stops

    with source_over(cycling) as source, pytest.raises(CaptureError, match="came back"):
        source.active_sessions(SINCE)


def test_a_cursor_that_doesnt_move_on_is_a_capture_error() -> None:
    session = load("codex/session.json")
    asked = 0

    def same_cursor(request: httpx.Request) -> httpx.Response:
        nonlocal asked
        asked += 1
        assert asked < 50, "the adapter kept asking"  # fail here, not after the loop has eaten the memory
        return listing(session, next_cursor="same")

    with source_over(same_cursor) as source, pytest.raises(CaptureError, match="cursor"):
        source.active_sessions(SINCE)


# ── messages ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("agent", AGENTS)
def test_messages_page_through_to_the_last(agent: str) -> None:
    with source_over(Agentsview()) as source:
        got = source.messages(ids(agent), 0).messages
    assert [m.ordinal for m in got] == [m["ordinal"] for m in load(f"{agent}/messages.json")["messages"]]


def test_messages_start_at_the_ordinal_asked_for_across_gaps() -> None:
    # Claude Code's ordinals skip the tool results: 0, 1, 3, 5, 7.
    with source_over(Agentsview()) as source:
        got = source.messages(ids("claude"), 2).messages
    assert [m.ordinal for m in got] == [3, 5, 7]


def test_a_failed_command_keeps_its_status_and_output() -> None:
    with source_over(Agentsview(), page=10) as source:
        claude = [c for m in source.messages(ids("claude"), 0).messages for c in m.tool_calls]
        copilot = [c for m in source.messages(ids("copilot"), 0).messages for c in m.tool_calls]
    check = claude[0]
    assert check.tool_name == "Bash"
    assert check.input_json is not None
    assert "python3 check.py" in check.input_json
    assert [e.status for e in check.result_events] == ["errored"]
    assert check.result_content is not None
    assert check.result_content.startswith("Exit code 1")
    # Claude Code leaves a success's status empty, which reads as none.
    assert [e.status for e in claude[1].result_events] == [None]
    edit = next(c for c in copilot if c.tool_name == "apply_patch")
    assert [e.status for e in edit.result_events] == ["started", "errored"]
    assert edit.result_content is None


def test_a_messages_timestamp_reads_as_a_time() -> None:
    with source_over(Agentsview(), page=10) as source:
        first = source.messages(ids("codex"), 0).messages[0]
    assert first.role == "user"
    assert first.timestamp is not None
    assert first.timestamp.year == 2026


class Edited(Agentsview):
    """agentsview with the recorded Claude Code messages changed by `edit`."""

    def __init__(self, edit: Callable[[list[dict[str, Any]]], object]) -> None:
        super().__init__()
        edit(self.messages[ids("claude")])


def test_a_message_without_a_timestamp_has_none() -> None:
    def undated(messages: list[dict[str, Any]]) -> None:
        messages[0]["timestamp"] = ""  # how agentsview stores a Cursor CLI message it can't date

    with source_over(Edited(undated)) as source:
        assert source.messages(ids("claude"), 0).messages[0].timestamp is None


def test_a_message_whose_tool_calls_are_null_has_none() -> None:
    def nulls(messages: list[dict[str, Any]]) -> None:
        messages[1]["tool_calls"] = None

    with source_over(Edited(nulls)) as source:
        assert source.messages(ids("claude"), 0).messages[1].tool_calls == ()


def test_a_read_returns_the_revision_its_messages_are_of() -> None:
    with source_over(Agentsview()) as source:
        read = source.messages(ids("codex"), 0)
    assert read.revision == load("codex/session.json")["transcript_revision"]


def test_a_read_past_the_last_message_finds_none_and_no_revision() -> None:
    # agentsview answers it with `"messages": null`, as Go writes an empty list it never filled.
    with source_over(Agentsview()) as source:
        read = source.messages(ids("codex"), 100)
    assert (read.revision, read.messages) == (None, ())


def test_a_page_of_only_earlier_messages_is_a_capture_error() -> None:
    def ignores_from(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load("codex/messages.json"))

    with source_over(ignores_from, page=100) as source, pytest.raises(CaptureError, match="only earlier"):
        source.messages(ids("codex"), 2)


class Reparsed(Agentsview):
    """agentsview re-parsing the transcript after the first `reparses` pages: each new revision renumbers it."""

    def __init__(self, reparses: int) -> None:
        super().__init__(cap=2)
        self.reparses = reparses
        self.current = 1

    def revision(self, sid: str) -> str:
        return str(self.current)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages") and request.url.params["from"] != "0" and self.reparses:
            self.reparses -= 1
            self.current += 1
        return super().handle(request)


def test_a_read_starts_over_when_the_transcript_is_reparsed_mid_read() -> None:
    fake = Reparsed(reparses=1)
    with source_over(fake, page=10) as source:
        got = source.messages(ids("claude"), 0).messages
    assert [m.ordinal for m in got] == [0, 1, 3, 5, 7]
    froms = [r.url.params["from"] for r in fake.requests]
    assert froms.count("0") == 2


def test_a_revision_after_a_page_without_one_is_a_reparse() -> None:
    pages = iter([None, "2", "2", "2", "2", "2"])

    class Unstamped(Agentsview):
        def revision(self, sid: str) -> str:
            return next(pages) or ""

    fake = Unstamped(cap=3)
    with source_over(fake, page=3) as source:
        read = source.messages(ids("claude"), 0)
    assert read.revision == "2"
    assert [r.url.params["from"] for r in fake.requests].count("0") == 2


def test_a_transcript_that_keeps_changing_is_a_capture_error() -> None:
    with source_over(Reparsed(reparses=99), page=10) as source, pytest.raises(CaptureError, match="kept changing"):
        source.messages(ids("claude"), 0)


# ── watch ─────────────────────────────────────────────────────────────────


async def ticks(source: AgentsviewSource, session_id: str) -> int:
    return len([tick async for tick in source.watch(session_id)])


@pytest.mark.parametrize("agent", AGENTS)
def test_watch_ticks_once_on_opening_and_once_per_change(agent: str) -> None:
    stream = (FIXTURES / agent / "watch.sse").read_text()
    events = [line.removeprefix("event: ") for line in stream.splitlines() if line.startswith("event: ")]
    with source_over(Agentsview()) as source:
        got = asyncio.run(ticks(source, ids(agent)))
    assert got == events.count("session_ready") + events.count("session_updated")
    assert got >= 2  # heartbeats and timing summaries are no change


def test_a_watch_on_a_missing_session_fails() -> None:
    with source_over(Agentsview()) as source, pytest.raises(CaptureError, match="HTTP 404"):
        asyncio.run(ticks(source, "codex:missing"))


# ── failures ──────────────────────────────────────────────────────────────


def test_a_missing_session_fails_with_agentsviews_reason() -> None:
    with source_over(Agentsview()) as source, pytest.raises(CaptureError, match=r"HTTP 404: .*not found"):
        source.session("codex:missing")


def test_agentsview_down_is_a_capture_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with source_over(refuse) as source, pytest.raises(CaptureError, match="ConnectError"):
        source.active_sessions(SINCE)


def test_an_answer_the_adapter_cant_read_is_a_capture_error() -> None:
    def changed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sessions": "none", "total": 1})

    with source_over(changed) as source, pytest.raises(CaptureError, match="can't read"):
        source.active_sessions(SINCE)


def test_an_answer_that_isnt_json_is_a_capture_error() -> None:
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>")

    with source_over(html) as source, pytest.raises(CaptureError, match="not JSON"):
        source.session(ids("codex"))
