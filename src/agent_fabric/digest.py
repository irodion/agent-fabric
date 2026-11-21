"""A session's digest: what it did, from its messages and git snapshots, with no model (docs/design.md §4.4).

`render` is a pure function of what it's given, so the same session renders the same bytes, and a reopened session
rewrites its file only where something changed. It doesn't redact, nor turn the home folder into `~`: whoever
writes the digest does both (§4.5). Every cut
leaves a pointer to the full text in the local archive (`kb show <session>#<ordinal>`); typed prompt text is never
cut, while a pasted block in a prompt is.
"""

import hashlib
import itertools
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from agent_fabric.capture import Message, SessionMeta
from agent_fabric.normalize import ERRORS, Action, events, shows_files
from agent_fabric.snapshot import Snapshot, parse

SCHEMA = "fabric.digest/1"
HARD_CAP = 200_000  # bytes; sections are cut to fit, the front matter never
PASTED = 40  # a block of a prompt longer than this many lines was pasted, not typed
PASTED_KEEP = 10  # lines kept of each end of a pasted block
DIFF_LINES = 200
ERROR_TAIL = 30  # lines of output kept with an error
MAX_ERRORS = 20
MAX_COMMAND = 300  # characters of a command shown in the table
MAX_MARKER = 200
MAX_FILES = 200  # files named in the front matter, the stat and the untracked list
# Over the cap, sections are cut in this order; prompts never, as typed text isn't cut.
CUT_ORDER = ("Commands", "Changes", "Tests", "Errors", "Commits", "Markers", "Final message")
IDLE = timedelta(minutes=5)  # a gap longer than this between two messages isn't active time

# A command's kind, by what one of its commands starts with: after `&&`, `;`, a pipe or a wrapper (`uv run`, `npx`).
# Each pattern ends at a word's end, so `pytest.ini` isn't pytest, and `uv add pytest` is an install.
END = r"(?=\s|$)"
COMMAND_KINDS = (
    (
        "test",
        re.compile(
            r"(?:pytest|py\.test|jest|vitest|mocha|tox|nox|rspec|phpunit|ctest|go test|cargo (?:test|nextest)"
            r"|(?:npm|pnpm|yarn|bun) (?:run )?test|make (?:test|check)|python3?(?:\.\d+)? -m (?:pytest|unittest))"
            + END
        ),
    ),
    (
        "lint",
        re.compile(
            r"(?:ruff|eslint|biome|mypy|pyright|cargo clippy|shellcheck|shfmt|golangci-lint|prettier|flake8|pylint"
            r"|actionlint|semgrep|gitleaks|tsc --noEmit|(?:npm|pnpm|yarn|bun) (?:run )?lint)" + END
        ),
    ),
    (
        "build",
        re.compile(
            r"(?:cargo (?:build|check)|go build|tsc|make|cmake|uv build|gradle|mvn|docker build"
            r"|(?:npm|pnpm|yarn|bun) (?:run )?build)" + END
        ),
    ),
    (
        "install",
        re.compile(
            r"(?:pip3? install|uv (?:add|sync|pip install|tool install)|(?:npm|pnpm|yarn|bun) (?:install|add|i|ci)"
            r"|cargo (?:add|install)|go (?:get|install)|apt(?:-get)? install|brew install|poetry (?:add|install))"
            + END
        ),
    ),
    ("git", re.compile(r"git" + END)),
)
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
SEPARATORS = re.compile(r"&&|\|\||[;|&(){}\n]")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")
# What runs the command after it, past its own options: `uv run --frozen pytest`, `timeout 60 pytest`.
WRAPPERS = re.compile(
    r"(?:uv (?:tool )?run|uvx|npx|bunx|pnpm (?:exec|dlx)|npm exec|yarn (?:dlx|exec)|poetry run|pdm run|hatch run"
    r"|pipenv run|rye run|timeout|time|env|nice|nohup|xvfb-run)" + END
)

MARKERS = (
    ("todo", re.compile(r"\b(?:TODO|FIXME|HACK|XXX)\b")),
    (
        "skip",
        re.compile(
            r"pytest\.mark\.(?:skip|xfail)|pytest\.skip\(|@unittest\.skip|\b(?:it|test|describe)\.(?:skip|todo)\("
            r"|\bx(?:it|describe)\(|\bt\.Skip(?:Now|f)?\(|#\[ignore\]|@Disabled\b"
        ),
    ),
    (
        "suppression",
        re.compile(
            r"#\s*noqa\b|type:\s*ignore|pyright:\s*ignore|eslint-disable|@ts-(?:ignore|expect-error|nocheck)"
            r"|//\s*nolint\b|#\[allow\(|biome-ignore|pylint:\s*disable|rubocop:disable|NOSONAR"
            r"|shellcheck disable|gitleaks:allow|#\s*nosec\b"
        ),
    ),
)
FENCE = re.compile(r"\s*(`{3,}|~{3,})(?!.*\1)")  # an opening or closing fence, alone
EXIT_HEADER = re.compile(r"Exit code \d+\s*$")
HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,\d+)? @@")

# What each test runner prints of its counts and its failures; a package script (`pnpm test`) may run any of them.
COUNT = re.compile(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|todo)\b")
RUNNERS = (
    ("pytest", re.compile(r"(?:pytest|py\.test|tox|nox|python3?(?:\.\d+)? -m pytest)" + END)),
    ("cargo test", re.compile(r"cargo (?:test|nextest)" + END)),
    ("go test", re.compile(r"go test" + END)),
    ("vitest", re.compile(r"vitest" + END)),
    ("jest", re.compile(r"jest" + END)),
)
COUNTED_AS: dict[str, str] = {
    "error": "failed",
    "errors": "failed",
    "xfailed": "skipped",
    "xpassed": "passed",
    "todo": "skipped",
}
PYTEST_SUMMARY = re.compile(
    r"^=*\s*((?:\d+ (?:passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)(?:, )?)+) in [\d.]+s\b",
    re.MULTILINE,
)
PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
CARGO_RESULT = re.compile(r"^test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored", re.MULTILINE)
CARGO_FAILED = re.compile(r"^test (\S+) \.\.\. FAILED$", re.MULTILINE)
GO_TESTS = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)", re.MULTILINE)
GO_PACKAGES = re.compile(r"^(ok|FAIL)\s+\S+\s+(?:[\d.]+s|\(cached\))", re.MULTILINE)
JEST_SUMMARY = re.compile(r"^Tests:\s+(\d+ \w+(?:, \d+ \w+)*)", re.MULTILINE)
JEST_FAILED = re.compile(r"^\s*● (.+?)\s*$", re.MULTILINE)
VITEST_SUMMARY = re.compile(r"^\s*Tests\s+(\d+ \w+(?: \| \d+ \w+)*)", re.MULTILINE)
VITEST_FAILED = re.compile(r"^\s*(?:FAIL|\u00d7|\u2717)\s+(.+?)\s*$", re.MULTILINE)  # FAIL, a cross or a ballot X


@dataclass(frozen=True)
class Tests:
    runner: str
    ordinal: int
    passed: int | None
    failed: int | None
    skipped: int | None
    failing: tuple[str, ...]
    ok: bool | None  # None: neither its counts nor its exit code say


@dataclass(frozen=True)
class _Section:
    title: str
    head: tuple[str, ...]  # lines that stay
    entries: tuple[str, ...]  # cut from the front when the digest is over its cap
    cut: str = "[… {n} earlier entries cut: kb show {session}]"  # what a cut says


def path(meta: SessionMeta, project: str, start: datetime) -> str:
    """`raw/sessions/<project>/<yyyy>/<mm>/<yyyy-mm-dd>-<agent>-<shortid>.md`, inside the wiki or the local folder."""
    when = start.astimezone(UTC)
    folder = "/".join(_segment(part) for part in project.split("/") if part.strip("./"))
    short = _segment(meta.id.rsplit(":", 1)[-1][:8])
    return f"raw/sessions/{folder or 'unknown'}/{when:%Y/%m}/{when:%Y-%m-%d}-{_segment(meta.agent)}-{short}.md"


def render(
    meta: SessionMeta,
    messages: Sequence[Message],
    snapshots: Sequence[Snapshot],
    *,
    project: str,
    revision: int,
) -> str:
    """The digest's Markdown: front matter, then §4.4's sections in order."""
    said = [message for message in messages if not message.is_system]
    found = events(meta, said)
    actions = [(event.ordinal, action) for event in found for action in event.actions]
    shells = [(ordinal, action) for ordinal, action in actions if action.kind == "shell"]
    prompts = [message for message in said if message.role == "user"]
    final = _final(said)
    tests = [run for ordinal, action in shells if (run := _tests(ordinal, action)) is not None]
    first, last = (snapshots[0], snapshots[-1]) if snapshots else (None, None)
    stamps = sorted(_stamps(said))
    start = stamps[0] if stamps else _utc(meta.started_at)
    end = stamps[-1] if stamps else _utc(meta.ended_at)
    front = {
        "schema": SCHEMA,
        "session": meta.id,
        "agent": meta.agent,
        "project": project,
        "cwd": meta.cwd,
        "branch": (last.branch if last else None) or meta.git_branch,
        "worktree": _worktree(first),
        "head": {"start": last.base[:7], "end": last.head[:7]} if last else None,
        "time": {"start": _when(start), "end": _when(end), "active_min": _active_minutes(stamps)},
        "models": list(dict.fromkeys(message.model for message in said if message.model)),
        "outcome": {
            "last_tests": None if not tests or tests[-1].ok is None else "passed" if tests[-1].ok else "failed",
            "uncommitted_changes": last.uncommitted if last else None,
            "ends_with_question": bool(final and final.content and final.content.rstrip().endswith("?")),
        },
        "counts": {
            "prompts": len(prompts),
            "tool_calls": len(actions),
            "commands": len(shells),
            "edits": sum(action.kind == "edit" for _, action in actions),
            "errors": sum(action.failed for _, action in actions),
        },
        "files_modified": list(last.files[:MAX_FILES]) if last else [],
        **({"files_unlisted": len(last.files) - MAX_FILES} if last and len(last.files) > MAX_FILES else {}),
        "revision": revision,
    }
    sections = [
        _Section("Prompts", (), tuple(_prompt(meta.id, message) for message in prompts)),
        _commands(meta.id, shells),
        _Section(
            "Tests",
            (),
            tuple(_test_line(found) for found in tests),
            "[… {n} earlier test runs cut: kb show {session}]",
        ),
        _errors(meta.id, actions),
        _changes(first, last),
        _Section(
            "Commits",
            (),
            tuple(f"- {commit}" for commit in last.commits) if last else (),
            "[… {n} earlier commits cut]",
        ),
        _Section("Markers", (), tuple(_markers(last.diff)) if last else (), "[… {n} earlier markers cut]"),
        _Section(
            "Final message",
            (),
            (_quoted(final.content),) if final and final.content else (),
            "[the final message cut to fit: kb show {session}#" + str(final.ordinal if final else "") + "]",
        ),
    ]
    head = "---\n" + "".join(f"{key}: {_yaml(value)}\n" for key, value in front.items()) + "---\n"
    return head + _fit(sections, HARD_CAP - len(head.encode()), meta.id)


# ── sections ──────────────────────────────────────────────────────────────


def _prompt(session_id: str, message: Message) -> str:
    """Its time, then its text verbatim and quoted, so no line of it reads as the digest's own structure."""
    text = _collapse(message.content or "", f"kb show {session_id}#{message.ordinal}")
    return f"### {_when(_utc(message.timestamp))} · #{message.ordinal}\n{_quoted(text)}"


def _collapse(text: str, pointer: str) -> str:
    """Each block of more than `PASTED` lines, a fenced one or one between blank lines, to its two ends."""
    out: list[str] = []
    for block in _blocks(text.split("\n")):
        if len(block) > PASTED:
            cut = len(block) - 2 * PASTED_KEEP
            out += [*block[:PASTED_KEEP], f"[… {cut} lines of a pasted block cut: {pointer}]", *block[-PASTED_KEEP:]]
        else:
            out += block
    return "\n".join(out)


def _blocks(lines: list[str]) -> Iterable[list[str]]:
    """A fenced block whole, from a fence to the next one; otherwise lines between blank lines. A line that opens and
    closes a fence (```x```) is no fence, and nor is a last fence with none to close it."""
    fences = [i for i, line in enumerate(lines) if FENCE.match(line)]
    closing = dict(zip(fences[::2], fences[1::2], strict=False))
    block: list[str] = []
    index = 0
    while index < len(lines):
        if index in closing:
            if block:
                yield block
            yield lines[index : closing[index] + 1]
            block, index = [], closing[index] + 1
            continue
        if lines[index].strip():
            block.append(lines[index])
        else:
            if block:
                yield block
            yield [lines[index]]
            block = []
        index += 1
    if block:
        yield block


def _commands(session_id: str, shells: list[tuple[int, Action]]) -> _Section:
    rows = []
    for ordinal, action in shells:
        command = action.command or action.input or ""
        if len(command) > MAX_COMMAND:
            command = command[:MAX_COMMAND] + f"… (kb show {session_id}#{ordinal})"
        code = str(action.exit_code) if action.exit_code is not None else action.status or ""
        error = _first_error(action.output) if action.failed else ""
        kind = "test" if _tests(ordinal, action) else _kind(action.command)
        rows.append(f"| #{ordinal} | {_cell(command)} | {code} | {kind} | {_cell(error)} |")
    head = ("| # | command | exit | kind | first error line |", "|---|---|---|---|---|") if rows else ()
    return _Section("Commands", head, tuple(rows), "[… {n} earlier commands cut: kb show {session}]")


def _kind(command: str | None) -> str:
    return _run(command)[0]


def _run(command: str | None) -> tuple[str, str]:
    """The command's kind, and the command that gave it from where its program starts."""
    for segment in SEPARATORS.split(QUOTED.sub("''", command or "")):
        words = segment.split()
        while words:
            while words and (ASSIGNMENT.fullmatch(words[0]) or words[0] in ("sudo", "command", "exec")):
                words = words[1:]
            if not words:
                break
            words = [words[0].rsplit("/", 1)[-1], *words[1:]]  # `.venv/bin/pytest` runs pytest
            if wrapper := WRAPPERS.match(" ".join(words)):
                words = _past_options(words[len(wrapper[0].split()) :])
                continue
            text = " ".join(words)
            for kind, pattern in COMMAND_KINDS:
                if pattern.match(text):
                    return kind, text
            break
    return "other", ""


def _past_options(words: list[str]) -> list[str]:
    """A wrapper's words from the program it runs: past its options, a timeout's duration and `X=1`. A long option
    with no `=` may take the next word as its value (`--with pytest`), so that word goes too."""
    index = 0
    while index < len(words):
        word = words[index]
        if word.startswith("--") and "=" not in word:
            index += 2
        elif word.startswith("-") or ASSIGNMENT.fullmatch(word) or word[:1].isdigit():
            index += 1
        else:
            break
    return words[index:]


def _tests(ordinal: int, action: Action) -> Tests | None:
    """A test run's counts and failures, by what its runner prints; None for a command that runs no tests.

    A runner's summary in the output makes a test run, however the command started it; a command that names a runner
    is one even without a summary, as when it crashed first. A command that only shows files, such as `cat ci.log`,
    is none, whatever the file says."""
    if shows_files(action.command):
        return None
    out = action.output or ""
    found = _summary(out)
    kind, run = _run(action.command)
    if found is None and kind != "test":
        return None
    runner, counts, failing = found or (next((n for n, p in RUNNERS if p.match(run)), "tests"), {}, [])
    if counts:
        ok: bool | None = not counts.get("failed") and not action.failed
    else:
        ok = False if action.failed else True if action.exit_code == 0 else None
    return Tests(
        runner,
        ordinal,
        counts.get("passed"),
        counts.get("failed"),
        counts.get("skipped"),
        tuple(dict.fromkeys(failing)),
        ok,
    )


def _summary(out: str) -> tuple[str, dict[str, int], list[str]] | None:
    """The runner whose summary the output holds, with its counts and failing tests."""
    if summaries := PYTEST_SUMMARY.findall(out):
        return "pytest", _counts(summaries[-1]), PYTEST_FAILED.findall(out)
    if results := CARGO_RESULT.findall(out):
        counts = {name: sum(int(r[i]) for r in results) for i, name in enumerate(("passed", "failed", "skipped"))}
        return "cargo test", counts, CARGO_FAILED.findall(out)
    if GO_PACKAGES.search(out):
        runs = GO_TESTS.findall(out)
        failing = [name for state, name in runs if state == "FAIL"]
        packages_failed = any(state == "FAIL" for state in GO_PACKAGES.findall(out))
        counts = {
            "passed": sum(state == "PASS" for state, _ in runs),
            "failed": max(len(failing), int(packages_failed)),
            "skipped": sum(state == "SKIP" for state, _ in runs),
        }
        return "go test", counts, failing
    if summaries := VITEST_SUMMARY.findall(out):
        return "vitest", _counts(summaries[-1]), VITEST_FAILED.findall(out)
    if summaries := JEST_SUMMARY.findall(out):
        failing = [name for name in JEST_FAILED.findall(out) if not name.startswith("Console")]
        return "jest", _counts(summaries[-1]), failing
    return None


def _counts(summary: str) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for number, word in COUNT.findall(summary):
        counts[COUNTED_AS.get(word) or word] += int(number)
    return counts


def _test_line(found: Tests) -> str:
    counts = ", ".join(
        f"{value} {name}"
        for name, value in (("passed", found.passed), ("failed", found.failed), ("skipped", found.skipped))
        if value is not None
    )
    verdict = {True: "passed", False: "failed", None: "no result"}[found.ok]
    line = f"- #{found.ordinal} {found.runner}: {verdict}" + (f" ({counts})" if counts else "")
    return line + (f"; failing: {', '.join(found.failing)}" if found.failing else "")


def _errors(session_id: str, actions: list[tuple[int, Action]]) -> _Section:
    """One entry per distinct failure, the last `MAX_ERRORS` of them, each with its output's end."""
    distinct: dict[str, tuple[int, Action]] = {}
    for ordinal, action in actions:
        if action.failed:
            key = hashlib.sha256(
                json.dumps([action.tool, action.command or action.input, action.output]).encode()
            ).hexdigest()
            distinct.pop(key, None)
            distinct[key] = (ordinal, action)
    entries = []
    kept = list(distinct.values())[-MAX_ERRORS:]
    for ordinal, action in kept:
        what = action.command or action.tool
        status = f"exit {action.exit_code}" if action.exit_code is not None else action.status or "failed"
        lines = (action.output or "").rstrip("\n").split("\n")
        tail = "\n".join(lines[-ERROR_TAIL:])
        cut = f"\n[… {len(lines) - ERROR_TAIL} lines before: kb show {session_id}#{ordinal}]" * (
            len(lines) > ERROR_TAIL
        )
        entry = f"- #{ordinal} {action.kind} `{_cell(what[:MAX_COMMAND])}` · {status}: {_first_error(action.output)}"
        entries.append(entry + cut + ("\n" + _fenced(tail) if tail else ""))
    head = (f"{len(distinct) - len(kept)} earlier errors not shown.",) if len(distinct) > len(kept) else ()
    return _Section("Errors", head, tuple(entries), "[… {n} earlier errors cut: kb show {session}]")


def _changes(first: Snapshot | None, last: Snapshot | None) -> _Section:
    """The working tree from first sight to the last snapshot: whoever changed it, the agent or someone by hand."""
    if first is None or last is None:
        return _Section("Changes", ("Not in a git repo, or no snapshot was taken.",), ())
    head = [f"Working tree during the session, {last.base[:7]} → {last.head[:7]} and uncommitted changes."]
    if first.uncommitted:
        head.append("It already had uncommitted changes when the session was first seen.")
    if last.stat:
        stat = last.stat.split("\n")
        if len(stat) > MAX_FILES + 1:  # its last line sums it up
            stat = [*stat[:MAX_FILES], f"… {len(stat) - MAX_FILES - 1} more files", stat[-1]]
        head.append(_fenced("\n".join(stat)))
    if last.untracked:
        more = len(last.untracked) - MAX_FILES
        names = ", ".join(f"`{_cell(name)}`" for name in last.untracked[:MAX_FILES])
        head.append("Untracked: " + names + (f", and {more} more" if more > 0 else ""))
    lines = last.diff.rstrip("\n").split("\n") if last.diff else []
    entries: tuple[str, ...] = ()
    if lines:
        shown = "\n".join(lines[:DIFF_LINES])
        more = len(lines) - DIFF_LINES
        note = f"\n[… {more} more lines of the diff cut]" if more > 0 or last.diff_cut else ""
        entries = (_fenced(shown, "diff") + note,)
    if not last.stat and not last.untracked:
        head.append("No changes.")
    return _Section("Changes", tuple(head), entries, "[the diff cut to fit the digest]")


def _markers(diff: str) -> Iterable[str]:
    """TODO/FIXME/HACK, skipped tests and lint suppressions on lines the diff adds."""
    for found in parse(diff):
        line = 0
        for text in found.lines:
            if match := HUNK.match(text):
                line = int(match[1])
            elif text.startswith("+"):
                added = text[1:]
                for kind, pattern in MARKERS:
                    if pattern.search(added):
                        shown = added.strip()[:MAX_MARKER]
                        yield f"- {kind} · `{_cell(found.path)}:{line}` · `{_cell(shown)}`"
                        break
                line += 1


# ── helpers ───────────────────────────────────────────────────────────────


def _fit(sections: list[_Section], room: int, session_id: str) -> str:
    """The sections, each cut from its front until the whole fits; the prompts last, since typed text isn't cut."""
    entries = {section.title: list(section.entries) for section in sections}
    cut = dict.fromkeys(entries, 0)

    def body() -> str:
        out = []
        for section in sections:
            lines = list(section.head)
            if cut[section.title]:
                lines.append(section.cut.format(n=cut[section.title], session=session_id))
            lines += entries[section.title]
            out.append(f"## {section.title}\n" + ("\n".join(lines) + "\n" if lines else ""))
        return "\n".join(out)

    text = body()
    for title in CUT_ORDER:
        while len(text.encode()) > room and entries[title]:
            over = len(text.encode()) - room
            # Drop as many entries from the front as the excess needs, at least one.
            drop, size = 0, 0
            while drop < len(entries[title]) and size < over:
                size += len(entries[title][drop].encode()) + 1
                drop += 1
            del entries[title][:drop]
            cut[title] += drop
            text = body()
    return text


def _final(said: Sequence[Message]) -> Message | None:
    """The agent's last answer: text, calling no tool."""
    return next(
        (m for m in reversed(said) if m.role == "assistant" and m.content and not m.tool_calls),
        None,
    )


def _first_error(output: str | None) -> str:
    # Not the `Exit code N` Claude Code starts a failed command's output with: the exit column has it.
    lines = [line.strip() for line in (output or "").split("\n") if line.strip() and not EXIT_HEADER.match(line)]
    found = next((line for line in lines if ERRORS.search(line)), lines[0] if lines else "")
    return found[:MAX_COMMAND]


def _stamps(messages: Iterable[Message]) -> Iterable[datetime]:
    for message in messages:
        if message.timestamp:
            yield _utc(message.timestamp)
        for call in message.tool_calls:
            for event in call.result_events:
                if event.timestamp:
                    yield _utc(event.timestamp)


def _active_minutes(stamps: list[datetime]) -> int:
    gaps = (later - earlier for earlier, later in itertools.pairwise(stamps))
    return round(sum((gap for gap in gaps if gap <= IDLE), timedelta()).total_seconds() / 60)


def _worktree(first: Snapshot | None) -> dict[str, str] | None:
    if first is None or first.worktree is None or first.main is None:
        return None
    return {"path": str(first.worktree), "main": str(first.main)}


def _utc(stamp: datetime | None) -> datetime:
    if stamp is None:
        return datetime(1970, 1, 1, tzinfo=UTC)  # a session with no times at all: Cursor CLI's in agentsview
    return stamp.astimezone(UTC) if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _when(stamp: datetime) -> str:
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _yaml(value: object) -> str:
    r"""YAML in flow style: JSON for every scalar and list, which YAML reads as it is, with bare keys for a map.

    Only printable ASCII is left as it is. JSON leaves DEL and C1 controls raw, which YAML forbids, and writes a
    character past U+FFFF as two `\u` halves, which YAML reads as two broken characters; YAML's own `\U` is one."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key}: {_yaml(item)}" for key, item in value.items()) + "}"
    return "".join(_escape(char) for char in json.dumps(value, ensure_ascii=False))


def _escape(char: str) -> str:
    code = ord(char)
    if code < 0x7F:
        return char  # JSON has escaped the controls below 0x20 already
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def _segment(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return cleaned or "unknown"


def _cell(text: str) -> str:
    """One table cell or inline code: one line, no pipe or backtick to end it early."""
    return text.replace("|", "\\|").replace("`", "'").replace("\r", "").replace("\n", " ⏎ ")


def _quoted(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))


def _fenced(text: str, info: str = "") -> str:
    """A fence longer than any run of backticks in the text, so the text can't close it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n{text}\n{fence}"
