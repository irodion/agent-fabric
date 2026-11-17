"""kb's own settings in ~/.config/agent-fabric/config.toml (docs/design.md §4.10, §5).

`[notes] hooks`, which of the agents' hooks `kb doctor` installs, and every workspace's settings (§4.11): the
default's under `[publish]` and `[ingest]`, a root's in its `[[workspace]]` entry. kb edits the file the way it edits
Codex's config: it changes or adds only its own lines, and writes only if the result reads back as intended."""

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from agent_fabric.paths import config_file, expand, tilde
from agent_fabric.sandboxes import replace_file

HOOKS = ("start", "stop")
NEXT_TABLE = re.compile(r"^[ \t]*\[", re.MULTILINE)
WORKSPACE_HEADER = re.compile(r"^[ \t]*\[\[[ \t]*workspace[ \t]*\]\][^\r\n]*(?:\r?\n|\Z)", re.MULTILINE)


def _recorded(text: str) -> tuple[str, ...] | None:
    """The choice the config's text records; None where it records none, ValueError where it can't be read."""
    notes: Any = tomllib.loads(text).get("notes", {})
    if not isinstance(notes, dict):
        raise ValueError("notes is not a table")
    if "hooks" not in notes:
        return None
    chosen = notes["hooks"]
    if not isinstance(chosen, list) or not all(isinstance(hook, str) and hook in HOOKS for hook in chosen):
        raise ValueError(f"notes.hooks must list some of: {', '.join(HOOKS)}")
    return tuple(hook for hook in HOOKS if hook in chosen)


def _text(env: Mapping[str, str]) -> str:
    path = config_file(env)
    return path.read_bytes().decode("utf-8") if path.exists() else ""  # bytes: keeps CRLF line endings


def hooks_choice(env: Mapping[str, str]) -> tuple[str, ...]:
    """The recorded choice of hooks; both when nothing is recorded."""
    recorded = _recorded(_text(env))
    return HOOKS if recorded is None else recorded


def setting(env: Mapping[str, str], root: Path | None, table: str, key: str) -> str:
    """Where a workspace's setting is, for a message: `[publish] allow`, or `allow in the [[workspace]] for ~/x`."""
    return f"[{table}] {key}" if root is None else f"{key} in the [[workspace]] for {tilde(root, env)}"


def _settings(env: Mapping[str, str], root: Path | None, table: str) -> dict[str, Any]:
    """A workspace's settings: the default's under `[table]`, a root's in the `[[workspace]]` entry whose root is
    `root` (resolved), the first where two name it (docs/design.md §4.11). ValueError where the file can't be read, or
    has no entry for `root`: defaults in its place could send a client's notes to another backend."""
    data: Any = tomllib.loads(_text(env))
    if root is None:
        found = data.get(table, {})
    else:
        entries = zip(workspace_roots(env), data.get("workspace", []), strict=True)
        found = next((entry for written, entry in entries if expand(written, env).resolve() == root), None)
        if found is None:
            raise ValueError(f"no [[workspace]] has the root {tilde(root, env)}")
    if not isinstance(found, dict):
        raise ValueError(f"{table} is not a table")
    return found


def allowed_projects(env: Mapping[str, str], root: Path | None = None) -> tuple[str, ...]:
    """The workspace's allow list of project ids (docs/design.md §4.5). It starts empty: nothing is published until a
    project is added. ValueError where the file can't be read."""
    allow = _settings(env, root, "publish").get("allow", [])
    if not isinstance(allow, list) or not all(isinstance(project, str) for project in allow):
        raise ValueError(f"{setting(env, root, 'publish', 'allow')} must be a list of project ids")
    return tuple(allow)


def ingest_backend(env: Mapping[str, str], root: Path | None = None) -> str:
    """The workspace's backend (docs/design.md §4.7): the default's under `[ingest]`; `codex` where none is set.
    ValueError where the file can't be read."""
    backend = _settings(env, root, "ingest").get("backend", "codex")
    if not isinstance(backend, str):
        raise ValueError(f"{setting(env, root, 'ingest', 'backend')} must be a name, such as codex")
    return backend


def workspace_roots(env: Mapping[str, str]) -> tuple[str, ...]:
    """The roots of the `[[workspace]]` entries, as written (docs/design.md §4.11). ValueError where the file can't be
    read."""
    entries: Any = tomllib.loads(_text(env)).get("workspace", [])
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("root"), str) for e in entries
    ):
        raise ValueError("each [[workspace]] needs a root, a folder")
    roots = tuple(entry["root"] for entry in entries)
    if relative := [root for root in roots if not expand(root, env).is_absolute()]:
        raise ValueError(f"a [[workspace]] root is a full path or starts with ~/, not {relative[0]!r}")
    return roots


def machine_opt_outs(env: Mapping[str, str]) -> tuple[str | None, ...]:
    """The workspaces with `share_machine_notes = false` (docs/design.md §4.11): roots as written, and None for the
    default workspace, whose setting is under `[publish]`. Sharing is on by default. ValueError where the file can't
    be read."""
    data: Any = tomllib.loads(_text(env))
    publish = data.get("publish", {})
    if not isinstance(publish, dict):
        raise ValueError("publish is not a table")
    entries = [publish, *data.get("workspace", [])]
    roots = (None, *workspace_roots(env))
    shares = [entry.get("share_machine_notes", True) for entry in entries]
    if not all(isinstance(share, bool) for share in shares):
        raise ValueError("share_machine_notes is true or false")
    return tuple(root for root, share in zip(roots, shares, strict=True) if not share)


def record_hooks(env: Mapping[str, str], chosen: tuple[str, ...]) -> None:
    """Record the choice: change the `hooks` line under [notes], or add it; ValueError where that can't do it."""
    replace_file(config_file(env), _hooks_text(_text(env), chosen))


def _hooks_text(text: str, chosen: tuple[str, ...]) -> str:
    """The config's text with the choice recorded; ValueError where that can't be done by changing one line."""
    nl = _newline(text)
    if (body := _table(text, "notes")) is None:
        gap = "" if not text else nl if text.endswith("\n") else nl * 2
        new = f"{text}{gap}[notes]{nl}hooks = {_toml(list(chosen))}{nl}"
    else:
        start, end, replacement = _set_key(text, body, "hooks", list(chosen), nl)
        new = text[:start] + replacement + text[end:]
    try:
        recorded = _recorded(new)
    except ValueError:  # TOMLDecodeError too
        recorded = None
    if recorded != chosen:
        raise ValueError(f"can't record the choice; set hooks = {json.dumps(list(chosen))} under [notes] by hand")
    return new


@dataclass(frozen=True)
class Settings:
    """One workspace's choices in `kb setup` (docs/design.md §4.11), each its default where nothing is recorded."""

    allow: tuple[str, ...] = ()
    backend: str = "codex"
    share_machine_notes: bool = True


Edit = tuple[int, int, str]  # replace text[start:end] with this
TABLES = {"allow": "publish", "share_machine_notes": "publish", "backend": "ingest"}  # the default workspace's


def settings_of(env: Mapping[str, str], root: str | None) -> Settings:
    """A workspace's recorded settings: None for the default, else its root as written. ValueError where the file
    can't be read."""
    path = None if root is None else expand(root, env).resolve()
    out = {None if r is None else expand(r, env).resolve() for r in machine_opt_outs(env)}
    return Settings(allowed_projects(env, path), ingest_backend(env, path), path not in out)


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)  # a tuple as a list


def _value_end(text: str, i: int) -> int:
    """Where the value starting at `i` ends: after its last character, before any comment on its last line."""
    depth, end = 0, i
    while i < len(text):
        c = text[i]
        if c in "\"'":
            quote = c * 3 if text.startswith(c * 3, i) else c
            i += len(quote)
            while i < len(text) and not text.startswith(quote, i):
                i += 2 if c == '"' and text[i] == "\\" else 1
            i = end = i + len(quote)
            continue
        if c == "#":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        if c == "\n" and depth == 0:
            break
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        if not c.isspace():
            end = i + 1
        i += 1
    return end


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _content_end(text: str, start: int, end: int) -> int:
    """Where a table body's content ends: after its last line that isn't blank or a comment. Blank lines and comments
    after it go with the next table."""
    at = start
    for line in re.finditer(r"[^\n]*\n?", text[start:end]):
        if line[0].strip() and not line[0].lstrip().startswith("#"):
            at = start + line.end()
    return at


def _comments_above(text: str, at: int) -> int:
    """Where the comment lines right above `at` start: they are about the entry there."""
    while at > 0:
        line = text.rfind("\n", 0, at - 1) + 1
        if not text[line:at].lstrip().startswith("#"):
            break
        at = line
    return at


def _blanks_after(text: str, at: int) -> int:
    """After the blank lines that start at `at`."""
    while (blank := re.compile(r"[ \t]*\r?\n").match(text, at)) is not None:
        at = blank.end()
    return at


def _set_key(text: str, body: tuple[int, int], key: str, value: object, nl: str) -> Edit:
    """The edit that sets `key` in the table body text[start:end]: its value replaced, or a line added after the
    body's last line of content."""
    start, end = body
    if found := re.compile(rf"^[ \t]*{key}[ \t]*=[ \t]*", re.MULTILINE).search(text, start, end):
        return found.end(), _value_end(text, found.end()), _toml(value)
    at = _content_end(text, start, end)
    # the last line of content may be the file's, with no line break
    return at, at, f"{'' if text[:at].endswith(chr(10)) else nl}{key} = {_toml(value)}{nl}"


def _table(text: str, name: str) -> tuple[int, int] | None:
    """The body of `[name]`: from after its header to the next table."""
    header = re.search(rf"^[ \t]*\[[ \t]*{name}[ \t]*\][^\r\n]*(?:\r?\n|\Z)", text, re.MULTILINE)
    if header is None:
        return None
    following = NEXT_TABLE.search(text, header.end())
    return header.end(), following.start() if following else len(text)


def _entries(text: str) -> list[tuple[int, int, int]]:
    """Each `[[workspace]]`: its header's start, its body's start and its body's end."""
    found = []
    for header in WORKSPACE_HEADER.finditer(text):
        following = NEXT_TABLE.search(text, header.end())
        found.append((header.start(), header.end(), following.start() if following else len(text)))
    return found


def _changed(before: Settings, after: Settings) -> dict[str, object]:
    return {
        f.name: getattr(after, f.name) for f in fields(Settings) if getattr(before, f.name) != getattr(after, f.name)
    }


def _written(entry: dict[str, Any]) -> Settings:
    """A `[[workspace]]` entry's settings as it writes them, each its default where it writes none."""
    written: dict[str, Any] = {key: tuple(v) if isinstance(v, list) else v for key, v in entry.items()}
    return replace(Settings(), **{f.name: written[f.name] for f in fields(Settings) if f.name in written})


def _plain(changed: dict[str, object]) -> dict[str, object]:
    """As `tomllib` reads them back."""
    return {key: list(value) if isinstance(value, tuple) else value for key, value in changed.items()}


def _check_entries(entries: list[tuple[int, int, int]], written: tuple[str, ...]) -> None:
    if len(entries) != len(written):
        raise ValueError("kb can only edit [[workspace]] entries written as tables")


def check_editable(env: Mapping[str, str]) -> None:
    """ValueError where kb can't read the config, or couldn't record a change to any of its settings: what `kb setup`
    checks before its first question, so that no answer is lost. It tries each change on the text, writing nothing."""
    text = _text(env)
    shown = tilde(config_file(env), env)
    recorded = hooks_choice(env)
    try:
        _hooks_text(text, HOOKS if recorded != HOOKS else ("start",))
    except ValueError as e:
        raise ValueError(f"kb can't change hooks under [notes] without changing other lines; edit {shown}") from e
    written = list(workspace_roots(env))
    every: dict[str | None, Settings] = {}
    for root in (None, *written):
        now = settings_of(env, root)
        every[root] = Settings((*now.allow, "kb/check"), f"{now.backend}-check", not now.share_machine_notes)
    _workspaces_text(text, env, written, every)
    # every entry dropped, and one added
    _workspaces_text(text, env, ["/kb-check"], {None: every[None], "/kb-check": Settings(("kb/check",), "x", False)})


def _entry_edits(
    text: str, env: Mapping[str, str], roots: list[str], chosen: Mapping[str | None, Settings], expected: Any
) -> list[Edit]:
    """Drop the entries whose root is left out; record the settings in each kept root's first entry."""
    nl = _newline(text)
    written = workspace_roots(env)
    entries = _entries(text)
    _check_entries(entries, written)
    edits: list[Edit] = []
    kept = []
    choices = {expand(root, env).resolve(): settings for root, settings in chosen.items() if root is not None}
    done = set()  # the folders whose first kept entry, the one kb reads, has its settings
    for index, (root, (head, start, end)) in enumerate(zip(written, entries, strict=True)):
        if root not in roots:
            edits.append((_comments_above(text, head), _blanks_after(text, _content_end(text, start, end)), ""))
            continue
        kept.append(entry := expected["workspace"][index])
        if (wanted := choices.get(folder := expand(root, env).resolve())) is None:
            continue
        # against this entry's own values: what was shown may be an earlier entry's for the same folder, dropped
        mine = _written(entry)
        if folder in done:  # a later entry counts only for its opt-out, which kb reads from any entry
            wanted = replace(mine, share_machine_notes=wanted.share_machine_notes)
        done.add(folder)
        changed = _changed(mine, wanted)
        edits += [_set_key(text, (start, end), key, value, nl) for key, value in changed.items()]
        entry.update(_plain(changed))
    if kept:
        expected["workspace"] = kept
    else:
        expected.pop("workspace", None)
    return edits


def _default_edits(
    text: str, env: Mapping[str, str], chosen: Settings, nl: str, expected: Any
) -> tuple[list[Edit], str]:
    """The default workspace's settings, in `[publish]` and `[ingest]`: the edits, and the tables to add."""
    edits: list[Edit] = []
    added: dict[str, str] = {}
    for key, value in _changed(settings_of(env, None), chosen).items():
        table = TABLES[key]
        expected.setdefault(table, {}).update(_plain({key: value}))
        if (body := _table(text, table)) is None:
            added[table] = added.get(table, f"{nl}[{table}]{nl}") + f"{key} = {_toml(value)}{nl}"
        else:
            edits.append(_set_key(text, body, key, value, nl))
    return edits, "".join(added.values())


def _new_entries(new: list[str], chosen: Mapping[str | None, Settings], nl: str, expected: Any) -> str:
    added = ""
    for root in new:
        changed = _changed(Settings(), chosen.get(root, Settings()))
        expected.setdefault("workspace", []).append({"root": root, **_plain(changed)})
        lines = "".join(f"{key} = {_toml(value)}{nl}" for key, value in {"root": root, **changed}.items())
        added += f"{nl}[[workspace]]{nl}{lines}"
    return added


def record_workspaces(env: Mapping[str, str], roots: list[str], chosen: Mapping[str | None, Settings]) -> None:
    """Keep only these `[[workspace]]` roots (as written), add the new ones, and record each workspace's settings
    (None: the default). Only the lines for what changed are touched, and the file is written only where it reads
    back as intended; ValueError where that can't do it, and then nothing is written."""
    text = _text(env)
    if (new := _workspaces_text(text, env, roots, chosen)) != text:
        replace_file(config_file(env), new)


def _workspaces_text(
    text: str, env: Mapping[str, str], roots: list[str], chosen: Mapping[str | None, Settings]
) -> str:
    """`record_workspaces`' new text, unchanged where nothing changes."""
    original = text
    nl = _newline(text)
    if text and not text.endswith("\n"):  # once, so that lines added at the end don't each add a line break
        text += nl
    expected: Any = tomllib.loads(text)
    edits = _entry_edits(text, env, roots, chosen, expected)
    appended = ""
    if None in chosen:
        default, appended = _default_edits(text, env, chosen[None], nl, expected)
        edits += default
    written = set(workspace_roots(env))
    appended += _new_entries([root for root in roots if root not in written], chosen, nl, expected)
    new = text
    for start, end, replacement in sorted(edits, reverse=True):
        new = new[:start] + replacement + new[end:]
    if appended:
        new = (new + nl if new and not new.endswith("\n") else new) + (appended if new else appended.removeprefix(nl))
    if new == text:
        return original
    try:
        matches = tomllib.loads(new) == expected
    except ValueError:  # TOMLDecodeError too
        matches = False
    if not matches:
        shown = tilde(config_file(env), env)
        raise ValueError(f"kb can't record the workspaces without changing other lines; edit {shown} by hand")
    return new
