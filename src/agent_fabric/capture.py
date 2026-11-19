"""Capture: what fabricd reads of the agents' sessions, and the agentsview adapter (docs/design.md §4.1).

All agentsview access goes through `AgentsviewSource`. An agent agentsview can't read gets its own `CaptureSource`
(Cursor CLI, §4.1) without touching the rest. The models keep agentsview's own fields and names; turning them into
the normalized events of §4.2 is the normalizer's job.
"""

from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Protocol, Self
from urllib.parse import quote

import httpx
from httpx_sse import aconnect_sse
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

PAGE = 500
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
    def _empty_is_none(cls, value: Any) -> Any:
        # agentsview writes "" for a value it doesn't have, such as a Cursor CLI message's timestamp.
        return None if value == "" else value


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
        """One tick when the stream opens and one per change: a signal to read messages, carrying none."""
        ...

    def messages(self, session_id: str, from_ordinal: int) -> list[Message]:
        """The session's messages from `from_ordinal` on, in order."""
        ...

    def session(self, session_id: str) -> SessionMeta: ...


class _Messages(_Model):
    messages: tuple[Message, ...]


class _Sessions(_Model):
    sessions: tuple[SessionMeta, ...]
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
        found: list[SessionMeta] = []
        while True:
            page = _parse(_Sessions, self._get("/api/v1/sessions", params))
            found.extend(page.sessions)
            # Only the cursor says whether more follow: agentsview caps `limit` (500 in v0.46.1) without saying so.
            if not page.next_cursor or not page.sessions:
                return found
            params["cursor"] = page.next_cursor

    def session(self, session_id: str) -> SessionMeta:
        return _parse(SessionMeta, self._get(f"/api/v1/sessions/{_id(session_id)}", {}))

    def messages(self, session_id: str, from_ordinal: int) -> list[Message]:
        return list(self._messages(session_id, from_ordinal))

    def _messages(self, session_id: str, from_ordinal: int) -> Iterator[Message]:
        path = f"/api/v1/sessions/{_id(session_id)}/messages"
        while True:
            page = _parse(_Messages, self._get(path, {"from": from_ordinal, "limit": self._page}))
            # agentsview caps `limit` (1000 in v0.46.1) without saying so, and a page has no "more follow": read on
            # until a page comes back empty.
            later = [message for message in page.messages if message.ordinal >= from_ordinal]
            if not later:
                return
            yield from later
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
