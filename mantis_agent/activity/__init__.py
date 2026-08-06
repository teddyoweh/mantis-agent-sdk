"""Unified activity graph — one typed tree over every execution engine.

Mantis runs concurrent work through several engines (background jobs, workflow
runs, subagents, watches, swarms, schedules), each with its own identity scheme,
status vocabulary and viewer. This package is the *read model* that projects all
of them into one tree with one normalized event stream. Engines keep executing
exactly as they do today; they gain a thin, failure-isolated emission call.

Four modules land first, and they are pure — no I/O, no asyncio, no UI:

* :mod:`~mantis_agent.activity.ids` — namespaced ids (``job:3``, ``wfp:4f2a/Review``)
  and the only place an id is ever parsed.
* :mod:`~mantis_agent.activity.status` — the unified vocabulary, per-engine
  mappers, and rollup.
* :mod:`~mantis_agent.activity.events` — the ``msgspec`` tagged-union envelope.
* :mod:`~mantis_agent.activity.registry` — the in-memory tree, its bounds, and
  subscriber fan-out.

Importing this package costs nothing at runtime beyond ``msgspec``, which the
SDK already imports, and nothing in it starts a task or opens a file.
"""

from __future__ import annotations

from .events import (
    ActivityEvent,
    NodeAction,
    NodeActivity,
    NodeCreated,
    NodeStatus,
    NodeUsage,
    decode_event,
    encode_event,
)
from .ids import (
    KIND_PREFIXES,
    KINDS,
    PREFIX_KINDS,
    ActivityError,
    InvalidIdError,
    is_id,
    make_id,
    parse_id,
    resolve_ref,
    resolve_refs,
)
from .registry import (
    ActivityConfig,
    ActivityNode,
    ActivityRegistry,
    sanitize_text,
)
from .status import (
    ROLLUP_KINDS,
    STATUSES,
    TERMINAL,
    is_terminal,
    resolve_status,
    roll_up,
)

__all__ = [
    "ActivityConfig",
    "ActivityError",
    "ActivityEvent",
    "ActivityNode",
    "ActivityRegistry",
    "InvalidIdError",
    "KINDS",
    "KIND_PREFIXES",
    "NodeAction",
    "NodeActivity",
    "NodeCreated",
    "NodeStatus",
    "NodeUsage",
    "PREFIX_KINDS",
    "ROLLUP_KINDS",
    "STATUSES",
    "TERMINAL",
    "decode_event",
    "encode_event",
    "is_id",
    "is_terminal",
    "make_id",
    "parse_id",
    "resolve_ref",
    "resolve_refs",
    "resolve_status",
    "roll_up",
    "sanitize_text",
]
