"""The protocol error taxonomy (plan §12).

Three rules shape this module, and all three exist because the errors are read
by clients that nobody here wrote:

* **``code`` is the contract, not the class name.** A mobile client branches on
  ``"lease_held"``; it cannot import ``LeaseHeldError``. Codes are frozen
  strings, registered in :data:`ERROR_CODES`, and a duplicate is a hard failure
  at import time rather than a client that silently mis-routes.
* **Recovery data is structured.** "Where recovery is possible" the plan says
  the error carries the data needed to recover — the earliest available ``seq``,
  the current lease holder, the supported protocol range. That goes in ``data``
  as JSON-encodable values, never interpolated into prose the client must parse.
* **Reconstruction never runs the typed constructor.** A client decoding an
  ``err`` frame has a code and a dict, not the arguments the constructor wants,
  so :meth:`ProtocolError.from_wire` rebuilds the instance directly. That is why
  every subclass may take whatever arguments read best on the raising side.

Everything inherits ``AgentError`` so that a caller who wraps the SDK in one
``except AgentError`` keeps working when it starts speaking the protocol.
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, Iterable, Mapping, Optional, Sequence, Type

from ..errors import AgentError

__all__ = [
    "ERROR_CODES",
    "CapabilityNotNegotiatedError",
    "CertificatePinMismatchError",
    "InternalError",
    "LeaseExpiredError",
    "LeaseHeldError",
    "MalformedFrameError",
    "MessageTooLargeError",
    "NotAuthenticatedError",
    "NotAuthorizedError",
    "PairingCodeInvalidError",
    "PairingRateLimitedError",
    "ProtocolError",
    "ReplayTruncatedError",
    "SessionNotFoundError",
    "SessionNotLiveError",
    "SlowConsumerError",
    "TeleportInFlightError",
    "TeleportIncompatibleError",
    "TeleportIntegrityError",
    "TeleportSealedError",
    "UnknownOperationError",
    "VersionMismatchError",
]

#: ``code`` → class, populated by ``__init_subclass__``. A decoder uses this to
#: turn a wire code back into the typed exception; an unknown code degrades to
#: :class:`ProtocolError` rather than failing, because a newer server is allowed
#: to invent codes an older client has never heard of.
ERROR_CODES: Dict[str, Type["ProtocolError"]] = {}


class ProtocolError(AgentError):
    """Base for every protocol-level failure.

    ``msg`` is the human string (also the ``Exception`` argument, so ``str(exc)``
    keeps working) and ``data`` is the structured recovery payload that lands in
    the error frame's ``d``.
    """

    #: Stable wire identifier. Subclasses override; the base is what an
    #: unrecognized code decodes to.
    code: ClassVar[str] = "protocol_error"

    __slots__ = ("msg", "data")

    def __init__(self, msg: str, data: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(msg)
        self.msg = msg
        self.data: Dict[str, Any] = dict(data or {})

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        code = cls.__dict__.get("code")
        if code is None:
            # An intermediate grouping class that reuses its parent's code is
            # fine; only a *declared* code claims a slot in the registry.
            return
        clash = ERROR_CODES.get(code)
        if clash is not None and clash is not cls:
            raise RuntimeError(f"duplicate protocol error code {code!r}: {clash} and {cls}")
        ERROR_CODES[code] = cls

    @classmethod
    def from_wire(
        cls,
        code: str,
        msg: str,
        data: Optional[Mapping[str, Any]] = None,
    ) -> "ProtocolError":
        """Rebuild an error received over the wire.

        Bypasses ``__init__`` deliberately: subclass constructors take typed
        arguments (``holder``, ``earliest_seq``, …) that we do not have and must
        not guess. The instance's ``code`` shadows the class attribute so an
        unknown future code round-trips intact.
        """

        target: Type[ProtocolError] = ERROR_CODES.get(code, cls)
        exc = target.__new__(target)
        ProtocolError.__init__(exc, msg, data)
        if code != target.code:
            # Only possible for an unregistered code landing on the base class.
            exc.code = code  # type: ignore[misc]
        return exc


class InternalError(ProtocolError):
    """An unexpected server-side failure.

    Deliberately opaque: a bare ``Exception`` on the server may carry a path, a
    prompt fragment, or a credential in its message, and an error frame crosses
    a trust boundary. The detail belongs in the host's log, not on the wire.
    """

    code = "internal_error"

    def __init__(self, msg: str = "internal error") -> None:
        super().__init__(msg)


# --------------------------------------------------------------------------
# framing / envelope
# --------------------------------------------------------------------------


class MalformedFrameError(ProtocolError):
    """A line was not a decodable frame.

    Not in the plan's §12 list because §12 enumerates *operational* failures;
    this one is the decoder's own. It is raised by :func:`decode_frame` and
    *collected* (never raised) by :class:`FrameReader`, so a peer that writes
    garbage costs the reader one recorded error, not the read loop.
    """

    code = "malformed_frame"

    def __init__(self, reason: str, *, snippet: str = "") -> None:
        super().__init__(f"malformed frame: {reason}", {"reason": reason, "snippet": snippet})


class MessageTooLargeError(ProtocolError):
    """A frame exceeded ``remote.stream.maxMessageBytes``."""

    code = "message_too_large"

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(
            f"message of {size} bytes exceeds the {limit} byte limit",
            {"size": size, "limit": limit},
        )


# --------------------------------------------------------------------------
# handshake
# --------------------------------------------------------------------------


class VersionMismatchError(ProtocolError):
    """No shared protocol major version.

    Carries the full supported list *and* its bounds: a client that only wants
    to print "upgrade to a newer mantis" uses ``max``, one that wants to retry
    picks from ``supported``.
    """

    code = "version_mismatch"

    def __init__(self, requested: Iterable[int], supported: Sequence[int]) -> None:
        req = list(requested)
        sup = list(supported)
        offered = ", ".join(str(v) for v in req) if req else "none"
        super().__init__(
            f"protocol version mismatch: client offered {offered}, "
            f"server supports {', '.join(str(v) for v in sup)}",
            {
                "requested": req,
                "supported": sup,
                "min": min(sup) if sup else 0,
                "max": max(sup) if sup else 0,
            },
        )


class UnknownOperationError(ProtocolError):
    """The ``op`` is not in this version's operation table."""

    code = "unknown_operation"

    def __init__(self, op: str) -> None:
        super().__init__(f"unknown operation {op!r}", {"op": op})


class CapabilityNotNegotiatedError(ProtocolError):
    """The operation exists but its capability was not part of the handshake.

    ``negotiated`` is included so a client can render "you have stream, this
    needs control" without a second round trip.
    """

    code = "capability_not_negotiated"

    def __init__(self, op: str, capability: str, negotiated: Iterable[str]) -> None:
        have = sorted(negotiated)
        super().__init__(
            f"operation {op!r} requires the {capability!r} capability "
            f"(negotiated: {', '.join(have) or 'none'})",
            {"op": op, "capability": capability, "negotiated": have},
        )


# --------------------------------------------------------------------------
# authentication and authorization
# --------------------------------------------------------------------------


class NotAuthenticatedError(ProtocolError):
    """No valid device credential or peer-credential check on this connection."""

    code = "not_authenticated"

    def __init__(self, msg: str = "not authenticated") -> None:
        super().__init__(msg)


class NotAuthorizedError(ProtocolError):
    """Authenticated, but this device may not do this."""

    code = "not_authorized"

    def __init__(self, msg: str = "not authorized", *, op: str = "", device: str = "") -> None:
        data: Dict[str, Any] = {}
        if op:
            data["op"] = op
        if device:
            data["device"] = device
        super().__init__(msg, data)


class PairingCodeInvalidError(ProtocolError):
    """Wrong, expired, or already-used pairing code."""

    code = "pairing_code_invalid"

    def __init__(self, *, attempts_remaining: int = 0) -> None:
        super().__init__(
            "pairing code invalid or expired",
            {"attempts_remaining": attempts_remaining},
        )


class PairingRateLimitedError(ProtocolError):
    """Too many pairing attempts; the host is locked out for a while."""

    code = "pairing_rate_limited"

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(
            f"pairing rate limited; retry in {retry_after_s:.0f}s",
            {"retry_after_s": retry_after_s},
        )


class CertificatePinMismatchError(ProtocolError):
    """The served certificate does not match the fingerprint pinned at pairing.

    Both fingerprints travel so a client can tell "the host rotated its
    certificate" from "somebody is in the middle" — the user has to re-pair
    either way, but the message differs.
    """

    code = "certificate_pin_mismatch"

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            "server certificate does not match the pinned fingerprint",
            {"expected": expected, "actual": actual},
        )


# --------------------------------------------------------------------------
# leases and sessions
# --------------------------------------------------------------------------


class LeaseHeldError(ProtocolError):
    """Another client holds the controller lease."""

    code = "lease_held"

    def __init__(self, holder: str, expires_at: float) -> None:
        super().__init__(
            f"controller lease held by {holder!r}",
            {"holder": holder, "expires_at": expires_at},
        )


class LeaseExpiredError(ProtocolError):
    """The caller's lease lapsed — it stopped heart-beating."""

    code = "lease_expired"

    def __init__(self, msg: str = "controller lease expired") -> None:
        super().__init__(msg)


class SessionNotFoundError(ProtocolError):
    """No session with that id is known to the daemon."""

    code = "session_not_found"

    def __init__(self, session_id: str) -> None:
        super().__init__(f"no such session {session_id!r}", {"session_id": session_id})


class SessionNotLiveError(ProtocolError):
    """The session exists on disk but no process is registered for it."""

    code = "session_not_live"

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session {session_id!r} is not live", {"session_id": session_id})


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------


class ReplayTruncatedError(ProtocolError):
    """The requested ``from_seq`` predates the retention window.

    ``earliest_seq`` is the whole point: the client resubscribes there and takes
    a state snapshot for the gap, instead of silently receiving a partial stream
    it believes is complete.
    """

    code = "replay_truncated"

    def __init__(self, requested_seq: int, earliest_seq: int) -> None:
        super().__init__(
            f"replay from seq {requested_seq} is outside the retention window "
            f"(earliest available: {earliest_seq})",
            {"requested_seq": requested_seq, "earliest_seq": earliest_seq},
        )


class SlowConsumerError(ProtocolError):
    """The client fell far enough behind that it was disconnected."""

    code = "slow_consumer"

    def __init__(self, dropped: int = 0, queue_size: int = 0) -> None:
        super().__init__(
            f"consumer too slow; {dropped} events dropped",
            {"dropped": dropped, "queue_size": queue_size},
        )


# --------------------------------------------------------------------------
# teleport
# --------------------------------------------------------------------------


class TeleportIncompatibleError(ProtocolError):
    """The target cannot host this session (version, provider, or cwd)."""

    code = "teleport_incompatible"

    def __init__(self, reason: str) -> None:
        super().__init__(f"teleport target incompatible: {reason}", {"reason": reason})


class TeleportInFlightError(ProtocolError):
    """The source refuses to offer while a tool call is running.

    Transferring mid-call would duplicate the call's side effects, so this is a
    refusal, not a warning.
    """

    code = "teleport_in_flight"

    def __init__(self, detail: str = "a tool call is in flight") -> None:
        super().__init__(f"cannot teleport: {detail}", {"detail": detail})


class TeleportIntegrityError(ProtocolError):
    """A chunk or the whole transfer failed its hash check."""

    code = "teleport_integrity"

    def __init__(self, expected: str, actual: str, *, chunk: int = -1) -> None:
        super().__init__(
            "teleport content failed integrity verification",
            {"expected": expected, "actual": actual, "chunk": chunk},
        )


class TeleportSealedError(ProtocolError):
    """The session was teleported away and is read-only here.

    ``moved_to`` is how a reopened source points the user at the machine that
    now owns the session, which is the only thing that prevents split brain from
    looking like data loss.
    """

    code = "teleport_sealed"

    def __init__(self, session_id: str, moved_to: str = "") -> None:
        super().__init__(
            f"session {session_id!r} was teleported and is sealed here",
            {"session_id": session_id, "moved_to": moved_to},
        )
