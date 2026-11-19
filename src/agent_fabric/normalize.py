"""The normalized event model: what each message did, whichever agent and source it came from (docs/design.md §4.2).

An event is one message, addressed by `(session_id, ordinal)`, and each tool call in it is an action with a kind, the
command a shell call ran, its exit code and whether it failed. Each agent keeps its command and exit code somewhere
else; the table per agent below says where. The kinds come from the categories agentsview gives every agent's tools,
which Copilot CLI's own reader gives too, except for Codex's code mode: one `exec` call runs a script that can call
several tools, and each of those becomes an action of its own.
"""

import json
import re
import shlex
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from agent_fabric.capture import Message, SessionMeta, ToolCall

type Kind = Literal["shell", "edit", "read", "search", "other"]

KINDS: dict[str, Kind] = {
    "Bash": "shell",
    "Edit": "edit",
    "Write": "edit",
    "Read": "read",
    "Grep": "search",
    "Glob": "search",
}
# §4.2's patterns, for a failure that left no status or exit code. They apply to an edit, and to a command only when
# its exit code isn't known: Codex reads files through the shell, and on the devbox's archive these words in source
# code would have failed 229 of its commands that exited 0.
ERRORS = re.compile(
    r"Traceback \(most recent call last\)|\berror:|\bFAILED\b|permission denied|patch did not apply"
    r"|Script failed|apply_patch verification failed|Failed to apply patch",
    re.IGNORECASE,
)
# A command that only shows files prints their text, not errors of its own: no pattern fails it.
READERS = frozenset(
    {"cat", "sed", "rg", "grep", "head", "tail", "less", "wc", "ls", "find", "nl", "tree", "jq", "diff"}
    | {"cd", "pwd", "echo", "printf", "sort", "uniq", "cut", "tr", "stat", "file", "du", "which", "realpath", "true"}
)
GIT_READERS = frozenset({"show", "diff", "log", "grep", "blame", "status"})
# An agent that ends a command with `echo EXIT:$?` (Copilot CLI and Cursor CLI did in the capture check) gets 0 from
# the shell; the code the command really exited with is in its output.
ECHOED_EXIT = re.compile(r"^EXIT:(\d+)\s*$", re.MULTILINE)
ECHOES_EXIT = re.compile(r"\b(?:echo|printf)\b[^\n;&|]*EXIT:[^\n;&|]*\$\?")  # the command prints its own status


@dataclass(frozen=True)
class Action:
    kind: Kind
    tool: str  # the agent's own name: `Bash`, `bash`, `exec_command`, …
    input: str | None
    command: str | None  # a shell call's command line
    exit_code: int | None  # a shell call's, when the agent recorded it; None while it runs or when unknown
    status: str | None  # the last result status the source has: started, completed, errored, …
    output: str | None
    failed: bool


@dataclass(frozen=True)
class Event:
    session_id: str
    ordinal: int
    ts: datetime | None
    role: str
    model: str | None
    text: str | None
    thinking: str | None
    actions: tuple[Action, ...]

    @property
    def failed(self) -> bool:
        return any(action.failed for action in self.actions)


def events(session: SessionMeta, messages: Iterable[Message]) -> list[Event]:
    """The session's messages as events, read by the table of the session's agent."""
    actions = ACTIONS.get(session.agent, _generic)
    return [
        Event(
            session_id=message.session_id,
            ordinal=message.ordinal,
            ts=message.timestamp,
            role=message.role,
            model=message.model,
            text=message.content,
            thinking=message.thinking_text,
            actions=tuple(action for call in message.tool_calls for action in actions(call)),
        )
        for message in messages
    ]


# ── what every agent shares ───────────────────────────────────────────────


def _status(call: ToolCall) -> str | None:
    statuses = [event.status for event in call.result_events if event.status]
    return statuses[-1] if statuses else None


def _action(
    call: ToolCall, *, command: str | None, exit_code: int | None, status: str | None = None, patterns: bool = True
) -> Action:
    kind = KINDS.get(call.category, "other")
    status = status or _status(call)
    output = call.result_content
    # The code a command echoed is its own, over the shell's 0 or no code at all; only when the command echoes `$?`,
    # so that a file showing such a line can't speak for the command that showed it.
    echoes = bool(command and ECHOES_EXIT.search(command))
    if kind == "shell" and exit_code in (0, None) and echoes and (echoed := ECHOED_EXIT.findall(output or "")):
        exit_code = int(echoed[-1])
    # A source that records no status (Cursor) still has the output of a call that finished.
    finished = status not in ("started", "running") and (status is not None or output is not None)
    unexplained = patterns and (kind == "edit" or (kind == "shell" and exit_code is None and not _reads(command)))
    failed = (
        status == "errored"
        or bool(exit_code)
        or (unexplained and finished and bool(output and ERRORS.search(output)))
    )
    return Action(kind, call.tool_name, call.input_json, command, exit_code, status, output, failed)


def _reads(command: str | None) -> bool:
    """Whether every part of the command only shows files: `rg 'a|b' | head` does; `cat a && pytest` and `cat > f`
    don't. A command the shell's quoting can't split doesn't."""
    if "$(" in (command or "") or "`" in (command or ""):
        return False  # a substitution runs a command of its own
    lexer = shlex.shlex(command or "", posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace, lexer.whitespace_split = " \t\r", True
    lexer.commenters = ""  # POSIX shlex ends a word at "#", and its comment would swallow the next line's command
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    parts: list[list[str]] = [[]]
    for index, token in enumerate(tokens):
        if ">" in token and set(token) <= set("<>&\n"):
            target = tokens[index + 1] if index + 1 < len(tokens) else ""
            if target != "/dev/null" and not (token.endswith("&") and target.isdigit()):
                return False  # a redirect writes a file; to /dev/null or another descriptor (2>&1) it doesn't
            if parts[-1] and parts[-1][-1].isdigit():
                parts[-1].pop()  # the descriptor redirected, as the 2 of 2>/dev/null
            continue
        if index and tokens[index - 1] in (">", ">>", ">&", "&>") and (token == "/dev/null" or token.isdigit()):
            continue
        if set(token) <= set("();|&\n"):
            parts.append([])  # shlex groups punctuation that follows itself: `;` and a newline are one token
        elif set(token) <= set("<"):
            continue  # input from a file or here-document: still only shown
        else:
            parts[-1].append(token)
    parts = [words for words in parts if words]
    return bool(parts) and all(_shows(words) for words in parts)


# The options with which a reader runs or changes something.
ACTING = {"find": {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"}, "sed": {"-i"}}


def _shows(words: list[str]) -> bool:
    if words[0] == "git":
        return len(words) > 1 and words[1] in GIT_READERS
    acting = ACTING.get(words[0], set())
    in_place = words[0] == "sed" and any(w.startswith(("-i", "--in-place")) for w in words[1:])
    return words[0] in READERS and not in_place and not acting.intersection(words[1:])


def _arguments(call: ToolCall) -> dict[str, object]:
    try:
        arguments = json.loads(call.input_json or "")
    except ValueError:
        return {}
    return arguments if isinstance(arguments, dict) else {}


def _command(call: ToolCall, *keys: str) -> str | None:
    arguments = _arguments(call)
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list) and value and all(isinstance(word, str) for word in value):
            # An argv, as the oldest Codex `shell` tool gave it: a shell's script is the command, else the words.
            if len(value) == 3 and value[1] in ("-c", "-lc"):
                return str(value[2])
            return shlex.join(value)
    return None


def _code(pattern: re.Pattern[str], text: str | None) -> int | None:
    found = pattern.findall(text or "")
    return int(found[-1]) if found else None


def _unwrapped(content: str | None) -> str | None:
    try:
        answer = json.loads(content or "")
    except ValueError:
        return content
    text = answer.get("content") if isinstance(answer, dict) else None
    return text if isinstance(text, str) else content


def _generic(call: ToolCall) -> Iterator[Action]:
    command = _command(call, "command", "cmd") if KINDS.get(call.category) == "shell" else None
    yield _action(call, command=command, exit_code=call.exit_code)


# ── Claude Code ───────────────────────────────────────────────────────────

CLAUDE_EXIT = re.compile(r"\AExit code (\d+)")


def _claude(call: ToolCall) -> Iterator[Action]:
    if KINDS.get(call.category) != "shell":
        yield _action(call, command=None, exit_code=None)
        return
    # Claude Code starts a failed command's result with `Exit code N`, and says nothing of a successful one's.
    status = _status(call)
    code = _code(CLAUDE_EXIT, call.result_content)
    if code is None and status not in ("errored", "started", "running") and call.result_content is not None:
        code = 0
    yield _action(call, command=_command(call, "command"), exit_code=code)


# ── Copilot CLI ───────────────────────────────────────────────────────────

COPILOT_EXIT = re.compile(r"completed with exit code (\d+)>")


def _copilot(call: ToolCall) -> Iterator[Action]:
    # agentsview keeps Copilot's result as Copilot sent it, `{"content": "…"}`; its own reader keeps the text.
    call = call.model_copy(update={"result_content": _unwrapped(call.result_content)})
    if KINDS.get(call.category) != "shell":
        yield _action(call, command=None, exit_code=None)
        return
    # Its own reader keeps the exit code Copilot records apart; agentsview only has the line Copilot ends output with.
    code = call.exit_code if call.exit_code is not None else _code(COPILOT_EXIT, call.result_content)
    yield _action(call, command=_command(call, "command"), exit_code=code)


# ── Codex ─────────────────────────────────────────────────────────────────

CODEX_EXIT = re.compile(r"Process exited with code (\d+)")
# A script that prints a result its own way, as `[exit_code=1]` on a line of its own.
PRINTED_EXIT = re.compile(r"^\s*\[?exit_code\s*[=:]\s*(\d+)\]?\s*$", re.MULTILINE)
READS_EXIT = re.compile(r"\.exit_code\b")
SCRIPT_CALL = re.compile(r"\btools\.(\w+)\s*\(")
PRINTED_AT_ONCE = re.compile(r"\btext\(\s*await\s*$")  # `text(await tools.x(…))`: printed as it returns
SCRIPT_KINDS: dict[str, Kind] = {"exec_command": "shell", "write_stdin": "shell", "apply_patch": "edit"}


def _codex(call: ToolCall) -> Iterator[Action]:
    if call.tool_name != "exec":
        # Before code mode, a call was the tool itself: `exec_command` with a `cmd`, its exit code in the output.
        command = _command(call, "cmd", "command") if KINDS.get(call.category) == "shell" else None
        # The code is in the call's own lines above `Output:`; below is the command's, which may say anything. The
        # oldest `shell` tool answered in JSON, the code in its `metadata`.
        metadata = (call.result_content or "").split("\nOutput:", 1)[0]
        code = _json_exit(call.result_content)
        yield _action(call, command=command, exit_code=code if code is not None else _code(CODEX_EXIT, metadata))
        return
    yield from _codex_script(call)


def _codex_script(call: ToolCall) -> Iterator[Action]:
    """Codex 0.162+'s code mode: one action per tool the script calls, with its own output where that can be told."""
    script = call.input_json or ""
    in_code = _code_positions(script)
    # A call is one in the script's code, not a name in a string or a comment; its arguments end at its own ")".
    found = [match for match in SCRIPT_CALL.finditer(script) if in_code[match.start()]]
    calls = [(match.group(1), match.end(), _closing(script, in_code, match.end() - 1)) for match in found]
    starts = [match.start() for match in found]
    header, outputs = _script_result(call.result_content)
    status = _status(call)
    # Only a script that reads `.exit_code` prints one: in any other's output, `exit_code=1` is a file's text.
    prints_codes = any(in_code[match.start()] for match in READS_EXIT.finditer(script))
    printed = [code for output in outputs if (code := _printed_code(output, prints_codes)) is not None]
    script_failed = header.startswith("Script failed") or any(printed)
    # The script prints what it likes: often one output per call, a chunk's JSON with its exit code, but also a
    # command's output and then its exit code apart, or nothing for a call. Only the same count pairs them; otherwise
    # each call gets the script's failure, its own being unknown.
    # Equal counts alone don't pair them: a script can print its results in another order, or as calls run
    # together finish. Only when each call prints its own result as it returns do the orders match.
    # A call run together with others (Promise.all) or kept to print later isn't written so, and can't pass.
    at_once = all(PRINTED_AT_ONCE.search(script, 0, match_start) for match_start in starts)
    paired = len(outputs) == len(calls) and (at_once or len(calls) == 1)  # one call and one output can't be mixed
    if not calls:
        yield _action(call, command=None, exit_code=None, status="errored" if script_failed else status)
        return
    for index, (tool, start, end) in enumerate(calls):
        output = outputs[index] if paired else None
        chunk = _chunk(output)
        kind = SCRIPT_KINDS.get(tool, "other")
        command = _argument(script, in_code, start, end, "cmd") if tool == "exec_command" else None
        raw_text = chunk.get("output")
        code = _printed_code(output, prints_codes) if paired else None
        # A chunk without an exit code is a command still running: the script that started it has finished, it hasn't.
        running = bool(chunk) and code is None
        text = raw_text if isinstance(raw_text, str) else output
        last = index == len(calls) - 1
        if not paired and last:
            # What the script printed can't be told apart by call: it all goes on the last, so that a failed script
            # keeps what it said, and no pattern reads it as one command's output.
            text = "\n".join(outputs) or None
        failed_here = header.startswith("Script failed") and last if paired else script_failed
        if failed_here and header.startswith("Script failed"):
            # The script's own error, a `TypeError` or the like, is in the header after its first line.
            text = "\n".join(part for part in (text, header) if part)
        inner = ToolCall(
            tool_use_id=call.tool_use_id,
            tool_name=tool,
            category={"shell": "Bash", "edit": "Edit"}.get(kind, "Other"),
            input_json=script,
            result_content=text,
        )
        inner_status = "errored" if failed_here else "running" if running else status
        yield _action(inner, command=command, exit_code=code, status=inner_status, patterns=paired)


def _json_exit(content: str | None) -> int | None:
    try:
        answer = json.loads(content or "")
    except ValueError:
        return None
    metadata = answer.get("metadata") if isinstance(answer, dict) else None
    code = metadata.get("exit_code") if isinstance(metadata, dict) else None
    return code if isinstance(code, int) else None


def _script_result(content: str | None) -> tuple[str, list[str]]:
    """The header line (`Script completed`, `Script failed`) and the outputs after it."""
    try:
        items = json.loads(content or "")
    except ValueError:
        return content or "", []  # the whole text: a failure's error follows its first line
    if not isinstance(items, list):
        return "", []
    texts = [item["text"] for item in items if isinstance(item, dict) and isinstance(item.get("text"), str)]
    if not texts:
        return "", []
    return texts[0], texts[1:]


def _printed_code(output: str | None, prints_codes: bool) -> int | None:
    """The exit code an output gives: a chunk's (None while the command runs), else one the script printed."""
    chunk = _chunk(output)
    if chunk:
        code = chunk.get("exit_code")
        return code if isinstance(code, int) else None
    return _code(PRINTED_EXIT, output) if prints_codes else None


def _chunk(output: str | None) -> dict[str, object]:
    try:
        chunk = json.loads(output or "")
    except ValueError:
        return {}
    return chunk if isinstance(chunk, dict) and "chunk_id" in chunk else {}


# A backslash escape in a JS string: \xHH, \uHHHH, \u{H…}, or one character (\n, \", \\, a line's end, …).
JS_ESCAPE = re.compile(
    r"\\(?:x(?P<hex>[0-9a-fA-F]{2})|u(?P<hex4>[0-9a-fA-F]{4})|u\{(?P<hexn>[0-9a-fA-F]{1,6})\}|(?P<char>[^xu]))"
)
# Where a "/" starts a regex literal rather than dividing: after one of these, or after one of the keywords below.
REGEX_AFTER = frozenset("(,=:[!&|?{};+-*%<>~^")
CONDITION_KEYWORDS = frozenset({"if", "while", "for", "with"})
REGEX_KEYWORDS = frozenset(
    {"return", "typeof", "case", "in", "of", "void", "delete", "throw", "new", "await", "yield"}
)


def _code_positions(script: str) -> list[bool]:
    """Which of the script's characters are code: not in a string, a template literal's text, a regex literal or a
    comment. The `${…}` in a template literal is code."""
    code = [True] * len(script)
    _scan(script, 0, code, in_template=False)
    return code


def _scan(script: str, index: int, code: list[bool], *, in_template: bool) -> int:
    """Marks what isn't code from `index`, to the script's end or, in a template's `${`, to its closing "}"."""
    depth = 0
    last = -1  # where the last character of code that isn't a space is
    conditions: list[bool] = []  # per open "(": whether it opened an if/while/for/with condition
    condition_closed = -1  # where a ")" closed one: a "/" after it starts a regex
    while index < len(script):
        char = script[index]
        if char == "(":
            conditions.append(_word_before(script, last + 1) in CONDITION_KEYWORDS)
        elif char == ")" and conditions and conditions.pop():
            condition_closed = index
        if in_template and char == "{":
            depth += 1
        elif in_template and char == "}":
            if depth == 0:
                return index
            depth -= 1
        if char == "`":
            end = _template(script, index, code)
        else:
            # Whether a regex may start matters only at a "/", and looking back costs: ask only there.
            regex_may_start = char == "/" and (last == condition_closed or _regex_may_start(script, last))
            found = _not_code_end(script, index, regex_may_start)
            if found is None:
                if not char.isspace():
                    last = index
                index += 1
                continue
            end = found
            code[index:end] = [False] * (end - index)
            if script.startswith(("//", "/*"), index):
                index = end  # a comment is nothing: what came before it still decides what a "/" after it is
                continue
        last = end - 1
        index = end
    return index


def _not_code_end(script: str, index: int, regex_may_start: bool) -> int | None:
    """Where the comment, string or regex literal opening at `index` ends; None if none opens there."""
    if script.startswith("//", index):
        end = script.find("\n", index)
        return len(script) if end < 0 else end
    if script.startswith("/*", index):
        end = script.find("*/", index + 2)
        return len(script) if end < 0 else end + 2
    if script[index] in "\"'":
        end = index + 1
        while end < len(script) and script[end] not in (script[index], "\n"):
            end += 2 if script[end] == "\\" else 1
        return min(end + 1, len(script))
    if script[index] == "/" and regex_may_start:
        return _regex_end(script, index)
    return None


def _template(script: str, index: int, code: list[bool]) -> int:
    """Marks a template literal's text from its opening "`", leaving each `${…}` as code; where it ends."""
    text_from = index
    index += 1
    while index < len(script):
        if script[index] == "\\":
            index += 2
        elif script[index] == "`":
            code[text_from : index + 1] = [False] * (index + 1 - text_from)
            return index + 1
        elif script.startswith("${", index):
            code[text_from : index + 2] = [False] * (index + 2 - text_from)
            index = _scan(script, index + 2, code, in_template=True)
            text_from = index  # the closing "}" is the template's again
            index += 1
        else:
            index += 1
    code[text_from:] = [False] * (len(script) - text_from)
    return len(script)


def _regex_may_start(script: str, last: int) -> bool:
    return last < 0 or script[last] in REGEX_AFTER or _word_before(script, last + 1) in REGEX_KEYWORDS


def _word_before(script: str, end: int) -> str:
    """The identifier that ends at `end`, if one does; looks back no further than the word itself."""
    start = end
    while start > 0 and (script[start - 1].isalnum() or script[start - 1] in "_$"):
        start -= 1
    word = script[start:end]
    return word if word and not word[0].isdigit() else ""


def _regex_end(script: str, index: int) -> int | None:
    """Where the regex literal opening at `index` ends, its flags included; None if it isn't one."""
    in_class = False
    position = index + 1
    while position < len(script):
        char = script[position]
        if char == "\n":
            return None
        if char == "\\":
            position += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            position += 1
            while position < len(script) and script[position].isalpha():
                position += 1
            return position
        position += 1
    return None


def _closing(script: str, code: list[bool], opening: int) -> int:
    """Where the bracket at `opening` closes, counting only brackets in code; the script's end if it doesn't."""
    depth = 0
    for index in range(opening, len(script)):
        if not code[index]:
            continue
        if script[index] in "([{":
            depth += 1
        elif script[index] in ")]}":
            depth -= 1
            if depth == 0:
                return index
    return len(script)


def _argument(script: str, code: list[bool], start: int, end: int, key: str) -> str | None:
    """The string literal of `key` in the object literal the call's arguments (`start` to `end`) begin with."""
    opening = start + len(script[start:end]) - len(script[start:end].lstrip())
    if opening >= end or script[opening] != "{":
        return None  # a variable or an expression: no one command line to give
    closing = _closing(script, code, opening)
    depth = 0
    pattern = re.compile(rf"(?<![\w$]){key}\s*:\s*")
    quoted = re.compile(rf"([\"']){key}\1\s*:\s*")  # `{"cmd": …}`, a key written as a string
    for index in range(opening, closing):
        if not code[index]:
            starts_string = script[index] in "\"'" and code[index - 1]
            if depth == 1 and starts_string and (match := quoted.match(script, index)):
                return _whole_literal(script, match.end(), closing)
            continue
        if script[index] in "([{":
            depth += 1
        elif script[index] in ")]}":
            depth -= 1
        elif depth == 1 and (match := pattern.match(script, index)):
            return _whole_literal(script, match.end(), closing)
    return None


def _whole_literal(script: str, index: int, limit: int) -> str | None:
    """The literal at `index` when it is the property's whole value; `"cat " + path` gives no command."""
    value, end = _literal(script, index, limit)
    rest = script[end:limit].lstrip()
    return value if value is not None and (rest == "" or rest[0] in ",}") else None


def _literal(script: str, index: int, limit: int) -> tuple[str | None, int]:
    """The JS string literal at `index`: "…", '…' or `…` without ${}; None for anything else."""
    if index >= limit or script[index] not in "\"'`":
        return None, index
    quote = script[index]
    out: list[str] = []
    escapes = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b", "f": "\f", "v": "\v"}
    index += 1
    while index < limit:
        char = script[index]
        if char == quote:
            return "".join(out), index + 1
        if char == "\\" and index + 1 < limit:
            escape = JS_ESCAPE.match(script, index, limit)
            if escape is None:
                return None, index  # an escape JS would refuse, or one this can't read: no command to give
            hexadecimal = escape.group("hex") or escape.group("hex4") or escape.group("hexn")
            if hexadecimal:
                if int(hexadecimal, 16) > 0x10FFFF:
                    return None, index
                out.append(chr(int(hexadecimal, 16)))
            elif escape.group("char") != "\n":  # a line continuation is nothing
                out.append(escapes.get(escape.group("char"), escape.group("char")))
            index = escape.end()
            continue
        if quote == "`" and script.startswith("${", index):
            return None, index  # a template with values in it has no one command line to give
        out.append(char)
        index += 1
    return None, index


# The table per agent, by agentsview's agent name; an agent without one gets the generic reading.
ACTIONS = {"claude": _claude, "codex": _codex, "copilot": _copilot}
