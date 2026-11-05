"""What a `kb` command sees of the world."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from agent_fabric.backends import Backend


@dataclass(frozen=True)
class Context:
    """`main` fills it from the process; tests pass their own."""

    env: Mapping[str, str]
    cwd: Path
    stdin: TextIO
    now: datetime
    backend: Backend | None = None  # what answers ingest's questions; None for the one `--backend` names
