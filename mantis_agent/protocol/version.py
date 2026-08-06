"""Protocol version and capability negotiation (plan §6, "Handshake").

The handshake decides two things, and both are decided *once*, before any work:

**Version.** ``v`` is a major version. There is no minor version on the wire and
no field-level feature detection, because a client author cannot test against a
server they do not have. A shared major or a refusal — :func:`negotiate` never
downgrades silently. The refusal carries the whole supported list so the client
can say something better than "connection failed".

**Capabilities.** The client states what it can do; the server answers with the
intersection of that and what it is willing to grant. The intersection is the
authorization boundary for the rest of the connection: :func:`check_operation`
refuses an operation outside it *before* it is attempted, so a phone that never
asked for ``control`` cannot stumble into ``session.prompt`` through a bug in
its own dispatch table.

This module is the leaf of the package's dependency graph — it imports only the
error taxonomy — so :mod:`~mantis_agent.protocol.envelope` can take its version
constant from here without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Sequence, Tuple

import msgspec

from .errors import (
    CapabilityNotNegotiatedError,
    MalformedFrameError,
    UnknownOperationError,
    VersionMismatchError,
)

__all__ = [
    "AUTH_MODES",
    "CAP_CONTROL",
    "CAP_NOTIFY",
    "CAP_REPLAY",
    "CAP_RESPOND_PERMISSION",
    "CAP_STREAM",
    "CAP_TELEPORT",
    "FREE_OPS",
    "OPERATIONS",
    "OP_CAPABILITIES",
    "PROTOCOL_VERSION",
    "SERVER_CAPABILITIES",
    "SUPPORTED_PROTOCOLS",
    "Hello",
    "Negotiated",
    "Welcome",
    "check_operation",
    "negotiate",
    "parse_hello",
    "require_capability",
]

#: The version this build speaks and the versions it accepts. A future build
#: that adds version 2 keeps 1 here for as long as it supports old clients;
#: dropping a number from this tuple is a breaking change and belongs in a
#: major release.
PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOLS: Tuple[int, ...] = (1,)

# Capability names. Constants rather than bare strings because these appear in
# the device-authorization records of §7 as well as on the wire, and a typo in
# one place would silently grant or withhold.
CAP_STREAM = "stream"  # read live events, discovery, transcript tail
CAP_REPLAY = "replay"  # subscribe with from_seq / journal replay
CAP_CONTROL = "control"  # prompt, interrupt, node actions, leases, lifecycle
CAP_RESPOND_PERMISSION = "respond_permission"  # answer a pending permission ask
CAP_TELEPORT = "teleport"  # move a session between machines
CAP_NOTIFY = "notify"  # receive notification-class pushes

#: Everything a server may grant. A deployment narrows this per device (§7); the
#: negotiated set is always ``client ∩ granted``.
SERVER_CAPABILITIES: Tuple[str, ...] = (
    CAP_STREAM,
    CAP_REPLAY,
    CAP_CONTROL,
    CAP_RESPOND_PERMISSION,
    CAP_TELEPORT,
    CAP_NOTIFY,
)

#: How the server authenticates this transport, reported in the welcome so a
#: client knows whether to present a credential: ``peer`` is the same-UID Unix
#: socket check (no token exists to send), ``required`` is a device credential,
#: ``none`` is a deliberately open loopback listener.
AUTH_MODES: Tuple[str, ...] = ("peer", "required", "none")

#: Operations that need no capability. ``hello`` is how capabilities are
#: obtained in the first place; ``ping`` and ``caps`` must work on a connection
#: that has none, or a client cannot tell "not permitted" from "dead socket".
#: ``pair.*`` is pre-authentication by construction.
FREE_OPS: FrozenSet[str] = frozenset(
    {"hello", "ping", "caps", "pair.begin", "pair.submit", "pair.cancel"}
)

#: Operation → required capability, from the §6 operation tables. Discovery
#: rides on ``stream``: listing sessions is watching, and a device permitted to
#: watch is permitted to find what to watch.
OP_CAPABILITIES: Dict[str, str] = {
    # discovery
    "session.list": CAP_STREAM,
    "session.get": CAP_STREAM,
    "project.list": CAP_STREAM,
    # streaming
    "session.subscribe": CAP_STREAM,
    "session.unsubscribe": CAP_STREAM,
    "transcript.tail": CAP_STREAM,
    # control (each of these additionally requires a lease at runtime — that is
    # the daemon's check, not this one; capability is "may this device ever",
    # the lease is "may this device right now")
    "control.acquire": CAP_CONTROL,
    "control.release": CAP_CONTROL,
    "control.heartbeat": CAP_CONTROL,
    "session.prompt": CAP_CONTROL,
    "session.interrupt": CAP_CONTROL,
    "node.action": CAP_CONTROL,
    "plan.approve": CAP_CONTROL,
    "session.mode": CAP_CONTROL,
    # answering a permission ask is split out from control on purpose: §7's
    # phone profile is allowed to approve prompts without being able to drive
    # the session.
    "permission.respond": CAP_RESPOND_PERMISSION,
    # lifecycle
    "session.create": CAP_CONTROL,
    "session.resume": CAP_CONTROL,
    "session.fork": CAP_CONTROL,
    # teleport
    "session.teleport.offer": CAP_TELEPORT,
    "session.teleport.accept": CAP_TELEPORT,
    "session.teleport.transfer": CAP_TELEPORT,
    "session.teleport.commit": CAP_TELEPORT,
    "session.teleport.abort": CAP_TELEPORT,
    # notifications
    "notify.subscribe": CAP_NOTIFY,
    "notify.unsubscribe": CAP_NOTIFY,
}

#: Every operation this version knows. An ``op`` outside it is refused as
#: unknown rather than ignored, so a typo in a client is loud.
OPERATIONS: FrozenSet[str] = frozenset(OP_CAPABILITIES) | FREE_OPS


class Hello(msgspec.Struct, frozen=True, omit_defaults=True):
    """The ``d`` of the client's ``hello`` request.

    Every field defaults, and the defaults are the *refusing* ones: an empty
    ``protocol`` fails negotiation and empty ``caps`` grants nothing. A client
    that forgets a field gets a clear error, never an accidental grant.
    """

    client: str = ""
    protocol: Tuple[int, ...] = ()
    caps: Tuple[str, ...] = ()


class Welcome(msgspec.Struct, frozen=True):
    """The ``d`` of the server's successful ``hello`` response.

    ``protocol`` is a single number — the one chosen — not a range: after the
    handshake there is exactly one version in play.
    """

    server: str
    protocol: int
    caps: Tuple[str, ...]
    auth: str = "required"
    session_count: int = 0


@dataclass(frozen=True)
class Negotiated:
    """The settled terms of one connection.

    Server-side state, never encoded, which is why it is a frozen dataclass and
    not a struct. Frozen because a connection's terms are decided at handshake
    and must not drift afterwards — re-negotiation replaces the object.
    """

    version: int
    caps: FrozenSet[str]
    client: str = ""

    def has(self, capability: str) -> bool:
        """Was ``capability`` granted?"""

        return capability in self.caps

    def allows(self, op: str) -> bool:
        """Is ``op`` permitted under these terms? Unknown ops are not."""

        if op in FREE_OPS:
            return True
        needed = OP_CAPABILITIES.get(op)
        return needed is not None and needed in self.caps

    def require(self, op: str) -> None:
        """Raise unless ``op`` is permitted. See :func:`check_operation`."""

        check_operation(self, op)


def parse_hello(payload: Mapping[str, Any]) -> Hello:
    """Decode a ``hello`` request's ``d`` into a :class:`Hello`.

    Raises :class:`MalformedFrameError` — not a ``msgspec`` error — so the
    daemon's connection loop has exactly one exception family to handle.
    """

    try:
        return msgspec.convert(payload, Hello, strict=True)
    except (msgspec.ValidationError, msgspec.DecodeError, TypeError) as exc:
        raise MalformedFrameError(f"invalid hello payload: {exc}") from None


def negotiate(
    hello: Hello,
    *,
    server: str = "mantisd",
    supported: Sequence[int] = SUPPORTED_PROTOCOLS,
    capabilities: Iterable[str] = SERVER_CAPABILITIES,
    auth: str = "required",
    session_count: int = 0,
) -> Tuple[Negotiated, Welcome]:
    """Settle version and capabilities, or refuse.

    Picks the **highest** mutually supported version: a client that offers
    ``[1, 2]`` to a server that speaks both gets 2, and one that offers only
    ``[1]`` still connects. No shared version raises
    :class:`VersionMismatchError` carrying the supported range — the plan is
    explicit that this is never a silent downgrade to something the client did
    not offer.

    Capabilities are the plain intersection. Names the server does not know are
    dropped rather than rejected: a newer client asking for a capability this
    build has never heard of should connect with less, not fail.
    """

    offered = tuple(hello.protocol)
    shared = sorted(set(offered) & set(supported))
    if not shared:
        raise VersionMismatchError(offered, tuple(supported))
    granted = frozenset(capabilities) & frozenset(hello.caps)
    negotiated = Negotiated(version=shared[-1], caps=granted, client=hello.client)
    welcome = Welcome(
        server=server,
        protocol=negotiated.version,
        # sorted so two servers with the same grants produce byte-identical
        # welcomes — worth it for testability and for diffing captured traffic
        caps=tuple(sorted(granted)),
        auth=auth,
        session_count=session_count,
    )
    return negotiated, welcome


def require_capability(negotiated: Negotiated, capability: str, op: str = "") -> None:
    """Raise :class:`CapabilityNotNegotiatedError` unless ``capability`` was granted.

    Exposed separately from :func:`check_operation` because some checks are
    argument-level rather than op-level: ``session.subscribe`` needs ``stream``,
    but ``session.subscribe`` *with* ``from_seq`` also needs ``replay``.
    """

    if capability not in negotiated.caps:
        raise CapabilityNotNegotiatedError(op or capability, capability, negotiated.caps)


def check_operation(negotiated: Negotiated, op: str) -> None:
    """Gate one operation against the negotiated set.

    Unknown before un-negotiated: telling a client "no such operation" when it
    misspelled one is more useful than telling it to negotiate a capability that
    would not have helped.
    """

    if op in FREE_OPS:
        return
    needed = OP_CAPABILITIES.get(op)
    if needed is None:
        raise UnknownOperationError(op)
    require_capability(negotiated, needed, op)
