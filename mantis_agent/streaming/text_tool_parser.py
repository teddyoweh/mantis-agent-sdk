"""Path B / C streaming parser — extract ``<tool_call>...</tool_call>`` blocks
from a raw text stream and emit normalized parser events.

The model emits Hermes-Pro-derived syntax::

    <tool_call>
    {"name": "<tool_name>", "arguments": {<JSON object>}}
    </tool_call>

We run a tiny state machine over the incoming text deltas:

    IN_TEXT          — flush as TextChunk, watch for an opening ``<tool_call>``
    IN_TOOL_CALL     — buffer everything until ``</tool_call>``; then parse the
                       JSON in one shot and emit Start + InputDelta + Stop

The "parse on close" strategy is intentional. Tool-call JSONs are tiny
(typically <1 KB) so the latency cost of buffering vs streaming the JSON is
negligible, and the implementation is bulletproof — we never have to worry
about partial JSON, escape sequences mid-delta, or nested quoting.

The trickiest bit is detecting an opening tag that arrives split across
deltas (``"<too"`` then ``"l_call>"``). The IN_TEXT state holds back the
*tail* of its buffer when that tail looks like a prefix of ``<tool_call>``;
otherwise it flushes the buffer as a TextChunk so streaming feels live.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Union
from uuid import uuid4

import msgspec

__all__ = [
    "ParserEvent",
    "TextChunk",
    "ToolCallStart",
    "ToolCallInputDelta",
    "ToolCallStop",
    "ToolCallTextParser",
]

_LOG = logging.getLogger("mantis_agent.streaming.text_tool_parser")

_OPEN_TAG = "<tool_call>"
_CLOSE_TAG = "</tool_call>"
# Hold back at most this many trailing chars while we wait to see whether
# we're staring at a partial ``<tool_call>`` prefix. The opening tag is 11
# chars, so 16 gives us plenty of slack without bloating buffers.
_HOLDBACK = 16


# ---------------------------------------------------------------------------
# Events (msgspec tagged union)
# ---------------------------------------------------------------------------


class TextChunk(msgspec.Struct, frozen=True, tag="text", tag_field="type"):
    """A run of plain text outside any tool-call block."""

    text: str


class ToolCallStart(
    msgspec.Struct,
    frozen=True,
    tag="tool_call_start",
    tag_field="type",
):
    """A ``<tool_call>`` block has been fully observed and successfully parsed."""

    call_id: str
    name: str


class ToolCallInputDelta(
    msgspec.Struct,
    frozen=True,
    tag="tool_call_input_delta",
    tag_field="type",
):
    """A chunk of the tool's input JSON. We currently emit one delta per call
    carrying the full arguments object; the shape is preserved so a future
    truly-incremental parser can drop in without changing the consumer."""

    call_id: str
    partial_json: str


class ToolCallStop(
    msgspec.Struct,
    frozen=True,
    tag="tool_call_stop",
    tag_field="type",
    omit_defaults=True,
):
    """A ``</tool_call>`` closing tag was seen. ``error=True`` indicates the
    enclosed payload was malformed JSON or missing ``name`` — the consumer
    should treat this as a failed tool call (no Start event was emitted in
    that case)."""

    call_id: str
    error: bool = False


ParserEvent = Union[TextChunk, ToolCallStart, ToolCallInputDelta, ToolCallStop]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class ToolCallTextParser:
    """Streaming parser that splits a text stream into text chunks and
    tool-call events.

    Usage::

        parser = ToolCallTextParser()
        for delta in stream:
            for event in parser.feed(delta):
                handle(event)
        for event in parser.finalize():
            handle(event)
    """

    __slots__ = ("_state", "_buf", "_tool_buf", "_pending_call_id")

    # State constants — plain ints beat enums on the hot path.
    _IN_TEXT = 0
    _IN_TOOL_CALL = 1

    def __init__(self) -> None:
        self._state = self._IN_TEXT
        self._buf = ""        # outside-tag accumulator (may hold back tail)
        self._tool_buf = ""   # inside-tag JSON accumulator
        self._pending_call_id: str | None = None

    def feed(self, text: str) -> Iterator[ParserEvent]:
        """Consume a chunk of text. Yields zero or more parser events."""
        if not text:
            return
        if self._state == self._IN_TEXT:
            self._buf += text
            yield from self._drain_text()
        else:
            self._tool_buf += text
            yield from self._drain_tool()

    def finalize(self) -> Iterator[ParserEvent]:
        """Flush remaining buffered output. Call once at end of stream."""
        if self._state == self._IN_TEXT:
            if self._buf:
                # No more text coming — even partial-tag holdback flushes.
                yield TextChunk(text=self._buf)
                self._buf = ""
        else:
            # Unterminated <tool_call> — emit a synthetic stop with error.
            call_id = self._pending_call_id or _new_call_id()
            _LOG.warning(
                "unterminated <tool_call> at stream end; payload=%r",
                self._tool_buf[:200],
            )
            self._tool_buf = ""
            self._pending_call_id = None
            self._state = self._IN_TEXT
            yield ToolCallStop(call_id=call_id, error=True)

    # ------------------------------------------------------------------
    # State drains
    # ------------------------------------------------------------------

    def _drain_text(self) -> Iterator[ParserEvent]:
        """Process ``_buf`` while in IN_TEXT. May flip into IN_TOOL_CALL and
        recurse via the tool drain to handle ``<tool_call>...</tool_call>``
        that arrived inside a single feed() call."""
        while True:
            idx = self._buf.find(_OPEN_TAG)
            if idx == -1:
                # No full opening tag in buf. Flush everything except a small
                # tail that *might* be a partial opening tag prefix.
                safe_len = _safe_flush_len(self._buf, _OPEN_TAG, _HOLDBACK)
                if safe_len > 0:
                    yield TextChunk(text=self._buf[:safe_len])
                    self._buf = self._buf[safe_len:]
                return

            # Emit everything before the opening tag, drop the tag, flip state.
            if idx > 0:
                yield TextChunk(text=self._buf[:idx])
            self._buf = self._buf[idx + len(_OPEN_TAG):]
            # Hand any remaining buffered text into the tool buffer.
            self._tool_buf += self._buf
            self._buf = ""
            self._state = self._IN_TOOL_CALL
            self._pending_call_id = _new_call_id()
            yield from self._drain_tool()
            if self._state == self._IN_TOOL_CALL:
                # Still inside a tool call — need more input.
                return
            # Closed; loop to look for another tool call in remaining text.

    def _drain_tool(self) -> Iterator[ParserEvent]:
        """Process ``_tool_buf`` while in IN_TOOL_CALL. Looks for ``</tool_call>``;
        on close, parses JSON and emits Start + InputDelta + Stop."""
        idx = self._tool_buf.find(_CLOSE_TAG)
        if idx == -1:
            # No close tag yet; keep buffering.
            return

        raw_json = self._tool_buf[:idx]
        tail = self._tool_buf[idx + len(_CLOSE_TAG):]
        call_id = self._pending_call_id or _new_call_id()
        self._tool_buf = ""
        self._pending_call_id = None
        self._state = self._IN_TEXT

        yield from _emit_tool_call(call_id, raw_json)

        # Re-enter text state with whatever followed the closing tag.
        if tail:
            self._buf += tail
            yield from self._drain_text()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_call_id() -> str:
    return f"tc_{uuid4().hex[:8]}"


def _safe_flush_len(buf: str, open_tag: str, holdback: int) -> int:
    """Return how many chars of ``buf`` we can safely flush as text.

    We must hold back any trailing substring that could be a prefix of
    ``open_tag``. Example: with open_tag=``<tool_call>``, if buf ends in
    ``"hello <to"``, we must keep ``"<to"`` buffered.
    """
    n = len(buf)
    if n <= holdback:
        # Cheap path: scan once for any partial match in the tail.
        max_prefix = min(n, len(open_tag) - 1)
    else:
        max_prefix = len(open_tag) - 1
    # Look for the longest suffix of buf that equals a prefix of open_tag.
    for k in range(max_prefix, 0, -1):
        if buf.endswith(open_tag[:k]):
            return n - k
    return n


def _trim_to_object(s: str) -> str:
    """Return the first brace-balanced ``{...}`` object in ``s``, dropping any
    leading/trailing prose the model wrapped around it (e.g. ``{...} run it``).

    Brace counting ignores braces inside JSON strings. If the string never
    balances we return from the first ``{`` to the end and let the caller's
    ``json.loads`` decide.
    """
    start = s.find("{")
    if start < 0:
        return s
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return s[start:]


def _escape_stray_quotes(s: str) -> str:
    """Escape double-quotes that appear *inside* a JSON string value but aren't a
    real delimiter — the #1 way small models break tool-call JSON, e.g.
    ``{"command":"grep "foo" x"}``. A real closing quote is followed (after
    optional whitespace) by one of ``,:}]`` or end-of-input; anything else inside
    a string is treated as a literal quote and escaped.
    """
    out: list[str] = []
    in_str = False
    esc = False
    n = len(s)
    for i, c in enumerate(s):
        if not in_str:
            out.append(c)
            if c == '"':
                in_str = True
            continue
        if esc:
            out.append(c)
            esc = False
        elif c == "\\":
            out.append(c)
            esc = True
        elif c == '"':
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j >= n or s[j] in ",:}]":
                out.append(c)  # genuine closing quote
                in_str = False
            else:
                out.append('\\"')  # literal quote inside the value
        else:
            out.append(c)
    return "".join(out)


def _loads_lenient(payload: str) -> object | None:
    """``json.loads`` with best-effort repair for the malformed JSON small local
    models routinely emit. Returns the parsed object, or ``None`` if every
    repair attempt still fails to produce a dict-or-list."""
    for candidate in (
        payload,
        _trim_to_object(payload),
        _escape_stray_quotes(payload),
        _escape_stray_quotes(_trim_to_object(payload)),
        _trim_to_object(_escape_stray_quotes(payload)),
    ):
        try:
            return json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            continue
    return None


def _emit_tool_call(call_id: str, raw_json: str) -> Iterator[ParserEvent]:
    """Parse the JSON between ``<tool_call>`` and ``</tool_call>`` and emit
    Start + InputDelta + Stop. Malformed JSON emits a single Stop(error=True)."""
    payload = raw_json.strip()
    if not payload:
        _LOG.warning("empty <tool_call> body")
        yield ToolCallStop(call_id=call_id, error=True)
        return
    parsed = _loads_lenient(payload)
    if parsed is None:
        _LOG.warning("malformed <tool_call> JSON (unrepairable); body=%r", payload[:200])
        yield ToolCallStop(call_id=call_id, error=True)
        return
    if not isinstance(parsed, dict):
        _LOG.warning("<tool_call> body is not a JSON object: %r", payload[:200])
        yield ToolCallStop(call_id=call_id, error=True)
        return
    name = parsed.get("name")
    if not isinstance(name, str) or not name:
        _LOG.warning("<tool_call> missing 'name' field: %r", payload[:200])
        yield ToolCallStop(call_id=call_id, error=True)
        return
    arguments = parsed.get("arguments", {})
    if not isinstance(arguments, dict):
        # Tolerate string args by wrapping; warn but don't fail outright.
        _LOG.warning("<tool_call> 'arguments' is not an object: %r", arguments)
        arguments = {}
    yield ToolCallStart(call_id=call_id, name=name)
    yield ToolCallInputDelta(call_id=call_id, partial_json=json.dumps(arguments))
    yield ToolCallStop(call_id=call_id)
