"""Claude-Code-faithful conversation transcripts: a parentUuid-linked **tree**
persisted as append-only JSONL, with resume / branch / rewind.

This is a direct port of Claude Code's session-storage model (see
``utils/sessionStorage.ts``, ``utils/listSessionsImpl.ts``,
``utils/conversationRecovery.ts``, ``commands/branch/branch.ts`` in the CLI):

* **One session = one ``.jsonl`` file**, append-only, under a project-scoped
  directory keyed by the (hashed) cwd — ``projects/{digest}/{session_id}.jsonl``.
* **Every entry carries its own ``uuid`` and a ``parent_uuid``**, so the file is
  a flat log but reconstructs into a *tree*. Any single resumed view is the
  linear path from root to a chosen leaf (``build_chain``).
* **Resume** = parse the file, find the newest non-sidechain leaf (a message
  that is no other message's parent), walk ``parent_uuid`` to the root, reverse.
* **Branch** = copy the main thread into a NEW file, re-stamp ``session_id`` and
  re-link the ``parent_uuid`` chain from scratch, recording a ``forked_from``
  backpointer. The original file is untouched, so both are independently
  resumable — the tree forks at the filesystem level into two linear sessions.
* **Rewind** = truncate the chain to (and including) a chosen message; the next
  appended message parents off it, which *is* a branch within the same file.
* **Listing** never full-parses: it reads only the head + tail of each file and
  substring-scans for the title / first prompt / mtime (cheap for a picker).

Storage is msgspec + stdlib only — no third-party deps.
"""

from __future__ import annotations

import json
import logging
import os
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import msgspec

from . import __version__
from .paths import get_project_dir
from .types import AssistantMessage, Message, TextBlock, UserMessage

_log = logging.getLogger("mantis_agent.session_tree")

# Message-bearing entry types (everything else on a line is metadata).
_MESSAGE_TYPES = frozenset({"user", "assistant", "system", "attachment"})
_LITE_READ_BYTES = 65_536  # head/tail window the picker reads (matches CC's 64KB)


# ---------------------------------------------------------------------------
# Entry model
# ---------------------------------------------------------------------------


class TranscriptEntry(msgspec.Struct, omit_defaults=True, kw_only=True):
    """One line of the transcript. Mirrors Claude Code's ``TranscriptMessage``.

    The ``parent_uuid`` linked list — NOT file order — is the source of truth
    for conversation ordering; that's what makes branching/rewind possible.
    """

    uuid: str
    parent_uuid: str | None
    type: str  # 'user' | 'assistant' | 'system' | 'attachment'
    message: dict  # {"role": ..., "content": str | [blocks]}
    timestamp: str
    session_id: str
    cwd: str
    is_sidechain: bool = False
    git_branch: str | None = None
    is_meta: bool = False
    version: str = __version__
    # Set on branched entries: {"session_id": ..., "message_uuid": ...}
    forked_from: dict | None = None


_ENC = msgspec.json.Encoder()
_DEC = msgspec.json.Decoder()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_session_id() -> str:
    """A session id is a uuid4 (the ``.jsonl`` filename is ``{id}.jsonl``)."""
    return str(_uuid.uuid4())


def _session_path(session_id: str, cwd: str | Path | None) -> Path:
    return get_project_dir(cwd) / f"{session_id}.jsonl"


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class SessionTranscript:
    """Append-only writer/reader for one session's ``.jsonl`` tree.

    Threads ``parent_uuid`` forward across appended messages (port of
    ``insertMessageChain``): each new message parents off the current tip, so
    the writer produces a strictly linear chain. Non-linear structure (rewind
    branches, and the DAG edges parallel tool calls create) is expressed by
    passing an explicit ``parent_uuid`` to :meth:`append_message`; the writer
    does not derive tool_result→originating-assistant edges on its own.
    """

    def __init__(self, session_id: str, *, cwd: str | Path | None = None) -> None:
        self.session_id = session_id
        self.cwd = str(Path(cwd).resolve()) if cwd else os.getcwd()
        self.path = _session_path(session_id, self.cwd)
        # The tip of the current chain — next message parents off this.
        self._tip: str | None = None
        self._seen: set[str] = set()
        if self.path.exists():
            self._rehydrate_tip()

    def _rehydrate_tip(self) -> None:
        entries = [e for e in self.load() if not e.is_sidechain]
        self._seen = {e.uuid for e in entries}
        leaf = latest_leaf(entries)
        self._tip = leaf.uuid if leaf else None

    # -- append ----------------------------------------------------------

    def append_message(
        self, role: str, content: Any, *, is_meta: bool = False,
        parent_uuid: str | None = "__tip__",
    ) -> TranscriptEntry:
        """Append a user/assistant/system message, threading the chain.

        ``content`` is a string or a list of content blocks. Pass an explicit
        ``parent_uuid`` to branch off a specific message (rewind); the default
        threads off the current tip.
        """
        etype = "assistant" if role == "assistant" else (
            "system" if role == "system" else "user"
        )
        parent = self._tip if parent_uuid == "__tip__" else parent_uuid
        # An explicit parent that names no known message would create an orphan
        # node — a spurious root that resume's leaf-picker can mistake for the
        # true head. Fall back to the current tip so the chain stays connected.
        if parent is not None and parent not in self._seen:
            _log.warning(
                "session_tree: parent %s is unknown; attaching new %s message "
                "to the current tip %s instead of orphaning it",
                parent, etype, self._tip,
            )
            parent = self._tip
        entry = TranscriptEntry(
            uuid=str(_uuid.uuid4()),
            parent_uuid=parent,
            type=etype,
            message={"role": role, "content": content},
            timestamp=_now_iso(),
            session_id=self.session_id,
            cwd=self.cwd,
            is_meta=is_meta,
            git_branch=_git_branch(self.cwd),
        )
        self._append_raw(_ENC.encode(entry))
        self._tip = entry.uuid
        self._seen.add(entry.uuid)
        return entry

    def append_meta(self, **fields: Any) -> None:
        """Append a non-message metadata line (``custom-title``/``summary``/
        ``last-prompt``). Keyed by ``session_id``; read from the file tail by the
        cheap session lister."""
        rec = {"session_id": self.session_id, **fields}
        self._append_raw(json.dumps(rec, separators=(",", ":")).encode())

    def set_title(self, title: str) -> None:
        self.append_meta(type="custom-title", custom_title=title)

    def record_last_prompt(self, prompt: str) -> None:
        self.append_meta(type="last-prompt", last_prompt=prompt)

    def _append_raw(self, line: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as fh:
            fh.write(line.rstrip(b"\n") + b"\n")
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass

    # -- read ------------------------------------------------------------

    def load(self) -> list[TranscriptEntry]:
        return load_entries(self.path)

    def messages(self) -> list[Message]:
        """The resumable conversation: chain to the newest leaf → Message objs."""
        entries = self.load()
        leaf = latest_leaf([e for e in entries if not e.is_sidechain])
        if leaf is None:
            return []
        return entries_to_messages(build_chain(entries, leaf.uuid))


# ---------------------------------------------------------------------------
# Tree reconstruction
# ---------------------------------------------------------------------------


def load_entries(path: Path) -> list[TranscriptEntry]:
    """Parse a transcript file. Message lines → ``TranscriptEntry``; metadata
    lines (no ``parent_uuid``) are skipped here (the lister reads those)."""
    out: list[TranscriptEntry] = []
    if not path.exists():
        return out
    dropped = 0  # message lines lost to decode/validation errors (not metadata)
    with path.open("rb") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = _DEC.decode(raw)
            except msgspec.DecodeError:
                # Could be a trailing partial line (benign) or a corrupted
                # interior message line (drops descendants). Count it either way.
                dropped += 1
                continue
            if not isinstance(obj, dict) or obj.get("type") not in _MESSAGE_TYPES:
                continue
            if "uuid" not in obj or "parent_uuid" not in obj:
                continue
            try:
                out.append(msgspec.convert(obj, TranscriptEntry))
            except msgspec.ValidationError:
                # A well-formed JSON line that no longer matches the schema —
                # its uuid is gone, so any descendants orphan. Surface it.
                dropped += 1
                continue
    if dropped:
        _log.warning(
            "session_tree: dropped %d unparseable line(s) from %s; "
            "conversation history may be truncated at a corrupted entry",
            dropped,
            path,
        )
    return out


def latest_leaf(entries: list[TranscriptEntry]) -> TranscriptEntry | None:
    """The newest message that is no other message's parent (a chain tip)."""
    if not entries:
        return None
    parents = {e.parent_uuid for e in entries if e.parent_uuid}
    leaves = [e for e in entries if e.uuid not in parents]
    if not leaves:
        leaves = entries
    return max(leaves, key=lambda e: e.timestamp)


def build_chain(entries: list[TranscriptEntry], leaf_uuid: str) -> list[TranscriptEntry]:
    """Walk ``parent_uuid`` from ``leaf_uuid`` to the root, return chronological.

    Cycle-guarded (a corrupt file can't loop us forever). This is the port of
    ``buildConversationChain`` — the linear path through the tree.
    """
    by_uuid = {e.uuid: e for e in entries}
    chain: list[TranscriptEntry] = []
    seen: set[str] = set()
    cur = by_uuid.get(leaf_uuid)
    while cur is not None and cur.uuid not in seen:
        seen.add(cur.uuid)
        chain.append(cur)
        parent_uuid = cur.parent_uuid
        if parent_uuid and parent_uuid not in by_uuid:
            # The parent line is missing (corrupt/dropped) — everything before
            # it is unreachable, so the walk stops here and the reconstructed
            # history is truncated. Warn rather than lose turns silently.
            _log.warning(
                "session_tree: entry %s references missing parent %s; "
                "conversation history is truncated at this gap",
                cur.uuid,
                parent_uuid,
            )
            break
        cur = by_uuid.get(parent_uuid) if parent_uuid else None
    chain.reverse()
    return chain


def _compact_boundary_fields(content: Any) -> dict[str, Any] | None:
    """If a persisted system entry's content marks a compaction boundary, return
    its ``{summary, compacted_count}`` fields; else ``None``. The writer records
    a boundary as ``{"__compact_boundary__": true, "summary": ..., ...}``."""
    if isinstance(content, dict) and content.get("__compact_boundary__"):
        return content
    return None


def entries_to_messages(chain: list[TranscriptEntry]) -> list[Message]:
    """Convert a transcript chain into mantis ``Message`` objects.

    Honors a runtime compaction boundary: if the chain contains one or more
    persisted compaction-boundary system entries, replay starts at the LAST
    one — everything before it is dropped and replaced by the boundary's
    summary (a :class:`CompactBoundaryMessage`), exactly as an in-memory
    compaction would have left the live context. Without this, resume would
    silently re-expand history the run had already compacted away.

    Dangling tool_uses with no matching tool_result are dropped so the array
    stays API-valid (port of ``filterUnresolvedToolUses``)."""
    from .compact import CompactBoundaryMessage  # noqa: PLC0415
    from .types import ContentBlock  # noqa: PLC0415

    # Start from the last compaction boundary if one was persisted.
    start = 0
    for i, e in enumerate(chain):
        if e.message.get("role") == "system" and _compact_boundary_fields(
            e.message.get("content")
        ):
            start = i

    msgs: list[Message] = []
    for e in chain[start:]:
        role = e.message.get("role")
        content = e.message.get("content")
        if role == "assistant":
            # Blocks round-trip through JSONL as plain dicts; decode them back
            # into typed ContentBlocks so downstream isinstance checks work.
            if isinstance(content, list):
                blocks = msgspec.convert(content, list[ContentBlock])
            else:
                blocks = [TextBlock(text=str(content))]
            msgs.append(AssistantMessage(content=blocks))
        elif role == "user":
            if isinstance(content, list):
                content = msgspec.convert(content, list[ContentBlock])
            msgs.append(UserMessage(content=content, isMeta=e.is_meta))
        elif role == "system":
            # A compaction boundary is preserved (it carries the summary that
            # stands in for the dropped turns); other system entries are folded
            # into the agent's system prompt, not the message array.
            fields = _compact_boundary_fields(content)
            if fields is not None:
                msgs.append(CompactBoundaryMessage(
                    summary=str(fields.get("summary", "")),
                    compacted_count=int(fields.get("compacted_count", 0) or 0),
                ))
    return _drop_dangling_tool_uses(msgs)


def _drop_dangling_tool_uses(msgs: list[Message]) -> list[Message]:
    """If the final assistant turn ended on tool_use blocks that never got a
    tool_result back (an interrupted/crashed turn), strip them — otherwise the
    next provider call rejects the unanswered tool_use."""
    from .types import ToolResultBlock, ToolUseBlock  # noqa: PLC0415

    answered: set[str] = set()
    for m in msgs:
        if isinstance(m, UserMessage) and isinstance(m.content, list):
            for b in m.content:
                if isinstance(b, ToolResultBlock):
                    answered.add(b.tool_use_id)
    cleaned: list[Message] = []
    for m in msgs:
        if isinstance(m, AssistantMessage):
            kept = [
                b for b in m.content
                if not (isinstance(b, ToolUseBlock) and b.id not in answered)
            ]
            if not kept:
                continue
            cleaned.append(AssistantMessage(content=kept, stop_reason=m.stop_reason))
        else:
            cleaned.append(m)
    return cleaned


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def load_for_resume(session_id: str, *, cwd: str | Path | None = None) -> list[Message]:
    """Reconstruct a prior session's conversation as ``Message`` objects, ready
    to seed an Agent. Empty list if the session doesn't exist."""
    entries = load_entries(_session_path(session_id, cwd))
    leaf = latest_leaf([e for e in entries if not e.is_sidechain])
    if leaf is None:
        return []
    return entries_to_messages(build_chain(entries, leaf.uuid))


# ---------------------------------------------------------------------------
# Branch (fork)
# ---------------------------------------------------------------------------


def branch_session(
    session_id: str, *, cwd: str | Path | None = None, new_id: str | None = None
) -> str:
    """Fork a session into a NEW file: copy the main thread, re-stamp the new
    ``session_id``, rebuild the ``parent_uuid`` chain from scratch, and record a
    ``forked_from`` backpointer per entry. Returns the new session id.

    Port of ``createFork`` — the original file is left untouched, so both
    sessions are independently resumable.
    """
    src = load_entries(_session_path(session_id, cwd))
    main = [e for e in src if not e.is_sidechain]
    if not main:
        raise ValueError(f"no conversation to branch in session {session_id}")

    fork_id = new_id or new_session_id()
    fork_path = _session_path(fork_id, cwd)
    fork_path.parent.mkdir(parents=True, exist_ok=True)

    parent: str | None = None
    lines: list[bytes] = []
    for e in main:
        forked = msgspec.structs.replace(
            e,
            uuid=str(_uuid.uuid4()),
            parent_uuid=parent,
            session_id=fork_id,
            forked_from={"session_id": session_id, "message_uuid": e.uuid},
        )
        lines.append(_ENC.encode(forked).rstrip(b"\n") + b"\n")
        parent = forked.uuid

    with fork_path.open("wb") as fh:
        fh.writelines(lines)
    return fork_id


# ---------------------------------------------------------------------------
# Rewind
# ---------------------------------------------------------------------------


def rewind_chain(
    entries: list[TranscriptEntry], target_uuid: str
) -> list[TranscriptEntry]:
    """Return the chain up to AND INCLUDING ``target_uuid`` (port of
    ``rewindConversationTo`` — ``messages[:index+1]``). The caller continues by
    appending with ``parent_uuid=target_uuid``, which branches off that point
    within the same file."""
    leaf = latest_leaf(entries)
    full = build_chain(entries, leaf.uuid) if leaf else []
    out: list[TranscriptEntry] = []
    for e in full:
        out.append(e)
        if e.uuid == target_uuid:
            return out
    return out  # target not on the path → return what we have


# ---------------------------------------------------------------------------
# Cheap session listing (for the /resume picker)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    title: str | None
    first_prompt: str | None
    last_prompt: str | None
    modified_at: float
    message_count: int
    path: Path


def list_sessions(*, cwd: str | Path | None = None) -> list[SessionInfo]:
    """List resumable sessions for the project, newest first. Cheap: reads only
    the head + tail of each file and substring-scans (no full parse), mirroring
    ``listSessionsImpl``."""
    proj = get_project_dir(cwd)
    if not proj.exists():
        return []
    out: list[SessionInfo] = []
    for f in proj.glob("*.jsonl"):
        if not _looks_like_uuid(f.stem):
            continue
        info = _read_session_lite(f)
        if info is not None:
            out.append(info)
    out.sort(key=lambda s: s.modified_at, reverse=True)
    return out


def _read_session_lite(path: Path) -> SessionInfo | None:
    try:
        size = path.stat().st_size
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if size == 0:
        return None
    with path.open("rb") as fh:
        head = fh.read(_LITE_READ_BYTES)
        if size > _LITE_READ_BYTES * 2:
            fh.seek(-_LITE_READ_BYTES, os.SEEK_END)
            tail = fh.read()
        else:
            tail = head if size <= _LITE_READ_BYTES else (head + fh.read())
    head_s = head.decode("utf-8", "replace")
    tail_s = tail.decode("utf-8", "replace")
    # Scan the whole head window, not just the first line: append_meta writes
    # plain metadata records (no is_sidechain) that can precede the first
    # message line, so a first-line-only check would miss a subagent session
    # whose head starts with a custom-title/summary record. A non-sidechain
    # session never encodes is_sidechain:true (the default False is omitted).
    if '"is_sidechain":true' in head_s.replace(" ", ""):
        return None  # don't list subagent sessions
    title = _last_json_str(tail_s, "custom_title") or _last_json_str(head_s, "custom_title")
    first_prompt = _first_user_text(head_s)
    last_prompt = _last_json_str(tail_s, "last_prompt") or first_prompt
    count = head_s.count('"type":"user"') + head_s.count('"type": "user"')
    return SessionInfo(
        session_id=path.stem,
        title=title,
        first_prompt=first_prompt,
        last_prompt=last_prompt,
        modified_at=mtime,
        message_count=count,
        path=path,
    )


# Known synthetic wrapper prefixes the SDK injects as user turns. We skip only
# these in the picker preview — a blanket "starts with '<'" test would also
# discard legitimate prompts like "<html> why is this invalid?".
_SYNTHETIC_PREFIXES = (
    "<system-reminder",
    "<command-name",
    "<command-message",
    "<command-args",
    "<local-command-stdout",
    "<local-command-stderr",
    "<bash-input",
    "<bash-stdout",
    "<bash-stderr",
    "<user-memory-input",
    "<ide_",
)


def _first_user_text(head: str) -> str | None:
    """First meaningful user-message text in the head (skips meta/tool lines)."""
    for line in head.split("\n"):
        if '"type":"user"' not in line.replace(" ", ""):
            continue
        if '"is_meta":true' in line.replace(" ", ""):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            stripped = content.strip()
            if stripped.startswith(_SYNTHETIC_PREFIXES):
                continue
            return stripped[:200]
    return None


def _last_json_str(text: str, field: str) -> str | None:
    """Last value of ``"field":"..."`` in a blob (no JSON parse)."""
    needle = f'"{field}":'
    idx = text.rfind(needle)
    if idx < 0:
        return None
    rest = text[idx + len(needle):].lstrip()
    if not rest.startswith('"'):
        return None
    out, i, esc = [], 1, False
    while i < len(rest):
        c = rest[i]
        if esc:
            out.append(c)
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            break
        else:
            out.append(c)
        i += 1
    return "".join(out) or None


def _looks_like_uuid(s: str) -> bool:
    try:
        _uuid.UUID(s)
        return True
    except ValueError:
        return False


def iter_session_files(*, cwd: str | Path | None = None) -> Iterator[Path]:
    proj = get_project_dir(cwd)
    if proj.exists():
        yield from (f for f in proj.glob("*.jsonl") if _looks_like_uuid(f.stem))


def _git_branch(cwd: str) -> str | None:
    head = Path(cwd) / ".git" / "HEAD"
    try:
        ref = head.read_text("utf-8").strip()
    except OSError:
        return None
    return ref.rsplit("/", 1)[-1] if ref.startswith("ref:") else ref[:12]


__all__ = [
    "SessionInfo",
    "SessionTranscript",
    "TranscriptEntry",
    "branch_session",
    "build_chain",
    "entries_to_messages",
    "latest_leaf",
    "list_sessions",
    "load_entries",
    "load_for_resume",
    "new_session_id",
    "rewind_chain",
]
