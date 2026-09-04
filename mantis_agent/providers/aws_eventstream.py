"""AWS ``vnd.amazon.eventstream`` decoder.

Bedrock's streaming endpoint does not speak SSE — it returns a sequence of
binary framed messages. The framing is fully specified and small, so decoding
it here keeps Bedrock working without botocore:

    ┌──────────────┬───────────────┬─────────────┬─────────┬─────────┬────────┐
    │ total_len 4B │ headers_len 4B│ prelude_crc │ headers │ payload │ msg crc│
    └──────────────┴───────────────┴─────────────┴─────────┴─────────┴────────┘

All integers are big-endian. ``total_len`` covers the whole frame including
both CRCs, so ``payload_len = total_len - headers_len - 16``. Each header is
``[name_len:1][name][value_type:1][value]``; Bedrock only uses the string type
(7, ``[len:2][bytes]``) for ``:message-type`` / ``:event-type`` /
``:exception-type``, but every type is skipped correctly so an unexpected one
cannot desynchronise the stream.

The CRCs are checked when :func:`decode_frames` is given ``verify=True``. They
are on by default: a corrupted frame that is silently accepted turns into a
JSON decode error hundreds of lines away from its cause.
"""

from __future__ import annotations

import binascii
import struct
from typing import Any, Iterator

__all__ = [
    "EventStreamError",
    "EventStreamMessage",
    "decode_frames",
    "iter_messages",
]

_PRELUDE_LEN = 12  # total_len + headers_len + prelude_crc
_FRAME_OVERHEAD = 16  # prelude + trailing message crc

# Header value types (only 6/7 carry the bytes we care about; the rest are
# fixed-width and skipped).
_FIXED_WIDTH = {0: 0, 1: 0, 2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}


class EventStreamError(ValueError):
    """A frame that cannot be decoded, or an ``exception`` frame from AWS."""


class EventStreamMessage:
    """One decoded frame: its headers and its raw payload."""

    __slots__ = ("headers", "payload")

    def __init__(self, headers: dict[str, Any], payload: bytes) -> None:
        self.headers = headers
        self.payload = payload

    @property
    def message_type(self) -> str:
        return str(self.headers.get(":message-type") or "")

    @property
    def event_type(self) -> str:
        return str(self.headers.get(":event-type") or "")

    def __repr__(self) -> str:  # pragma: no cover — display only
        return (
            f"EventStreamMessage(type={self.message_type!r}, "
            f"event={self.event_type!r}, {len(self.payload)}B)"
        )


def decode_frames(buf: bytes, *, verify: bool = True) -> tuple[list[EventStreamMessage], bytes]:
    """Decode every complete frame in ``buf``.

    Returns ``(messages, remainder)`` — the remainder is the partial trailing
    frame, which the caller carries into the next chunk. A stream arrives in
    arbitrary TCP-sized pieces, so "decode what you can, keep the rest" is the
    only correct shape here.
    """

    out: list[EventStreamMessage] = []
    pos = 0
    while len(buf) - pos >= _PRELUDE_LEN:
        total_len, headers_len = struct.unpack_from(">II", buf, pos)
        if total_len < _PRELUDE_LEN + 4 or headers_len > total_len:
            raise EventStreamError(
                f"event-stream frame claims total={total_len} headers={headers_len}, "
                "which cannot be a valid frame"
            )
        if len(buf) - pos < total_len:
            break  # incomplete — wait for more bytes
        frame = buf[pos:pos + total_len]
        if verify:
            _verify_crcs(frame, headers_len)
        headers = _decode_headers(frame[_PRELUDE_LEN:_PRELUDE_LEN + headers_len])
        payload = frame[_PRELUDE_LEN + headers_len:total_len - 4]
        out.append(EventStreamMessage(headers, payload))
        pos += total_len
    return out, buf[pos:]


def _verify_crcs(frame: bytes, headers_len: int) -> None:
    prelude_crc = struct.unpack_from(">I", frame, 8)[0]
    if binascii.crc32(frame[:8]) & 0xFFFFFFFF != prelude_crc:
        raise EventStreamError("event-stream prelude CRC mismatch")
    message_crc = struct.unpack_from(">I", frame, len(frame) - 4)[0]
    if binascii.crc32(frame[:-4]) & 0xFFFFFFFF != message_crc:
        raise EventStreamError("event-stream message CRC mismatch")


def _decode_headers(raw: bytes) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    pos = 0
    while pos < len(raw):
        name_len = raw[pos]
        pos += 1
        name = raw[pos:pos + name_len].decode("utf-8", "replace")
        pos += name_len
        value_type = raw[pos]
        pos += 1
        if value_type in (6, 7):  # byte array / string
            (length,) = struct.unpack_from(">H", raw, pos)
            pos += 2
            value: Any = raw[pos:pos + length]
            if value_type == 7:
                value = value.decode("utf-8", "replace")
            pos += length
        elif value_type == 0:
            value = True
        elif value_type == 1:
            value = False
        else:
            width = _FIXED_WIDTH.get(value_type)
            if width is None:
                raise EventStreamError(
                    f"unknown event-stream header type {value_type} for {name!r}"
                )
            value = int.from_bytes(raw[pos:pos + width], "big") if width else None
            pos += width
        headers[name] = value
    return headers


def iter_messages(chunks: Iterator[bytes], *, verify: bool = True) -> Iterator[EventStreamMessage]:
    """Decode a synchronous iterable of byte chunks into frames (for tests and
    for anything replaying a recorded body)."""

    buffer = b""
    for chunk in chunks:
        buffer += chunk
        messages, buffer = decode_frames(buffer, verify=verify)
        yield from messages
