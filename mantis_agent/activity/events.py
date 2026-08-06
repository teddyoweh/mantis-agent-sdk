"""The activity event envelope.

Encoding follows ``mantis_agent/events.py`` exactly — ``msgspec.Struct``,
``frozen=True``, ``tag_field="type"``, one struct per event type — so the
envelope decodes as a discriminated union with the same machinery the streaming
events already use, and a journal line is just ``encode_event(ev)``.

Two fields are filled in by the registry, never by the emitter:

* ``seq`` — a per-session monotonic counter. Emitters race; the registry
  serializes, so an emitter passes ``0`` and gets a stamped copy back.
* ``ts`` — wall-clock epoch seconds. Engines measure in ``time.monotonic()``
  (``Job.started``, ``AgentRun.elapsed_ms``); the registry normalizes once at
  its boundary against a captured origin pair so a replayed timestamp is stable.

``omit_defaults=True`` keeps journal lines small, which matters because a chatty
watch can produce thousands of ``NodeActivity`` events per minute.
"""

from __future__ import annotations

from typing import Union

import msgspec

__all__ = [
    "ActivityEvent",
    "DECODE_EVENT",
    "ENCODE_EVENT",
    "NodeAction",
    "NodeActivity",
    "NodeCreated",
    "NodeStatus",
    "NodeUsage",
    "decode_event",
    "encode_event",
]


class NodeCreated(
    msgspec.Struct,
    frozen=True,
    tag="node_created",
    tag_field="type",
    omit_defaults=True,
):
    """A node enters the tree.

    ``parent_id`` may name a node that does not exist yet — engines discover
    structure in whatever order they execute — so the registry links eagerly and
    lets the parent arrive late.

    Everything after ``title`` is descriptive metadata the cockpit renders.
    ``effort``, ``permission_mode``, ``transcript_ref`` and ``actions`` are
    carried here (rather than in a later event) because §9 requires an agent
    node to know them at start: they are properties of how the work was
    *dispatched*, and they never change afterwards.
    """

    seq: int
    ts: float
    node_id: str
    parent_id: str | None
    kind: str
    title: str
    detail: str = ""
    source: str = "model"
    model: str = ""
    provider: str = ""
    isolation: str = "none"
    effort: str = ""
    permission_mode: str = ""
    transcript_ref: str | None = None
    actions: tuple[str, ...] = ()


class NodeStatus(
    msgspec.Struct,
    frozen=True,
    tag="node_status",
    tag_field="type",
    omit_defaults=True,
):
    """A node's own status changed.

    ``status`` is already in the unified vocabulary — mapping happens in
    ``activity/status.py`` at the emitting engine, not here. ``error`` carries
    terminal failure text and is redacted before display.
    """

    seq: int
    ts: float
    node_id: str
    status: str
    error: str | None = None


class NodeActivity(
    msgspec.Struct,
    frozen=True,
    tag="node_activity",
    tag_field="type",
    omit_defaults=True,
):
    """One line of "what this node is doing right now".

    Model-authored: the text originates from tool arguments and child output, so
    the registry sanitizes it on ingest.
    """

    seq: int
    ts: float
    node_id: str
    text: str


class NodeUsage(
    msgspec.Struct,
    frozen=True,
    tag="node_usage",
    tag_field="type",
    omit_defaults=True,
):
    """An increment of token/cost usage. Deltas, not totals — the registry
    accumulates, so a dropped event costs accuracy but never double-counts."""

    seq: int
    ts: float
    node_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0


class NodeAction(
    msgspec.Struct,
    frozen=True,
    tag="node_action",
    tag_field="type",
    omit_defaults=True,
):
    """Somebody asked a node to do something.

    Recorded *before* the attempt and independently of its outcome — the
    resulting ``NodeStatus`` is what says whether it worked. ``actor`` is what
    makes the journal an audit trail: refusals are recorded too, with the reason
    in ``detail``.
    """

    seq: int
    ts: float
    node_id: str
    action: str  # stop | pause | resume | retry | skip | message | approve
    actor: str  # user | parent | policy | schedule
    detail: str = ""


#: The tagged union. ``msgspec.json.Decoder(ActivityEvent)`` dispatches on the
#: ``type`` field, so a journal reader needs no peek-and-route helper (unlike
#: ``types.Message``, whose union is untagged).
ActivityEvent = Union[NodeCreated, NodeStatus, NodeActivity, NodeUsage, NodeAction]

# One encoder/decoder per process, mirroring ``types.ENCODE_MESSAGE`` — msgspec
# codecs are thread-safe and reusing them is measurably faster than building one
# per call at journal volumes.
ENCODE_EVENT = msgspec.json.Encoder()
DECODE_EVENT = msgspec.json.Decoder(ActivityEvent)


def encode_event(ev: ActivityEvent) -> bytes:
    """Encode one event to a single JSON line's worth of bytes."""

    return ENCODE_EVENT.encode(ev)


def decode_event(data: bytes | str) -> ActivityEvent:
    """Decode one encoded event, dispatching on its ``type`` tag."""

    return DECODE_EVENT.decode(data)
