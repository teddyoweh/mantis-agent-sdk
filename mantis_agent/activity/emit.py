"""The only activity API an engine touches.

Every engine in Mantis is already working code that someone depends on. What
this package adds to them is *instrumentation*, and instrumentation has exactly
one hard rule: it must never change the outcome of the work it describes. That
rule is what this module is for, and it is why the emission surface is five
functions rather than a registry reference passed around the engines.

Two properties, both of them load-bearing:

* **``reg=None`` costs nothing.** The first statement of every helper is
  ``if reg is None: return``. Activity is off by default, so the common path is
  a null check against a local attribute — no event constructed, no id built, no
  ``try`` block entered. That is what keeps the diff in ``jobs.py`` and
  ``workflow.py`` to single guarded lines instead of conditional plumbing.
* **A registry problem is never an engine problem.** Every call into the
  registry is wrapped and swallowed, exactly as ``JobManager._fire_on_event``
  swallows notifier errors — "a broken notifier must never turn a completed job
  into a failure". :meth:`ActivityRegistry.apply` already counts its own
  rejections rather than raising, but an engine must not have to *trust* that:
  a registry double in a test, a partially-initialized registry during startup,
  or a future subscriber that raises from a ``__del__`` must all be non-events
  from where the engine stands.

``except Exception`` and not ``except BaseException``, deliberately.
``asyncio.CancelledError`` is a ``BaseException`` on every Python this package
supports, and several of these calls happen inside a job's cancellation path —
swallowing a cancellation there would break structured cancellation, which is a
far worse failure than a lost rail line.

The helpers take an already-namespaced ``node_id`` (see
:func:`~mantis_agent.activity.ids.make_id`) rather than an engine-local one:
building the id is the emitting engine's business, and keeping it out of here
means one node's id can be reused across several events without re-deriving it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .events import NodeAction, NodeActivity, NodeCreated, NodeStatus, NodeUsage

__all__ = [
    "node_action",
    "node_activity",
    "node_created",
    "node_status",
    "node_usage",
]

#: The descriptive fields :class:`NodeCreated` accepts beyond the positional
#: five. ``**kw`` is filtered against this set rather than splatted straight in:
#: a caller that passes a field this version does not have should lose that
#: field, not the whole node. Losing the node would lose every event that hangs
#: off it, which is the opposite of failure isolation.
_CREATED_FIELDS = frozenset(NodeCreated.__struct_fields__) - {
    "seq",
    "ts",
    "node_id",
    "parent_id",
    "kind",
    "title",
}


def _send(reg: Any, ev: Any) -> None:
    """Hand one event to the registry, swallowing anything it does.

    ``reg`` is duck-typed: anything with an ``apply`` method works, and anything
    without one is simply a no-op rather than an ``AttributeError`` in the
    middle of an engine's ``finally``.
    """

    try:
        reg.apply(ev)
    except Exception:  # noqa: BLE001 — see module docstring
        pass


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:  # noqa: BLE001 — a bad counter is a zero, not a crash
        return 0


def _as_float(value: Any) -> float:
    try:
        out = float(value)
    except Exception:  # noqa: BLE001
        return 0.0
    return out if out == out and abs(out) != float("inf") else 0.0  # NaN/inf → 0


def _read(source: Any, *names: str) -> Any:
    """First present value among ``names``, from a mapping or an object.

    Usage arrives as a :class:`~mantis_agent.types.Usage` struct from the
    providers, as a plain dict from a JSON payload, and as a
    ``BudgetTracker``-shaped object from the workflow engine. All three answer
    to the same field names; only the access syntax differs.
    """

    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return None
    for name in names:
        got = getattr(source, name, None)
        if got is not None:
            return got
    return None


def node_created(
    reg: Any,
    node_id: str,
    parent_id: str | None,
    kind: str,
    title: str,
    **kw: Any,
) -> None:
    """Announce a node.

    ``parent_id`` may name a node that does not exist yet — engines discover
    structure in the order they execute, and the registry links eagerly — or be
    ``None`` for a root. Extra keyword arguments are the descriptive metadata of
    :class:`~mantis_agent.activity.events.NodeCreated` (``detail``, ``source``,
    ``model``, ``provider``, ``isolation``, ``effort``, ``permission_mode``,
    ``transcript_ref``, ``actions``); unknown ones are dropped.
    """

    if reg is None:
        return
    try:
        ev = NodeCreated(
            seq=0,
            ts=0.0,
            node_id=node_id,
            parent_id=parent_id,
            kind=kind,
            title=title,
            **{k: v for k, v in kw.items() if k in _CREATED_FIELDS},
        )
    except Exception:  # noqa: BLE001
        return
    _send(reg, ev)


def node_status(
    reg: Any, node_id: str, status: str, error: str | None = None
) -> None:
    """Record a node's own status, already in the unified vocabulary.

    Engines map with the ``activity.status`` helpers (``from_job_status`` and
    friends) at the call site, so the registry never has to know which engine an
    event came from.
    """

    if reg is None:
        return
    try:
        ev = NodeStatus(seq=0, ts=0.0, node_id=node_id, status=status, error=error)
    except Exception:  # noqa: BLE001
        return
    _send(reg, ev)


def node_activity(reg: Any, node_id: str, text: str) -> None:
    """Record one "what this node is doing right now" line.

    The text is model-authored in almost every case; the registry sanitizes and
    rate-limits it on ingest, so callers pass it through untouched.
    """

    if reg is None:
        return
    try:
        ev = NodeActivity(seq=0, ts=0.0, node_id=node_id, text=text)
    except Exception:  # noqa: BLE001
        return
    _send(reg, ev)


def node_usage(reg: Any, node_id: str, usage: Any = None, *, cost_usd: Any = 0.0) -> None:
    """Add a usage *delta* to a node.

    ``usage`` is anything shaped like :class:`~mantis_agent.types.Usage` — the
    struct itself, a mapping, or a tracker object with the same field names.
    The registry accumulates, so callers must pass increments; an all-zero delta
    is dropped here rather than spending a sequence number and a slot in the
    node's rate-limit window on a no-op.
    """

    if reg is None:
        return
    try:
        inp = _as_int(_read(usage, "input_tokens", "prompt_tokens"))
        out = _as_int(_read(usage, "output_tokens", "completion_tokens"))
        cached = _as_int(
            _read(usage, "cache_read_input_tokens", "cache_read_tokens")
        )
        cost = _as_float(cost_usd) or _as_float(_read(usage, "cost_usd"))
        if not (inp or out or cached or cost):
            return
        ev = NodeUsage(
            seq=0,
            ts=0.0,
            node_id=node_id,
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cached,
            cost_usd=cost,
        )
    except Exception:  # noqa: BLE001
        return
    _send(reg, ev)


def node_action(
    reg: Any, node_id: str, action: str, actor: str = "user", detail: str = ""
) -> None:
    """Record that somebody asked a node to do something.

    Emitted *before* the attempt and independently of its outcome: the resulting
    :class:`~mantis_agent.activity.events.NodeStatus` is what says whether it
    worked. ``actor`` is what makes the journal an audit trail, so a refusal is
    recorded too, with the reason in ``detail``.
    """

    if reg is None:
        return
    try:
        ev = NodeAction(
            seq=0,
            ts=0.0,
            node_id=node_id,
            action=action,
            actor=actor,
            detail=detail,
        )
    except Exception:  # noqa: BLE001
        return
    _send(reg, ev)
