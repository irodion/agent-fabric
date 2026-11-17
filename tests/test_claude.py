"""Seam: the Claude Code adapter against a stub `claude` on PATH (docs/design.md §4.7). It shows the command line kb
sends and how kb reads the reply. That real Claude Code then makes no tool call is the live check in §4.7."""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import BRANCH, PAGE, Lab, a_stub

from agent_fabric.backends import BackendError, ClaudeBackend, InvalidAnswer, Question, backend_named

QUESTIONS = [
    Question("ref-0", "Ref 0: does it support the note?", ("supports", "contradicts", "says_nothing")),
    Question("useful", "Would a future agent act better for knowing this?", ("yes", "no")),
]
# Claude Code 2.1.326's `--output-format json` reply: one object, the answer under `structured_output`.
STUB = f"""#!{sys.executable}
import json, os, pathlib, sys, time
folder = pathlib.Path(os.environ["CLAUDE_STUB"])
args = sys.argv[1:]
schema = json.loads(args[args.index("--json-schema") + 1])
(folder / "call.json").write_text(json.dumps({{
    "args": args,
    "stdin": sys.stdin.read(),
    "schema": schema,
    "cwd_listing": sorted(path.name for path in pathlib.Path.cwd().iterdir()),
}}))
if (folder / "sleep").exists():
    time.sleep(30)
if (folder / "stderr").exists():
    sys.stderr.write((folder / "stderr").read_text())
    sys.exit(1)
if (folder / "stdout").exists():
    sys.stdout.write((folder / "stdout").read_text())
    sys.exit(int((folder / "exit").read_text()) if (folder / "exit").exists() else 0)
answer = {{key: value["enum"][0] for key, value in schema["properties"].items()}}
sys.stdout.write(json.dumps({{
    "type": "result", "subtype": "success", "is_error": False, "result": json.dumps(answer),
    "structured_output": answer,
}}))
"""


@dataclass
class Claude:
    folder: Path
    env: dict[str, str]

    def call(self) -> dict[str, Any]:
        recorded: dict[str, Any] = json.loads((self.folder / "call.json").read_text())
        return recorded

    def replies(self, reply: object, *, exit_code: int = 0) -> None:
        (self.folder / "stdout").write_text(reply if isinstance(reply, str) else json.dumps(reply))
        (self.folder / "exit").write_text(str(exit_code))


def install_stub(tmp_path: Path) -> Claude:
    folder = a_stub(tmp_path / "claude-stub", "claude", STUB).parent
    env = {"HOME": str(tmp_path), "PATH": f"{folder}:{os.environ['PATH']}", "CLAUDE_STUB": str(folder)}
    return Claude(folder, env)


@pytest.fixture
def claude(tmp_path: Path) -> Claude:
    return install_stub(tmp_path)


def test_the_structured_output_comes_back_as_the_answers(claude: Claude) -> None:
    answer = {"ref-0": "contradicts", "useful": "no"}
    claude.replies({"type": "result", "subtype": "success", "is_error": False, "structured_output": answer})

    assert ClaudeBackend(claude.env).answer("the state", QUESTIONS) == answer


def test_the_prompt_goes_on_stdin_with_the_state_and_each_question(claude: Claude) -> None:
    ClaudeBackend(claude.env).answer("the state", QUESTIONS)

    prompt = claude.call()["stdin"]
    assert "the state" in prompt
    assert "Ref 0: does it support the note?" in prompt
    assert "Would a future agent act better" in prompt


def test_the_schema_allows_only_each_questions_options(claude: Claude) -> None:
    ClaudeBackend(claude.env).answer("the state", QUESTIONS)

    assert claude.call()["schema"] == {
        "type": "object",
        "properties": {
            "ref-0": {"type": "string", "enum": ["supports", "contradicts", "says_nothing"]},
            "useful": {"type": "string", "enum": ["yes", "no"]},
        },
        "required": ["ref-0", "useful"],
        "additionalProperties": False,
    }


def test_it_runs_with_no_tools_customizations_or_session_in_an_empty_folder(claude: Claude) -> None:
    ClaudeBackend(claude.env).answer("the state", QUESTIONS)

    call = claude.call()
    args = call["args"]
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--output-format") + 1] == "json"
    assert args[args.index("--permission-prompts") + 1] == "none"
    assert {
        "-p",
        "--safe-mode",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    } <= set(args)
    assert call["cwd_listing"] == []


def test_a_failed_call_is_a_backend_error_with_what_claude_said(claude: Claude) -> None:
    (claude.folder / "stderr").write_text("Not logged in\n")

    with pytest.raises(BackendError, match="Not logged in"):
        ClaudeBackend(claude.env).answer("the state", QUESTIONS)


def test_an_error_reported_in_the_reply_is_a_backend_error(claude: Claude) -> None:
    claude.replies(
        {"type": "result", "subtype": "success", "is_error": True, "result": "Invalid API key"}, exit_code=1
    )

    with pytest.raises(BackendError, match="Invalid API key"):
        ClaudeBackend(claude.env).answer("the state", QUESTIONS)


def test_a_failed_run_with_a_reply_that_cannot_be_read_is_a_backend_error(claude: Claude) -> None:
    claude.replies("Traceback (most recent call last):", exit_code=1)

    with pytest.raises(BackendError, match="claude exited 1"):
        ClaudeBackend(claude.env).answer("the state", QUESTIONS)


def test_a_failed_run_says_what_claude_printed_on_stderr_not_its_reply(claude: Claude) -> None:
    claude.replies({"type": "result", "subtype": "success", "is_error": False, "result": "model text"}, exit_code=1)
    (claude.folder / "exit").write_text("1")

    with pytest.raises(BackendError, match="claude exited 1") as raised:
        ClaudeBackend(claude.env).answer("the state", QUESTIONS)
    assert "model text" not in str(raised.value)


def test_a_name_kb_has_no_backend_for_runs_none(claude: Claude) -> None:
    with pytest.raises(ValueError, match="cursor"):
        backend_named("cursor", claude.env)


def test_a_run_that_failed_to_execute_is_a_backend_error(claude: Claude) -> None:
    claude.replies({"type": "result", "subtype": "error_during_execution", "is_error": True})

    with pytest.raises(BackendError, match="error_during_execution"):
        ClaudeBackend(claude.env).answer("the state", QUESTIONS)


@pytest.mark.parametrize("subtype", ["error_max_turns", "error_max_structured_output_retries"])
def test_a_run_the_model_ended_in_error_is_that_notes_invalid_answer(claude: Claude, subtype: str) -> None:
    claude.replies({"type": "result", "subtype": subtype, "is_error": True, "result": "model text"})

    with pytest.raises(InvalidAnswer) as raised:
        ClaudeBackend(claude.env).answer("the state", QUESTIONS)
    assert "model text" not in str(raised.value)


def test_a_missing_claude_is_a_backend_error(tmp_path: Path) -> None:
    with pytest.raises(BackendError, match="claude"):
        ClaudeBackend({"HOME": str(tmp_path), "PATH": str(tmp_path)}).answer("the state", QUESTIONS)


def test_a_call_that_hangs_is_a_backend_error(claude: Claude) -> None:
    (claude.folder / "sleep").write_text("")

    with pytest.raises(BackendError, match="time"):
        ClaudeBackend(claude.env, timeout=0.5).answer("the state", QUESTIONS)


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        "",
        {"type": "result", "subtype": "success", "is_error": False, "result": "no structured output"},
        {"type": "result", "subtype": "error_max_structured_output_retries", "is_error": True},
    ],
    ids=["junk", "empty", "no-structured-output", "schema-retries-spent"],
)
def test_a_reply_without_an_answer_is_an_invalid_answer(claude: Claude, reply: object) -> None:
    claude.replies(reply)

    with pytest.raises(InvalidAnswer):
        ClaudeBackend(claude.env).answer("the state", QUESTIONS)


def test_kb_ingest_uses_claude_where_the_workspace_chose_it(lab: Lab, tmp_path: Path) -> None:
    stub = install_stub(tmp_path)
    lab.kb.backend = None
    lab.kb.env["PATH"] = f"{stub.folder}:{lab.kb.env['PATH']}"
    lab.kb.env["CLAUDE_STUB"] = str(stub.folder)
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(config.read_text() + '\n[ingest]\nbackend = "claude"\n')
    lab.leave("gotcha", "uv needs --cache-dir in the sandbox")
    lab.kb.later(minutes=1)
    lab.leave("fix", "the DB fixture needs a socket; run -m 'not db'")
    lab.kb.later(minutes=1)
    lab.leave("decision", "the wiki stays local, since the machines have no link")

    result = lab.kb("ingest")

    assert result.exit_code == 0, result.stderr
    assert "the wiki stays local" in stub.call()["stdin"]
    assert "### uv needs --cache-dir in the sandbox\n" in lab.show(BRANCH, PAGE)
