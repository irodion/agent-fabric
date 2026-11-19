"""Seam: Copilot CLI's own reader against a session folder (docs/design.md §4.1).

The fixture is the live check's Copilot session (2026-11-19, Copilot CLI 1.0.105): its `events.jsonl`, scrubbed of
the machine's paths, with Copilot's system prompt, its usage figures and its encrypted reasoning left out.
agentsview's recording of the same session is a fixture of test_capture.py, so the two readings can be compared."""

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_fabric.capture import CaptureError
from agent_fabric.copilot import CopilotSource

FIXTURES = Path(__file__).parent / "fixtures"
SESSION = "3e7dddb4-4c41-489e-877a-4b5c684bcd6c"
ID = f"copilot:{SESSION}"
LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home whose `~/.copilot` holds the recorded session and one that never got a message."""
    state = tmp_path / ".copilot" / "session-state"
    shutil.copytree(FIXTURES / "copilot" / "session-state", state)
    (state / "52fb86ce-a685-4ec2-80bb-eba9ec6da0e3").mkdir()
    (state / "52fb86ce-a685-4ec2-80bb-eba9ec6da0e3" / "workspace.yaml").write_text("id: 52fb86ce\n")
    return tmp_path


def events(home: Path) -> Path:
    return home / ".copilot" / "session-state" / SESSION / "events.jsonl"


def source(home: Path, **kwargs: Any) -> CopilotSource:
    return CopilotSource({"HOME": str(home)}, **kwargs)


def append(home: Path, *lines: dict[str, Any]) -> None:
    with events(home).open("a") as file:
        file.writelines(json.dumps(line) + "\n" for line in lines)


def reply(text: str, *, calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    data = {"content": text, "toolRequests": calls or []}
    return {"type": "assistant.message", "timestamp": "2026-11-19T07:50:00.000Z", "data": data}


# ── sessions ──────────────────────────────────────────────────────────────


def test_a_session_reads_from_its_start_event(home: Path) -> None:
    session = source(home).session(ID)
    assert session.agent == "copilot"
    assert (session.cwd, session.git_branch, session.project) == ("/home/user/code/spikeproj", "main", "spikeproj")
    assert session.started_at == datetime(2026, 11, 19, 7, 44, 45, 552000, tzinfo=UTC)
    assert session.ended_at == datetime(2026, 11, 19, 7, 45, 15, 149000, tzinfo=UTC)
    assert session.message_count == 7


def test_active_sessions_lists_the_sessions_written_to_since(home: Path) -> None:
    os.utime(events(home), (1_800_000_000, 1_800_000_000))  # 2027-01-15
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
    assert source(home).active_sessions(datetime(2027, 2, 1, tzinfo=UTC)) == []


def test_without_copilot_there_are_no_sessions(tmp_path: Path) -> None:
    assert source(tmp_path).active_sessions(LONG_AGO) == []


def test_copilot_home_moves_the_sessions(home: Path, tmp_path: Path) -> None:
    moved = tmp_path / "elsewhere"
    (home / ".copilot").rename(moved)
    found = CopilotSource({"HOME": str(home), "COPILOT_HOME": str(moved)}).active_sessions(LONG_AGO)
    assert [s.id for s in found] == [ID]


def test_copilot_home_given_with_a_tilde_moves_the_sessions(home: Path) -> None:
    (home / ".copilot").rename(home / "copilot-alt")
    found = CopilotSource({"HOME": str(home), "COPILOT_HOME": "~/copilot-alt"}).active_sessions(LONG_AGO)
    assert [s.id for s in found] == [ID]


def test_a_session_removed_mid_listing_is_skipped_quietly(
    home: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def gone(self: CopilotSource, folder: str) -> object:
        raise CaptureError("gone") from FileNotFoundError(folder)

    monkeypatch.setattr(CopilotSource, "_read", gone)
    assert source(home).active_sessions(LONG_AGO) == []
    assert "skipping" not in caplog.text


def test_a_session_that_cant_be_read_is_skipped_alone(home: Path, caplog: pytest.LogCaptureFixture) -> None:
    other = home / ".copilot" / "session-state" / "11111111-2222-3333-4444-555555555555"
    other.mkdir()
    (other / "events.jsonl").write_text(json.dumps({"type": "user.message", "data": {"content": "hi"}}) + "\n")
    (other / "events.jsonl").chmod(0)
    if os.access(other / "events.jsonl", os.R_OK):
        pytest.skip("running as a user who reads any file")
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
    assert "11111111" in caplog.text


@pytest.mark.parametrize("bad", ["3e7dddb4-4c41-489e-877a-4b5c684bcd6c", "copilot:../../etc", "codex:" + SESSION])
def test_an_id_that_isnt_a_copilot_session_is_refused(home: Path, bad: str) -> None:
    with pytest.raises(CaptureError, match="not a Copilot CLI session id"):
        source(home).session(bad)


def test_a_missing_session_is_a_capture_error(home: Path) -> None:
    with pytest.raises(CaptureError, match="no "):
        source(home).session("copilot:00000000-0000-0000-0000-000000000000")


# ── messages ──────────────────────────────────────────────────────────────


def test_messages_read_as_agentsview_read_the_same_session(home: Path) -> None:
    mine = source(home).messages(ID, 0).messages
    theirs = json.loads((FIXTURES / "agentsview" / "copilot" / "messages.json").read_text())["messages"]
    assert [m.role for m in mine] == [m["role"] for m in theirs]
    assert [
        (c.tool_use_id, c.tool_name, c.category, c.input_json, [e.status for e in c.result_events])
        for m in mine
        for c in m.tool_calls
    ] == [
        (c["tool_use_id"], c["tool_name"], c["category"], c["input_json"], [e["status"] for e in c["result_events"]])
        for m in theirs
        for c in m.get("tool_calls") or []
    ]
    assert mine[0].content == theirs[0]["content"]
    assert mine[-1].content == theirs[-1]["content"]


def test_ordinals_have_no_gaps(home: Path) -> None:
    assert [m.ordinal for m in source(home).messages(ID, 0).messages] == list(range(7))


def test_a_failed_command_keeps_its_exit_code_and_output(home: Path) -> None:
    command = source(home).messages(ID, 0).messages[1].tool_calls[0]
    assert command.tool_name == "bash"
    assert command.exit_code == 1
    assert [e.status for e in command.result_events] == ["started", "completed"]
    assert command.result_content is not None
    assert command.result_content.startswith("FAILED test_retry: expected 3, got 2")


def test_a_failed_edit_keeps_the_error_agentsview_drops(home: Path) -> None:
    edit = source(home).messages(ID, 0).messages[2].tool_calls[0]
    assert [e.status for e in edit.result_events] == ["started", "errored"]
    assert edit.result_content is not None
    assert edit.result_content.startswith("Failed to apply patch: Error: File already exists")
    assert edit.exit_code is None


def test_messages_start_at_the_ordinal_asked_for(home: Path) -> None:
    read = source(home).messages(ID, 5)
    assert [m.ordinal for m in read.messages] == [5, 6]
    assert read.revision == source(home).session(ID).transcript_revision


def test_a_read_past_the_last_message_finds_none(home: Path) -> None:
    read = source(home).messages(ID, 7)
    assert (read.revision, read.messages) == (None, ())


def test_appended_events_show_on_the_next_read_of_the_same_revision(home: Path) -> None:
    before = source(home).messages(ID, 0)
    append(home, reply("one more"))
    after = source(home).messages(ID, 0)
    assert [m.content for m in after.messages[len(before.messages) :]] == ["one more"]
    assert after.revision == before.revision


def test_a_call_still_running_has_only_its_start(home: Path) -> None:
    call = {"toolCallId": "call_x", "name": "bash", "arguments": {"command": "sleep 60"}}
    start = {
        "type": "tool.execution_start",
        "timestamp": "2026-11-19T07:50:01.000Z",
        "data": {"toolCallId": "call_x"},
    }
    append(home, reply("", calls=[call]), start)
    running = source(home).messages(ID, 7).messages[0].tool_calls[0]
    assert running.input_json == '{"command":"sleep 60"}'
    assert [e.status for e in running.result_events] == ["started"]
    assert running.result_content is None


def test_a_line_still_being_written_waits_for_its_newline(home: Path) -> None:
    with events(home).open("a") as file:
        file.write(json.dumps(reply("half"))[:40])
    assert source(home).messages(ID, 7).messages == ()


def test_a_line_that_isnt_json_is_skipped(home: Path, caplog: pytest.LogCaptureFixture) -> None:
    with events(home).open("a") as file:
        file.write("{not json\n")
    append(home, reply("after"))
    assert [m.content for m in source(home).messages(ID, 7).messages] == ["after"]
    assert "isn't JSON" in caplog.text


@pytest.mark.parametrize(
    "odd",
    [
        {"type": "assistant.message", "data": {"content": "x", "toolRequests": 1}},
        {"type": "assistant.message", "data": {"content": "x", "toolRequests": [{"name": "bash", "arguments": {1}}]}},
        {"type": "tool.execution_complete", "data": {"toolCallId": 5}},
        {"type": "session.start", "data": {"context": {"cwd": 3}}},
    ],
    ids=["calls-not-a-list", "arguments-not-json", "call-id-not-text", "cwd-not-text"],
)
def test_an_event_of_an_unknown_shape_spoils_nothing_else(home: Path, odd: dict[str, Any]) -> None:
    with events(home).open("a") as file:
        file.write(json.dumps(odd, default=list) + "\n")
    append(home, reply("after"))
    assert source(home).messages(ID, 0).messages[-1].content == "after"
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]


def test_an_event_with_a_bad_timestamp_spoils_nothing_else(home: Path) -> None:
    append(home, reply("dated") | {"timestamp": "not a time"})
    read = source(home).messages(ID, 7).messages
    assert [(m.content, m.timestamp) for m in read] == [("dated", None)]


def test_a_call_that_ends_with_an_error_and_no_success_flag_failed(home: Path) -> None:
    request = {"toolCallId": "call_e", "name": "bash", "arguments": {"command": "rm x"}}
    end = {"type": "tool.execution_complete", "data": {"toolCallId": "call_e", "error": {"message": "denied"}}}
    append(home, reply("", calls=[request]), end)
    call = source(home).messages(ID, 7).messages[0].tool_calls[0]
    assert ([e.status for e in call.result_events], call.result_content) == (["errored"], "denied")


def test_a_skills_text_isnt_a_prompt(home: Path) -> None:
    skill = {"type": "user.message", "data": {"content": "<skill-context>…</skill-context>", "source": "skill-x"}}
    append(home, skill, reply("after"))
    assert [m.content for m in source(home).messages(ID, 7).messages] == ["after"]


# ── watch ─────────────────────────────────────────────────────────────────


def test_watch_ticks_on_opening_and_when_the_file_grows(home: Path) -> None:
    async def watch() -> int:
        ticks = 0
        async for _ in source(home, poll=0.01).watch(ID):
            ticks += 1
            if ticks == 1:
                append(home, reply("more"))
            if ticks == 2:
                return ticks
        return ticks

    assert asyncio.run(asyncio.wait_for(watch(), timeout=5)) == 2


def test_watch_ends_when_the_session_is_removed(home: Path) -> None:
    async def watch() -> int:
        ticks = 0
        async for _ in source(home, poll=0.01).watch(ID):
            ticks += 1
            shutil.rmtree(events(home).parent)
        return ticks

    assert asyncio.run(asyncio.wait_for(watch(), timeout=5)) == 1


def test_watch_on_a_missing_session_fails(home: Path) -> None:
    async def watch() -> None:
        async for _ in source(home).watch("copilot:00000000-0000-0000-0000-000000000000"):
            pass

    with pytest.raises(CaptureError, match="no "):
        asyncio.run(watch())
