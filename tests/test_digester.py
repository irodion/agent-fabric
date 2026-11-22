"""Seam: the digester over the tracker's changes, with a fixed clock (docs/design.md §4.3-§4.5).

A made-up source's session runs in a real git repo; the tracker polls it, and the digester follows its changes into
the wiki, the local folder or the quarantine."""

import logging
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from conftest import GITLEAKS, a_stub
from test_tracker import MINUTE, SECOND, T0, Source, feed, turn

from agent_fabric import snapshot
from agent_fabric.digester import BATCH, RETRY, Digester
from agent_fabric.tracker import IDLE, Tracker, connect
from agent_fabric.writer import Written

PROJECT = "github.com/me/proj"
LATER = T0 + IDLE + MINUTE  # when the first turn's session has finalized


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    a_stub(tmp_path / "stubs", "gitleaks", GITLEAKS)
    return {
        "HOME": str(home),
        "PATH": f"{tmp_path / 'stubs'}:{os.environ['PATH']}",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.test",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.test",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


@pytest.fixture
def repo(tmp_path: Path, env: dict[str, str]) -> Path:
    folder = tmp_path / "proj"
    folder.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("remote", "add", "origin", f"git@{PROJECT.replace('/', ':', 1)}.git"),
        ("commit", "-q", "--allow-empty", "-m", "start"),
    ):
        git(folder, env, *args)
    return folder


def git(folder: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(["git", *args], cwd=folder, env=env, capture_output=True, text=True, check=True).stdout


def allow(env: dict[str, str]) -> None:
    config = Path(env["HOME"]) / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f'[publish]\nallow = ["{PROJECT}"]\n')


class Lab:
    """A session in the repo, the tracker following it and the digester on its own connection, as fabricd has them."""

    def __init__(self, tmp_path: Path, env: dict[str, str], repo: Path) -> None:
        self.source = Source()
        self.feed = feed(self.source)
        self.path = tmp_path / "state.db"
        self.tracker = Tracker([self.feed], connect(self.path))
        self.digester = Digester([self.feed], self.path, env)
        self.env, self.repo = env, repo
        self.source.add("s1", cwd=str(repo))

    def step(self, now: datetime) -> list[Written]:
        self.tracker.poll(now)
        return self.digester.step(now)

    def wiki(self) -> Path:
        return Path(self.env["HOME"]) / "wiki"

    def wiki_git(self, *args: str) -> str:
        return git(self.wiki(), self.env, *args)


@pytest.fixture
def lab(tmp_path: Path, env: dict[str, str], repo: Path) -> Lab:
    return Lab(tmp_path, env, repo)


def a_session(lab: Lab) -> None:
    """First seen, a file changed in its turn, the turn ended: snapshots at both."""
    turn(lab.source, "s1", T0)
    assert lab.step(T0 + 3 * SECOND) == []
    (lab.repo / "retry.py").write_text("print('retry')\n")
    assert lab.step(T0 + 10 * SECOND) == []


def test_a_finalized_session_is_committed_with_its_snapshots(lab: Lab) -> None:
    allow(lab.env)
    a_session(lab)

    [written] = lab.step(LATER)

    assert written.where == "wiki"
    assert written.committed
    text = written.path.read_text()
    assert f'project: "{PROJECT}"' in text
    assert "retry.py" in text  # from the turn end's snapshot: untracked, so in the changes
    assert "revision: 1" in text
    assert (
        written.path.relative_to(lab.wiki()).as_posix()
        == "raw/sessions/github.com/me/proj/2026/11/2026-11-20-claude-s1.md"
    )


def test_a_project_not_allowed_gets_a_local_digest(lab: Lab) -> None:
    a_session(lab)

    [written] = lab.step(LATER)

    assert written.where == "local"
    assert "retry.py" in written.path.read_text()


def test_nothing_is_written_before_a_session_finalizes(lab: Lab) -> None:
    allow(lab.env)
    a_session(lab)

    assert lab.step(LATER - 2 * MINUTE) == []
    assert not lab.wiki().exists()


def test_a_digest_that_cant_be_written_waits_and_is_tried_again(lab: Lab, caplog: pytest.LogCaptureFixture) -> None:
    allow(lab.env)
    a_session(lab)
    lab.step(LATER)
    (lab.wiki() / "mine.md").write_text("my own\n")
    turn(lab.source, "s1", LATER + MINUTE, prompt="again")
    lab.step(LATER + MINUTE + 10 * SECOND)
    again = LATER + IDLE + 3 * MINUTE

    with caplog.at_level(logging.WARNING, logger="agent_fabric.digester"):
        assert lab.step(again) == []
        (lab.wiki() / "more.md").write_text("more of mine\n")  # the reason's list of files changes; it doesn't
        assert lab.step(again + RETRY) == []
    [logged] = caplog.records  # the same reason, logged once
    assert "mine.md" in logged.message

    (lab.wiki() / "mine.md").unlink()
    (lab.wiki() / "more.md").unlink()
    assert lab.step(again + RETRY + SECOND) == []  # not a minute since the last try
    [written] = lab.step(again + 2 * RETRY)
    assert "revision: 2" in written.path.read_text()


def test_a_session_reopened_before_its_digest_is_written_waits_for_its_next_finalization(lab: Lab) -> None:
    allow(lab.env)
    a_session(lab)
    lab.wiki().mkdir()
    git(lab.wiki(), lab.env, "init", "-q", "-b", "other")  # a repo with no main: no digest goes in
    assert lab.step(LATER) == []
    turn(lab.source, "s1", LATER + MINUTE, prompt="again")
    lab.tracker.poll(LATER + MINUTE + 3 * SECOND)
    shutil.rmtree(lab.wiki())

    assert lab.digester.step(LATER + MINUTE + RETRY) == []  # reopened: due no more
    assert not lab.wiki().exists()

    [written] = lab.step(LATER + MINUTE + IDLE + MINUTE)
    assert "revision: 2" in written.path.read_text()


def test_a_reopened_session_rewrites_the_same_file(lab: Lab) -> None:
    allow(lab.env)
    a_session(lab)
    [first] = lab.step(LATER)
    turn(lab.source, "s1", LATER + MINUTE, prompt="again", answer="Done again.")
    lab.step(LATER + MINUTE + 10 * SECOND)

    [second] = lab.step(LATER + MINUTE + IDLE + MINUTE)

    assert second.path == first.path
    assert "Done again." in second.path.read_text()
    assert lab.wiki_git("log", "--format=%s").count("digest:") == 2


def test_a_restarted_digester_reads_on_from_its_place(lab: Lab) -> None:
    allow(lab.env)
    a_session(lab)
    lab.step(LATER)

    restarted = Digester([lab.feed], lab.path, lab.env)

    assert restarted.step(LATER + MINUTE) == []
    assert lab.wiki_git("log", "--format=%s").count("digest:") == 1


def test_a_session_its_source_no_longer_has_is_dropped(lab: Lab) -> None:
    allow(lab.env)
    a_session(lab)
    lab.tracker.poll(LATER)
    del lab.source.texts["s1"]

    assert lab.digester.step(LATER) == []
    lab.source.texts["s1"] = []
    assert lab.digester.step(LATER + 2 * RETRY) == []


def test_a_session_outside_git_has_a_digest_with_no_changes(lab: Lab, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    lab.source.add("s2", cwd=str(plain))
    turn(lab.source, "s2", T0)
    lab.step(T0 + 10 * SECOND)

    written = {w.path.name: w for w in lab.step(LATER)}

    assert "uncommitted_changes: null" in written["2026-11-20-claude-s2.md"].path.read_text()


def test_one_step_snapshots_a_session_once_however_many_of_its_turns_ended(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken: list[object] = []
    take = snapshot.take

    def counted(*args: Any) -> snapshot.Snapshot | None:
        taken.append(args)
        return take(*args)

    monkeypatch.setattr(snapshot, "take", counted)
    turn(lab.source, "s1", T0)
    lab.tracker.poll(T0 + 3 * SECOND)
    lab.tracker.poll(T0 + 10 * SECOND)

    lab.digester.step(T0 + 10 * SECOND)

    assert len(taken) == 1


def test_a_snapshot_survives_the_database(lab: Lab) -> None:
    a_session(lab)
    row: sqlite3.Row = connect(lab.path).execute("SELECT first, last FROM digests").fetchone()

    assert row["first"] is not None
    assert "retry.py" in row["last"]
    assert "retry.py" not in row["first"]


def test_a_session_keeps_the_workspace_it_was_first_seen_in_after_its_worktree_goes(lab: Lab, tmp_path: Path) -> None:
    root = lab.repo.resolve()
    worktree = tmp_path / "elsewhere" / "proj"  # outside the root, as Codex's are
    git(lab.repo, lab.env, "worktree", "add", "-q", str(worktree))
    config = Path(lab.env["HOME"]) / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f'[[workspace]]\nroot = "{root}"\nallow = ["{PROJECT}"]\n')
    lab.source.add("s2", cwd=str(worktree))
    turn(lab.source, "s2", T0)
    lab.step(T0 + 10 * SECOND)
    shutil.rmtree(worktree)  # removed when its session ended

    [written] = [w for w in lab.step(LATER) if w.path.name.endswith("-s2.md")]

    assert written.where == "wiki"
    assert written.path.is_relative_to(root / ".fabric" / "wiki")


def test_a_session_finalized_before_its_turn_end_was_read_isnt_snapshotted_after(lab: Lab) -> None:
    turn(lab.source, "s1", T0)
    lab.tracker.poll(T0 + 3 * SECOND)
    lab.tracker.poll(T0 + 10 * SECOND)
    lab.tracker.poll(LATER)  # all while the digester was down
    (lab.repo / "later.py").write_text("a later session's\n")

    [written] = lab.digester.step(LATER + MINUTE)

    text = written.path.read_text()
    assert "later.py" not in text
    assert "uncommitted_changes: null" in text


def test_a_digest_no_configured_feed_reads_is_dropped_and_logged(lab: Lab, caplog: pytest.LogCaptureFixture) -> None:
    a_session(lab)
    lab.tracker.poll(LATER)

    with caplog.at_level(logging.WARNING, logger="agent_fabric.digester"):
        assert Digester([], lab.path, lab.env).step(LATER) == []

    assert "no feed configured" in caplog.text
    assert lab.digester.step(LATER + 2 * RETRY) == []


def test_a_stored_snapshot_that_no_longer_loads_is_left_out(lab: Lab) -> None:
    a_session(lab)
    with connect(lab.path) as db:  # as a version with another `Snapshot` would have stored it
        db.execute("UPDATE digests SET last = json_remove(last, '$.stat')")

    [written] = lab.step(LATER)

    text = written.path.read_text()
    assert "retry.py" not in text  # the first snapshot's, from before the file
    assert "uncommitted_changes: false" in text


def test_a_backlog_is_read_in_batches_each_saved(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    for n in range(BATCH + 20):
        lab.source.add(f"b{n}", cwd=str(lab.repo))
        lab.source.say(f"b{n}", "user", "hi", T0)
    lab.tracker.poll(T0 + SECOND)
    taken: list[object] = []

    def failing(*args: Any) -> None:
        taken.append(args)
        if len(taken) > BATCH:
            raise RuntimeError("stopped")

    monkeypatch.setattr(snapshot, "take", failing)

    with pytest.raises(RuntimeError):
        lab.digester.step(T0 + SECOND)
    place = connect(lab.path).execute("SELECT place FROM digester").fetchone()["place"]
    assert place == BATCH


def test_a_retry_waits_a_minute_whatever_the_clocks_zone(lab: Lab) -> None:
    allow(lab.env)
    a_session(lab)
    lab.wiki().mkdir()
    git(lab.wiki(), lab.env, "init", "-q", "-b", "other")  # a repo with no main: no digest goes in
    lab.step(LATER)
    tried = lab.source.attempts

    lab.digester.step((LATER + 30 * SECOND).astimezone(timezone(timedelta(hours=2))))

    assert lab.source.attempts == tried
