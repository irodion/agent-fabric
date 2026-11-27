"""Seam: Cursor CLI's own reader against a `~/.cursor/chats` folder (docs/design.md §4.1).

The fixture is a synthetic session recorded with Cursor CLI 2026.11.22 (`cursor-agent -p --trust -f`, 2026-11-27) in
a throwaway repo whose test fails: a greeting, a test run that exits 1, and a search for text that isn't there. It
went through `scripts/cursor-fixture.py`, which leaves out Cursor's system prompt and the context it sends first,
zeroes the blob key and moves the folders to `/home/ann`."""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_fabric.capture import CaptureError, Transcript
from agent_fabric.cursor import CursorSource

FIXTURES = Path(__file__).parent / "fixtures"
FOLDER = "943175a1755d318f4b8342d3d4d419c5"  # md5 of the session's folder, as Cursor names it
SESSION = "c7da6f1e-a42a-464b-bd21-1fde77114411"
ID = f"cursor:{SESSION}"
LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)
RECORDED = 1_795_772_678.165  # the session's updatedAtMs, in seconds
OTHER = "5e8f3636-1cb7-4d58-8523-368e9b08abfb"
MESSAGE_BLOBS = 12  # the recorded root's field 1s, the conversation's messages, which the reader looks up first


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home whose `~/.cursor/chats` holds the recorded session, its files dated as Cursor left them, and a chat
    that never got a message, which Cursor leaves without a store."""
    chats = tmp_path / ".cursor" / "chats"
    shutil.copytree(FIXTURES / "cursor" / "chats", chats)
    for file in (chats / FOLDER / SESSION).iterdir():
        os.utime(file, (RECORDED, RECORDED))
    empty = chats / FOLDER / "24f6df56-6a6d-4963-b6ef-aca7318a34f8"
    empty.mkdir()
    (empty / "meta.json").write_text(
        '{"schemaVersion":1,"createdAtMs":1795772600000,"hasConversation":false,"updatedAtMs":1795772600000,'
        '"cwd":"/home/ann/code/sandbox2/spikeproj"}'
    )
    return tmp_path


def chat(home: Path) -> Path:
    return home / ".cursor" / "chats" / FOLDER / SESSION


def source(home: Path, **kwargs: Any) -> CursorSource:
    return CursorSource({"HOME": str(home)}, **kwargs)


def write_chat(
    home: Path,
    messages: list[Any],
    *,
    meta: str | None = None,
    cwd: str = "/home/ann/x",
    tail: bytes = b"",
) -> Path:
    """A chat folder of its own, its store built as Cursor builds one: each message a JSON blob named by its sha256,
    and a root blob, named by `meta` key `0`, whose field 1 lists them in order, then `tail`. A message given as
    bytes is stored as they are."""
    folder = home / ".cursor" / "chats" / hashlib.md5(cwd.encode()).hexdigest() / OTHER
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(
        meta
        if meta is not None
        else json.dumps({"cwd": cwd, "createdAtMs": 1795772600000, "updatedAtMs": 1795772600000})
    )
    write_store(folder, messages, tail=tail)
    return folder


def write_store(folder: Path, messages: list[Any], *, tail: bytes = b"") -> None:
    """The chat's store, written again whole, as it stands after Cursor's latest write."""
    (folder / "store.db").unlink(missing_ok=True)
    blobs = [message if isinstance(message, bytes | None) else json.dumps(message).encode() for message in messages]
    ids = [hashlib.sha256(blob or b"").digest() for blob in blobs]
    root = b"".join(b"\x0a\x20" + id for id in ids) + tail  # field 1, 32 bytes long, once per message
    with closing(sqlite3.connect(folder / "store.db")) as db, db:
        db.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.executemany(
            "INSERT OR IGNORE INTO blobs VALUES (?, ?)",
            [(id.hex(), blob) for id, blob in zip(ids, blobs, strict=True)],
        )
        db.execute("INSERT INTO blobs VALUES (?, ?)", (hashlib.sha256(root).hexdigest(), root))
        head = {"agentId": OTHER, "latestRootBlobId": hashlib.sha256(root).hexdigest()}
        db.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps(head).encode().hex(),))


def prompt(text: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": f"<timestamp>Friday, Nov 27, 2026, 9:44 AM (UTC)</timestamp>\n"
                f"<user_query>\n{text}\n</user_query>",
            }
        ],
    }


def answer(text: str, *parts: dict[str, Any]) -> dict[str, Any]:
    return {"id": "1", "role": "assistant", "content": [{"type": "text", "text": text}, *parts]}


def messages(home: Path, session: str = ID, start: int = 0) -> list[Any]:
    return list(source(home).messages(session, start).messages)


# ── sessions ──────────────────────────────────────────────────────────────


def test_a_session_reads_from_its_meta(home: Path) -> None:
    session = source(home).session(ID)
    assert session.agent == "cursor"
    assert (session.cwd, session.project) == ("/home/ann/code/sandbox2/spikeproj", "spikeproj")
    assert session.started_at == datetime(2026, 11, 27, 9, 44, 10, 374000, tzinfo=UTC)
    assert session.ended_at == datetime(2026, 11, 27, 9, 44, 38, 165000, tzinfo=UTC)
    assert session.message_count == 8


def test_a_session_written_after_its_meta_ends_when_its_store_was_written(home: Path) -> None:
    os.utime(chat(home) / "store.db", (1_800_000_000, 1_800_000_000))  # Cursor updates meta.json only now and then
    assert source(home).session(ID).ended_at == datetime(2027, 1, 15, 8, 0, tzinfo=UTC)


def test_a_session_ends_when_its_files_were_last_written_whatever_its_meta_says(home: Path) -> None:
    meta = chat(home) / "meta.json"
    meta.write_text(meta.read_text().replace("1795772678165", "1900000000000"))
    os.utime(meta, (RECORDED, RECORDED))
    assert source(home).session(ID).ended_at == datetime(2026, 11, 27, 9, 44, 38, 165000, tzinfo=UTC)


def test_active_sessions_lists_the_sessions_written_to_since(home: Path) -> None:
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
    assert source(home).active_sessions(datetime(2026, 11, 28, tzinfo=UTC)) == []
    os.utime(chat(home) / "store.db", (1_800_000_000, 1_800_000_000))
    assert [s.id for s in source(home).active_sessions(datetime(2026, 11, 28, tzinfo=UTC))] == [ID]


def test_without_cursor_there_are_no_sessions(tmp_path: Path) -> None:
    assert source(tmp_path).active_sessions(LONG_AGO) == []


@pytest.mark.parametrize("bad", ["c7da6f1e-a42a-464b-bd21-1fde77114411", "cursor:../x", "copilot:" + SESSION])
def test_an_id_that_isnt_a_cursor_session_is_refused(home: Path, bad: str) -> None:
    with pytest.raises(CaptureError, match="not a Cursor CLI session"):
        source(home).session(bad)


def test_a_missing_session_is_a_capture_error(home: Path) -> None:
    with pytest.raises(CaptureError) as caught:
        source(home).session("cursor:0b0e2a37-5a70-4fd2-9f8a-2c0bb0d1c6a1")
    assert caught.value.gone


def test_a_session_that_cant_be_read_is_skipped_alone(home: Path, caplog: pytest.LogCaptureFixture) -> None:
    other = home / ".cursor" / "chats" / ("0" * 32) / OTHER
    other.mkdir(parents=True)
    (other / "meta.json").write_text("{")
    (other / "store.db").write_bytes(b"not a database")
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
    assert "5e8f3636" in caplog.text


@pytest.mark.parametrize("stamp", ["NaN", "Infinity", "1e20", "-1e20", '"yesterday"'])
def test_a_meta_time_that_isnt_one_is_left_out(home: Path, stamp: str) -> None:
    write_chat(
        home,
        [prompt("hi"), answer("hello")],
        meta=f'{{"cwd":"/home/ann/x","createdAtMs":{stamp},"updatedAtMs":{stamp}}}',
    )
    listed = {s.id: s for s in source(home).active_sessions(LONG_AGO)}
    assert listed[f"cursor:{OTHER}"].started_at is None
    assert listed[f"cursor:{OTHER}"].ended_at is not None  # when its store was written


def test_a_message_blob_that_isnt_json_text_spoils_its_session_alone(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_chat(home, [prompt("hi"), None])
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
    assert OTHER in caplog.text


def test_a_store_without_its_meta_is_unreadable_not_gone(home: Path, caplog: pytest.LogCaptureFixture) -> None:
    (write_chat(home, [prompt("hi")]) / "meta.json").unlink()
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
    assert OTHER in caplog.text
    with pytest.raises(CaptureError) as caught:
        source(home).session(f"cursor:{OTHER}")
    assert not caught.value.gone


@pytest.mark.parametrize(
    "content",
    [
        "<user_info>\nOS Version: linux\n</user_info>",
        [{"type": "text", "text": "<user_info>\nOS Version: linux\n</user_info>"}],
        "\n<rules>be brief</rules>\n<user_info>\nOS Version: linux\n</user_info>",
    ],
)
def test_cursors_context_isnt_a_prompt_in_any_form(home: Path, content: Any) -> None:
    context = {"role": "user", "content": content}
    write_chat(home, [{"role": "system", "content": "You are"}, context, prompt("hi"), answer("hello")])
    assert source(home).session(f"cursor:{OTHER}").message_count == 2


def test_a_prompt_that_mentions_user_info_is_still_a_prompt(home: Path) -> None:
    write_chat(home, [prompt("what goes in <user_info>?"), answer("the machine")])
    assert source(home).session(f"cursor:{OTHER}").message_count == 2


def test_a_varint_longer_than_protobuf_allows_spoils_its_session_alone(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_chat(home, [prompt("hi")], tail=b"\x08" + b"\x80" * 10 + b"\x01")
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
    assert OTHER in caplog.text


@pytest.mark.parametrize(
    "head",
    [
        "not hex",
        b"\x00".hex(),
        b"[]".hex(),
        b'{"agentId":"x"}'.hex(),
        b'{"latestRootBlobId":"ab"}'.hex(),  # a root that isn't there
    ],
)
def test_a_store_of_a_shape_this_reader_doesnt_know_is_skipped_alone(
    home: Path, head: str, caplog: pytest.LogCaptureFixture
) -> None:
    other = home / ".cursor" / "chats" / ("0" * 32) / OTHER
    other.mkdir(parents=True)
    (other / "meta.json").write_text('{"cwd":"/home/ann/code/other","createdAtMs":1795772600000}')
    with closing(sqlite3.connect(other / "store.db")) as db, db:
        db.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO meta VALUES ('0', ?)", (head,))
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
    assert "5e8f3636" in caplog.text


@pytest.mark.parametrize("tables", [(), ("blobs",), ("blobs", "meta")])  # as Cursor makes it, step by step
def test_a_store_cursor_hasnt_filled_yet_is_skipped_quietly(
    home: Path, tables: tuple[str, ...], caplog: pytest.LogCaptureFixture
) -> None:
    other = home / ".cursor" / "chats" / ("0" * 32) / OTHER
    other.mkdir(parents=True)
    (other / "meta.json").write_text('{"cwd":"/home/ann/code/other","createdAtMs":1795772600000}')
    (other / "store.db").touch()
    with closing(sqlite3.connect(other / "store.db")) as db, db:
        for table in tables:
            columns = "id TEXT PRIMARY KEY, data BLOB" if table == "blobs" else "key TEXT PRIMARY KEY, value TEXT"
            db.execute(f"CREATE TABLE {table} ({columns})")
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
    assert caplog.text == ""
    with pytest.raises(CaptureError) as caught:
        source(home).session(f"cursor:{OTHER}")
    assert not caught.value.gone


@pytest.mark.parametrize("blob", [b"[]", b'"text"', b"7"])
def test_a_message_blob_that_isnt_an_object_spoils_its_session_alone(
    home: Path, blob: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    write_chat(home, [prompt("hi"), blob])
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
    assert OTHER in caplog.text


def test_a_message_nested_too_deep_to_read_spoils_its_session_alone(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_chat(home, [prompt("hi"), b"[" * 100_000 + b"]" * 100_000])
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
    assert OTHER in caplog.text


@pytest.mark.parametrize("stale", ["0" * 32, "f" * 32])  # sorting before the live folder, and after
def test_a_session_in_two_folders_reads_from_the_one_written_last(home: Path, stale: str) -> None:
    copy = home / ".cursor" / "chats" / stale / SESSION
    shutil.copytree(chat(home), copy)
    (copy / "meta.json").write_text('{"cwd":"/home/ann/elsewhere","createdAtMs":1795772600000}')
    for file in copy.iterdir():
        os.utime(file, (RECORDED - 60, RECORDED - 60))
    assert [s.cwd for s in source(home).active_sessions(LONG_AGO)] == ["/home/ann/code/sandbox2/spikeproj"]
    assert source(home).session(ID).cwd == "/home/ann/code/sandbox2/spikeproj"


def test_a_wal_checkpointed_away_mid_read_leaves_the_session_there(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (chat(home) / "store.db-wal").write_bytes(b"")
    stat = Path.stat

    def checkpointed(path: Path, **kwargs: Any) -> os.stat_result:
        if path.name == "store.db-wal":  # SQLite removes it when Cursor's last connection closes
            raise FileNotFoundError(path)
        return stat(path, **kwargs)

    monkeypatch.setattr(Path, "stat", checkpointed)
    assert source(home).session(ID).message_count == 8
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]


def test_a_session_removed_mid_read_is_gone(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    read_bytes = Path.read_bytes

    def removed(path: Path) -> bytes:
        if path.name == "meta.json":  # read after the store
            shutil.rmtree(path.parent)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", removed)
    with pytest.raises(CaptureError) as caught:
        source(home).session(ID)
    assert caught.value.gone


@pytest.mark.parametrize("tail", [b"\x09\x00\x00", b"\x0d\x00"])  # a fixed64 and a fixed32 cut short
def test_a_root_cut_short_spoils_its_session_alone(home: Path, tail: bytes, caplog: pytest.LogCaptureFixture) -> None:
    write_chat(home, [prompt("hi"), answer("hello")], tail=tail)
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
    assert OTHER in caplog.text


@pytest.mark.parametrize("unreadable", [Path(), Path(FOLDER)])  # the chats folder, and a folder of chats in it
def test_chats_that_cant_be_looked_at_are_a_capture_error_not_none(home: Path, unreadable: Path) -> None:
    folder = home / ".cursor" / "chats" / unreadable
    folder.chmod(0)
    try:
        with pytest.raises(CaptureError) as listing:
            source(home).active_sessions(LONG_AGO)
        with pytest.raises(CaptureError) as reading:
            source(home).session(ID)
    finally:
        folder.chmod(0o755)
    assert not listing.value.gone
    assert not reading.value.gone


def test_a_file_where_a_folder_should_be_spoils_nothing(home: Path) -> None:
    chats = home / ".cursor" / "chats"
    (chats / FOLDER / OTHER).write_text("")
    (chats / ("e" * 32)).write_text("")
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
    assert source(home).session(ID).id == ID


def test_a_session_folder_that_cant_be_looked_at_is_skipped_alone(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = write_chat(home, [prompt("hi"), answer("hello")])
    folder.chmod(0)
    try:
        assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
        assert source(home).session(ID).id == ID
        with pytest.raises(CaptureError) as caught:
            source(home).session(f"cursor:{OTHER}")
    finally:
        folder.chmod(0o755)
    assert OTHER in caplog.text
    assert not caught.value.gone


@pytest.mark.parametrize("cwd", ["", "code/x", 7])
def test_a_folder_that_isnt_an_absolute_path_is_left_out(home: Path, cwd: object) -> None:
    write_chat(home, [prompt("hi"), answer("hello")], meta=json.dumps({"cwd": cwd, "createdAtMs": 1795772600000}))
    session = source(home).session(f"cursor:{OTHER}")
    assert (session.cwd, session.project) == (None, "")


# ── messages ──────────────────────────────────────────────────────────────


def test_messages_read_in_the_conversations_order(home: Path) -> None:
    read = messages(home)
    assert [(m.ordinal, m.role, m.content) for m in read] == [
        (0, "user", "Reply with the single word: hello. Do not run any commands."),
        (1, "assistant", "hello"),
        (
            2,
            "user",
            "Run `python3 --version`, then run `python3 test_calc.py` and tell me whether the test passed. "
            "Don't fix anything.",
        ),
        (3, "assistant", "I'll run both commands and report the test result without changing anything."),
        (
            4,
            "assistant",
            "Python 3.14.4. The test did not pass: `python3 test_calc.py` exited with code 1 and reported "
            "`FAIL: add(2, 3) == 6`.",
        ),
        (
            5,
            "user",
            "Use your edit tool to replace the exact text `return a - b` with `return a + b` in calc.py. "
            "If that text isn't there, stop and say so; don't make any other change.",
        ),
        (
            6,
            "assistant",
            "I'll check `calc.py` for the exact subtraction return and replace only that line if it's present.",
        ),
        (7, "assistant", "The exact text `return a - b` is not in `calc.py`. No change was made."),
    ]
    assert {m.session_id for m in read} == {ID}
    assert {m.model for m in read if m.role == "assistant"} == {"grok-4.7-high"}
    assert {m.model for m in read if m.role == "user"} == {None}
    assert not any(m.is_system for m in read)


def test_a_call_reads_as_the_agent_made_it(home: Path) -> None:
    shell, grep = (m.tool_calls[0] for m in messages(home) if m.tool_calls)
    assert (
        shell.tool_use_id == "call-8297070e-0d19-4f25-b3cb-0f4167cdfd7b-0\nfc_40dcc20d-b23a-4990-8b84-b1974f4caa48_0"
    )
    assert (shell.tool_name, shell.category) == ("Shell", "Bash")
    assert json.loads(shell.input_json or "") == {
        "command": "python3 --version && python3 test_calc.py",
        "description": "Check Python version and run tests",
    }
    assert (grep.tool_name, grep.category) == ("Grep", "Grep")
    assert [m.ordinal for m in messages(home) if m.tool_calls] == [3, 6]


@pytest.mark.parametrize(
    ("tool", "category"),
    [
        ("StrReplace", "Edit"),
        ("ApplyPatch", "Edit"),
        ("Read", "Read"),
        ("LS", "Read"),
        ("Glob", "Glob"),
        ("Write", "Write"),
        ("Subagent", "Task"),
        ("spawn_subagent", "Task"),
        ("Edit", "Edit"),
        ("Shell", "Bash"),
        ("Grep", "Grep"),
        ("TodoWrite", "Tool"),
        ("WebFetch", "Tool"),
        ("WebSearch", "Tool"),
        ("Delete", "Other"),
        ("AskQuestion", "Other"),
    ],
)
def test_a_tool_falls_in_agentsviews_category_for_it(home: Path, tool: str, category: str) -> None:
    call = {"type": "tool-call", "toolCallId": "c1", "toolName": tool, "args": {}}
    write_chat(home, [prompt("go"), answer("on it", call)])
    assert messages(home, f"cursor:{OTHER}")[1].tool_calls[0].category == category


def test_a_reply_keeps_its_reasoning_and_its_texts_together(home: Path) -> None:
    reasoning = {
        "type": "reasoning",
        "text": "The user wants a word.",
        "providerOptions": {"cursor": {"modelName": "m1"}},
    }
    reply = {
        "id": "1",
        "role": "assistant",
        "content": [
            reasoning,
            {"type": "text", "text": "```py\n    x = 1"},
            {"type": "text", "text": "    y = 2\n```\n"},
        ],
    }
    write_chat(home, [prompt("hi"), reply])
    said = messages(home, f"cursor:{OTHER}")[1]
    assert (said.content, said.thinking_text, said.model) == (
        "```py\n    x = 1\n    y = 2\n```",  # joined as agentsview joins them, trimmed only at the ends
        "The user wants a word.",
        "m1",
    )


def test_a_user_message_cursor_didnt_wrap_as_a_prompt_isnt_one(home: Path) -> None:
    injected = {"role": "user", "content": "<system_reminder>be brief</system_reminder>"}
    write_chat(home, [prompt("hi"), answer("hello"), injected, prompt("again"), answer("ok")])
    assert [m.content for m in messages(home, f"cursor:{OTHER}")] == ["hi", "hello", "again", "ok"]


def test_a_prompt_keeps_what_comes_before_cursors_closing_tag(home: Path) -> None:
    write_chat(home, [prompt("why does <user_query> end at </user_query>?"), answer("it doesn't")])
    assert messages(home, f"cursor:{OTHER}")[0].content == "why does <user_query> end at </user_query>?"


def test_a_prompt_with_skills_attached_reads_as_what_the_user_typed(home: Path) -> None:
    text = (
        "<manually_attached_skills>\nkb-note\n</manually_attached_skills>\n<user_query>\nleave a note\n</user_query>"
    )
    write_chat(home, [{"role": "user", "content": [{"type": "text", "text": text}]}, answer("done")])
    assert messages(home, f"cursor:{OTHER}")[0].content == "leave a note"


def test_arguments_already_written_as_json_are_kept_as_they_are(home: Path) -> None:
    call = {"type": "tool-call", "toolCallId": "c1", "toolName": "Shell", "args": '{"command":"ls"}'}
    write_chat(home, [prompt("go"), answer("on it", call)])
    assert messages(home, f"cursor:{OTHER}")[1].tool_calls[0].input_json == '{"command":"ls"}'


def test_a_reply_without_reasoning_keeps_the_model_of_the_reply_before(home: Path) -> None:
    reasoning = {"type": "reasoning", "text": "", "providerOptions": {"cursor": {"modelName": "m1"}}}
    first = {"id": "1", "role": "assistant", "content": [reasoning, {"type": "text", "text": "one"}]}
    write_chat(home, [prompt("hi"), first, prompt("again"), answer("two")])
    assert [m.model for m in messages(home, f"cursor:{OTHER}")] == [None, "m1", None, "m1"]


def test_a_part_of_a_shape_this_reader_doesnt_know_is_skipped(home: Path) -> None:
    odd = {
        "id": "1",
        "role": "assistant",
        "content": [{"type": "image"}, "text", {"type": "tool-call"}, {"type": "text", "text": "ok"}],
    }
    write_chat(home, [prompt("hi"), odd])
    said = messages(home, f"cursor:{OTHER}")[1]
    assert (said.content, said.tool_calls) == ("ok", ())


def test_messages_start_at_the_ordinal_asked_for(home: Path) -> None:
    assert [m.ordinal for m in messages(home, start=6)] == [6, 7]


def test_a_read_past_the_last_message_finds_none(home: Path) -> None:
    assert source(home).messages(ID, 8) == Transcript(None, ())


def test_the_revision_moves_with_each_write_cursor_makes(home: Path) -> None:
    folder = write_chat(home, [prompt("hi")])
    before = source(home).messages(f"cursor:{OTHER}", 0).revision
    write_store(folder, [prompt("hi"), answer("hello")])
    after = source(home).messages(f"cursor:{OTHER}", 0)
    assert before is not None
    assert after.revision not in (None, before)
    assert after.revision == source(home).session(f"cursor:{OTHER}").transcript_revision


def test_a_reply_or_prompt_with_nothing_to_read_takes_no_ordinal(home: Path) -> None:
    thought = {"type": "reasoning", "text": "", "providerOptions": {"cursor": {"modelName": "m1"}}}
    empty_reply = {"id": "1", "role": "assistant", "content": [thought]}
    empty_prompt = {"role": "user", "content": "<user_query>\n\n</user_query>"}
    write_chat(home, [prompt("hi"), empty_reply, answer("done"), empty_prompt])
    read = messages(home, f"cursor:{OTHER}")
    assert [(m.ordinal, m.content, m.model) for m in read] == [(0, "hi", None), (1, "done", "m1")]


def test_a_reply_written_as_a_string_reads_as_its_text(home: Path) -> None:
    write_chat(home, [prompt("hi"), {"role": "assistant", "content": " hello "}])
    assert messages(home, f"cursor:{OTHER}")[1].content == "hello"


def test_a_prompt_cut_before_its_closing_tag_reads_from_its_opening_one(home: Path) -> None:
    cut = {"role": "user", "content": "<timestamp>Friday</timestamp>\n<user_query>\nfix it"}
    write_chat(home, [cut, answer("ok")])
    assert messages(home, f"cursor:{OTHER}")[0].content == "fix it"


def test_arguments_json_cant_write_leave_the_input_out(home: Path) -> None:
    part = b'{"type":"tool-call","toolCallId":"c1","toolName":"Shell","args":{"n":NaN}}'
    call = b'{"id":"1","role":"assistant","content":[' + part + b"]}"
    write_chat(home, [prompt("go"), call])
    shell = messages(home, f"cursor:{OTHER}")[1].tool_calls[0]
    assert (shell.tool_name, shell.input_json) == ("Shell", None)


def test_a_prompt_reads_only_the_part_cursor_wrapped(home: Path) -> None:
    query = {"type": "text", "text": "<user_query>\nfix it\n</user_query>"}
    attached = {"type": "text", "text": "<attached_file>notes on </user_query> tags</attached_file>"}
    hidden = {"type": "file", "text": "<user_query>not typed</user_query>"}
    write_chat(
        home, [{"role": "user", "content": [query, attached]}, {"role": "user", "content": [hidden]}, answer("ok")]
    )
    assert [m.content for m in messages(home, f"cursor:{OTHER}")] == ["fix it", "ok"]


def test_a_calls_input_under_the_ai_sdks_newer_name_reads_too(home: Path) -> None:
    call = {"type": "tool-call", "toolCallId": "c1", "toolName": "Shell", "input": {"command": "ls"}}
    write_chat(home, [prompt("go"), answer("on it", call)])
    assert messages(home, f"cursor:{OTHER}")[1].tool_calls[0].input_json == '{"command":"ls"}'


# ── tool results ──────────────────────────────────────────────────────────


def result(call: str, tool: str, text: str, output: dict[str, Any], *, error: bool = False) -> dict[str, Any]:
    """A tool message as Cursor writes one, named for its call: the result's text, and Cursor's own record of how the
    call ended."""
    part = {"type": "tool-result", "toolCallId": call, "toolName": tool, "result": text}
    outcome = {"output": output, "isError": error}
    return {
        "id": call,
        "role": "tool",
        "content": [part],
        "providerOptions": {"cursor": {"highLevelToolCallResult": outcome}},
    }


def shell(call: str, command: str) -> dict[str, Any]:
    return {"type": "tool-call", "toolCallId": call, "toolName": "Shell", "args": {"command": command}}


def test_a_failed_command_keeps_its_exit_code_and_output(home: Path) -> None:
    (command,) = messages(home)[3].tool_calls
    assert command.exit_code == 1
    assert (command.result_content or "").startswith("Exit code: 1\n\nCommand output:")
    assert "FAIL: add(2, 3) == 6" in (command.result_content or "")
    assert [(event.status, event.content) for event in command.result_events] == [
        ("errored", command.result_content)  # Cursor recorded it as a failure
    ]


def test_a_search_keeps_what_it_found(home: Path) -> None:
    (grep,) = messages(home)[6].tool_calls
    assert (grep.exit_code, [event.status for event in grep.result_events]) == (None, ["completed"])
    assert (grep.result_content or "").startswith(
        '<workspace_result workspace_path="/home/ann/code/sandbox2/spikeproj">'
    )


def test_a_command_that_succeeded_exits_0(home: Path) -> None:
    write_chat(
        home,
        [
            prompt("go"),
            answer("on it", shell("c1", "true")),
            result("c1", "Shell", "Exit code: 0", {"success": {"command": "true", "stdout": ""}}),
        ],
    )
    (command,) = messages(home, f"cursor:{OTHER}")[1].tool_calls
    assert (command.exit_code, command.result_content) == (0, "Exit code: 0")


def test_a_rejected_command_errored_with_no_exit_code(home: Path) -> None:
    rejected = result("c1", "Shell", "Rejected: ", {"rejected": {"command": "rm -rf x"}}, error=True)
    write_chat(home, [prompt("go"), answer("on it", shell("c1", "rm -rf x")), rejected])
    (command,) = messages(home, f"cursor:{OTHER}")[1].tool_calls
    assert (command.exit_code, [e.status for e in command.result_events]) == (None, ["errored"])
    assert command.result_content == "Rejected: "


def test_a_failed_edit_keeps_its_error(home: Path) -> None:
    edit = {"type": "tool-call", "toolCallId": "e1", "toolName": "StrReplace", "args": {"path": "/x/calc.py"}}
    failed = result(
        "e1",
        "StrReplace",
        "Error: String not found in file",
        {"error": {"error": "String not found in file"}},
        error=True,
    )
    write_chat(home, [prompt("fix"), answer("on it", edit), failed])
    (call,) = messages(home, f"cursor:{OTHER}")[1].tool_calls
    assert (call.result_content, [e.status for e in call.result_events]) == (
        "Error: String not found in file",
        ["errored"],
    )


def test_a_call_still_running_has_no_result_yet(home: Path) -> None:
    write_chat(home, [prompt("go"), answer("on it", shell("c1", "sleep 9"))])
    (command,) = messages(home, f"cursor:{OTHER}")[1].tool_calls
    assert (command.result_content, command.result_events, command.exit_code) == (None, (), None)


def test_results_fold_into_their_own_calls_whatever_the_order(home: Path) -> None:
    calls = answer("both", shell("c1", "a"), shell("c2", "b"))
    second = result("c2", "Shell", "Exit code: 2", {"failure": {"command": "b", "exitCode": 2}})
    first = result("c1", "Shell", "Exit code: 0", {"success": {"command": "a"}})
    stray = result("c9", "Shell", "Exit code: 5", {"failure": {"command": "c", "exitCode": 5}})
    write_chat(home, [prompt("go"), calls, second, stray, first, answer("done")])
    read = messages(home, f"cursor:{OTHER}")
    assert [(c.tool_use_id, c.exit_code) for c in read[1].tool_calls] == [("c1", 0), ("c2", 2)]
    assert [m.content for m in read] == ["go", "both", "done"]


def test_a_result_written_as_parts_reads_as_their_text(home: Path) -> None:
    parts = {"type": "tool-result", "toolCallId": "c1", "result": [{"type": "text", "text": "out"}]}
    odd = {"id": "t", "role": "tool", "content": [parts, "x"]}
    write_chat(home, [prompt("go"), answer("on it", shell("c1", "a")), odd])
    (command,) = messages(home, f"cursor:{OTHER}")[1].tool_calls
    assert (command.result_content, command.exit_code) == ("out", None)


def test_a_result_with_no_record_of_its_end_has_no_status(home: Path) -> None:
    bare = {"id": "t", "role": "tool", "content": [{"type": "tool-result", "toolCallId": "c1", "result": "out"}]}
    write_chat(home, [prompt("go"), answer("on it", shell("c1", "a")), bare])
    (command,) = messages(home, f"cursor:{OTHER}")[1].tool_calls
    assert [(e.status, e.content) for e in command.result_events] == [(None, "out")]


def test_a_record_is_the_result_whose_call_the_tool_message_is_named_for(home: Path) -> None:
    both = result("c1", "Shell", "Rejected: ", {"rejected": {"command": "a"}}, error=True)
    both["content"].append({"type": "tool-result", "toolCallId": "c2", "toolName": "Shell", "result": "Exit code: 0"})
    write_chat(home, [prompt("go"), answer("on it", shell("c1", "a"), shell("c2", "b")), both])
    calls = messages(home, f"cursor:{OTHER}")[1].tool_calls
    assert [[e.status for e in c.result_events] for c in calls] == [["errored"], [None]]


@pytest.mark.parametrize(
    "output",
    [
        {"rejected": {"command": "a"}},
        {"error": {"error": "boom"}},
        {"failure": {"command": "sleep 9"}},
        {"failure": {"command": "a", "exitCode": True}},
    ],
)
def test_a_call_cursor_recorded_as_failing_errored_whatever_its_flag_says(home: Path, output: dict[str, Any]) -> None:
    ended = result("c1", "Shell", "?", output)
    del ended["providerOptions"]["cursor"]["highLevelToolCallResult"]["isError"]
    write_chat(home, [prompt("go"), answer("on it", shell("c1", "a")), ended])
    (command,) = messages(home, f"cursor:{OTHER}")[1].tool_calls
    assert ([e.status for e in command.result_events], command.exit_code) == (["errored"], None)


def test_a_result_json_cant_write_strictly_still_shows(home: Path) -> None:
    odd = b'{"id":"c1","role":"tool","content":[{"type":"tool-result","toolCallId":"c1","result":{"n":NaN}}]}'
    write_chat(home, [prompt("go"), answer("on it", shell("c1", "a")), odd])
    assert messages(home, f"cursor:{OTHER}")[1].tool_calls[0].result_content == '{"n":NaN}'


def test_a_command_left_running_in_the_background_has_no_exit_code_yet(home: Path) -> None:
    started = result("c1", "Shell", "Started", {"success": {"command": "npm run dev"}, "isBackground": True})
    write_chat(home, [prompt("go"), answer("on it", shell("c1", "npm run dev")), started])
    (command,) = messages(home, f"cursor:{OTHER}")[1].tool_calls
    assert (command.exit_code, command.result_content) == (None, "Started")
    assert [e.status for e in command.result_events] == ["running"]


@pytest.mark.parametrize("code", [0, 1])
def test_a_failure_cursor_recorded_errored_whatever_its_code(home: Path, code: int) -> None:
    failed = result("c1", "Shell", f"Exit code: {code}", {"failure": {"command": "a", "exitCode": code}})
    write_chat(home, [prompt("go"), answer("on it", shell("c1", "a")), failed])
    (command,) = messages(home, f"cursor:{OTHER}")[1].tool_calls
    assert ([e.status for e in command.result_events], command.exit_code) == (["errored"], code)


def test_a_record_with_error_messages_errored(home: Path) -> None:
    crashed = result("c1", "Read", "?", {"success": {}})
    crashed["providerOptions"]["cursor"]["highLevelToolCallResult"]["rawErrorMessages"] = ["tool crashed"]
    read = {"type": "tool-call", "toolCallId": "c1", "toolName": "Read", "args": {}}
    write_chat(home, [prompt("go"), answer("on it", read), crashed])
    assert [e.status for e in messages(home, f"cursor:{OTHER}")[1].tool_calls[0].result_events] == ["errored"]


def test_a_result_of_no_text_leaves_its_data_out(home: Path) -> None:
    image = {"type": "tool-result", "toolCallId": "c1", "result": [{"type": "image", "data": "iVBORw0KGgo="}]}
    write_chat(
        home, [prompt("go"), answer("on it", shell("c1", "a")), {"id": "t", "role": "tool", "content": [image]}]
    )
    assert messages(home, f"cursor:{OTHER}")[1].tool_calls[0].result_content is None


@pytest.mark.parametrize(
    ("answered", "content"),
    [
        ([{"file": "calc.py", "line": 3}], '[{"file":"calc.py","line":3}]'),
        ([], "[]"),
        ([{"type": "text", "text": "out"}, "x"], '[{"type":"text","text":"out"},"x"]'),
        ([{"type": [], "text": "x"}], '[{"type":[],"text":"x"}]'),
    ],
)
def test_a_list_that_isnt_content_parts_reads_as_its_json(home: Path, answered: list[Any], content: str) -> None:
    listed = {"id": "t", "role": "tool", "content": [{"type": "tool-result", "toolCallId": "c1", "result": answered}]}
    write_chat(home, [prompt("go"), answer("on it", shell("c1", "a")), listed])
    assert messages(home, f"cursor:{OTHER}")[1].tool_calls[0].result_content == content


def test_an_outcome_other_than_success_errored(home: Path) -> None:
    timed_out = result("c1", "Shell", "Timed out", {"timeout": {"command": "pytest"}})
    write_chat(home, [prompt("go"), answer("on it", shell("c1", "pytest")), timed_out])
    (command,) = messages(home, f"cursor:{OTHER}")[1].tool_calls
    assert ([e.status for e in command.result_events], command.exit_code) == (["errored"], None)


@pytest.mark.parametrize(
    "ended", [{"isError": False}, {"isError": False, "output": None}, {"output": {"success": None}}]
)
def test_a_record_with_no_outcome_says_nothing_of_how_the_call_ended(home: Path, ended: dict[str, Any]) -> None:
    told = result("c1", "Shell", "?", {})
    told["providerOptions"]["cursor"]["highLevelToolCallResult"] = ended
    write_chat(home, [prompt("go"), answer("on it", shell("c1", "a")), told])
    (command,) = messages(home, f"cursor:{OTHER}")[1].tool_calls
    assert ([e.status for e in command.result_events], command.exit_code) == ([None], None)


def test_records_with_a_type_that_arent_content_parts_read_as_their_json(home: Path) -> None:
    hits = [{"type": "file", "path": "calc.py"}, {"type": "dir", "path": "src"}]
    listed = {"id": "t", "role": "tool", "content": [{"type": "tool-result", "toolCallId": "c1", "result": hits}]}
    write_chat(home, [prompt("go"), answer("on it", shell("c1", "a")), listed])
    assert messages(home, f"cursor:{OTHER}")[1].tool_calls[0].result_content == json.dumps(
        hits, separators=(",", ":")
    )


def test_a_success_with_its_own_exit_code_keeps_it(home: Path) -> None:
    found = result("c1", "Shell", "Exit code: 1", {"success": {"command": "grep x f", "exitCode": 1}})
    write_chat(home, [prompt("go"), answer("on it", shell("c1", "grep x f")), found])
    assert messages(home, f"cursor:{OTHER}")[1].tool_calls[0].exit_code == 1


# ── reading under a read-only home ────────────────────────────────────────


def wal_store(home: Path, tmp_path: Path, *, shm: bool) -> Path:
    """A chat whose store is as Cursor leaves it while it runs: in WAL mode, its last write still in the WAL, and
    with `shm` the -shm beside it, as Cursor's open connection keeps it."""
    folder = write_chat(home, [prompt("hi")])
    live = tmp_path / "live"
    live.mkdir()
    shutil.copy(folder / "store.db", live / "store.db")
    with closing(sqlite3.connect(live / "store.db")) as db:
        db.execute("PRAGMA journal_mode=wal")
        db.execute("PRAGMA wal_autocheckpoint=0")  # the write below stays in the WAL
        reply = json.dumps(answer("hello")).encode()
        root = b"\x0a\x20" + hashlib.sha256(json.dumps(prompt("hi")).encode()).digest()
        root += b"\x0a\x20" + hashlib.sha256(reply).digest()
        with db:
            db.execute("INSERT INTO blobs VALUES (?, ?)", (hashlib.sha256(reply).hexdigest(), reply))
            db.execute("INSERT INTO blobs VALUES (?, ?)", (hashlib.sha256(root).hexdigest(), root))
            head = {"agentId": OTHER, "latestRootBlobId": hashlib.sha256(root).hexdigest()}
            db.execute("UPDATE meta SET value = ? WHERE key = '0'", (json.dumps(head).encode().hex(),))
        for name in ("store.db", "store.db-wal", *(["store.db-shm"] if shm else [])):  # while the connection is open
            shutil.copy(live / name, folder / name)
    return folder


def closed(home: Path) -> Path:
    """The recorded chat with its store as Cursor leaves it on closing: in WAL mode, all in the store, with no -wal
    or -shm beside it. (The fixture's store is a file of its own, out of WAL mode.)"""
    with closing(sqlite3.connect(chat(home) / "store.db")) as db:
        db.execute("PRAGMA journal_mode=wal")
    assert sorted(path.name for path in chat(home).iterdir()) == ["meta.json", "store.db"]
    return chat(home)


@pytest.mark.parametrize("mode", [0o555, 0o755])  # a read-only home, and fabricd's unit, which leaves it writable
def test_a_store_cursor_closed_reads_and_nothing_is_made_beside_it(home: Path, mode: int) -> None:
    """Cursor's store is in WAL mode, and with no -shm beside it SQLite can't open it read only, nor make one in a
    folder it can't write (§4.1); in one it can, it would, and capture writes nothing there (§8)."""
    folder = closed(home)
    folder.chmod(mode)
    try:
        assert source(home).session(ID).message_count == 8
        assert sorted(path.name for path in folder.iterdir()) == ["meta.json", "store.db"]
    finally:
        folder.chmod(0o755)


def test_a_store_cursor_is_writing_reads_with_its_wal_from_a_folder_it_cant_write(home: Path, tmp_path: Path) -> None:
    folder = wal_store(home, tmp_path, shm=True)
    folder.chmod(0o555)
    try:
        assert [m.content for m in messages(home, f"cursor:{OTHER}")] == ["hi", "hello"]
    finally:
        folder.chmod(0o755)


def test_a_wal_a_crash_left_without_its_shm_is_unreadable_not_gone(
    home: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = wal_store(home, tmp_path, shm=False)
    folder.chmod(0o555)
    try:
        assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
        with pytest.raises(CaptureError) as caught:
            source(home).session(f"cursor:{OTHER}")
    finally:
        folder.chmod(0o755)
    assert OTHER in caplog.text
    assert not caught.value.gone


def test_a_read_during_which_cursor_wrote_is_made_again(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connect = sqlite3.connect
    opened: list[str] = []

    def writing(database: str, *, uri: bool, timeout: float) -> sqlite3.Connection:
        opened.append(database)
        if len(opened) == 1:  # Cursor writes as the first read is made
            os.utime(chat(home) / "store.db", (RECORDED + 1, RECORDED + 1))
        return connect(database, uri=uri, timeout=timeout)

    monkeypatch.setattr(sqlite3, "connect", writing)
    assert source(home).session(ID).message_count == 8
    assert len(opened) == 2


def test_a_wal_a_crash_left_without_its_shm_gets_none_made_in_a_folder_it_can_write(
    home: Path, tmp_path: Path
) -> None:
    """fabricd's unit leaves the home writable: SQLite would make the -shm in Cursor's folder, which capture never
    writes to (§8)."""
    folder = wal_store(home, tmp_path, shm=False)
    with pytest.raises(CaptureError) as caught:
        source(home).session(f"cursor:{OTHER}")
    assert not caught.value.gone
    assert sorted(path.name for path in folder.iterdir()) == ["meta.json", "store.db", "store.db-wal"]


def test_an_empty_wal_cursor_left_reads_as_none(home: Path) -> None:
    (closed(home) / "store.db-wal").write_bytes(b"")  # checkpointed, but kept open by a reader as Cursor closed
    chat(home).chmod(0o555)
    try:
        assert source(home).session(ID).message_count == 8
    finally:
        chat(home).chmod(0o755)


def test_a_store_cursor_writes_to_reads_once_under_sqlites_own_locks(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = wal_store(home, tmp_path, shm=True)
    connect = sqlite3.connect
    opened: list[str] = []

    def writing(database: str, *, uri: bool, timeout: float) -> sqlite3.Connection:
        opened.append(database)
        stamp = RECORDED + len(opened)
        os.utime(folder / "store.db-wal", (stamp, stamp))  # Cursor appends as each read is made
        return connect(database, uri=uri, timeout=timeout)

    monkeypatch.setattr(sqlite3, "connect", writing)
    assert source(home).session(f"cursor:{OTHER}").message_count == 2
    assert len(opened) == 1


def test_a_store_still_being_made_throughout_every_read_is_skipped_quietly(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    other = home / ".cursor" / "chats" / ("0" * 32) / OTHER
    other.mkdir(parents=True)
    (other / "meta.json").write_text('{"cwd":"/home/ann/x","createdAtMs":1795772600000}')
    (other / "store.db").touch()
    connect = sqlite3.connect
    opened: list[str] = []

    def making(database: str, *, uri: bool, timeout: float) -> sqlite3.Connection:
        opened.append(database)
        stamp = RECORDED + len(opened)
        os.utime(other / "store.db", (stamp, stamp))
        return connect(database, uri=uri, timeout=timeout)

    monkeypatch.setattr(sqlite3, "connect", making)
    assert [s.id for s in source(home).active_sessions(LONG_AGO)] == [ID]
    assert caplog.text == ""


def test_a_store_removed_as_it_opens_is_gone_and_isnt_made_again(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connect = sqlite3.connect

    def removed(database: str, *, uri: bool, timeout: float) -> sqlite3.Connection:
        (chat(home) / "store.db").unlink()
        return connect(database, uri=uri, timeout=timeout)

    monkeypatch.setattr(sqlite3, "connect", removed)
    with pytest.raises(CaptureError) as caught:
        source(home).session(ID)
    assert caught.value.gone
    assert sorted(path.name for path in chat(home).iterdir()) == ["meta.json"]


def test_a_store_cursor_closes_as_it_opens_reads_as_closed(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = wal_store(home, tmp_path, shm=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for name in ("store.db", "store.db-wal"):
        shutil.copy(folder / name, scratch / name)
    with closing(sqlite3.connect(scratch / "store.db")) as db:  # what Cursor's close leaves: all in the store
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copy(scratch / "store.db", tmp_path / "closed.db")
    connect = sqlite3.connect
    opened: list[str] = []

    def closing_now(database: str, *, uri: bool, timeout: float) -> sqlite3.Connection:
        opened.append(database)
        if len(opened) == 1:  # Cursor closes: its writes in the store, its -wal and -shm gone
            shutil.copy(tmp_path / "closed.db", folder / "store.db")
            for name in ("store.db-wal", "store.db-shm"):
                (folder / name).unlink(missing_ok=True)
            folder.chmod(0o555)
        return connect(database, uri=uri, timeout=timeout)

    monkeypatch.setattr(sqlite3, "connect", closing_now)
    try:
        assert [m.content for m in messages(home, f"cursor:{OTHER}")] == ["hi", "hello"]
    finally:
        folder.chmod(0o755)
    assert sorted(path.name for path in folder.iterdir()) == ["meta.json", "store.db"]


def test_a_store_read_as_cursor_removes_its_shm_is_read_again(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close removes the -shm before the -wal: a read made between the two finds a WAL it can't open."""
    folder = wal_store(home, tmp_path, shm=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for name in ("store.db", "store.db-wal"):
        shutil.copy(folder / name, scratch / name)
    with closing(sqlite3.connect(scratch / "store.db")) as db:  # what Cursor's close leaves: all in the store
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copy(scratch / "store.db", tmp_path / "closed.db")
    connect = sqlite3.connect
    opened: list[str] = []

    def closing_now(database: str, *, uri: bool, timeout: float) -> sqlite3.Connection:
        opened.append(database)
        if len(opened) == 1:
            (folder / "store.db-shm").unlink()
            folder.chmod(0o555)
        elif len(opened) == 2:
            folder.chmod(0o755)
            shutil.copy(tmp_path / "closed.db", folder / "store.db")
            (folder / "store.db-wal").unlink()
            folder.chmod(0o555)
        return connect(database, uri=uri, timeout=timeout)

    monkeypatch.setattr(sqlite3, "connect", closing_now)
    try:
        assert [m.content for m in messages(home, f"cursor:{OTHER}")] == ["hi", "hello"]
    finally:
        folder.chmod(0o755)


def test_a_store_read_in_the_gap_between_cursor_removing_its_shm_and_its_wal_is_read_again(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = wal_store(home, tmp_path, shm=False)  # the -shm gone, the -wal not yet
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for name in ("store.db", "store.db-wal"):
        shutil.copy(folder / name, scratch / name)
    with closing(sqlite3.connect(scratch / "store.db")) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    folder.chmod(0o555)
    sleep = time.sleep

    def closes(seconds: float) -> None:  # Cursor finishes closing during the pause
        folder.chmod(0o755)
        shutil.copy(scratch / "store.db", folder / "store.db")
        (folder / "store.db-wal").unlink()
        folder.chmod(0o555)
        sleep(0)

    monkeypatch.setattr(time, "sleep", closes)
    try:
        assert [m.content for m in messages(home, f"cursor:{OTHER}")] == ["hi", "hello"]
    finally:
        folder.chmod(0o755)


# ── timestamps ────────────────────────────────────────────────────────────


def ms(stamp: int) -> datetime:
    return datetime.fromtimestamp(stamp / 1000, UTC)


def test_prompts_and_replies_take_their_times_from_cursors_turns(home: Path) -> None:
    assert [(m.ordinal, m.timestamp) for m in messages(home)] == [
        (0, ms(1795772657298)),
        (1, ms(1795772658790)),
        (2, ms(1795772663204)),
        (3, ms(1795772665176)),
        (4, ms(1795772667826)),
        (5, ms(1795772672076)),
        (6, ms(1795772673558)),
        (7, ms(1795772677891)),
    ]


def test_a_calls_result_takes_the_time_the_call_ended(home: Path) -> None:
    shell_call, grep = (m.tool_calls[0] for m in messages(home) if m.tool_calls)
    assert [e.timestamp for e in shell_call.result_events] == [ms(1795772666377)]
    assert [e.timestamp for e in grep.result_events] == [ms(1795772674830)]


def varint(value: int) -> bytes:
    out = b""
    while value > 0x7F:
        out += bytes([value & 0x7F | 0x80])
        value >>= 7
    return out + bytes([value])


def field(number: int, value: int | bytes) -> bytes:
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    return varint(number << 3 | 2) + varint(len(value)) + value


def asked(text: str, request: str) -> dict[str, Any]:
    """A prompt as Cursor sends one, with the id of its request, which names its turn."""
    return prompt(text) | {"providerOptions": {"cursor": {"requestId": request}}}


def turn(request: str, at: int, *steps: bytes, said: bytes | None = None) -> bytes:
    """A turn as Cursor indexes one, inline in the root's field 8: its prompt (`said`, else one with its time), the
    request it answers in field 3, and its steps."""
    prompt_ = said if said is not None else field(1, field(1, b"what was asked") + field(25, at))
    return field(8, field(1, prompt_ + b"".join(steps) + field(3, request.encode())))


def reply(text: str, at: int) -> bytes:
    return field(2, field(1, field(1, text.encode()) + field(2, at)))


def call_step(call: str, start: int, end: int) -> bytes:
    """A tool call's step: the call's id in field 57, its start and end in 59 and 60."""
    return field(2, field(2, field(57, call.encode()) + field(59, start) + field(60, end)))


def times(home: Path) -> list[datetime | None]:
    return [m.timestamp for m in messages(home, f"cursor:{OTHER}")]


def test_a_prompt_takes_the_turn_named_for_its_request_whatever_its_text(home: Path) -> None:
    said = [asked("continue", "r1"), answer("on it"), asked("continue", "r2"), answer("done")]
    write_chat(home, said, tail=turn("r2", 1795772602000, reply("done", 1795772603000)))  # r1 never indexed
    assert times(home) == [None, None, ms(1795772602000), ms(1795772603000)]


@pytest.mark.parametrize("said", [prompt("hi"), asked("hi", "r9")])  # no request, and one no turn names
def test_a_prompt_whose_request_no_turn_names_takes_no_time(home: Path, said: dict[str, Any]) -> None:
    write_chat(home, [said, answer("hello")], tail=turn("r1", 1795772600000, reply("hello", 1795772601000)))
    assert times(home) == [None, None]


def test_a_reply_cursor_indexed_no_text_for_has_no_time(home: Path) -> None:
    write_chat(
        home, [asked("hi", "r1"), answer("hello")], tail=turn("r1", 1795772600000, reply("other", 1795772601000))
    )
    assert times(home) == [ms(1795772600000), None]


def test_a_reply_after_an_empty_prompt_takes_no_time_from_the_turn_before(home: Path) -> None:
    empty = {"role": "user", "content": "<user_query>\n\n</user_query>"}
    steps = (reply("Looking", 1795772601000), reply("Done.", 1795772602000))
    write_chat(
        home,
        [asked("fix it", "r1"), answer("Working on it"), empty, answer("Done.")],
        tail=turn("r1", 1795772600000, *steps),
    )
    assert times(home) == [ms(1795772600000), None, None]


@pytest.mark.parametrize(
    "tail",
    [
        field(8, b"\xff\xff"),  # not protobuf
        field(8, b"\x01" * 32),  # names a blob that isn't there
        field(8, field(1, field(3, b"r1") + field(1, field(1, b"hi") + field(25, 7)))),  # a time that isn't one
    ],
)
def test_a_turn_of_a_shape_this_reader_doesnt_know_times_nothing_and_spoils_nothing(home: Path, tail: bytes) -> None:
    write_chat(home, [asked("hi", "r1"), answer("hello")], tail=tail)
    assert [(m.content, m.timestamp) for m in messages(home, f"cursor:{OTHER}")] == [("hi", None), ("hello", None)]


def test_a_prompt_timed_in_its_second_field_and_a_step_in_a_blob_of_its_own_read_too(home: Path) -> None:
    step = field(1, field(1, b"hello") + field(2, 1795772602000))
    said = field(1, field(1, b"hi") + field(25, 7) + field(26, 1795772601000))
    tail = turn("r1", 0, field(2, hashlib.sha256(step).digest()), said=said)
    folder = write_chat(home, [asked("hi", "r1"), answer("thinking"), answer("hello")], tail=tail)
    with closing(sqlite3.connect(folder / "store.db")) as db, db:
        db.execute("INSERT INTO blobs VALUES (?, ?)", (hashlib.sha256(step).hexdigest(), step))
    assert times(home) == [ms(1795772601000), None, ms(1795772602000)]


def test_a_step_of_a_shape_this_reader_doesnt_know_leaves_the_rest_of_its_turn_timed(home: Path) -> None:
    odd = field(2, field(1, b"\xff\xff"))  # field 1 not a message
    tail = turn("r1", 1795772600000, odd, reply("hello", 1795772601000))
    write_chat(home, [asked("hi", "r1"), answer("hello")], tail=tail)
    assert times(home) == [ms(1795772600000), ms(1795772601000)]


def test_a_reply_that_only_calls_tools_takes_the_time_its_first_call_started(home: Path) -> None:
    calls = {"id": "1", "role": "assistant", "content": [shell("c1", "make"), shell("c2", "make test")]}
    steps = (call_step("c1", 1795772601000, 1795772609000), call_step("c2", 1795772610000, 1795772620000))
    write_chat(home, [asked("build it", "r1"), calls], tail=turn("r1", 1795772600000, *steps))
    (said,) = messages(home, f"cursor:{OTHER}")[1:]
    assert said.timestamp == ms(1795772601000)
    assert [[e.timestamp for e in c.result_events] for c in said.tool_calls] == [[], []]  # no results written yet


def test_a_reply_takes_the_start_of_its_first_call_cursor_indexed(home: Path) -> None:
    calls = {"id": "1", "role": "assistant", "content": [shell("c1", "a"), shell("c2", "b")]}
    tail = turn("r1", 1795772600000, call_step("c2", 1795772605000, 1795772606000))  # c1 never indexed
    write_chat(home, [asked("go", "r1"), calls], tail=tail)
    assert times(home)[1] == ms(1795772605000)


def test_a_call_with_no_id_takes_no_times(home: Path) -> None:
    unnamed = {"type": "tool-call", "toolName": "Shell", "args": {"command": "ls"}}
    tail = turn("r1", 1795772600000, call_step("", 1795772601000, 1795772602000))
    write_chat(home, [asked("go", "r1"), answer("", unnamed)], tail=tail)
    assert times(home)[1] is None


@pytest.mark.parametrize(
    "said",
    [b"", field(1, 7), field(1, b"\xff\xff"), field(1, field(1, b"build it"))],  # none, not a message, not
)  # protobuf, and one with no time
def test_a_turn_whose_prompt_cant_be_read_still_times_its_calls(home: Path, said: bytes) -> None:
    calls = {"id": "1", "role": "assistant", "content": [shell("c1", "make")]}
    ended = result("c1", "Shell", "Exit code: 0", {"success": {"command": "make"}})
    tail = turn("r1", 0, call_step("c1", 1795772601000, 1795772609000), said=said)
    write_chat(home, [asked("build it", "r1"), calls, ended], tail=tail)
    built = messages(home, f"cursor:{OTHER}")
    assert (built[0].timestamp, built[1].timestamp) == (None, ms(1795772601000))
    assert built[1].tool_calls[0].result_events[0].timestamp == ms(1795772609000)


def test_the_fixtures_root_names_the_messages_the_lock_test_counts() -> None:
    with closing(sqlite3.connect(FIXTURES / "cursor" / "chats" / FOLDER / SESSION / "store.db")) as db:
        (head,) = db.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
        root = json.loads(bytes.fromhex(head))["latestRootBlobId"]
        (data,) = db.execute("SELECT data FROM blobs WHERE id = ?", (root,)).fetchone()
    keys = [data[n * 34 : n * 34 + 2] for n in range(MESSAGE_BLOBS + 1)]  # each field 1: its key, 32, an id
    assert keys == [b"\x0a\x20"] * MESSAGE_BLOBS + [keys[-1]]
    assert keys[-1] != b"\x0a\x20"


def test_a_store_cursor_holds_as_a_turn_is_read_is_a_capture_error_not_timeless(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = sqlite3.connect

    class Locked(sqlite3.Connection):
        reads = 0

        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            if "WHERE id = ?" in sql:
                self.reads += 1
                if self.reads > 1 + MESSAGE_BLOBS:  # past the root and the messages: the turns' blobs
                    raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, *args)

    def locked(database: str, *, uri: bool, timeout: float) -> sqlite3.Connection:
        return connect(database, uri=uri, timeout=timeout, factory=Locked)

    monkeypatch.setattr(sqlite3, "connect", locked)
    with pytest.raises(CaptureError) as caught:
        source(home).session(ID)
    assert not caught.value.gone


def test_a_call_id_used_again_in_a_later_turn_takes_its_own_turns_times(home: Path) -> None:
    first = {"id": "1", "role": "assistant", "content": [shell("functions.Shell:0", "a")]}
    second = {"id": "2", "role": "assistant", "content": [shell("functions.Shell:0", "b")]}
    tail = turn("r1", 1795772600000, call_step("functions.Shell:0", 1795772601000, 1795772602000))
    tail += turn("r2", 1795772610000, call_step("functions.Shell:0", 1795772611000, 1795772612000))
    write_chat(home, [asked("one", "r1"), first, asked("two", "r2"), second], tail=tail)
    assert times(home) == [ms(1795772600000), ms(1795772601000), ms(1795772610000), ms(1795772611000)]


def test_a_request_cursor_indexed_as_two_turns_is_timed_from_both(home: Path) -> None:
    tail = turn("r1", 1795772600000, reply("first", 1795772601000))
    tail += turn("r1", 1795772605000, reply("second", 1795772606000))
    write_chat(home, [asked("go", "r1"), answer("first"), answer("second")], tail=tail)
    assert times(home) == [ms(1795772600000), ms(1795772601000), ms(1795772606000)]


def test_a_call_whose_prompt_has_no_turn_yet_takes_no_time_from_another_turn(home: Path) -> None:
    first = {"id": "1", "role": "assistant", "content": [shell("tool_0", "a")]}
    again = {"id": "2", "role": "assistant", "content": [shell("tool_0", "b")]}
    tail = turn("r1", 1795772600000, call_step("tool_0", 1795772601000, 1795772602000))  # r2 not indexed yet
    write_chat(home, [asked("one", "r1"), first, asked("two", "r2"), again], tail=tail)
    assert times(home) == [ms(1795772600000), ms(1795772601000), None, None]


def test_two_calls_with_one_id_keep_their_own_results_times(home: Path) -> None:
    first = {"id": "1", "role": "assistant", "content": [shell("call_1", "a")]}
    again = {"id": "2", "role": "assistant", "content": [shell("call_1", "b")]}
    done = result("call_1", "Shell", "Exit code: 0", {"success": {"command": "b"}})
    tail = turn("r1", 1795772600000, call_step("call_1", 1795772601000, 1795772602000))
    tail += turn("r2", 1795772610000, call_step("call_1", 1795772611000, 1795772612000))
    write_chat(home, [asked("one", "r1"), first, done, asked("two", "r2"), again, done], tail=tail)
    read = messages(home, f"cursor:{OTHER}")
    assert [read[n].tool_calls[0].result_events[0].timestamp for n in (1, 3)] == [
        ms(1795772602000),
        ms(1795772612000),
    ]


def test_a_request_whose_first_turn_has_no_prompt_to_read_takes_the_next_ones_time(home: Path) -> None:
    tail = turn("r1", 0, said=b"") + turn("r1", 1795772605000, reply("hello", 1795772606000))
    write_chat(home, [asked("go", "r1"), answer("hello")], tail=tail)
    assert times(home) == [ms(1795772605000), ms(1795772606000)]


def test_a_request_two_prompts_carry_times_the_first_only(home: Path) -> None:
    tail = turn("r1", 1795772600000, reply("hello", 1795772601000))
    write_chat(home, [asked("go", "r1"), answer("hello"), asked("go", "r1"), answer("hello")], tail=tail)
    assert times(home) == [ms(1795772600000), ms(1795772601000), None, None]


def test_a_call_cursor_indexed_across_a_requests_two_turns_takes_its_end_from_either(home: Path) -> None:
    calls = {"id": "1", "role": "assistant", "content": [shell("c1", "make")]}
    done = result("c1", "Shell", "Exit code: 0", {"success": {"command": "make"}})
    started = field(2, field(2, field(57, b"c1") + field(59, 1795772601000)))  # stopped before it ended
    tail = turn("r1", 1795772600000, started) + turn(
        "r1", 1795772605000, call_step("c1", 1795772601000, 1795772609000)
    )
    write_chat(home, [asked("build it", "r1"), calls, done], tail=tail)
    assert messages(home, f"cursor:{OTHER}")[1].tool_calls[0].result_events[0].timestamp == ms(1795772609000)


def test_a_field_written_twice_reads_as_its_last(home: Path) -> None:
    twice = field(2, field(1, field(1, b"hello") + field(2, 1795772601000) + field(2, 1795772603000)))
    write_chat(home, [asked("hi", "r1"), answer("hello")], tail=turn("r1", 1795772600000, twice))
    assert times(home) == [ms(1795772600000), ms(1795772603000)]


def test_a_result_folds_into_the_call_before_it_with_its_id(home: Path) -> None:
    failed = result("call_1", "Shell", "Exit code: 1", {"failure": {"command": "false", "exitCode": 1}})
    passed = result("call_1", "Shell", "Exit code: 0", {"success": {"command": "true"}})
    said = [
        asked("one", "r1"),
        answer("trying", shell("call_1", "false")),
        failed,
        asked("two", "r2"),
        answer("again", shell("call_1", "true")),
        passed,
    ]
    write_chat(home, said)
    first, second = (m.tool_calls[0] for m in messages(home, f"cursor:{OTHER}") if m.tool_calls)
    assert (first.exit_code, first.result_content) == (1, "Exit code: 1")
    assert (second.exit_code, second.result_content) == (0, "Exit code: 0")


# ── watching ──────────────────────────────────────────────────────────────


def watched(home: Path, change: Any, *, session: str = ID, ticks_wanted: int = 2) -> int:
    """How many times a watch ticks, `change` made after the first."""

    async def watch() -> int:
        ticks = 0
        async for _ in source(home, poll=0.01).watch(session):
            ticks += 1
            if ticks == 1:
                change()
            if ticks == ticks_wanted:
                break
        return ticks

    return asyncio.run(asyncio.wait_for(watch(), timeout=5))


@pytest.mark.parametrize("written", ["store.db", "store.db-wal", "meta.json"])
def test_watch_ticks_on_opening_and_when_cursor_writes(home: Path, written: str) -> None:
    def write() -> None:
        with (chat(home) / written).open("ab") as file:
            file.write(b"\0")

    assert watched(home, write) == 2


@pytest.mark.parametrize("change", ["shm made", "shm removed", "meta removed"])
def test_watch_ticks_when_cursor_opens_or_closes_the_store(home: Path, change: str) -> None:
    shm, meta = chat(home) / "store.db-shm", chat(home) / "meta.json"
    if change == "shm removed":
        shm.write_bytes(b"")
    act = {"shm made": lambda: shm.write_bytes(b""), "shm removed": shm.unlink, "meta removed": meta.unlink}[change]
    assert watched(home, act) == 2


def test_watch_follows_the_copy_cursor_writes_to(home: Path) -> None:
    copy = home / ".cursor" / "chats" / ("f" * 32) / SESSION
    shutil.copytree(chat(home), copy)
    for file in copy.iterdir():
        os.utime(file, (RECORDED + 60, RECORDED + 60))  # the copy written last as the watch opens

    def cursor_writes_the_other() -> None:
        for file in chat(home).iterdir():
            os.utime(file, (RECORDED + 120, RECORDED + 120))

    assert watched(home, cursor_writes_the_other) == 2


def test_a_watched_session_that_cant_be_looked_at_is_a_capture_error(home: Path) -> None:
    async def watch() -> None:
        async for _ in source(home, poll=0.01).watch(ID):
            chat(home).chmod(0)

    try:
        with pytest.raises(CaptureError) as caught:
            asyncio.run(asyncio.wait_for(watch(), timeout=5))
    finally:
        chat(home).chmod(0o755)
    assert not caught.value.gone


def test_watch_ends_when_the_session_is_removed(home: Path) -> None:
    assert watched(home, lambda: shutil.rmtree(chat(home)), ticks_wanted=99) == 1


def test_watch_on_a_missing_session_fails(home: Path) -> None:
    async def watch() -> None:
        async for _ in source(home).watch("cursor:0b0e2a37-5a70-4fd2-9f8a-2c0bb0d1c6a1"):
            pass

    with pytest.raises(CaptureError) as caught:
        asyncio.run(watch())
    assert caught.value.gone


def test_a_reader_using_the_shm_isnt_a_change(home: Path, tmp_path: Path) -> None:
    folder = wal_store(home, tmp_path, shm=True)

    async def watch() -> bool:
        ticks = source(home, poll=0.01).watch(f"cursor:{OTHER}")
        await anext(ticks)  # the opening tick
        (folder / "store.db-shm").write_bytes(b"\1" * 32768)  # as SQLite's readers mark it
        try:
            await asyncio.wait_for(anext(ticks), timeout=0.2)
        except TimeoutError:
            return False
        return True

    assert asyncio.run(watch()) is False
