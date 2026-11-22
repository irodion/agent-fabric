"""Seam: fabricd, the tracker and the digester run together (docs/design.md §3, §5).

A made-up source's session runs in a real git repo, as in test_digester; fabricd's loops follow it on a clock the test
sets, and `start` runs the whole process on a fake home until a signal stops it."""

import asyncio
import fcntl
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import GITLEAKS, a_stub
from test_digester import LATER, PROJECT
from test_tracker import SECOND, T0, Source, feed, turn

from agent_fabric import digester
from agent_fabric.capture import AgentsviewSource, SessionMeta
from agent_fabric.config import Config
from agent_fabric.copilot import CopilotSource
from agent_fabric.digester import Digester
from agent_fabric.fabricd import Settings, capture_feeds, serve, settings, start
from agent_fabric.paths import local_dir, state_db
from agent_fabric.tracker import IDLE, Feed, Tracker, connect


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
        subprocess.run(["git", *args], cwd=folder, env=env, capture_output=True, check=True)
    return folder


class Listed(Source):
    """A source that counts its listings."""

    def __init__(self) -> None:
        super().__init__()
        self.listings = 0

    def active_sessions(self, since: datetime) -> list[SessionMeta]:
        self.listings += 1
        return super().active_sessions(since)


def a_source(repo: Path, at: datetime) -> Listed:
    """A session in the repo, its first turn ended at `at`."""
    found = Listed()
    found.add("s1", cwd=str(repo))
    turn(found, "s1", at)
    return found


@pytest.fixture
def source(repo: Path) -> Listed:
    return a_source(repo, T0)


def configure(env: Mapping[str, str], text: str) -> None:
    config = Path(env["HOME"]) / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text)


def digests(env: Mapping[str, str]) -> list[Path]:
    return sorted(local_dir(env).rglob("*.md"))


async def until(done: Callable[[], bool]) -> None:
    for _ in range(500):
        if done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


def test_a_change_the_tracker_records_wakes_the_digester(env: dict[str, str], source: Source, tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    the_feed = feed(source)
    clock = [T0 + 3 * SECOND]

    async def scenario() -> None:
        # Neither loop comes round again by itself: only the watch's tick, and what the tracker records of it, moves.
        task = asyncio.create_task(
            serve(
                Tracker([the_feed], connect(path)),
                Digester([the_feed], path, env),
                clock=lambda: clock[0],
                poll=3600,
                turn_quiet=3600,
            )
        )
        await until(lambda: len(source.reads) == 2)  # the poll's read, and the watch's opening tick's
        clock[0] = LATER
        source.ticks["s1"].put_nowait(None)
        await until(lambda: bool(digests(env)))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    [written] = digests(env)
    assert written.name == "2026-11-20-claude-s1.md"


def test_a_stopped_fabricd_leaves_nothing_running(env: dict[str, str], source: Source, tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    the_feed = feed(source)

    async def scenario() -> None:
        task = asyncio.create_task(
            serve(Tracker([the_feed], connect(path)), Digester([the_feed], path, env), clock=lambda: T0, poll=3600)
        )
        await until(lambda: len(source.reads) == 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_a_loop_that_fails_stops_the_other(
    env: dict[str, str], source: Source, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.db"
    the_feed = feed(source)
    started = asyncio.Event()

    async def broken(*args: Any, **kwargs: Any) -> None:
        started.set()
        await asyncio.sleep(0)
        raise RuntimeError("a bug")

    monkeypatch.setattr(digester, "run", broken)

    async def scenario() -> None:
        with pytest.raises(ExceptionGroup) as raised:
            await serve(
                Tracker([the_feed], connect(path)), Digester([the_feed], path, env), clock=lambda: T0, poll=3600
            )
        assert raised.group_contains(RuntimeError, match="a bug")
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_settings_default_to_agentsviews_port_and_a_30_minute_timeout(env: dict[str, str]) -> None:
    assert settings(Config.parse("", env)) == Settings(8080, IDLE)
    chosen = settings(Config.parse("[agentsview]\nport = 8123\n[sessions]\nidle_minutes = 45\n", env))
    assert (chosen.port, chosen.idle.total_seconds()) == (8123, 45 * 60)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[agentsview]\nport = 0\n", "port is a port number"),
        ('[agentsview]\nport = "8080"\n', "port is a port number"),
        ("[agentsview]\nport = true\n", "port is a port number"),
        ("[sessions]\nidle_minutes = 0\n", "idle_minutes is a whole number"),
        ("[sessions]\nidle_minutes = 1.5\n", "idle_minutes is a whole number"),
        ("[sessions]\nidle_minutes = 10081\n", "from 1 to 10080"),
        ("[sessions]\nidle_minutes = 99999999999999\n", "from 1 to 10080"),  # no timedelta holds it
        ("sessions = 30\n", r"\[sessions\] is not a table"),
    ],
)
def test_a_wrong_setting_is_refused(env: dict[str, str], text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        settings(Config.parse(text, env))


def test_each_agent_is_read_through_one_source(env: dict[str, str]) -> None:
    feeds = capture_feeds(env, 8080)
    try:
        assert {(f.name, type(f.source)) for f in feeds} == {
            ("agentsview", AgentsviewSource),
            ("copilot", CopilotSource),
        }
        agents = [agent for f in feeds for agent in f.agents]
        assert sorted(agents) == ["claude", "codex", "copilot"]
    finally:
        for f in feeds:
            if isinstance(f.source, AgentsviewSource):
                f.source.close()


def test_a_config_it_cant_read_stops_it_before_it_starts(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    configure(env, "[agentsview]\nport = 99999\n")

    assert start(env, feeds=lambda env, port: pytest.fail("started")) == 2
    assert capsys.readouterr().err == (
        "fabricd: ~/.config/agent-fabric/config.toml: [agentsview] port is a port number, not 99999\n"
    )
    assert not state_db(env).exists()


def test_a_second_fabricd_on_one_data_folder_doesnt_start(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    folder = state_db(env).parent
    folder.mkdir(parents=True)
    with open(folder / "fabricd.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        assert start(env, feeds=lambda env, port: pytest.fail("started")) == 1

    assert capsys.readouterr().err == "fabricd: another fabricd is running on ~/.local/share/agent-fabric\n"
    assert not state_db(env).exists()


def test_a_lock_it_cant_take_stops_it_with_a_message(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    (state_db(env).parent / "fabricd.lock").mkdir(parents=True)

    assert start(env, feeds=lambda env, port: pytest.fail("started")) == 1
    assert capsys.readouterr().err == (
        "fabricd: can't lock ~/.local/share/agent-fabric/fabricd.lock: Is a directory\n"
    )
    assert not state_db(env).exists()


def started(env: Mapping[str, str]) -> bool:
    """Whether fabricd's tracker has recorded a session's start."""
    if not state_db(env).exists():
        return False
    with closing(sqlite3.connect(state_db(env))) as db:
        try:
            return db.execute("SELECT 1 FROM changes WHERE kind = 'started'").fetchone() is not None
        except sqlite3.OperationalError:
            return False


def run_until(
    env: Mapping[str, str],
    feeds: Callable[[Mapping[str, str], int], list[Feed]],
    done: Callable[[], bool],
    *,
    now: datetime,
    signum: signal.Signals = signal.SIGTERM,
) -> int:
    """`start` on a fixed clock, sent `signum` once `done` holds. Never signals once `start` has returned, when no
    handler of fabricd's would catch it, and fails if `done` never comes."""
    returned = threading.Event()
    timed_out = threading.Event()

    def stop() -> None:
        for _ in range(500):
            if done() or returned.is_set():
                break
            time.sleep(0.01)
        else:
            timed_out.set()
        if not returned.is_set():
            os.kill(os.getpid(), signum)

    stopper = threading.Thread(target=stop)
    stopper.start()
    try:
        return start(env, feeds=feeds, clock=lambda: now, poll=0.05)
    finally:
        returned.set()
        stopper.join()
        assert not timed_out.is_set(), "never happened"


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_stops_fabricd(env: dict[str, str], source: Source, signum: signal.Signals) -> None:
    configure(env, "[agentsview]\nport = 8123\n")
    ports: list[int] = []

    def feeds(env: Mapping[str, str], port: int) -> list[Feed]:
        ports.append(port)
        return [feed(source)]

    assert run_until(env, feeds, lambda: started(env), now=T0 + 3 * SECOND, signum=signum) == 0
    assert ports == [8123, 8123]  # the tracker's sources, and the digester's own
    assert started(env)


def test_a_fabricd_started_after_another_stopped_runs_and_writes_nothing_twice(
    env: dict[str, str], source: Listed
) -> None:
    assert run_until(env, lambda env, port: [feed(source)], lambda: bool(digests(env)), now=LATER) == 0
    listings, reads = source.listings, len(source.reads)
    # Two polls, so that the first one's changes, had it recorded any, have woken the digester.
    assert run_until(env, lambda env, port: [feed(source)], lambda: source.listings >= listings + 2, now=LATER) == 0
    assert len(source.reads) == reads  # finalized, and its listing as it was: nothing to read again
    assert len(digests(env)) == 1


STUCK = """
import sys, threading
from agent_fabric import digester, fabricd

def stuck(self, now):
    print("stuck", flush=True)
    threading.Event().wait()

digester.Digester.step = stuck
sys.exit(fabricd.start({"HOME": sys.argv[1]}, feeds=lambda env, port: [], poll=0.05))
"""


def test_a_second_signal_stops_fabricd_without_waiting_for_a_stuck_step(env: dict[str, str], tmp_path: Path) -> None:
    script = tmp_path / "stuck.py"
    script.write_text(STUCK)
    process = subprocess.Popen(
        [sys.executable, str(script), env["HOME"]], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == "stuck\n"
        process.send_signal(signal.SIGTERM)
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(0.5)  # the first waits for the step
        process.send_signal(signal.SIGTERM)
        _, err = process.communicate(timeout=5)
    finally:
        process.kill()
    assert process.returncode == 128 + signal.SIGTERM
    assert "stopped without waiting for the step under way" in err
