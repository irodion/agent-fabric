"""Seam: the per-agent tables of the normalized event model (docs/design.md §4.2).

The recorded sessions of the live check (2026-11-19) each run a failing `python3 check.py` and write a file; the same
Copilot CLI session is read through agentsview and through Copilot's own reader, which must come out the same. The
rest are made-up calls in the shapes the devbox's archive holds."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from agent_fabric.capture import Message, SessionMeta, ToolCall, ToolResultEvent
from agent_fabric.copilot import CopilotSource
from agent_fabric.normalize import Action, Event, events

FIXTURES = Path(__file__).parent / "fixtures"


def recorded(agent: str) -> list[Event]:
    session = SessionMeta.model_validate(json.loads((FIXTURES / "agentsview" / agent / "session.json").read_text()))
    body = json.loads((FIXTURES / "agentsview" / agent / "messages.json").read_text())
    return events(session, [Message.model_validate(m) for m in body["messages"]])


def actions(found: list[Event]) -> list[Action]:
    return [action for event in found for action in event.actions]


def summary(found: list[Event]) -> list[tuple[str, str | None, int | None, bool]]:
    return [(a.kind, a.command, a.exit_code, a.failed) for a in actions(found)]


def one(agent: str, call: dict[str, Any]) -> list[Action]:
    session = SessionMeta.model_validate(
        {"id": "s", "agent": agent, "project": "p", "is_automated": False, "started_at": None, "ended_at": None}
        | {"message_count": 1}
    )
    message = Message.model_validate(
        {"session_id": "s", "ordinal": 0, "role": "assistant", "timestamp": None, "content": None}
        | {"thinking_text": None, "model": None, "is_system": False, "tool_calls": [call]}
    )
    return list(events(session, [message])[0].actions)


def call(
    tool: str, category: str, arguments: object, result: str | None, *statuses: str, **more: Any
) -> dict[str, Any]:
    events_ = [{"status": status} for status in statuses]
    text = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return (
        {"tool_name": tool, "category": category, "input_json": text, "result_content": result}
        | {"result_events": events_}
        | more
    )


# ── the recorded sessions ─────────────────────────────────────────────────


def test_claude_codes_failed_command_and_the_rest() -> None:
    found = recorded("claude")
    assert summary(found) == [
        ("shell", "python3 check.py", 1, True),
        ("shell", "cat notes.txt", 0, False),
        ("edit", None, None, False),
    ]
    assert [e.failed for e in found] == [False, True, False, False, False]


def test_codexs_script_calls_become_actions_with_their_own_exit_codes() -> None:
    found = actions(recorded("codex"))
    assert [(a.kind, a.tool, a.exit_code, a.failed) for a in found] == [
        ("shell", "exec_command", 1, True),
        ("shell", "exec_command", 0, False),
    ]
    assert found[0].command == "python3 check.py"
    assert found[1].command == "printf '%s\\n' 'checked spk69f8b69e' > notes.txt && cat notes.txt"
    assert found[0].output == "FAILED test_retry: expected 3, got 2\n"


def test_copilot_through_agentsview_reads_the_exit_code_from_the_output() -> None:
    assert summary(recorded("copilot")) == [
        ("shell", "python3 check.py", 1, True),
        ("edit", None, None, True),
        ("read", None, None, False),
        ("edit", None, None, False),
        ("read", None, None, False),
    ]


def test_copilots_own_reader_comes_out_as_agentsview_does_and_keeps_the_error(tmp_path: Path) -> None:
    shutil.copytree(FIXTURES / "copilot" / "session-state", tmp_path / ".copilot" / "session-state")
    source = CopilotSource({"HOME": str(tmp_path)})
    session_id = "copilot:3e7dddb4-4c41-489e-877a-4b5c684bcd6c"
    own = events(source.session(session_id), source.messages(session_id, 0).messages)
    assert summary(own) == summary(recorded("copilot"))
    failed_edit = actions(own)[1]
    assert failed_edit.output is not None
    assert failed_edit.output.startswith("Failed to apply patch")


# ── exit codes ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("agent", "result", "statuses", "code"),
    [
        ("claude", "Exit code 2\nboom", ("errored",), 2),
        ("claude", "all good", (), 0),
        ("claude", "Command timed out", ("errored",), None),
        ("copilot", "x\n<shellId: 3 completed with exit code 127>", ("started", "completed"), 127),
        ("copilot", None, ("started",), None),
    ],
)
def test_each_agent_keeps_its_exit_code_in_its_own_place(
    agent: str, result: str | None, statuses: tuple[str, ...], code: int | None
) -> None:
    (action,) = one(
        agent, call("Bash" if agent == "claude" else "bash", "Bash", {"command": "make"}, result, *statuses)
    )
    assert (action.command, action.exit_code) == ("make", code)


def test_an_echoed_exit_code_counts_over_the_shells_zero() -> None:
    result = "FAIL tests\nEXIT:1\n<shellId: 0 completed with exit code 0>"
    (action,) = one("copilot", call("bash", "Bash", {"command": "npm test; echo EXIT:$?"}, result, "completed"))
    assert (action.exit_code, action.failed) == (1, True)


def test_an_error_in_the_output_fails_a_command_whose_exit_code_is_unknown() -> None:
    result = "Traceback (most recent call last):\n  ...\nValueError: x"
    (action,) = one("cursor", call("Shell", "Bash", {"command": "python3 x.py"}, result, "completed"))
    assert (action.exit_code, action.failed) == (None, True)


@pytest.mark.parametrize("command", ["rg -n error: src", "cat log.txt", "git show HEAD:x.py", "sed -n 1,9p a"])
def test_a_command_that_only_shows_files_isnt_failed_by_their_words(command: str) -> None:
    (action,) = one("cursor", call("Shell", "Bash", {"command": command}, "error: in the text\nFAILED", "completed"))
    assert (action.exit_code, action.failed) == (None, False)


@pytest.mark.parametrize(
    ("command", "code"),
    [("false; echo EXIT:$?", 1), ("cat run.log", None), ("rg '^EXIT:' build.log", None), ('printf "EXIT:%s" $?', 1)],
    ids=["echoed", "shown-in-a-file", "searched-for", "printf"],
)
def test_an_echoed_exit_code_counts_where_the_shell_gave_none(command: str, code: int | None) -> None:
    (action,) = one("cursor", call("Shell", "Bash", {"command": command}, "EXIT:1\n"))
    assert action.exit_code == code


def test_a_call_with_output_and_no_status_finished() -> None:
    # Cursor records no status at all: its output is all there is to go by.
    (action,) = one(
        "cursor", call("Shell", "Bash", {"command": "python3 check.py"}, "Traceback (most recent call last)")
    )
    assert (action.status, action.failed) == (None, True)


@pytest.mark.parametrize("command", ["cat config.toml && pytest", "rg x src; make test", "ls\npython3 x.py"])
def test_a_command_that_also_runs_something_can_fail_on_its_words(command: str) -> None:
    (action,) = one("cursor", call("Shell", "Bash", {"command": command}, "FAILED test_retry", "completed"))
    assert action.failed


@pytest.mark.parametrize(
    ("command", "reads"),
    [
        ("rg -n 'a|def' src | head -5", True),
        ("cd repo && git show HEAD:x.py", True),
        ("cat > notes.txt <<EOF\nerror: x\nEOF", False),
        ("ls\npytest", False),
        ('echo "unclosed', False),
    ],
)
def test_only_a_command_that_only_shows_files_hides_its_words(command: str, reads: bool) -> None:
    (action,) = one("cursor", call("Shell", "Bash", {"command": command}, "error: x\nFAILED", "completed"))
    assert action.failed is not reads


@pytest.mark.parametrize(
    "command", ["find . -name check.py -exec python3 {} \\;", "find . -name '*.pyc' -delete", "sed -i s/a/b/ x.py"]
)
def test_a_reader_that_runs_or_changes_something_can_fail(command: str) -> None:
    (action,) = one("cursor", call("Shell", "Bash", {"command": command}, "Traceback (most recent call last)"))
    assert action.failed


@pytest.mark.parametrize("command", ['echo "$(python3 check.py 2>&1)"', "cat `ls *.log`"])
def test_a_reader_around_a_substitution_runs_a_command(command: str) -> None:
    (action,) = one("cursor", call("Shell", "Bash", {"command": command}, "Traceback (most recent call last)"))
    assert action.failed


def test_a_pipe_of_readers_still_only_shows_files() -> None:
    (action,) = one(
        "cursor", call("Shell", "Bash", {"command": "rg -n error: src | head -5"}, "error: x", "completed")
    )
    assert not action.failed


def test_an_error_word_from_a_command_that_exited_zero_fails_nothing() -> None:
    # Codex reads files through the shell: source code full of `error:` mustn't fail the read.
    result = 'raise ValueError("error: bad input")\n<shellId: 0 completed with exit code 0>'
    (action,) = one("copilot", call("bash", "Bash", {"command": "sed -n 1,40p x.py"}, result, "completed"))
    assert (action.exit_code, action.failed) == (0, False)


def test_an_error_word_in_a_file_read_fails_nothing() -> None:
    (action,) = one("claude", call("Read", "Read", {"file_path": "log.txt"}, "error: disk full\nFAILED", ""))
    assert (action.kind, action.failed) == ("read", False)


def test_a_command_still_running_hasnt_failed() -> None:
    (action,) = one("copilot", call("bash", "Bash", {"command": "pytest"}, "FAILED so far", "started"))
    assert (action.status, action.failed) == ("started", False)


def test_a_failed_edit_with_no_output_failed() -> None:
    (action,) = one("copilot", call("apply_patch", "Edit", "*** Begin Patch", None, "started", "errored"))
    assert (action.kind, action.failed) == ("edit", True)


def test_the_exit_code_copilots_own_reader_keeps_wins() -> None:
    result = "<shellId: 0 completed with exit code 0>"
    (action,) = one("copilot", call("bash", "Bash", {"command": "x"}, result, "completed", exit_code=4))
    assert action.exit_code == 4


# ── Codex ─────────────────────────────────────────────────────────────────


def script_result(header: str, *outputs: object) -> str:
    texts = [header] + [o if isinstance(o, str) else json.dumps(o) for o in outputs]
    return json.dumps([{"type": "input_text", "text": text} for text in texts])


def chunk(code: int | None, output: str = "") -> dict[str, object]:
    return {"chunk_id": "c", "wall_time_seconds": 0, "exit_code": code, "output": output}


def test_a_codex_script_pairs_each_command_with_its_output() -> None:
    script = 'text(await tools.exec_command({cmd:"echo a"}));\ntext(await tools.exec_command({cmd:"false"}));\n'
    result = script_result("Script completed\nOutput:\n", chunk(0, "a\n"), chunk(1))
    found = one("codex", call("exec", "Bash", script, result, "completed"))
    assert [(a.command, a.exit_code, a.failed, a.output) for a in found] == [
        ("echo a", 0, False, "a\n"),
        ("false", 1, True, None),  # no output reads as none
    ]


def test_a_codex_script_with_a_patch_and_a_web_call_gives_an_edit_and_an_other() -> None:
    script = 'await tools.apply_patch("*** Begin Patch\\n*** End Patch");\nawait tools.web__run({open:[]});\n'
    result = script_result("Script completed\nOutput:\n", "Success. Updated the following files", "page text")
    found = one("codex", call("exec", "Bash", script, result, "completed"))
    assert [(a.kind, a.tool, a.failed) for a in found] == [
        ("edit", "apply_patch", False),
        ("other", "web__run", False),
    ]


def test_a_failed_codex_script_fails_its_last_call() -> None:
    script = 'text(await tools.exec_command({cmd:"ls"}));\ntext(await tools.apply_patch("*** Begin Patch"));\n'
    result = script_result("Script failed\nError: apply_patch verification failed", chunk(0, "x"), "verification")
    found = one("codex", call("exec", "Bash", script, result, "completed"))
    assert [(a.kind, a.failed) for a in found] == [("shell", False), ("edit", True)]


def test_a_codex_script_that_prints_its_own_way_fails_on_a_printed_exit_code() -> None:
    script = (
        'const r = await tools.exec_command({cmd:"make"});\ntext(r.output);\ntext(`[exit_code=${r.exit_code}]`);\n'
    )
    result = script_result("Script completed\nOutput:\n", "make: *** [all] Error 2", "\n[exit_code=2]")
    (action,) = one("codex", call("exec", "Bash", script, result, "completed"))
    assert (action.command, action.exit_code, action.failed) == ("make", None, True)


def test_a_codex_command_still_running_has_no_exit_code_and_hasnt_failed() -> None:
    running = {"chunk_id": "c", "session_id": 7, "output": "watching…"}
    script = 'text(await tools.exec_command({cmd:"npm run dev"}));'
    (action,) = one("codex", call("exec", "Bash", script, script_result("Script completed", running), "completed"))
    assert (action.exit_code, action.failed, action.output) == (None, False, "watching…")


@pytest.mark.parametrize(
    "script",
    [
        'text(await tools.exec_command({cmd:"echo tools.apply_patch("}));',
        '// tools.apply_patch("x")\ntext(await tools.exec_command({cmd:"echo tools.apply_patch("}));',
        "/* tools.web__run({}) */ text(await tools.exec_command({cmd:`echo tools.apply_patch(`}));",
    ],
)
def test_a_tool_named_in_a_string_or_a_comment_is_no_call(script: str) -> None:
    result = script_result("Script completed", chunk(0, "tools.apply_patch(\n"))
    (action,) = one("codex", call("exec", "Bash", script, result, "completed"))
    assert (action.tool, action.command, action.exit_code) == ("exec_command", "echo tools.apply_patch(", 0)


def test_a_command_comes_from_its_own_calls_arguments_only() -> None:
    script = 'await tools.exec_command(args);\nawait tools.exec_command({env:{cmd:"no"}, cmd:"ls"});\n'
    found = one(
        "codex", call("exec", "Bash", script, script_result("Script completed", chunk(0), chunk(0)), "completed")
    )
    assert [a.command for a in found] == [None, "ls"]


def test_a_codex_command_still_running_isnt_failed_by_its_output_so_far() -> None:
    running = {"chunk_id": "c", "session_id": 7, "output": "1 FAILED so far"}
    script = 'text(await tools.exec_command({cmd:"pytest"}));'
    (action,) = one("codex", call("exec", "Bash", script, script_result("Script completed", running), "completed"))
    assert (action.status, action.exit_code, action.failed) == ("running", None, False)


@pytest.mark.parametrize("key", ['"cmd"', "'cmd'"])
def test_a_quoted_key_names_the_command_too(key: str) -> None:
    script = f'text(await tools.exec_command({{{key}:"false", "max_output_tokens": 10}}));'
    (action,) = one("codex", call("exec", "Bash", script, script_result("Script completed", chunk(1)), "completed"))
    assert (action.command, action.exit_code) == ("false", 1)


def test_an_exit_code_in_a_files_text_isnt_the_commands() -> None:
    script = 'const r = await tools.exec_command({cmd:"cat example.py"});\ntext(r.output);\n'
    result = script_result("Script completed", "exit_code=1\nprint('done')\n")
    (action,) = one("codex", call("exec", "Bash", script, result, "completed"))
    assert (action.exit_code, action.failed) == (None, False)


@pytest.mark.parametrize(
    "script",
    [
        'const a = await tools.exec_command({cmd:"false"});\nconst b = await tools.exec_command({cmd:"true"});\n'
        "text(b);\ntext(a);\n",
        'const [a, b] = await Promise.all([tools.exec_command({cmd:"false"}), tools.exec_command({cmd:"true"})]);\n'
        "text(b);\ntext(a);\n",
    ],
    ids=["printed-in-another-order", "run-together"],
)
def test_outputs_pair_only_with_calls_that_print_as_they_return(script: str) -> None:
    result = script_result("Script completed", chunk(0, "true"), chunk(1, "false"))
    found = one("codex", call("exec", "Bash", script, result, "completed"))
    assert [(a.command, a.exit_code, a.failed) for a in found] == [("false", None, True), ("true", None, True)]


@pytest.mark.parametrize(
    ("printed", "status", "code"),
    [(chunk(0, "EXIT:1\n"), "completed", 1), ({"chunk_id": "c", "session_id": 3, "output": "…"}, "running", None)],
    ids=["echoed-exit", "still-running"],
)
def test_one_call_printed_later_still_pairs_with_its_one_output(
    printed: dict[str, object], status: str, code: int | None
) -> None:
    script = 'const r = await tools.exec_command({cmd:"false; echo EXIT:$?"});\ntext(r);\n'
    (action,) = one("codex", call("exec", "Bash", script, script_result("Script completed", printed), "completed"))
    assert (action.status, action.exit_code) == (status, code)


def test_a_scripts_output_that_cant_be_paired_stays_on_its_last_call() -> None:
    script = 'const r = await tools.exec_command({cmd:"make"});\ntext(r.output);\ntext(`exit_code=${r.exit_code}`);\n'
    result = script_result("Script completed", "make: *** [all] Error 2", "exit_code=2")
    (action,) = one("codex", call("exec", "Bash", script, result, "completed"))
    assert (action.failed, action.output) == (True, "make: *** [all] Error 2\nexit_code=2")


@pytest.mark.parametrize("value", ['"cat " + path + " && pytest"', '"ls".trim()', "`a` + b"])
def test_a_command_built_by_an_expression_is_unknown(value: str) -> None:
    script = f"text(await tools.exec_command({{cmd:{value}}}));"
    result = script_result("Script completed", chunk(None, "FAILED test_x"))
    (action,) = one("codex", call("exec", "Bash", script, result, "completed"))
    assert action.command is None


def test_a_codex_script_whose_outputs_dont_pair_keeps_only_its_failure() -> None:
    script = 'await tools.exec_command({cmd:"a"});\nawait tools.exec_command({cmd:"b"});\n'
    result = script_result("Script failed\nTypeError: x", chunk(0))
    found = one("codex", call("exec", "Bash", script, result, "completed"))
    assert [(a.command, a.exit_code, a.failed) for a in found] == [("a", None, True), ("b", None, True)]


def test_a_codex_script_without_a_tool_call_is_one_action() -> None:
    result = script_result("Script failed\nSyntaxError: x")
    (action,) = one("codex", call("exec", "Bash", "const x = ;", result, "completed"))
    assert action.failed


@pytest.mark.parametrize(
    ("literal", "command"),
    [
        (r'"grep -n \"x\" a.txt"', 'grep -n "x" a.txt'),
        (r"'it\'s'", "it's"),
        ("`ls\n-la`", "ls\n-la"),
        ("`echo ${name}`", None),
        (r'"tab\there \u00e9"', "tab\there \u00e9"),
        (r'"\x63at log.txt"', "cat log.txt"),
        (r'"\u{1F600} \u0041"', "\U0001f600 A"),
        ('"one \\\ntwo"', "one two"),
        (r'"\u{110000}"', None),
        ("cmdVariable", None),
    ],
)
def test_a_codex_command_reads_from_any_js_string(literal: str, command: str | None) -> None:
    script = f"await tools.exec_command({{cmd:{literal},max_output_tokens:100}});"
    (action,) = one("codex", call("exec", "Bash", script, script_result("Script completed", chunk(0)), "completed"))
    assert action.command == command


def test_codex_before_code_mode_had_the_exit_code_in_its_output() -> None:
    result = "Chunk ID: 1\nWall time: 0.1 seconds\nProcess exited with code 2\nOutput:\nno such file\n"
    (action,) = one("codex", call("exec_command", "Bash", {"cmd": "cat x"}, result, "completed"))
    assert (action.command, action.exit_code, action.failed) == ("cat x", 2, True)


def test_codex_before_code_mode_reads_its_exit_code_above_the_output_only() -> None:
    result = "Chunk ID: 1\nProcess exited with code 0\nOutput:\nlog: Process exited with code 2\n"
    (action,) = one("codex", call("exec_command", "Bash", {"cmd": "cat log.txt"}, result, "completed"))
    assert (action.exit_code, action.failed) == (0, False)


def test_a_failed_script_in_plain_text_keeps_its_error() -> None:
    script = 'text(await tools.exec_command({cmd:"ls"}));\nboom();\n'
    (action,) = one("codex", call("exec", "Bash", script, "Script failed\nTypeError: boom", "completed"))
    assert action.failed
    assert action.output is not None
    assert "TypeError: boom" in action.output


@pytest.mark.parametrize("outputs", [(), (chunk(0, "a"),)], ids=["before-any-output", "after-one"])
def test_a_failed_scripts_own_error_stays_with_its_failure(outputs: tuple[dict[str, object], ...]) -> None:
    script = 'text(await tools.exec_command({cmd:"ls"}));\nundefinedThing();\n'
    result = script_result("Script failed\nReferenceError: undefinedThing is not defined", *outputs)
    found = one("codex", call("exec", "Bash", script, result, "completed"))
    assert found[-1].failed
    assert found[-1].output is not None
    assert "ReferenceError: undefinedThing is not defined" in found[-1].output


@pytest.mark.parametrize("mention", ["// r.exit_code", '"r.exit_code"', "/* r.exit_code */"])
def test_an_exit_code_read_named_only_in_a_comment_or_string_prints_none(mention: str) -> None:
    script = f'const r = await tools.exec_command({{cmd:"cat example.py"}});\n{mention};\ntext(r.output);\n'
    result = script_result("Script completed", "exit_code=1\n")
    (action,) = one("codex", call("exec", "Bash", script, result, "completed"))
    assert (action.exit_code, action.failed) == (None, False)


@pytest.mark.parametrize(
    "before",
    [
        "const quote = /'/g;",
        'const q = /["`]/;',
        "if (x) return /'/.test(y);",
        "if (ok) /'/.test(value);",
        "while (f(a)) /'/.exec(b);",
        'const half = total / 2; const s = "\'";',
        "const third = (a + b) / 3; const t = '\"';",
    ],
)
def test_a_regex_or_a_division_hides_no_call(before: str) -> None:
    script = f'{before}\ntext(await tools.exec_command({{cmd:"false"}}));'
    (action,) = one("codex", call("exec", "Bash", script, script_result("Script completed", chunk(1)), "completed"))
    assert (action.tool, action.command, action.exit_code) == ("exec_command", "false", 1)


# ── any other agent ───────────────────────────────────────────────────────


def test_an_agent_without_a_table_gets_the_kind_and_command_its_category_gives() -> None:
    (action,) = one("cursor", call("Shell", "Bash", {"command": "ls"}, "a b", "completed"))
    assert (action.kind, action.command, action.exit_code, action.failed) == ("shell", "ls", None, False)


def test_an_event_keeps_its_message() -> None:
    (event,) = [e for e in recorded("codex") if e.text and "spk" in e.text and e.role == "assistant"][-1:]
    assert (event.session_id, event.role, event.actions) == (
        "codex:01a1f2c2-eb87-7743-97f3-eba0fe2f6192",
        "assistant",
        (),
    )
    assert event.ts is not None


def test_result_statuses_are_read_in_order() -> None:
    tool = ToolCall(tool_name="bash", category="Bash", result_events=(ToolResultEvent(status="started"),))
    assert tool.result_events[0].status == "started"
