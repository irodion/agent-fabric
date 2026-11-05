"""Seam A: the `kb` CLI called in-process, with its environment, folder, stdin and clock passed in."""

import io
import json
import os
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_fabric.backends import Backend, Question
from agent_fabric.cli import Result, run_in
from agent_fabric.context import Context

PROJECT = "github.com/me/proj"
PAGE = f"wiki/projects/{PROJECT}.md"
BRANCH = "ingest/2026-10-31"
DEFAULTS = {"ref": "says_nothing", "claim": "unrelated", "doc": "no", "useful": "yes", "sure": "high"}
# Stands in for gitleaks in the end-to-end tests: reports a rule for any text holding the marker, in gitleaks' own
# report shape, which tests/test_secret_gate.py checks against the real binary.
LEAK = "FAKELEAK"
GITLEAKS = f"""#!/bin/sh
if grep -q {LEAK} -; then
  echo '[{{"RuleID": "fake-rule", "Secret": "not-for-printing"}}]'
else
  echo '[]'
fi
"""


@dataclass
class Kb:
    home: Path
    cwd: Path
    now: datetime
    env: dict[str, str] = field(default_factory=dict)
    backend: Backend | None = None

    def __call__(self, *argv: str, stdin: str = "") -> Result:
        env = {"HOME": str(self.home), **self.env}
        context = Context(env=env, cwd=self.cwd, stdin=io.StringIO(stdin), now=self.now, backend=self.backend)
        return run_in(list(argv), context)

    def later(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def kb(tmp_path: Path) -> Kb:
    home = tmp_path / "home"
    cwd = home / "code" / "scratch"
    cwd.mkdir(parents=True)
    return Kb(home=home, cwd=cwd, now=datetime(2026, 10, 31, 17, 5, 12, tzinfo=UTC))


@dataclass
class FakeBackend:
    """Answers typed questions: each kind's default, or what `script` gives for a note whose text is in the state.

    A script's value maps a question's kind or its full id to an answer, or is an exception to raise."""

    script: dict[str, Mapping[str, object] | Exception] = field(default_factory=dict)
    calls: list[tuple[str, list[Question]]] = field(default_factory=list)

    def answer(self, state: str, questions: list[Question]) -> object:
        self.calls.append((state, questions))
        planned: Mapping[str, object] = {}
        for fragment, plan in self.script.items():
            if fragment in state:
                if isinstance(plan, Exception):
                    raise plan
                planned = plan
        return {q.id: planned.get(q.id, planned.get(q.kind, DEFAULTS[q.kind])) for q in questions}


@dataclass
class Lab:
    """A home with one allowed project in git, a fake model and a stand-in gitleaks, for `kb ingest` and `kb lint`."""

    kb: Kb
    repo: Path
    fake: FakeBackend

    @property
    def wiki(self) -> Path:
        return self.kb.home / "wiki"

    @property
    def inbox(self) -> Path:
        return self.kb.home / ".local" / "share" / "agent-fabric" / "inbox"

    def git(self, *args: str, cwd: Path | None = None) -> str:
        env = {"HOME": str(self.kb.home), **self.kb.env}
        done = subprocess.run(
            ["git", *args], cwd=cwd or self.repo, env=env, capture_output=True, text=True, check=True
        )
        return done.stdout

    def wiki_git(self, *args: str) -> str:
        return self.git(*args, cwd=self.wiki)

    def leave(self, kind: str, text: str, *refs: str) -> str:
        flags = [flag for ref in refs for flag in ("--ref", ref)]
        result = self.kb("note", "--kind", kind, "--agent", "Codex", *flags, text)
        assert result.exit_code == 0, result.stderr
        return result.stdout.split()[0]

    def show(self, rev: str, path: str) -> str:
        return self.wiki_git("show", f"{rev}:{path}")

    def manifest(self, rev: str = BRANCH, name: str = "2026-10-31") -> dict[str, object]:
        loaded: dict[str, object] = json.loads(self.show(rev, f"manifests/{name}.json"))
        return loaded

    def merge(self, branch: str = BRANCH) -> None:
        self.wiki_git("merge", "-q", "--no-edit", branch)

    def commit_in_repo(self, path: str, text: str) -> None:
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / path).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"change {path}")


@pytest.fixture
def lab(kb: Kb, tmp_path: Path) -> Lab:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    gitleaks = stubs / "gitleaks"
    gitleaks.write_text(GITLEAKS)
    gitleaks.chmod(gitleaks.stat().st_mode | stat.S_IEXEC)
    codex = stubs / "codex"  # for `kb doctor`, whatever this machine has: the fake model answers instead
    codex.write_text("#!/bin/sh\nexit 1\n")
    codex.chmod(codex.stat().st_mode | stat.S_IEXEC)
    kb.env.update(
        PATH=f"{stubs}:{os.environ['PATH']}",
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.test",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.test",
        GIT_CONFIG_NOSYSTEM="1",
    )
    repo = kb.home / "code" / "proj"
    repo.mkdir(parents=True)
    kb.cwd = repo
    fake = FakeBackend()
    kb.backend = fake
    lab = Lab(kb=kb, repo=repo, fake=fake)
    lab.git("init", "-q", "-b", "main")
    lab.git("remote", "add", "origin", "git@github.com:me/proj.git")
    lab.commit_in_repo("src/app.py", "def main():\n    return 1\n")
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f'[publish]\nallow = ["{PROJECT}"]\n')
    return lab
