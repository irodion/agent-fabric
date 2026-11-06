"""A project's wiki page: claims by kind, each a note's own text with its evidence (docs/design.md §4.6).

A claim is kept as its exact lines, and nothing rewrites them: filing only adds claims, and moves superseded ones
with a pointer below. A note is untrusted, so a line of its text that could read as page structure is escaped when
filed."""

import re
from dataclasses import dataclass, field
from datetime import datetime

from agent_fabric.notes import Note
from agent_fabric.shown import shown

SECTIONS = {"gotcha": "Gotchas", "fix": "Fixes", "decision": "Decisions"}
SUPERSEDED = "Superseded"
PROJECTS = "wiki/projects"
IDEAS = "ideas.md"
IDEAS_HEAD = ("# Ideas", "", "Suggestions agents left and nobody has decided on: not claims, and not checked.", "")
MAX_TITLE = 80
STRUCTURE = re.compile(r"\s*(#|- source:|- superseded by:|- from:)")
SOURCE = re.compile(
    r"- source: (?P<id>n[0-9a-f]{6}) \(.*, \d{4}-\d{2}-\d{2}\) · anchor: (?P<anchor>\S+)"
    r" · verified: (?P<verified>\S+) · supersedes: (?P<supersedes>\S+(?:, \S+)*)"
    r"(?: · (?:extends|conflicts): \S+(?:, \S+)*)?"
)


@dataclass(frozen=True)
class Claim:
    lines: tuple[str, ...]

    @property
    def meta(self) -> re.Match[str] | None:
        return next((m for line in self.lines if (m := SOURCE.fullmatch(line))), None)

    @property
    def id(self) -> str | None:
        return self.meta["id"] if self.meta else None

    @property
    def title(self) -> str:
        return self.lines[0].removeprefix("### ")

    @property
    def body(self) -> str:
        """The note's text as filed."""
        end = next((i for i, line in enumerate(self.lines) if SOURCE.fullmatch(line)), len(self.lines))
        return "\n".join(self.lines[1:end])

    @property
    def supersedes(self) -> list[str]:
        if not self.meta or self.meta["supersedes"] == "none":
            return []
        return self.meta["supersedes"].split(", ")

    def render(self) -> str:
        return "".join(f"{line}\n" for line in self.lines)


@dataclass
class Section:
    name: str
    intro: list[str] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)


@dataclass
class Page:
    head: list[str]
    sections: list[Section]

    @classmethod
    def blank(cls, project: str) -> "Page":
        names = (*SECTIONS.values(), SUPERSEDED)
        return cls([f"# {project}", ""], [Section(name) for name in names])

    @classmethod
    def parse(cls, text: str) -> "Page":
        page = cls([], [])
        section: Section | None = None
        lines: list[str] | None = None  # the open claim's

        def close() -> None:
            nonlocal lines
            if lines is not None and section is not None:
                while lines and not lines[-1]:
                    lines.pop()
                section.claims.append(Claim(tuple(lines)))
            lines = None

        for line in text.split("\n")[:-1] if text.endswith("\n") else text.split("\n"):
            if line.startswith("## "):
                close()
                section = Section(line[3:])
                page.sections.append(section)
            elif section is None:
                page.head.append(line)
            elif line.startswith("### "):
                close()
                lines = [line]
            elif lines is not None:
                lines.append(line)
            elif line:
                section.intro.append(line)
        close()
        return page

    def render(self) -> str:
        out = "".join(f"{line}\n" for line in self.head)
        for section in self.sections:
            out += f"## {section.name}\n\n"
            if section.intro:
                out += "".join(f"{line}\n" for line in section.intro) + "\n"
            out += "".join(claim.render() + "\n" for claim in section.claims)
        return out

    def _section(self, name: str) -> Section:
        for section in self.sections:
            if section.name == name:
                return section
        section = Section(name)
        self.sections.append(section)
        return section

    def live_claims(self) -> list[Claim]:
        return [claim for section in self.sections if section.name != SUPERSEDED for claim in section.claims]

    def add(self, kind: str, claim: Claim) -> None:
        self._section(SECTIONS[kind]).claims.append(claim)

    def add_idea(self, project: str, idea: Claim) -> None:
        """On the ideas page, a section per project."""
        self._section(safe(project).replace("\n", " ")).claims.append(idea)

    def below(self, target: str, claim: Claim, kind: str) -> None:
        """Right below the target, whichever section it's in; at the end of the kind's section if it has gone."""
        for section in self.sections:
            for at, other in enumerate(section.claims):
                if other.id == target:
                    section.claims.insert(at + 1, claim)
                    return
        self.add(kind, claim)

    def supersede(self, target: str, by: str, now: datetime) -> None:
        """Move the old claim, as it stands, under `## Superseded`, with a pointer to what replaced it."""
        for section in self.sections:
            for claim in section.claims:
                if claim.id == target:
                    section.claims.remove(claim)
                    pointer = f"- superseded by: {by} ({now:%Y-%m-%d})"
                    self._section(SUPERSEDED).claims.append(Claim((*claim.lines, pointer)))
                    return


def _slug(project: str) -> str:
    """A project id as a relative path, staying under its folder whatever the id holds."""
    return "/".join(part for part in project.split("/") if part not in ("", ".", "..", "~"))


def page_path(project: str) -> str:
    """`github.com/me/repo` → `wiki/projects/github.com/me/repo.md`."""
    return f"{PROJECTS}/{_slug(project)}.md"


def raw_path(note: Note) -> str:
    """Where ingest keeps its copy of a note, so that pages can cite it (§4.10)."""
    return f"raw/notes/{_slug(note.scope)}/{note.time:%Y}/{note.time:%m}/{note.filename}"


def safe(text: str) -> str:
    """A note's text as a claim shows it, line by line, so it can't drive a terminal or reorder itself."""
    return shown(text, lines=True)


def _title(text: str) -> str:
    first = safe(text.split("\n", 1)[0]).strip()
    sentence = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0]
    return sentence[:MAX_TITLE].rstrip()


def _body(text: str) -> list[str]:
    return [f"\\{line}" if STRUCTURE.match(line) else line for line in safe(text).split("\n")]


def new_claim(
    note: Note,
    now: datetime,
    *,
    anchor: str = "none",
    supersedes: tuple[str, ...] = (),
    relation: str | None = None,
) -> Claim:
    agent = safe(note.agent["name"]).replace("\n", " ")
    source = f"{note.id} ({agent}, {note.time:%Y-%m-%d})"
    replaced = ", ".join(supersedes) or "none"
    meta = f"- source: {source} · anchor: {anchor} · verified: {now:%Y-%m-%d} · supersedes: {replaced}"
    return Claim((f"### {_title(note.text)}", *_body(note.text), meta + (f" · {relation}" if relation else "")))


def ideas_page(text: str | None) -> Page:
    """`ideas.md` as it stands, or a new one."""
    return Page.parse(text) if text else Page(list(IDEAS_HEAD), [])


def new_idea(note: Note) -> Claim:
    """An idea as filed: its own text, escaped like a claim's, and who left it. No anchor or verified date: nobody
    checked it, and it isn't a claim about the code."""
    agent = safe(note.agent["name"]).replace("\n", " ")
    return Claim(
        (f"### {_title(note.text)}", *_body(note.text), f"- from: {note.id} ({agent}, {note.time:%Y-%m-%d})")
    )
