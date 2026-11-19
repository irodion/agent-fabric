"""Capture: what fabricd reads of the agents' sessions, and the agentsview adapter (docs/design.md §4.1).

All agentsview access goes through `AgentsviewSource`. An agent agentsview can't read gets its own `CaptureSource`
(Cursor CLI, §4.1) without touching the rest. The models keep agentsview's own fields and names; turning them into
the normalized events of §4.2 is the normalizer's job.
"""

import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import NoneType, TracebackType
from typing import Any, Protocol, Self, get_args, get_origin
from urllib.parse import quote

import httpx
from httpx_sse import aconnect_sse
from pydantic import BaseModel, ConfigDict, ValidationError, ValidationInfo, field_validator

log = logging.getLogger(__name__)

PAGE = 500
# How often a read of a session's messages starts over because agentsview re-parsed the transcript mid-read.
REVISION_TRIES = 3
TIMEOUT = 10.0
# A session's watch sends a heartbeat every 30 s, so a stream quiet for longer than this has died.
WATCH_READ_TIMEOUT = 90.0
# The watch events that mean the session changed. session_ready comes first on every stream: whatever changed
# between the caller's last read and the stream opening shows up there.
CHANGE_EVENTS = frozenset({"session_ready", "session_updated"})


class CaptureError(Exception):
    """agentsview couldn't be asked, or answered with an error or with something the adapter can't read."""


class _Model(BaseModel):
    # agentsview adds fields between versions; the contract tests catch what the adapter reads changing.
    model_config = ConfigDict(frozen=True, extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _empty_is_none(cls, value: Any, info: ValidationInfo) -> Any:
        if not info.field_name:
            return value
        annotation = cls.model_fields[info.field_name].annotation
        # agentsview writes "" for a value it doesn't have, such as a Cursor CLI message's timestamp. A field that
        # must hold a string keeps it: a session with no project worked out is still a session.
        if value == "" and NoneType in get_args(annotation):
            return None
        # Go writes an empty list it never filled as null, as agentsview does for a page past the last message.
        if value is None and get_origin(annotation) is tuple:
            return ()
        return value


class ToolResultEvent(_Model):
    status: str | None = None  # completed, errored, running, started, …; Claude Code leaves a success's empty
    content: str | None = None
    timestamp: datetime | None = None


class ToolCall(_Model):
    tool_use_id: str | None = None
    tool_name: str
    category: str
    input_json: str | None = None
    result_content: str | None = None  # in full, except Claude Code's Read and Glob results (§4.1)
    result_events: tuple[ToolResultEvent, ...] = ()


class Message(_Model):
    session_id: str
    ordinal: int  # Claude Code and Copilot CLI skip the ordinals of tool results, which fold into the call
    role: str
    timestamp: datetime | None
    content: str | None
    thinking_text: str | None
    model: str | None
    is_system: bool
    tool_calls: tuple[ToolCall, ...] = ()


class SessionMeta(_Model):
    id: str  # `codex:…`, `copilot:…`; Claude Code's are bare
    agent: str
    project: str
    cwd: str | None = None  # None for Copilot CLI (§11)
    git_branch: str | None = None
    parent_session_id: str | None = None
    relationship_type: str | None = None  # `subagent` for Claude Code's and Codex's headless runs too (§4.3)
    session_kind: str | None = None  # Codex: `non-interactive` for `codex exec`
    entrypoint: str | None = None  # Claude Code: `sdk-cli` for `claude -p` and SDK runs
    is_automated: bool
    started_at: datetime | None
    ended_at: datetime | None
    message_count: int
    termination_status: str | None = None  # only Codex sets it
    transcript_revision: str | None = None


class CaptureSource(Protocol):
    def active_sessions(self, since: datetime) -> list[SessionMeta]:
        """Sessions with activity since `since`, headless runs and one-message sessions included."""
        ...

    def watch(self, session_id: str) -> AsyncIterator[None]:
        """One tick when the stream opens and one per change: a signal to read messages, carrying none.

        It ends when agentsview closes the stream, as on a restart: the caller opens it again while the session is
        active, and the opening tick covers what changed in between."""
        ...

    def messages(self, session_id: str, from_ordinal: int) -> "Transcript":
        """The session's messages from `from_ordinal` on, in order, all of the one transcript revision returned.

        A message read before its tool calls finished can change: a result folds into the message that made the
        call. So the caller reads again from the first message with a call not yet finished, not only past the
        last ordinal it has. A revision other than the one the caller read before means agentsview re-parsed the
        session, and ordinals read before may no longer hold."""
        ...

    def session(self, session_id: str) -> SessionMeta: ...


@dataclass(frozen=True)
class Transcript:
    revision: str | None  # agentsview's `transcript_revision`; None when the read found no messages
    messages: tuple[Message, ...]


class _Messages(_Model):
    messages: tuple[Message, ...]
    transcript_revision: str | None = None


class _Sessions(_Model):
    sessions: tuple[dict[str, Any], ...]  # read one by one, so one session the adapter can't read skips alone
    next_cursor: str | None = None


class AgentsviewSource:
    """`CaptureSource` over agentsview's REST API and a session's SSE watch.

    `base_url` is the unit's `http://127.0.0.1:<port>` (§4.1). The transports are for tests."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
        page: int = PAGE,
    ) -> None:
        self._base_url = base_url
        self._async_transport = async_transport
        self._page = page
        self._client = httpx.Client(base_url=base_url, transport=transport, timeout=TIMEOUT)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        self.close()

    def active_sessions(self, since: datetime) -> list[SessionMeta]:
        params: dict[str, str | int] = {
            "active_since": _iso(since),
            "include_children": "true",  # headless runs are filed as children (§4.1)
            "include_one_shot": "true",  # the default list hides sessions with one user message
            "limit": self._page,
        }
        found: dict[str, SessionMeta] = {}
        cursors: set[str] = set()
        while True:
            page = _parse(_Sessions, self._get("/api/v1/sessions", params))
            for listed in page.sessions:
                try:
                    session = SessionMeta.model_validate(listed)
                except ValidationError as error:
                    # One session the adapter can't read mustn't stop capture of every other (§4.1).
                    log.warning("skipping agentsview session %s: %s", listed.get("id"), error)
                    continue
                # The list is ordered by activity, so a session active during the read can show on two pages.
                found.setdefault(session.id, session)
            # Only the cursor says whether more follow: agentsview caps `limit` (500 in v0.46.1) without saying so.
            if not page.next_cursor or not page.sessions:
                return list(found.values())
            if page.next_cursor in cursors:
                raise CaptureError("agentsview /api/v1/sessions: the cursor came back to a page already read")
            cursors.add(page.next_cursor)
            params["cursor"] = page.next_cursor

    def session(self, session_id: str) -> SessionMeta:
        return _parse(SessionMeta, self._get(f"/api/v1/sessions/{_id(session_id)}", {}))

    def messages(self, session_id: str, from_ordinal: int) -> Transcript:
        for _ in range(REVISION_TRIES):
            read = self._one_revision(session_id, from_ordinal)
            if read is not None:
                return read
        raise CaptureError(f"agentsview session {session_id}: the transcript kept changing while it was read")

    def _one_revision(self, session_id: str, from_ordinal: int) -> Transcript | None:
        """The messages from `from_ordinal` on, or None when agentsview re-parsed the transcript between pages."""
        path = f"/api/v1/sessions/{_id(session_id)}/messages"
        found: list[Message] = []
        revisions: set[str | None] = set()
        while True:
            page = _parse(_Messages, self._get(path, {"from": from_ordinal, "limit": self._page}))
            # agentsview caps `limit` (1000 in v0.46.1) without saying so, and a page has no "more follow": read on
            # until a page comes back empty. An empty page carries no revision: agentsview looks it up with the rows.
            if not page.messages:
                return Transcript(next(iter(revisions), None), tuple(found))
            revisions.add(page.transcript_revision)
            if len(revisions) > 1:
                return None
            later = [message for message in page.messages if message.ordinal >= from_ordinal]
            if not later:
                raise CaptureError(f"agentsview {path}: asked from ordinal {from_ordinal}, got only earlier ones")
            found.extend(later)
            from_ordinal = later[-1].ordinal + 1

    async def watch(self, session_id: str) -> AsyncIterator[None]:
        timeout = httpx.Timeout(TIMEOUT, read=WATCH_READ_TIMEOUT)
        path = f"/api/v1/sessions/{_id(session_id)}/watch"
        try:
            async with (
                httpx.AsyncClient(
                    base_url=self._base_url, transport=self._async_transport, timeout=timeout
                ) as client,
                aconnect_sse(client, "GET", path) as events,
            ):
                if events.response.status_code != httpx.codes.OK:
                    await events.response.aread()
                    raise CaptureError(_status(path, events.response))
                async for event in events.aiter_sse():
                    if event.event in CHANGE_EVENTS:
                        yield None
        except httpx.HTTPError as error:
            raise CaptureError(f"agentsview {path}: {error!r}") from error

    def _get(self, path: str, params: Mapping[str, str | int]) -> Any:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as error:
            raise CaptureError(f"agentsview {path}: {error!r}") from error
        if response.status_code != httpx.codes.OK:
            raise CaptureError(_status(path, response))
        try:
            return response.json()
        except ValueError as error:
            raise CaptureError(f"agentsview {path}: not JSON") from error


def _parse[M: BaseModel](model: type[M], body: Any) -> M:
    try:
        return model.model_validate(body)
    except ValidationError as error:
        raise CaptureError(f"agentsview answered with what the adapter can't read: {error}") from error


def _id(session_id: str) -> str:
    return quote(session_id, safe="")


def _iso(when: datetime) -> str:
    if when.tzinfo is None:
        raise ValueError("a naive datetime has no zone to send")
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _status(path: str, response: httpx.Response) -> str:
    detail = response.text[:200].strip()
    return f"agentsview {path}: HTTP {response.status_code}" + (f": {detail}" if detail else "")
