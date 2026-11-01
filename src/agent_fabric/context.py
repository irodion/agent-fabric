"""What a `kb` command sees of the world."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO


@dataclass(frozen=True)
class Context:
    """`main` fills it from the process; tests pass their own."""

    env: Mapping[str, str]
    cwd: Path
    stdin: TextIO
    now: datetime
