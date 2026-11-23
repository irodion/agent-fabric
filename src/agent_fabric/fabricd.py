"""fabricd: the tracker and the digester in one long-running process (docs/design.md §3, §5).

The tracker follows the sessions through their sources and records what it notices in `state.db`; each change it
records wakes the digester, which reads on from its own place there. The two share nothing else: each has its own
sources, its own connection and its own worker thread. One fabricd runs at a time on a data folder, since two would
write each digest twice. SIGTERM or SIGINT stops it once the step under way is done, and a second one at once.

Its settings are read once, at start, from `~/.config/agent-fabric/config.toml`:

    [agentsview]
    port = 8080          # the port agentsview's unit serves on (§4.1)

    [sessions]
    idle_minutes = 30    # how long a session is quiet before it finalizes (§4.3)

Restart it after changing them (`systemctl --user restart fabricd.service`). Each workspace's allow list is read at
each write, so a change to it needs no restart.
"""

import asyncio
import fcntl
import logging
import os
import signal
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, closing
from datetime import UTC, datetime
from typing import NoReturn

from agent_fabric import digester, tracker
from agent_fabric.capture import AgentsviewSource
from agent_fabric.config import BAD_CONFIG, LOCKED, fabricd_settings, read_config
from agent_fabric.copilot import AGENT as COPILOT
from agent_fabric.copilot import CopilotSource
from agent_fabric.paths import config_file, state_db, tilde
from agent_fabric.tracker import POLL, TURN_QUIET, Change, Feed, Tracker

log = logging.getLogger(__name__)


def capture_feeds(env: Mapping[str, str], port: int) -> list[Feed]:
    """Each agent through its one source (§4.3): agentsview for Claude Code and Codex, Copilot CLI's own reader for
    Copilot. Cursor CLI waits for its own reader (§4.1)."""
    return [
        Feed("agentsview", AgentsviewSource(f"http://127.0.0.1:{port}"), frozenset({"claude", "codex"})),
        Feed("copilot", CopilotSource(env), frozenset({COPILOT})),
    ]


async def serve(
    following: Tracker,
    digesting: digester.Digester,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    poll: float = POLL,
    turn_quiet: float = TURN_QUIET.total_seconds(),
) -> NoReturn:
    """Run the tracker and the digester until cancelled, each change the tracker records waking the digester, so that
    a turn end's snapshot is taken close to it. Cancelled, it returns once both have stopped; should either fail, the
    other is stopped too and the failure raised, for the service manager to restart fabricd."""
    wake = asyncio.Event()

    def emit(change: Change) -> None:
        wake.set()

    async with asyncio.TaskGroup() as group:
        group.create_task(tracker.run(following, emit=emit, clock=clock, poll=poll, turn_quiet=turn_quiet))
        group.create_task(digester.run(digesting, wake=wake, clock=clock, every=poll))
    raise AssertionError("both loops run until cancelled")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(start(os.environ))


def start(
    env: Mapping[str, str],
    *,
    feeds: Callable[[Mapping[str, str], int], Sequence[Feed]] = capture_feeds,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    poll: float = POLL,
) -> int:
    """fabricd itself: 0 once stopped by SIGTERM or SIGINT, 1 if it failed or the lock can't be taken, 2 if the config
    can't be read, 3 if another is running. `feeds` is called once for the tracker and once for the digester, so that
    neither uses the other's sources from its worker thread."""
    try:
        chosen = fabricd_settings(read_config(env))
    except (OSError, ValueError) as error:
        print(f"fabricd: {tilde(config_file(env), env)}: {error}", file=sys.stderr)
        return BAD_CONFIG
    path = state_db(env)
    lock_path = path.parent / "fabricd.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"fabricd: can't make {tilde(path.parent, env)}: {error.strerror or error}", file=sys.stderr)
        return 1
    with ExitStack() as stack:
        try:
            fcntl.flock(stack.enter_context(open(lock_path, "a")), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"fabricd: another fabricd is running on {tilde(path.parent, env)}", file=sys.stderr)
            return LOCKED
        except OSError as error:
            print(f"fabricd: can't lock {tilde(lock_path, env)}: {error.strerror or error}", file=sys.stderr)
            return 1
        tracked, digested = (_entered(stack, feeds(env, chosen.port)) for _ in range(2))
        try:
            following = Tracker(tracked, stack.enter_context(closing(tracker.connect(path))), idle=chosen.idle)
            digesting = stack.enter_context(closing(digester.Digester(digested, path, env, idle=chosen.idle)))
        except (OSError, sqlite3.Error) as error:
            print(f"fabricd: {tilde(path, env)}: {error}", file=sys.stderr)
            return 1
        return asyncio.run(_until_stopped(following, digesting, clock, poll))


def _entered(stack: ExitStack, feeds: Sequence[Feed]) -> Sequence[Feed]:
    """The feeds, each source that holds something, such as agentsview's client, closed when fabricd stops."""
    for feed in feeds:
        if isinstance(feed.source, AbstractContextManager):
            stack.enter_context(feed.source)
    return feeds


async def _until_stopped(
    following: Tracker, digesting: digester.Digester, clock: Callable[[], datetime], poll: float
) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, _first_signal, stop, signum)
    work = asyncio.create_task(serve(following, digesting, clock=clock, poll=poll))
    stopping = asyncio.create_task(stop.wait())
    await asyncio.wait([work, stopping], return_when=asyncio.FIRST_COMPLETED)
    stopping.cancel()
    if not work.done():
        log.info("fabricd stopping once the step under way is done; a second signal stops it now")
        work.cancel()
    try:
        await work
    except asyncio.CancelledError:
        return 0
    except Exception:
        log.exception("fabricd failed")
    return 1


def _first_signal(stop: asyncio.Event, signum: int) -> None:
    """Stop once the step under way is done; a second signal, as when that step hangs, ends the process at once."""
    if stop.is_set():
        log.warning("fabricd stopped without waiting for the step under way")
        os._exit(128 + signum)
    stop.set()
