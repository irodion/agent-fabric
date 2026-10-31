"""The notes inbox: one file per note, append-only (docs/design.md §4.10)."""

from dataclasses import dataclass, field, replace
from datetime import timedelta
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

    def repeat_of(self, note: Note) -> tuple[Path, Note] | None:
        """An open note with the same scope, kind and text from the last 24 h."""
        for file, old in self.open():
            same = (old.scope, old.kind, old.text) == (note.scope, note.kind, note.text)
            if same and timedelta(0) <= note.time - old.time <= REPEAT_WINDOW:
                return file, old
        return None


class Inbox:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> Snapshot:
        """Notes still in the inbox (not moved to `done/`); unreadable files are skipped."""
        found = []
        for file in self.path.glob("*.md"):
            try:
                found.append((file, parse(file.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return Snapshot(sorted(found, key=lambda item: (item[1].time, item[1].id)))

    def add(self, note: Note) -> tuple[Path, Note]:
        """Write a new note file; never overwrites, drawing a fresh id if the name is taken."""
        self.path.mkdir(parents=True, exist_ok=True)
        for _ in range(ID_ATTEMPTS):
            data = render(note).encode("utf-8")  # before the file exists: an encoding error leaves nothing behind
            file = self.path / note.filename
            try:
                with file.open("xb") as f:
                    f.write(data)
            except FileExistsError:
                note = replace(note, id=new_id())
                continue
            return file, note
        raise FileExistsError(f"no free note id in {self.path} after {ID_ATTEMPTS} attempts")
