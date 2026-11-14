"""The notes inbox: one file per note, append-only (docs/design.md §4.10)."""

import contextlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from agent_fabric.notes import Note, new_id, parse, render

REPEAT_WINDOW = timedelta(hours=24)
ID_ATTEMPTS = 5


@dataclass(frozen=True)
class Snapshot:
    """The inbox's notes as read once, oldest first."""

    notes: list[tuple[Path, Note]]
    resolved_by: dict[str, tuple[Path, Note]] = field(init=False)

    def __post_init__(self) -> None:
        resolutions = {note.resolves: (file, note) for file, note in self.notes if note.resolves}
        object.__setattr__(self, "resolved_by", resolutions)

    def open(self) -> list[tuple[Path, Note]]:
        """Notes still to act on: not a resolution, and not resolved by a later note."""
        return [(f, n) for f, n in self.notes if not n.resolves and n.id not in self.resolved_by]

    def find(self, note_id: str) -> Note | None:
        return next((note for _, note in self.notes if note.id == note_id), None)

    def repeats(self, note: Note) -> list[tuple[Path, Note]]:
        """Open notes with the same scope, kind and text from the last 24 h. A resolution is never a repeat: its
        text may match the note it resolves, and a retried resolve is caught by `resolved_by`."""
        if note.resolves:
            return []
        return [
            (file, old)
            for file, old in self.open()
            if (old.scope, old.kind, old.text) == (note.scope, note.kind, note.text)
            and timedelta(0) <= note.time - old.time <= REPEAT_WINDOW
        ]


class Inbox:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self, *, with_done: bool = False) -> Snapshot:
        """Notes still in the inbox, and with `with_done` those moved to `done/`; unreadable files are skipped."""
        found = []
        moved = (self.path / "done").rglob("*.md") if with_done else ()
        for file in (*self.path.glob("*.md"), *moved):
            try:
                found.append((file, parse(file.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return Snapshot(sorted(found, key=lambda item: (item[1].time, item[1].id)))

    def add(self, note: Note, *, keep_id: bool = False) -> tuple[Path, Note]:
        """Write a new note file; never overwrites, drawing a fresh id if a note in the inbox already has it. With
        `keep_id`, a copy of a note in another inbox, that's a FileExistsError instead: the copies share one id."""
        self.path.mkdir(parents=True, exist_ok=True)
        for _ in range(ID_ATTEMPTS):
            data = render(note).encode("utf-8")  # before the file exists: an encoding error leaves nothing behind
            file = self.path / note.filename
            try:
                if any(self.path.glob(f"*-{note.id}.md")):  # ids name notes across days, the file name only in one
                    raise FileExistsError(f"a note in {self.path} already has the id {note.id}")
                with file.open("xb") as f:
                    f.write(data)
            except FileExistsError:
                if keep_id:
                    raise
                note = replace(note, id=new_id())
                continue
            return file, note
        raise FileExistsError(f"no free note id in {self.path} after {ID_ATTEMPTS} attempts")

    def retire(self, file: Path, when: datetime) -> None:
        """Move a note to `done/<yyyy>/<mm>/`, by the month given. A note already moved is left where it is."""
        folder = self.path / "done" / f"{when:%Y}" / f"{when:%m}"
        folder.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            file.rename(folder / file.name)
