"""What Copilot CLI's and Cursor CLI's own readers share: each reads an agent's session folders, read only (§4.1)."""

import re
from dataclasses import dataclass
from pathlib import Path

from agent_fabric.capture import Message, SessionMeta, Transcript

SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class Read:
    """One read of a session: its listing and its whole transcript."""

    session: SessionMeta
    messages: tuple[Message, ...]

    def transcript(self, from_ordinal: int) -> Transcript:
        """The messages from `from_ordinal` on, with the transcript's revision only where there are any."""
        later = tuple(message for message in self.messages if message.ordinal >= from_ordinal)
        return Transcript(self.session.transcript_revision if later else None, later)


def file_stamp(path: Path) -> tuple[int, int, int] | None:
    """A file's inode, size and time; None where it isn't there. Any other error is raised, on Python 3.13 as on
    3.14, where Path.exists would swallow it."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def nonempty(value: object) -> str | None:
    """A string with something in it, else None."""
    return value if isinstance(value, str) and value else None


def tool_category(tool: str, categories: dict[str, str]) -> str:
    """A tool's category as agentsview's NormalizeToolCategory (v0.46.1) gives it, from the agent's table of names."""
    return "Task" if "subagent" in tool else categories.get(tool, "Other")
