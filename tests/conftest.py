"""Seam A: the `kb` CLI called in-process, with its environment, folder, stdin and clock passed in."""

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_fabric.cli import Result, run


@dataclass
class Kb:
    home: Path
    cwd: Path
    now: datetime
    env: dict[str, str] = field(default_factory=dict)

    def __call__(self, *argv: str, stdin: str = "") -> Result:
        env = {"HOME": str(self.home), **self.env}
        return run(list(argv), env=env, cwd=self.cwd, stdin=io.StringIO(stdin), now=self.now)

    def later(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def kb(tmp_path: Path) -> Kb:
    home = tmp_path / "home"
    cwd = home / "code" / "scratch"
    cwd.mkdir(parents=True)
    return Kb(home=home, cwd=cwd, now=datetime(2026, 10, 31, 17, 5, 12, tzinfo=UTC))
