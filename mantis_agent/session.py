"""Sessions — persistent conversation state with fork/resume.

A ``Session`` is a list of messages plus a small bag of metadata, identified
by a string id. Stores are plug-in: ship in-memory and SQLite backings, leave
Redis/Postgres to user code.

Design notes
------------
* The ``SessionStore`` protocol is intentionally tiny — five methods. Anything
  fancier (incremental append, snapshotting, vector indexing) belongs in a
  layer above; the protocol is the contract every backend must satisfy.
* Messages serialize as msgspec JSON. We hand back the bytes blob to SQLite
  rather than re-encoding into TEXT — msgspec round-trips a list[Message] in
  about a third of the time that ``json.dumps`` does for our shapes.
* SQLite I/O is dispatched to a worker thread via ``anyio.to_thread.run_sync``
  so we don't block the event loop. SQLite's own GIL handling is fine for the
  query rates we expect (sub-1k QPS); if a user needs more, they should bring
  Postgres.
* Fork copies the row to a new id, resets ``created_at`` to now, and records
  the parent in ``meta.forked_from``. The forked session's messages are a
  deep copy in the sense that they're a new BLOB — the original is untouched.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol, runtime_checkable

import anyio
import msgspec

from .hooks import HookContext, HookDispatcher
from .types import AssistantMessage, Message, SystemMessage, UserMessage

# ---------------------------------------------------------------------------
# Shared encoder/decoder. Reusing these is ~30% faster than constructing per call.
#
# Note: ``Message`` is a Union of *untagged* msgspec Structs (SystemMessage,
# UserMessage, AssistantMessage) — distinguished only by their ``role`` literal.
# msgspec won't dispatch a tagged union for us, so we decode to generic dicts
# and convert per-row using the ``role`` field. The cost vs a tagged decoder is
# ~one extra dict-build per message, which is negligible relative to disk I/O.
# ---------------------------------------------------------------------------

_ENCODE_RAW = msgspec.json.Encoder()
_DECODE_RAW = msgspec.json.Decoder(list)  # list of dicts

_ROLE_TO_TYPE: dict[str, type] = {
    "system": SystemMessage,
    "user": UserMessage,
    "assistant": AssistantMessage,
}


def _encode_messages(messages: list[Message]) -> bytes:
    """Encode messages as a JSON list of dicts with ``role`` forced in.

    The message structs use ``omit_defaults=True`` so encoding straight via
    msgspec drops the ``role`` field (it equals its Literal default). We
    dispatch on ``role`` at decode time, so we have to keep it in the blob.
    Builds an intermediate list-of-dicts representation and forces ``role``
    back onto each item.
    """

    payload: list[dict[str, Any]] = []
    for m in messages:
        d = msgspec.to_builtins(m)
        if isinstance(d, dict):
            # ``role`` is the dispatch key. Force it in even when omit_defaults
            # would otherwise drop it.
            d.setdefault("role", getattr(m, "role", None))
        payload.append(d)
    return _ENCODE_RAW.encode(payload)


def strip_context_messages(messages: list[Message]) -> list[Message]:
    """Return ``messages`` without the synthetic ``isMeta`` context/reminder
    messages (env/git/memory head, recall, todo). Those are ambient context the
    agent re-derives each run — replaying an old snapshot on resume would feed
    the model stale environment/memory. Real user turns (``isMeta`` False) and
    all assistant/tool messages are kept."""
    return [m for m in messages if not getattr(m, "isMeta", False)]


def strip_dangling_tool_uses(messages: list[Message]) -> list[Message]:
    """Drop assistant ``tool_use`` blocks that have no matching ``tool_result``
    anywhere in ``messages``.

    Truncating to a checkpoint (fork/resume) can land inside an open tool
    exchange, leaving a trailing assistant ``tool_use`` whose answering
    ``tool_result`` was cut off. Submitting that to a provider returns HTTP 400
    (unanswered tool_use). This mirrors ``session_tree._drop_dangling_tool_uses``
    for the SQLite/``Message`` path."""
    from .types import ToolResultBlock, ToolUseBlock  # noqa: PLC0415

    answered: set[str] = set()
    for m in messages:
        content = getattr(m, "content", None)
        if isinstance(content, list):
            for b in content:
                if isinstance(b, ToolResultBlock):
                    answered.add(b.tool_use_id)
    cleaned: list[Message] = []
    for m in messages:
        if isinstance(m, AssistantMessage):
            kept = [
                b
                for b in m.content
                if not (isinstance(b, ToolUseBlock) and b.id not in answered)
            ]
            if not kept:
                continue
            cleaned.append(msgspec.structs.replace(m, content=kept))
        else:
            cleaned.append(m)
    return cleaned


def _decode_messages(blob: bytes) -> list[Message]:
    """Decode a JSON blob back into typed messages, dispatching on ``role``."""

    raw = _DECODE_RAW.decode(blob)
    out: list[Message] = []
    for item in raw:
        role = item.get("role") if isinstance(item, dict) else None
        cls = _ROLE_TO_TYPE.get(role)
        if cls is None:
            # Unknown role — best effort: treat as user. Keeps load() lossy-safe.
            cls = UserMessage
        out.append(msgspec.convert(item, cls))
    return out


def _utcnow_iso() -> str:
    """ISO-8601 with Z suffix, microsecond precision. Sortable lexicographically.

    Sub-second precision keeps ``list_sessions`` ordering stable when several
    sessions are saved within the same wall-clock second (both stores sort on
    ``updated_at``)."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _updated_at_sort_key(ts: str) -> str:
    """Normalize a stored ``updated_at`` for lexicographic ordering.

    Legacy rows were written at second precision (``...:SSZ``); new rows carry
    microseconds (``...:SS.ffffffZ``). Because ``'.'`` (0x2E) sorts before
    ``'Z'`` (0x5A), a naive string compare ranks a genuinely-newer microsecond
    value *below* a same-second legacy value. Padding the legacy form to
    ``.000000Z`` restores a correct total order across the format boundary."""

    if ts.endswith("Z") and "." not in ts:
        return f"{ts[:-1]}.000000Z"
    return ts


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class SessionInfo(msgspec.Struct, frozen=True, omit_defaults=True):
    """Lightweight summary of a stored session — what ``list_sessions`` returns.

    Keep this small; the heavy payload (messages, full meta) is loaded on
    demand via ``load(id)``.
    """

    id: str
    created_at: str
    updated_at: str
    n_messages: int
    title: str | None = None
    tags: list[str] = []


@runtime_checkable
class SessionStore(Protocol):
    """Pluggable persistence for sessions.

    All methods are async. Implementations should make sure their disk/network
    I/O never blocks the event loop — wrap sync work with ``anyio.to_thread``.
    """

    async def save(
        self, session_id: str, messages: list[Message], meta: dict[str, Any]
    ) -> None: ...

    async def load(
        self, session_id: str
    ) -> tuple[list[Message], dict[str, Any]]: ...

    async def list_sessions(self) -> list[SessionInfo]: ...

    async def fork(self, session_id: str, new_id: str) -> None:
        """Copy ``session_id`` to ``new_id``. The new row gets ``created_at=now``
        and ``meta.forked_from = session_id``."""

    async def delete(self, session_id: str) -> None: ...


class SessionNotFoundError(KeyError):
    """Raised by stores when an id has no row."""


# ---------------------------------------------------------------------------
# Internal row shape
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    """Internal representation used by both stores."""

    id: str
    created_at: str
    updated_at: str
    title: str | None
    tags: list[str]
    meta: dict[str, Any]
    messages: bytes  # msgspec JSON of list[Message]
    n_messages: int

    def to_info(self) -> SessionInfo:
        return SessionInfo(
            id=self.id,
            created_at=self.created_at,
            updated_at=self.updated_at,
            n_messages=self.n_messages,
            title=self.title,
            tags=list(self.tags),
        )


def _row_from_save(
    session_id: str,
    messages: list[Message],
    meta: dict[str, Any],
    *,
    prev_created_at: str | None,
) -> _Row:
    """Build a row from a save() call. Preserves ``created_at`` if known."""

    now = _utcnow_iso()
    blob = _encode_messages(messages)
    title = meta.get("title")
    tags = list(meta.get("tags") or [])
    return _Row(
        id=session_id,
        created_at=prev_created_at or now,
        updated_at=now,
        title=title if isinstance(title, str) else None,
        tags=[t for t in tags if isinstance(t, str)],
        meta=dict(meta),
        messages=blob,
        n_messages=len(messages),
    )


# ---------------------------------------------------------------------------
# InMemorySessionStore
# ---------------------------------------------------------------------------


class InMemorySessionStore:
    """Dict-backed store. Useful for tests and short-lived processes.

    Concurrency: a single ``anyio.Lock`` guards mutation. Reads under the lock
    too — the cost is negligible and keeps the protocol race-free.
    """

    def __init__(self) -> None:
        self._rows: dict[str, _Row] = {}
        self._lock = anyio.Lock()

    async def save(
        self, session_id: str, messages: list[Message], meta: dict[str, Any]
    ) -> None:
        async with self._lock:
            prev = self._rows.get(session_id)
            prev_created = prev.created_at if prev is not None else None
            self._rows[session_id] = _row_from_save(
                session_id, messages, meta, prev_created_at=prev_created
            )

    async def load(
        self, session_id: str
    ) -> tuple[list[Message], dict[str, Any]]:
        async with self._lock:
            row = self._rows.get(session_id)
            if row is None:
                raise SessionNotFoundError(session_id)
            msgs = _decode_messages(row.messages)
            return msgs, dict(row.meta)

    async def list_sessions(self) -> list[SessionInfo]:
        async with self._lock:
            rows = list(self._rows.values())
        rows.sort(key=lambda r: _updated_at_sort_key(r.updated_at), reverse=True)
        return [r.to_info() for r in rows]

    async def fork(self, session_id: str, new_id: str) -> None:
        async with self._lock:
            src = self._rows.get(session_id)
            if src is None:
                raise SessionNotFoundError(session_id)
            if new_id in self._rows:
                raise ValueError(f"session {new_id!r} already exists")
            now = _utcnow_iso()
            new_meta = dict(src.meta)
            new_meta["forked_from"] = session_id
            self._rows[new_id] = _Row(
                id=new_id,
                created_at=now,
                updated_at=now,
                title=src.title,
                tags=list(src.tags),
                meta=new_meta,
                messages=bytes(src.messages),
                n_messages=src.n_messages,
            )

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._rows.pop(session_id, None)


# ---------------------------------------------------------------------------
# SqliteSessionStore
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  title TEXT,
  tags TEXT,
  meta TEXT,
  messages BLOB
);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);
"""


class SqliteSessionStore:
    """Single-file SQLite-backed store.

    A fresh connection is opened per worker call. ``check_same_thread=False``
    lets us hand the connection to the anyio thread pool. We rely on SQLite's
    own locking; for the throughput this SDK sees it's plenty.

    Pass ``path=":memory:"`` for tests, or any filesystem path for durability.
    The file is created with schema on first use.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        # Eagerly create the file + schema. Done synchronously at construct
        # time because callers expect the store to be usable immediately, and
        # this runs once per process.
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        # WAL gives us much better concurrent-read behavior at zero cost.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # async surface — every method dispatches to a worker thread
    # ------------------------------------------------------------------

    async def save(
        self, session_id: str, messages: list[Message], meta: dict[str, Any]
    ) -> None:
        await anyio.to_thread.run_sync(self._save_sync, session_id, messages, meta)

    async def load(
        self, session_id: str
    ) -> tuple[list[Message], dict[str, Any]]:
        return await anyio.to_thread.run_sync(self._load_sync, session_id)

    async def list_sessions(self) -> list[SessionInfo]:
        return await anyio.to_thread.run_sync(self._list_sync)

    async def fork(self, session_id: str, new_id: str) -> None:
        await anyio.to_thread.run_sync(self._fork_sync, session_id, new_id)

    async def delete(self, session_id: str) -> None:
        await anyio.to_thread.run_sync(self._delete_sync, session_id)

    # ------------------------------------------------------------------
    # blocking implementations — run on the worker thread
    # ------------------------------------------------------------------

    def _save_sync(
        self, session_id: str, messages: list[Message], meta: dict[str, Any]
    ) -> None:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT created_at FROM sessions WHERE id = ?", (session_id,)
            )
            existing = cur.fetchone()
            prev_created = existing[0] if existing is not None else None
            row = _row_from_save(session_id, messages, meta, prev_created_at=prev_created)
            conn.execute(
                """
                INSERT INTO sessions (id, created_at, updated_at, title, tags, meta, messages)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  updated_at=excluded.updated_at,
                  title=excluded.title,
                  tags=excluded.tags,
                  meta=excluded.meta,
                  messages=excluded.messages
                """,
                (
                    row.id,
                    row.created_at,
                    row.updated_at,
                    row.title,
                    json.dumps(row.tags),
                    json.dumps(row.meta),
                    row.messages,
                ),
            )
        finally:
            conn.close()

    def _load_sync(self, session_id: str) -> tuple[list[Message], dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT messages, meta FROM sessions WHERE id = ?", (session_id,)
            )
            row = cur.fetchone()
            if row is None:
                raise SessionNotFoundError(session_id)
            blob, meta_json = row
            messages = _decode_messages(blob)
            meta = json.loads(meta_json) if meta_json else {}
            return messages, meta
        finally:
            conn.close()

    def _list_sync(self) -> list[SessionInfo]:
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                SELECT id, created_at, updated_at, title, tags, messages
                FROM sessions
                """
            )
            out: list[SessionInfo] = []
            for sid, created, updated, title, tags_json, blob in cur.fetchall():
                # Cheap n_messages: decode raw list length only — no per-message
                # typed conversion needed for the summary view. Promote to a
                # stored column if list_sessions ever shows up in a profile.
                try:
                    n = len(_DECODE_RAW.decode(blob)) if blob else 0
                except msgspec.DecodeError:
                    n = 0
                out.append(
                    SessionInfo(
                        id=sid,
                        created_at=created,
                        updated_at=updated,
                        n_messages=n,
                        title=title,
                        tags=json.loads(tags_json) if tags_json else [],
                    )
                )
            out.sort(
                key=lambda i: _updated_at_sort_key(i.updated_at), reverse=True
            )
            return out
        finally:
            conn.close()

    def _fork_sync(self, session_id: str, new_id: str) -> None:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT title, tags, meta, messages FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise SessionNotFoundError(session_id)
            title, tags_json, meta_json, blob = row
            meta = json.loads(meta_json) if meta_json else {}
            meta["forked_from"] = session_id
            now = _utcnow_iso()
            try:
                conn.execute(
                    """
                    INSERT INTO sessions (id, created_at, updated_at, title, tags, meta, messages)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (new_id, now, now, title, tags_json, json.dumps(meta), blob),
                )
            except sqlite3.IntegrityError as e:
                raise ValueError(f"session {new_id!r} already exists") from e
        finally:
            conn.close()

    def _delete_sync(self, session_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# High-level Session API — checkpoints + fork + resume-from-checkpoint
# ---------------------------------------------------------------------------
#
# ``Session`` is a thin, friendly wrapper over ``SessionStore``. The store
# protocol is correct but raw — most users want an object they can append
# messages to, fork at an arbitrary point in the conversation, and resume
# (truncate to a checkpoint and continue running).
#
# The model we expose:
#
#   • A *checkpoint* is a stable handle to the boundary AFTER message N. So a
#     session with K messages has K checkpoints; ``checkpoint.index`` is the
#     number of messages to keep when we resume from that point. ``index=0``
#     means "rewind to before message 0" (an empty conversation); ``index=K``
#     means "current head".
#
#   • A *fork* makes a new session whose messages are a deep copy. Forking at
#     a specific checkpoint truncates the copy to that checkpoint's history.
#
#   • A *resume* mutates the current session in place — truncates to the
#     checkpoint and persists. The next ``run()`` will continue from that
#     truncated state. This is what users want when they hit a dead end and
#     want to back up two turns and try a different prompt.
#
# Keeping fork and resume on the same primitive (``index`` of a checkpoint)
# means there's exactly one rewind concept the user has to learn.


def _summarize_message(msg: Message) -> str:
    """Build a short one-line preview of a message — used in checkpoint
    summaries so the user can recognize what they're rewinding past.

    Content blocks are tagged msgspec Structs (``tag_field="type"``); the tag
    is class-level metadata, not an instance attribute, so we dispatch by
    duck-typed attribute lookup instead. That keeps this helper working as
    new content-block classes are added — anything with a ``text`` attribute
    is treated as prose, anything with a ``name`` + ``input`` shape is a tool
    use, etc.
    """

    role = getattr(msg, "role", "?")
    content = getattr(msg, "content", None)

    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            # Prefer explicit class-name dispatch so each block type gets a
            # recognizable marker. Falls back to a generic "[block]" tag for
            # anything we don't know about — checkpoints stay informative
            # without crashing when new block types appear.
            cls_name = type(block).__name__
            if cls_name == "TextBlock":
                parts.append(getattr(block, "text", "") or "")
            elif cls_name == "ThinkingBlock":
                # Thinking blocks are noisy — collapse to a marker.
                parts.append("[thinking]")
            elif cls_name == "ToolUseBlock":
                name = getattr(block, "name", "?")
                parts.append(f"[tool_use:{name}]")
            elif cls_name == "ToolResultBlock":
                parts.append("[tool_result]")
            elif cls_name == "ImageBlock":
                parts.append("[image]")
            else:
                parts.append(f"[{cls_name}]")
        text = " ".join(parts).strip()
    else:
        text = ""

    text = " ".join(text.split())  # collapse whitespace
    if len(text) > 80:
        text = text[:77] + "..."
    return f"{role}: {text}" if text else role


class Checkpoint(msgspec.Struct, frozen=True, omit_defaults=True):
    """A handle to a point in a session's message history.

    ``index`` is the number of messages BEFORE this checkpoint. Forking or
    resuming at the checkpoint keeps messages ``[0:index]``. This is the
    natural "slice" semantics — ``checkpoint.index == len(messages)`` means
    HEAD, ``index == 0`` means an empty conversation.

    ``role`` and ``summary`` are decorative — they let callers display a list
    of checkpoints to a user ("rewind to: user: what's the weather?") without
    re-decoding every message blob.
    """

    index: int
    role: str
    summary: str


def make_checkpoints(messages: Iterable[Message]) -> list[Checkpoint]:
    """Compute checkpoints from a message list. One per message.

    The checkpoint with ``index=k`` represents the state AFTER message
    ``messages[k-1]`` (i.e. ``messages[:k]`` is what survives a resume).
    """

    out: list[Checkpoint] = []
    for i, msg in enumerate(messages, start=1):
        out.append(
            Checkpoint(
                index=i,
                role=getattr(msg, "role", "?"),
                summary=_summarize_message(msg),
            )
        )
    return out


def _is_cancellation(exc: BaseException) -> bool:
    """Is ``exc`` this backend's cancellation exception?

    asyncio raises ``CancelledError`` and trio raises ``trio.Cancelled``; anyio
    knows which one is live, so ask it instead of hard-coding either. Outside a
    running async context there is no backend to ask — sniffio signals that
    with ``AsyncLibraryNotFoundError`` (a ``RuntimeError``) — so treat it as
    "not a cancellation" rather than letting the probe escape.
    """

    try:
        return isinstance(exc, anyio.get_cancelled_exc_class())
    except RuntimeError:
        return False


async def _dispatch_session_event(
    dispatcher: "HookDispatcher | None",
    event: str,
    session: "Session",
    *,
    reason: str,
    error: BaseException | None = None,
    shield: bool = False,
) -> None:
    """Fire ``SessionStart`` / ``SessionEnd`` for ``session``.

    Both are observability events — neither is in ``BLOCKING_EVENTS``, so the
    dispatcher swallows a raising hook and the session lifecycle continues
    regardless. ``reason`` distinguishes the several ways each end of the
    lifecycle is reached ("create" / "resume" / "fork"; "closed" / "error" /
    "cancelled") because that is the first thing a session-auditing hook wants
    and it cannot be recovered afterwards.

    The ``has()`` guard keeps the no-hook path to a single dict lookup, which
    matters because ``Session.load`` is on the interactive resume path.

    ``shield`` is set for ``SessionEnd`` only. Teardown is exactly when the
    surrounding task is likely to be under cancellation, and a plain ``await``
    there is interrupted at the hook's first checkpoint — losing the one event
    a cleanup hook exists for. ``SessionStart`` deliberately does NOT shield: a
    startup hook that hangs must stay cancellable.
    """

    if dispatcher is None or not dispatcher.has(event):
        return
    extras: dict[str, Any] = {
        "session_id": session.id,
        "reason": reason,
        "message_count": len(session.messages),
    }
    if error is not None:
        extras["error"] = str(error)
        extras["error_type"] = type(error).__name__
    ctx = HookContext(
        event=event,
        messages_snapshot=session.messages,
        arbitrary=extras,
    )
    # A shielded scope runs the dispatch to completion even if the enclosing
    # scope is already cancelled; the cancellation is still delivered to the
    # caller the moment we return, so this defers teardown by the hook's own
    # duration and no longer. See the ``shield`` note in the docstring.
    with anyio.CancelScope(shield=shield):
        await dispatcher.dispatch(event, ctx)


class Session:
    """High-level conversation handle backed by a ``SessionStore``.

    The session owns an in-memory message list. ``save()`` flushes it to the
    store. ``load`` and ``create`` are the two entry points.

    Resume + fork are both checkpoint-driven. A checkpoint is the boundary
    AFTER message N; truncating to a checkpoint with ``index=k`` keeps the
    first ``k`` messages.

    Passing ``dispatcher`` opts the session into the ``SessionStart`` /
    ``SessionEnd`` hook lifecycle. ``SessionStart`` fires from the factories
    (``create``, ``load``, ``fork``); ``SessionEnd`` fires from ``close()``,
    which ``async with`` calls for you on every exit path including error and
    cancellation::

        async with await Session.create(store, dispatcher=d) as sess:
            ...
        # SessionEnd has fired exactly once by here
    """

    __slots__ = ("_store", "_id", "_messages", "_meta", "_dispatcher", "_ended")

    def __init__(
        self,
        store: SessionStore,
        session_id: str,
        messages: list[Message] | None = None,
        meta: dict[str, Any] | None = None,
        *,
        dispatcher: "HookDispatcher | None" = None,
    ) -> None:
        self._store = store
        self._id = session_id
        self._messages = list(messages) if messages is not None else []
        self._meta = dict(meta) if meta is not None else {}
        self._dispatcher = dispatcher
        # Latch: SessionEnd is a once-per-session event. Set before the hook is
        # awaited so a re-entrant or concurrent ``close()`` cannot double-fire.
        self._ended = False

    # -- factory methods -------------------------------------------------

    @classmethod
    async def create(
        cls,
        store: SessionStore,
        session_id: str | None = None,
        *,
        title: str | None = None,
        tags: Iterable[str] | None = None,
        meta: dict[str, Any] | None = None,
        dispatcher: "HookDispatcher | None" = None,
    ) -> "Session":
        """Create a fresh session and persist it immediately.

        ``session_id`` is auto-generated (UUID4) if not provided. The empty
        message list is saved so subsequent ``list_sessions`` calls see it.

        Fires ``SessionStart`` (reason ``"create"``) after the row exists, so a
        hook that reads the store back sees the session it was told about.
        """

        sid = session_id or f"sess_{uuid.uuid4().hex[:16]}"
        m = dict(meta) if meta else {}
        if title is not None:
            m["title"] = title
        if tags is not None:
            m["tags"] = list(tags)
        sess = cls(store, sid, messages=[], meta=m, dispatcher=dispatcher)
        await sess.save()
        await _dispatch_session_event(
            dispatcher, "SessionStart", sess, reason="create"
        )
        return sess

    @classmethod
    async def load(
        cls,
        store: SessionStore,
        session_id: str,
        *,
        fresh_context: bool = True,
        dispatcher: "HookDispatcher | None" = None,
    ) -> "Session":
        """Load an existing session from the store.

        ``fresh_context`` (default True) drops the synthetic ``isMeta``
        context/reminder messages (env + git + memory head, recall, todo) so the
        agent RE-DERIVES current context on the next run instead of replaying a
        stale snapshot from when the session was first created — the env, git
        branch, or MANTIS.md may have changed since. Pass False to keep the
        frozen head verbatim.

        Fires ``SessionStart`` (reason ``"resume"``) — loading an existing
        session IS the resume path, and a hook wants to tell the two apart.
        """

        messages, meta = await store.load(session_id)
        if fresh_context:
            messages = strip_context_messages(messages)
        sess = cls(
            store, session_id, messages=messages, meta=meta, dispatcher=dispatcher
        )
        await _dispatch_session_event(
            dispatcher, "SessionStart", sess, reason="resume"
        )
        return sess

    # -- properties ------------------------------------------------------

    @property
    def id(self) -> str:
        return self._id

    @property
    def messages(self) -> list[Message]:
        # Return the live list — callers can append/extend directly if they
        # don't want the helper methods. The store doesn't see mutations
        # until ``save()`` is called either way.
        return self._messages

    @property
    def meta(self) -> dict[str, Any]:
        return self._meta

    @property
    def store(self) -> SessionStore:
        return self._store

    def __len__(self) -> int:
        return len(self._messages)

    # -- mutation helpers ------------------------------------------------

    def append(self, message: Message) -> None:
        """Append a message in memory. Call ``save()`` to persist."""

        self._messages.append(message)

    def extend(self, messages: Iterable[Message]) -> None:
        """Append many messages in memory. Call ``save()`` to persist."""

        self._messages.extend(messages)

    def clear(self) -> None:
        """Remove all messages in memory. Call ``save()`` to persist."""

        self._messages.clear()

    # -- persistence -----------------------------------------------------

    async def save(self) -> None:
        """Persist the current state to the store."""

        await self._store.save(self._id, self._messages, self._meta)

    async def reload(self) -> None:
        """Re-read this session from the store, discarding in-memory edits."""

        msgs, meta = await self._store.load(self._id)
        self._messages = list(msgs)
        self._meta = dict(meta)

    async def delete(self) -> None:
        """Remove this session from the store. The in-memory state is left
        intact so callers can re-save under a new id if they want."""

        await self._store.delete(self._id)

    # -- lifecycle -------------------------------------------------------

    async def close(
        self, *, reason: str = "closed", error: BaseException | None = None
    ) -> None:
        """End the session, firing ``SessionEnd`` exactly once.

        Idempotent: the second and later calls are no-ops, so a caller that
        closes explicitly inside an ``async with`` block does not double-fire.
        This does NOT save, delete, or otherwise touch the store — closing is
        about the *lifecycle*, and silently flushing here would make ``close``
        a hidden write that a caller who deliberately discarded in-memory edits
        did not ask for.

        ``reason`` / ``error`` describe how the session ended; ``__aexit__``
        fills them in from the exception that is unwinding.
        """

        if self._ended:
            return
        self._ended = True
        await _dispatch_session_event(
            self._dispatcher,
            "SessionEnd",
            self,
            reason=reason,
            error=error,
            shield=True,
        )

    async def __aenter__(self) -> "Session":
        # ``SessionStart`` already fired in the factory that built this object;
        # firing again here would double-report every ``async with await
        # Session.create(...)``, which is the documented usage.
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        # Classify the exit so a cleanup hook can distinguish a clean close
        # from a crash from an operator cancelling the run. anyio's cancellation
        # exception differs by backend (asyncio's CancelledError vs trio's
        # Cancelled), so ask anyio rather than isinstance-ing one of them.
        if exc_type is None:
            reason, err = "closed", None
        elif isinstance(exc, BaseException) and _is_cancellation(exc):
            reason, err = "cancelled", None
        else:
            reason, err = "error", exc if isinstance(exc, BaseException) else None
        await self.close(reason=reason, error=err)
        return False  # never swallow — we observe the exit, we don't own it

    # -- checkpoints + fork + resume ------------------------------------

    def checkpoints(self) -> list[Checkpoint]:
        """List checkpoints — one per message. See :class:`Checkpoint`."""

        return make_checkpoints(self._messages)

    def _resolve_checkpoint(
        self, checkpoint: "Checkpoint | int | None"
    ) -> int:
        """Normalize ``checkpoint`` to an integer ``index`` and validate it.

        Accepts a ``Checkpoint`` instance, a raw int index, or ``None`` to
        mean "current head". Raises ``ValueError`` if out of range.
        """

        if checkpoint is None:
            return len(self._messages)
        if isinstance(checkpoint, Checkpoint):
            idx = checkpoint.index
        else:
            idx = int(checkpoint)
        if idx < 0 or idx > len(self._messages):
            raise ValueError(
                f"checkpoint index {idx} out of range [0, {len(self._messages)}]"
            )
        return idx

    async def fork(
        self,
        new_id: str | None = None,
        *,
        checkpoint: "Checkpoint | int | None" = None,
    ) -> "Session":
        """Make a forked Session.

        ``new_id`` is auto-generated if omitted.

        ``checkpoint`` controls how much of the conversation the fork
        inherits. ``None`` (default) clones the full history; otherwise the
        fork keeps only ``messages[:checkpoint.index]``.

        The forked session is persisted before returning. Its ``meta`` is a
        shallow copy of the source's plus ``forked_from = source_id`` and,
        when truncated, ``forked_at_index = idx``.

        The fork inherits this session's dispatcher and fires ``SessionStart``
        (reason ``"fork"``) for the new id — a fork IS a session creation, and
        a hook that indexes sessions must not miss the ones born this way.
        """

        new_sid = new_id or f"sess_{uuid.uuid4().hex[:16]}"

        if checkpoint is None:
            # Full fork — copy the CURRENT in-memory state, not the persisted
            # row. Delegating to store.fork() would clone the last-saved BLOB
            # and silently drop any unsaved in-memory turns (append() only
            # mutates memory); it would also resurrect the isMeta context a
            # fresh_context load stripped. Forking from self._messages keeps the
            # fork in sync with what the caller sees, matching the truncated
            # path below.
            new_meta = dict(self._meta)
            new_meta["forked_from"] = self._id
            forked = list(self._messages)
            await self._store.save(new_sid, forked, new_meta)
            child = Session(
                self._store,
                new_sid,
                messages=forked,
                meta=new_meta,
                dispatcher=self._dispatcher,
            )
            await _dispatch_session_event(
                self._dispatcher, "SessionStart", child, reason="fork"
            )
            return child

        idx = self._resolve_checkpoint(checkpoint)
        # Strip any tool_use left dangling by cutting inside an open tool
        # exchange, else the fork's next run 400s on the unanswered tool_use.
        truncated = strip_dangling_tool_uses(list(self._messages[:idx]))
        new_meta = dict(self._meta)
        new_meta["forked_from"] = self._id
        new_meta["forked_at_index"] = idx
        await self._store.save(new_sid, truncated, new_meta)
        child = Session(
            self._store,
            new_sid,
            messages=truncated,
            meta=new_meta,
            dispatcher=self._dispatcher,
        )
        await _dispatch_session_event(
            self._dispatcher, "SessionStart", child, reason="fork"
        )
        return child

    async def resume_from(
        self, checkpoint: "Checkpoint | int"
    ) -> "Session":
        """Truncate THIS session in place to the checkpoint and persist.

        Returns self for chaining. Use ``fork(checkpoint=...)`` instead if
        you want to keep the original history untouched.
        """

        idx = self._resolve_checkpoint(checkpoint)
        # Strip any tool_use left dangling by cutting inside an open tool
        # exchange, else the next run 400s on the unanswered tool_use.
        self._messages = strip_dangling_tool_uses(self._messages[:idx])
        # Track the rewind in meta for audit — useful when debugging
        # "why did this session lose its tail?" later.
        history = list(self._meta.get("resume_history") or [])
        history.append(
            {"index": idx, "at": _utcnow_iso()}
        )
        self._meta["resume_history"] = history
        await self.save()
        return self


# ---------------------------------------------------------------------------
# Top-level convenience — for users who don't want the Session wrapper
# ---------------------------------------------------------------------------


async def fork_session(
    store: SessionStore,
    src_id: str,
    new_id: str | None = None,
    *,
    checkpoint: "Checkpoint | int | None" = None,
) -> str:
    """Fork ``src_id`` into ``new_id`` (auto-generated if omitted).

    With ``checkpoint=None`` (default) this calls the store's native ``fork``,
    which is typically a single-row copy. With a checkpoint, it loads,
    truncates, and saves under the new id — the same operation
    ``Session.fork(checkpoint=...)`` performs.

    Returns the new session id.
    """

    new_sid = new_id or f"sess_{uuid.uuid4().hex[:16]}"

    if checkpoint is None:
        await store.fork(src_id, new_sid)
        return new_sid

    # Truncated fork — round-trip through load/save so we don't need a
    # truncate-aware fork primitive on every store.
    messages, meta = await store.load(src_id)
    # Strip the synthetic isMeta context first so ``checkpoint.index`` means the
    # same thing here as in ``Session.checkpoints()`` (which runs on the
    # fresh_context-stripped list). Without this, a checkpoint chosen from a
    # Session view would cut at a different message on the unstripped list.
    messages = strip_context_messages(messages)
    if isinstance(checkpoint, Checkpoint):
        idx = checkpoint.index
    else:
        idx = int(checkpoint)
    if idx < 0 or idx > len(messages):
        raise ValueError(
            f"checkpoint index {idx} out of range [0, {len(messages)}]"
        )
    new_meta = dict(meta)
    new_meta["forked_from"] = src_id
    new_meta["forked_at_index"] = idx
    # Drop any tool_use left dangling by cutting inside an open tool exchange.
    truncated = strip_dangling_tool_uses(messages[:idx])
    await store.save(new_sid, truncated, new_meta)
    return new_sid


async def resume_session(
    store: SessionStore,
    session_id: str,
    checkpoint: "Checkpoint | int",
    *,
    dispatcher: "HookDispatcher | None" = None,
) -> Session:
    """Truncate ``session_id`` to ``checkpoint`` in place. Returns a
    :class:`Session` you can continue using.

    With a ``dispatcher``, the underlying ``Session.load`` fires
    ``SessionStart`` (reason ``"resume"``) — once, for the load, not again for
    the truncation, which is a mutation of an already-started session."""

    sess = await Session.load(store, session_id, dispatcher=dispatcher)
    await sess.resume_from(checkpoint)
    return sess
