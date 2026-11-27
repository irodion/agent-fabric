"""Make a Cursor CLI chat folder into a test fixture: tests/fixtures/cursor/chats/<md5 of the folder>/<id>/.

    uv run python scripts/cursor-fixture.py <chat folder> <recorded>=<fixture> ...

The chat is a synthetic session recorded with `cursor-agent -p` in a throwaway repo (test_cursor.py says which).
The copy leaves out Cursor's system prompt and the context it sends as the first user message (the user's rules,
skills and plugins), with their options, which hold hashes of the recorded folders, and the reasoning's signatures,
and zeroes the blob key. Each `<recorded>=<fixture>` replaces a text, in
the order given, and the two must be as long in UTF-8, since protobuf blobs carry a text with its length: the
recorded folder, Cursor's name for it in its projects folder, and the home. Each blob whose bytes change gets a new
id, the sha256 of its bytes as Cursor names it, and every blob that names it is rewritten to match, up to the root
that `meta` points at. The recording is read from a copy, so its folder stays as Cursor left it.

The script knows the one shape it was written for, Cursor CLI 2026.11.22's, and refuses the rest rather than guess:
a store with tables other than `blobs` and `meta`, other than one system prompt and one context, or either in any
other form stops it, as does a recorded text, a signature, a line of the system prompt or the context, or the old id
of a blob it changed, left anywhere, raw or as JSON writes it, since a protobuf blob that embeds them can't be cut
without rewriting its length prefixes. The context is a user message with `<user_info>` and no `<user_query>`, as
the reader tells it, and any other blob that holds `<user_info>` stops it. Whatever pairs it's given, it also looks
for this machine's home, as a path and as Cursor's projects folder names it, user name and host name, and git's
name and email, and the recorded folder's own name there,
and refuses a recording whose folder no pair replaced. The copy is made in a temporary folder and moves into the
fixtures only once it passes, so a recording the script refuses never lands in a folder that gets committed.
"""

import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "cursor" / "chats"
LEFT_OUT = "(left out of the fixture)"
HASH = re.compile(r"[0-9a-f]{16,}")
LONG_ENOUGH = 20  # a line of the left-out text this long is looked for everywhere; a shorter one could be anything
# A reasoning part's signature, in a message's own blob or in the protobuf blobs that embed the message's JSON. Any
# other `signature` the copy holds, written some other way, stops the script.
SIGNATURE = re.compile(rb'"signature":"([^"]*)"')
BLANKED = re.compile(rb'"signature":"x*"')

Pairs = list[tuple[bytes, bytes]]


def main(chat: Path, pairs: Pairs) -> None:
    for recorded, fixture in pairs:
        if len(recorded) != len(fixture):
            sys.exit(f"{fixture!r} must be as long as {recorded!r}")
        # Cursor writes the folder as a file:// URI too: a text that a URI or JSON would write otherwise could hide.
        if quote(recorded.decode(), safe="/") != recorded.decode() or not recorded.isascii():
            sys.exit(f"{recorded!r} has characters a URI would encode, which this script doesn't look for")
    meta = json.loads((chat / "meta.json").read_bytes())
    recorded_cwd = meta["cwd"]
    scrubbed = _replace(recorded_cwd.encode(), pairs).decode()
    if scrubbed == recorded_cwd:
        sys.exit(f"no pair replaces the recorded folder {recorded_cwd!r}")
    meta["cwd"] = scrubbed
    # Cursor names a chat's folder by the md5 of the folder it ran in.
    target = FIXTURES / hashlib.md5(meta["cwd"].encode()).hexdigest() / chat.name
    if target.exists():
        sys.exit(f"{target} is there already")
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / chat.name
        copy.mkdir()
        _copy(chat, copy, meta, pairs, recorded_cwd)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(copy, target)
    print(target)


def _copy(chat: Path, target: Path, meta: dict[str, object], pairs: Pairs, recorded_cwd: str) -> None:
    recorded_folder = hashlib.md5(recorded_cwd.encode()).hexdigest().encode()  # Cursor's name for the chat's folder
    (target / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
    # Read from a copy, as SQLite can add a -shm file even to read; the backup reads the copy as SQLite does, its
    # WAL included, so a write not yet checkpointed is kept.
    original = target.parent / "recording"
    original.mkdir()
    for name in ("store.db", "store.db-wal"):
        if (chat / name).exists():
            shutil.copy(chat / name, original / name)
    recording = sqlite3.connect(original / "store.db")
    with closing(recording), closing(sqlite3.connect(target / "store.db")) as db:
        recording.backup(db)
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")}
        if tables != {"blobs", "meta"}:
            sys.exit(f"a store with {sorted(tables)}, where this script knows only blobs and meta")
        db.execute("PRAGMA journal_mode=delete")  # one file, standing alone
        with db:
            removed = _scrub_store(db, pairs) | {recorded_folder} | _machine() | {_dashed(recorded_cwd)}
        db.execute("VACUUM")
        left = [id for id, data in db.execute("SELECT id, data FROM blobs") if _left(data, pairs, removed)]
        heads = db.execute("SELECT key, value FROM meta")  # hex-encoded JSON
        left += [f"meta {key}" for key, value in heads if _left(bytes.fromhex(value), pairs, removed)]
    if _left((target / "meta.json").read_bytes(), pairs, removed):
        left.append("meta.json")
    if left:
        sys.exit(f"{', '.join(left)} still hold a recorded text, a signature or a line left out")


def _machine() -> set[bytes]:
    """What names this machine and its user, wherever a pair missed it. A name too short to tell from a word, such as
    `dev`, is left to the home folder's path, which holds it."""
    names = {getpass.getuser(), socket.gethostname(), *(_git(key) for key in ("user.name", "user.email"))}
    home = os.path.expanduser("~")
    return {home.encode(), _dashed(home)} | {name.encode() for name in names if len(name) >= 5}


def _dashed(folder: str) -> bytes:
    """A folder as Cursor names it in `~/.cursor/projects`: `/home/ann/x` is `home-ann-x`."""
    return folder.strip("/").replace("/", "-").encode()


def _git(key: str) -> str:
    answer = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True, check=False)
    return answer.stdout.strip()


def _left(data: bytes, pairs: Pairs, removed: set[bytes]) -> bool:
    if any(recorded in data for recorded, _ in pairs) or any(line in data for line in removed):
        return True
    return data.count(b"signature") != len(BLANKED.findall(data))


def _scrub_store(db: sqlite3.Connection, pairs: Pairs) -> set[bytes]:
    """Scrub the store in place, and return what it left out, to look for anywhere else: the lines of the system
    prompt and the context, and the old ids of the blobs it changed."""
    blobs: dict[str, bytes] = dict(db.execute("SELECT id, data FROM blobs"))
    removed: set[bytes] = set()
    found: list[str] = []
    scrubbed = {id: _scrub(data, pairs, removed, found) for id, data in blobs.items()}
    if sorted(found) != ["context", "system"]:
        sys.exit(f"found {sorted(found)}, where this script knows one system prompt and one context")
    # A blob's new id depends on the new ids of the blobs it names, so each pass renames from the scrubbed original,
    # until a pass changes nothing: the blobs form a tree, so that takes at most as many passes as it is deep.
    renamed: dict[str, str] = {}
    while True:
        data = {id: _rename(blob, renamed) for id, blob in scrubbed.items()}
        now = {id: hashlib.sha256(blob).hexdigest() for id, blob in data.items() if blob != blobs[id]}
        if now == renamed:
            break
        renamed = now
    removed.update(form for old in renamed for form in (bytes.fromhex(old), old.encode()))
    db.execute("DELETE FROM blobs")
    # Two blobs that differed only in what was left out are now one, under one id.
    db.executemany(
        "INSERT OR IGNORE INTO blobs VALUES (?, ?)", [(renamed.get(id, id), blob) for id, blob in data.items()]
    )
    (key,) = db.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
    head = json.loads(bytes.fromhex(key))
    head["latestRootBlobId"] = renamed.get(head["latestRootBlobId"], head["latestRootBlobId"])
    if "blobEncryptionKey" in head:
        removed.add(str(head["blobEncryptionKey"]).encode())
        head["blobEncryptionKey"] = "0" * len(head["blobEncryptionKey"])
    value = json.dumps(head, ensure_ascii=False, separators=(",", ":")).encode().hex()
    db.execute("UPDATE meta SET value = ? WHERE key = '0'", (value,))
    return removed


def _rename(blob: bytes, renamed: dict[str, str]) -> bytes:
    for old, new in renamed.items():
        blob = blob.replace(bytes.fromhex(old), bytes.fromhex(new))
    return blob


def _replace(text: bytes, pairs: Pairs) -> bytes:
    for recorded, fixture in pairs:
        text = text.replace(recorded, fixture)
    return text


def _scrub(blob: bytes, pairs: Pairs, removed: set[bytes], found: list[str]) -> bytes:
    # Each signature keeps its length, as each replaced text does, so a protobuf blob's length prefixes still hold.
    blob = SIGNATURE.sub(lambda m: b'"signature":"' + b"x" * len(m.group(1)) + b'"', _replace(blob, pairs))
    try:
        message = json.loads(blob)
    except ValueError:
        if b"<user_info>" in blob:  # Cursor's context, embedded where it can't be cut
            sys.exit(f"a protobuf blob holds Cursor's context: {blob[:200]!r}")
        return blob  # protobuf
    content = message.get("content") if isinstance(message, dict) else None
    role = message.get("role") if isinstance(message, dict) else None
    system = role == "system"
    context = role == "user" and b"<user_info>" in blob and b"<user_query>" not in blob
    if not system and not context:
        if b"<user_info>" in blob and not (role == "user" and b"<user_query>" in blob):
            sys.exit(f"a blob holds Cursor's context besides its own message: {blob[:200]!r}")
        return blob
    found.append("system" if system else "context")
    if not isinstance(content, str) or (context and not content.startswith("<user_info>")):
        sys.exit(f"a {message['role']} message of a shape this script doesn't know: {blob[:200]!r}")
    message["content"] = LEFT_OUT if system else f"<user_info>\n{LEFT_OUT}\n</user_info>"
    # Each line long enough to tell, raw and as JSON writes it inside a string, to look for in every other blob.
    for line in content.splitlines():
        if len(line.strip()) >= LONG_ENOUGH:
            removed.update({line.encode(), json.dumps(line, ensure_ascii=False)[1:-1].encode()})
    removed.update(_strings(message.pop("providerOptions", None)))
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()


def _strings(value: object) -> set[bytes]:
    """The hashes in a JSON value, to look for elsewhere: the options hold hashes of the recorded folders, besides
    tool and model names that are Cursor's own."""
    if isinstance(value, dict):
        return {found for item in value.values() for found in _strings(item)}
    if isinstance(value, list):
        return {found for item in value for found in _strings(item)}
    return {value.encode()} if isinstance(value, str) and HASH.fullmatch(value) else set()


def _pair(argument: str) -> tuple[bytes, bytes]:
    recorded, equals, fixture = argument.partition("=")
    if not equals or not recorded:
        sys.exit(f"{argument!r} isn't <recorded>=<fixture>")
    return recorded.encode(), fixture.encode()


if __name__ == "__main__":
    main(Path(sys.argv[1]), [_pair(argument) for argument in sys.argv[2:]])
