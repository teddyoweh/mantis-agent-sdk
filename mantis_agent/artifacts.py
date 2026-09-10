"""Private, durable content-addressed evidence for recoverable compaction.

IDs are lowercase SHA-256 hex digests of the stored UTF-8 text. JSON is
canonical, sanitized, and readable as text. Offsets/limits count Unicode
characters, not lines or bytes; callers can page even a single huge result.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
import uuid

import msgspec

from .tools import Tool

MAX_READ_CHARS = 16_000
MAX_SEARCH_CHARS = 1_000_000
MAX_QUERY_CHARS = 512
_ID = re.compile(r"[0-9a-f]{64}\Z")
_DROP = object()


def _sanitize(value: Any) -> Any:
    if isinstance(value, msgspec.Struct):
        value = msgspec.to_builtins(value)
    if isinstance(value, dict):
        if value.get("type") in ("thinking", "redacted_thinking"):
            return _DROP
        return {k: clean for k, v in value.items() if (clean := _sanitize(v)) is not _DROP}
    if isinstance(value, (list, tuple)):
        return [clean for v in value if (clean := _sanitize(v)) is not _DROP]
    return value


class ArtifactStore:
    """Store artifacts in a dedicated private directory (0700, files 0600).

    Filesystem/serialization errors propagate: callers must keep original
    evidence unless the write succeeds. No retention or garbage collection is
    performed: references can survive session save/load and repeated compaction.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = self._directory()
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)

    def _directory(self) -> int:
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    @staticmethod
    def validate_id(id: str) -> str:
        if not isinstance(id, str) or _ID.fullmatch(id) is None:
            raise ValueError("artifact id must be exactly 64 lowercase SHA-256 hex characters")
        return id

    def put_text(self, text: str) -> str:
        """Atomically archive exact text, returning its content-addressed ID."""
        data = text.encode("utf-8")
        id = hashlib.sha256(data).hexdigest()
        directory = self._directory()
        temporary = f".tmp-{uuid.uuid4().hex}"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, id, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            finally:
                os.close(directory)
        return id

    def put_json(self, payload: Any) -> str:
        """Archive structured data/messages, recursively excluding private thinking."""
        clean = _sanitize(payload)
        if clean is _DROP:
            clean = None
        return self.put_text(json.dumps(clean, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")))

    def read(self, id: str, offset: int = 0, limit: int = 4000,
             query: str | None = None) -> dict[str, Any]:
        """Read a bounded page or search a bounded region for literal text.

        Search scans at most MAX_SEARCH_CHARS starting at offset. If no match
        is found, next_offset advances with overlap so cross-page matches aren't
        lost. Follow next_offset until eof. Content is always <= MAX_READ_CHARS.
        """
        self.validate_id(id)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, MAX_READ_CHARS)
        if query is not None and (not isinstance(query, str) or not 1 <= len(query) <= MAX_QUERY_CHARS):
            raise ValueError(f"query must contain 1..{MAX_QUERY_CHARS} characters")
        directory = self._directory()
        try:
            fd = os.open(id, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, "r", encoding="utf-8", newline="") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("artifact must be a regular, non-linked file")
            remaining = offset
            while remaining:
                skipped = stream.read(min(remaining, MAX_READ_CHARS))
                if not skipped:
                    return dict(id=id, content="", offset=offset, next_offset=offset, eof=True)
                remaining -= len(skipped)
            size = MAX_SEARCH_CHARS if query else limit
            chunk = stream.read(size + 1)
            eof = len(chunk) <= size
            chunk = chunk[:size]
        if query:
            match = chunk.find(query)
            if match < 0:
                advance = len(chunk) if eof else max(1, len(chunk) - len(query) + 1)
                return dict(id=id, content="", offset=offset, next_offset=offset + advance,
                            eof=eof, matched=False)
            offset += match
            chunk = chunk[match:]
        content = chunk[:limit]
        return dict(id=id, content=content, offset=offset, next_offset=offset + len(content),
                    eof=eof and len(chunk) <= limit, **({"matched": True} if query else {}))


def make_artifact_tool(store: ArtifactStore) -> Tool:
    """Build the registry-compatible read-only retrieval tool for this store."""
    async def read_artifact(id: str, offset: int = 0, limit: int = 4000,
                            query: str | None = None) -> str:
        import anyio
        result = await anyio.to_thread.run_sync(lambda: store.read(id, offset, limit, query))
        return json.dumps(result, ensure_ascii=False)

    return Tool(
        name="read_artifact",
        description=("Recover archived compaction evidence by SHA-256 id. offset and limit "
                     "are character counts (offset starts at 0; limit capped at 16000). "
                     "Optional query is literal search, scanning up to 1000000 characters. "
                     "Follow next_offset until eof to page or continue a search. No filesystem paths."),
        input_schema={"type": "object", "properties": {
            "id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_READ_CHARS, "default": 4000},
            "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
        }, "required": ["id"], "additionalProperties": False},
        fn=read_artifact, is_read_only=True,
    )
