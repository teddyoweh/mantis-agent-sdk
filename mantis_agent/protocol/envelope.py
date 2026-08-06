"""The wire envelope and its newline-delimited JSON framing (plan §6).

```text
{"v":1,"id":"c7","t":"req","op":"session.subscribe","d":{...}}
{"v":1,"id":"c7","t":"res","ok":true,"d":{...}}
{"v":1,"t":"ev","seq":4821,"d":{"type":"node_status",...}}
{"v":1,"id":"c7","t":"err","code":"not_authorized","msg":"..."}
```

Encoding follows :mod:`mantis_agent.activity.events` exactly — ``msgspec.Struct``,
``frozen=True``, one struct per message type discriminated by a tag field — so
the four frame types decode as a tagged union with the same machinery the
activity journal already uses. The tag field here is ``t``; the activity
envelope's is ``type``; they nest without collision.

**An event's ``d`` is the activity struct itself.** :class:`Event` types ``d`` as
``ActivityEvent``, so decoding an event yields a ``NodeStatus``, not a dict that
somebody has to re-parse. There is no translation layer and no second event
model — that is the single most important line in this file.

Framing is newline-delimited JSON because it is inspectable with ``nc`` and
implementable in any language without a library. That choice puts the burden on
the reader: :class:`FrameReader` is fed arbitrary bytes from a socket the peer
controls, so it *collects* decode failures instead of raising them. A read loop
that dies on a malformed byte is a denial of service with extra steps.
"""

from __future__ import annotations

from typing import Any, List, Union

import msgspec

from ..activity.events import ActivityEvent
from .errors import InternalError, MalformedFrameError, MessageTooLargeError, ProtocolError
from .version import PROTOCOL_VERSION

__all__ = [
    "DECODE_FRAME",
    "ENCODE_FRAME",
    "MAX_MESSAGE_BYTES",
    "ErrorFrame",
    "Event",
    "Frame",
    "FrameReader",
    "Request",
    "Response",
    "decode_frame",
    "encode_frame",
    "encode_line",
    "error_frame",
    "error_from_frame",
    "response_to",
]

#: ``remote.stream.maxMessageBytes`` from §10. A single frame larger than this
#: is refused rather than buffered: the limit exists to stop a peer from making
#: the daemon allocate, so it has to be enforced *while* reading, not after.
MAX_MESSAGE_BYTES = 1048576


class Request(msgspec.Struct, frozen=True, tag="req", tag_field="t"):
    """A client operation. ``id`` correlates the eventual response or error."""

    op: str
    id: str = ""
    v: int = PROTOCOL_VERSION
    d: dict[str, Any] = msgspec.field(default_factory=dict)


class Response(msgspec.Struct, frozen=True, tag="res", tag_field="t"):
    """A successful reply to one request.

    ``ok`` is always ``True`` here — failure is a separate frame type, so a
    client never has to check both a type tag and a boolean.
    """

    id: str = ""
    ok: bool = True
    v: int = PROTOCOL_VERSION
    d: dict[str, Any] = msgspec.field(default_factory=dict)


class Event(msgspec.Struct, frozen=True, tag="ev", tag_field="t"):
    """An unsolicited activity event.

    ``seq`` is lifted to the envelope so a client can de-duplicate and resume
    without decoding the payload; it mirrors the payload's own ``seq``, which
    the activity registry assigned. Build with :meth:`from_activity` so the two
    cannot disagree.

    ``session_id`` is not in the plan's example line but is required in
    practice: one connection may hold up to ``maxSubscriptionsPerClient``
    subscriptions, and an event with no session identity cannot be routed to the
    right view. It defaults to empty so the plan's exact wire example still
    decodes.
    """

    d: ActivityEvent
    seq: int = 0
    session_id: str = ""
    v: int = PROTOCOL_VERSION

    @classmethod
    def from_activity(cls, payload: ActivityEvent, session_id: str = "") -> "Event":
        """Wrap one activity event, taking ``seq`` from the payload."""

        return cls(d=payload, seq=payload.seq, session_id=session_id)


class ErrorFrame(msgspec.Struct, frozen=True, tag="err", tag_field="t"):
    """A failed reply.

    ``code`` is the stable identifier clients branch on; ``msg`` is for humans;
    ``d`` carries the structured recovery data of §12 (earliest ``seq``, lease
    holder, supported range). ``id`` is empty for errors that are not a reply to
    any request — a protocol violation on an unsolicited frame, for instance.
    """

    code: str
    msg: str = ""
    id: str = ""
    v: int = PROTOCOL_VERSION
    d: dict[str, Any] = msgspec.field(default_factory=dict)


#: The tagged union. ``msgspec.json.Decoder(Frame)`` dispatches on ``t``.
Frame = Union[Request, Response, Event, ErrorFrame]

# One codec pair per process, as in ``activity.events`` — msgspec codecs are
# thread-safe and reusing them is measurably faster at stream volumes.
ENCODE_FRAME = msgspec.json.Encoder()
DECODE_FRAME = msgspec.json.Decoder(Frame)


def encode_frame(frame: Frame) -> bytes:
    """Encode one frame. No trailing newline — see :func:`encode_line`."""

    return ENCODE_FRAME.encode(frame)


def encode_line(frame: Frame) -> bytes:
    """Encode one frame as a complete NDJSON line.

    Safe because JSON escapes every newline inside a string, so the encoded
    form of any frame is guaranteed to contain no ``\\n`` of its own.
    """

    return ENCODE_FRAME.encode(frame) + b"\n"


def decode_frame(data: "bytes | str") -> Frame:
    """Decode one frame, raising :class:`MalformedFrameError` on anything else.

    The ``msgspec`` exception is deliberately swallowed: callers handle one
    error family, and a ``msgspec.ValidationError`` escaping into a daemon's
    connection loop would be an unhandled type from a third-party library.
    """

    try:
        return DECODE_FRAME.decode(data)
    except (msgspec.ValidationError, msgspec.DecodeError, UnicodeDecodeError) as exc:
        raise MalformedFrameError(str(exc), snippet=_snippet(data)) from None


def response_to(request: Request, d: "dict[str, Any] | None" = None) -> Response:
    """Build the response to ``request``, echoing its correlation id."""

    return Response(id=request.id, d=dict(d or {}))


def error_frame(exc: BaseException, request_id: str = "") -> ErrorFrame:
    """Render an exception as an ``err`` frame.

    A non-protocol exception becomes an opaque :class:`InternalError`: an
    arbitrary exception's message may contain a path, a prompt fragment, or a
    credential, and this frame crosses a trust boundary.
    """

    if not isinstance(exc, ProtocolError):
        exc = InternalError()
    return ErrorFrame(code=exc.code, msg=exc.msg, id=request_id, d=dict(exc.data))


def error_from_frame(frame: ErrorFrame) -> ProtocolError:
    """Rebuild the typed exception a peer sent, so ``except LeaseHeldError`` works."""

    return ProtocolError.from_wire(frame.code, frame.msg, frame.d)


def _snippet(data: "bytes | str", limit: int = 80) -> str:
    """A short, printable excerpt of a bad line for logs. Never the whole line —
    it is attacker-controlled and may be a megabyte of anything."""

    if isinstance(data, str):
        raw = data.encode("utf-8", "replace")
    else:
        raw = bytes(data)
    text = raw[:limit].decode("utf-8", "replace")
    return text + ("…" if len(raw) > limit else "")


class FrameReader:
    """Incremental newline-delimited JSON reader.

    Feed it whatever the socket returned; it yields the frames that completed.
    Three behaviours matter and each is a deliberate refusal to raise:

    * **A malformed line is recorded, not raised.** :meth:`feed` never raises;
      failures accumulate for :meth:`drain_errors`, and the reader resynchronizes
      at the next newline. The peer chooses the bytes, so it must not choose
      the control flow.
    * **A truncated final line is discarded.** A connection that dies
      mid-frame leaves a partial line that will never complete; :meth:`close`
      drops it silently and reports how many bytes went. Half a frame is not an
      error, it is a hangup.
    * **An oversized line is dropped, not buffered.** Past
      ``max_message_bytes`` the reader stops accumulating and skips to the next
      newline, so a peer cannot grow the daemon's memory by never sending one.

    Not thread-safe: one reader per connection, owned by that connection.
    """

    __slots__ = ("max_message_bytes", "_buf", "_errors", "_skipping")

    def __init__(self, *, max_message_bytes: int = MAX_MESSAGE_BYTES) -> None:
        self.max_message_bytes = max_message_bytes
        self._buf = bytearray()
        self._errors: List[ProtocolError] = []
        #: True while discarding the tail of an over-long line.
        self._skipping = False

    @property
    def pending(self) -> int:
        """Bytes buffered for an incomplete line."""

        return len(self._buf)

    def feed(self, chunk: "bytes | str") -> List[Frame]:
        """Consume bytes; return the frames that completed. Never raises."""

        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", "surrogateescape")
        self._buf += chunk
        frames: List[Frame] = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl])
            del self._buf[: nl + 1]
            if self._skipping:
                # tail of an oversized line: discard it and resume normal service
                self._skipping = False
                continue
            self._consume(line, frames)
        if self._skipping:
            # still hunting for the newline that ends the oversized line
            self._buf.clear()
        elif len(self._buf) > self.max_message_bytes:
            # no newline in sight and already over the limit: refuse now rather
            # than let the peer decide how much memory this costs
            self._errors.append(MessageTooLargeError(len(self._buf), self.max_message_bytes))
            self._buf.clear()
            self._skipping = True
        return frames

    def _consume(self, line: bytes, frames: List[Frame]) -> None:
        line = line.rstrip(b"\r")  # tolerate CRLF peers
        if not line.strip():
            return  # blank lines are legal NDJSON padding
        if len(line) > self.max_message_bytes:
            self._errors.append(MessageTooLargeError(len(line), self.max_message_bytes))
            return
        try:
            frames.append(decode_frame(line))
        except ProtocolError as exc:
            self._errors.append(exc)

    def drain_errors(self) -> List[ProtocolError]:
        """Take the errors recorded since the last drain."""

        out = self._errors
        self._errors = []
        return out

    def close(self) -> int:
        """Discard any partial trailing line; return how many bytes were dropped.

        Silent by design: a truncated final line means the peer hung up, which
        is normal, and is not worth an error a caller would have to filter out.
        """

        dropped = len(self._buf)
        self._buf.clear()
        self._skipping = False
        return dropped
