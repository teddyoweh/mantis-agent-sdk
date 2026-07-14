"""JSONL session transcripts — Claude Code-compatible on-disk format.

Each session is one append-only file at
``~/.mantis-agent/sessions/{session_id}.jsonl``, with one JSON-encoded
``SDKMessage`` per line. This is the same shape Claude Code writes to
``~/.claude/projects/{hash}/{session_id}.jsonl`` — a third-party tool
that can read Claude's transcripts will read ours unchanged.

Why JSONL not SQLite for transcripts
------------------------------------
Two stores serve two needs:

  * ``transcripts.JsonlTranscript`` — append-only log, one SDKMessage per
    line, perfect for ``tail -f``, ``jq``, log shipping, replay. Matches
    Claude. Designed to be human-grep-able mid-run.
  * ``session.SqliteSessionStore`` — random-access, transactional,
    used for resume/fork. Internal Message list lives here.

Most users only care about the JSONL transcript. We write to both
automatically when ``Agent`` is constructed with ``persist=True``.

Atomicity
---------

Each line is written + flushed + fsync'd in one ``write_line`` call so a
crash mid-turn leaves a valid prefix. The reader skips any trailing
partial line (it can happen if the writer was SIGKILL'd between
``write`` and the trailing ``\\n``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import msgspec

from .paths import (
    ensure_dir,
    get_session_path,
    iter_sessions,
    sanitize_session_id,
)

__all__ = [
    "JsonlTranscript",
    "iter_transcripts",
    "read_transcript",
]


_ENCODER = msgspec.json.Encoder()


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class JsonlTranscript:
    """Append-only JSONL writer for one session.

    Construct with a ``session_id`` (a UUID string is conventional). The
    parent directory is created lazily on first write. ``close()`` is
    idempotent.

    Usage::

        async with JsonlTranscript("01HXXX...") as t:
            async for msg in query(prompt=..., options=...):
                t.write(msg)
    """

    __slots__ = ("session_id", "path", "_fh")

    def __init__(self, session_id: str, *, base_dir: Path | None = None) -> None:
        self.session_id = sanitize_session_id(session_id)
        if base_dir is not None:
            ensure_dir(base_dir)
            self.path = base_dir / f"{self.session_id}.jsonl"
        else:
            self.path = get_session_path(self.session_id)
        self._fh = None  # type: ignore[assignment]

    # -- public API ------------------------------------------------------

    def open(self) -> None:
        """Open the file for append. No-op if already open."""

        if self._fh is not None:
            return
        ensure_dir(self.path.parent)
        # Line-buffered; we still flush+fsync per write for crash safety.
        self._fh = open(self.path, "ab", buffering=0)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._fh = None

    def write(self, message: Any) -> None:
        """Append one SDKMessage. ``message`` is encoded with msgspec when
        it's a Struct, with stdlib ``json`` when it's a plain dict, and
        passed through if it's already bytes (caller's responsibility)."""

        if self._fh is None:
            self.open()
        assert self._fh is not None
        if isinstance(message, (bytes, bytearray)):
            line = bytes(message)
        elif isinstance(message, dict):
            line = json.dumps(message, separators=(",", ":")).encode()
        else:
            line = _ENCODER.encode(message)
        # Strip any newline the encoder included (msgspec doesn't, but be safe)
        line = line.rstrip(b"\n") + b"\n"
        self._fh.write(line)
        # Per-line fsync is paranoid but matches Claude's behavior; transcripts
        # are precious and a partial-line crash should never lose user content.
        try:
            os.fsync(self._fh.fileno())
        except OSError:
            # fsync fails on some pseudo-filesystems (tmpfs in containers).
            # The buffered write still hit the kernel; that's enough.
            pass

    # -- context manager sugar -------------------------------------------

    def __enter__(self) -> JsonlTranscript:
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    async def __aenter__(self) -> JsonlTranscript:
        self.open()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def read_transcript(session_id: str) -> Iterator[dict[str, Any]]:
    """Yield each line of a session's transcript as a parsed dict.

    Tolerates a trailing partial line (crash recovery).
    """

    path = get_session_path(session_id)
    if not path.exists():
        return
    with open(path, "rb") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # Trailing partial line from a crashed writer. Skip.
                continue


def iter_transcripts() -> Iterable[tuple[str, Path]]:
    """Yield ``(session_id, path)`` for every persisted transcript."""

    for path in iter_sessions():
        yield (path.stem, path)
