"""The versioned session protocol — one contract for every remote surface.

Mantis's remote surfaces (dashboard, IDE extension, phone, automation script)
must not each reverse-engineer a private JSON API. This package is the contract
they share: a framed envelope, a handshake that settles version and
capabilities, and a stable error taxonomy.

Three modules land first, and they are pure — no sockets, no TLS, no asyncio,
no daemon:

* :mod:`~mantis_agent.protocol.errors` — the §12 taxonomy, each error with a
  stable ``code`` and structured recovery data.
* :mod:`~mantis_agent.protocol.version` — protocol constants, the
  operation/capability table, and negotiation.
* :mod:`~mantis_agent.protocol.envelope` — the ``req`` / ``res`` / ``ev`` /
  ``err`` structs and newline-delimited JSON framing.

The event payload is the activity envelope from
:mod:`mantis_agent.activity.events`, unchanged and untranslated. That plan's
journal is this plan's replay log; defining a second event model here would
guarantee the two drift.

Nothing in this package performs I/O or starts a task, and importing it costs
only ``msgspec``, which the SDK already imports.
"""

from __future__ import annotations

from .envelope import (
    DECODE_FRAME,
    ENCODE_FRAME,
    MAX_MESSAGE_BYTES,
    ErrorFrame,
    Event,
    Frame,
    FrameReader,
    Request,
    Response,
    decode_frame,
    encode_frame,
    encode_line,
    error_frame,
    error_from_frame,
    response_to,
)
from .errors import (
    ERROR_CODES,
    CapabilityNotNegotiatedError,
    CertificatePinMismatchError,
    InternalError,
    LeaseExpiredError,
    LeaseHeldError,
    MalformedFrameError,
    MessageTooLargeError,
    NotAuthenticatedError,
    NotAuthorizedError,
    PairingCodeInvalidError,
    PairingRateLimitedError,
    ProtocolError,
    ReplayTruncatedError,
    SessionNotFoundError,
    SessionNotLiveError,
    SlowConsumerError,
    TeleportInFlightError,
    TeleportIncompatibleError,
    TeleportIntegrityError,
    TeleportSealedError,
    UnknownOperationError,
    VersionMismatchError,
)
from .version import (
    AUTH_MODES,
    CAP_CONTROL,
    CAP_NOTIFY,
    CAP_REPLAY,
    CAP_RESPOND_PERMISSION,
    CAP_STREAM,
    CAP_TELEPORT,
    FREE_OPS,
    OP_CAPABILITIES,
    OPERATIONS,
    PROTOCOL_VERSION,
    SERVER_CAPABILITIES,
    SUPPORTED_PROTOCOLS,
    Hello,
    Negotiated,
    Welcome,
    check_operation,
    negotiate,
    parse_hello,
    require_capability,
)

__all__ = [
    "AUTH_MODES",
    "CAP_CONTROL",
    "CAP_NOTIFY",
    "CAP_REPLAY",
    "CAP_RESPOND_PERMISSION",
    "CAP_STREAM",
    "CAP_TELEPORT",
    "DECODE_FRAME",
    "ENCODE_FRAME",
    "ERROR_CODES",
    "FREE_OPS",
    "MAX_MESSAGE_BYTES",
    "OPERATIONS",
    "OP_CAPABILITIES",
    "PROTOCOL_VERSION",
    "SERVER_CAPABILITIES",
    "SUPPORTED_PROTOCOLS",
    "CapabilityNotNegotiatedError",
    "CertificatePinMismatchError",
    "ErrorFrame",
    "Event",
    "Frame",
    "FrameReader",
    "Hello",
    "InternalError",
    "LeaseExpiredError",
    "LeaseHeldError",
    "MalformedFrameError",
    "MessageTooLargeError",
    "Negotiated",
    "NotAuthenticatedError",
    "NotAuthorizedError",
    "PairingCodeInvalidError",
    "PairingRateLimitedError",
    "ProtocolError",
    "ReplayTruncatedError",
    "Request",
    "Response",
    "SessionNotFoundError",
    "SessionNotLiveError",
    "SlowConsumerError",
    "TeleportInFlightError",
    "TeleportIncompatibleError",
    "TeleportIntegrityError",
    "TeleportSealedError",
    "UnknownOperationError",
    "VersionMismatchError",
    "Welcome",
    "check_operation",
    "decode_frame",
    "encode_frame",
    "encode_line",
    "error_frame",
    "error_from_frame",
    "negotiate",
    "parse_hello",
    "require_capability",
    "response_to",
]
