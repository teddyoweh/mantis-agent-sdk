"""The in-memory activity tree.

The registry is a *read model*, not a runtime. It executes nothing, owns no
tasks and makes no UI decisions. It accepts events from engines that keep
running exactly as they do today, and in exchange it guarantees five things:

* **Order.** ``seq`` is assigned here, so concurrent emitters cannot disagree
  about what happened first.
* **Time.** Engines measure with ``time.monotonic()``; the registry normalizes
  against a captured ``(monotonic_origin, wallclock_origin)`` pair so a
  timestamp survives being written down and read back later.
* **Structure.** Parent links, ancestor rollup, and orphan attachment for the
  engines that emit progress before they emit the node.
* **Bounds.** Every counter in §7 is enforced here: live nodes, recent ring,
  terminal retention, orphan buffer, per-node event rate.
* **Isolation.** A subscriber that raises is counted and eventually detached —
  the rule ``JobManager._fire_on_event`` already follows, plus the threshold.

Nothing in here may raise into an engine. A registry failure must never turn a
completed job into a failed one, so every rejection is a counter (see
:attr:`ActivityRegistry.stats`), not an exception. That covers the envelope
itself: :meth:`ActivityRegistry.apply` type-checks the fields it does arithmetic
and dictionary lookups on, and a value that fails is counted and dropped rather
than allowed to surface as a ``TypeError`` inside somebody's ``finally``.
"""

from __future__ import annotations

import inspect
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import msgspec

from ..types import Usage
from . import status as statuses
from .events import (
    ActivityEvent,
    NodeAction,
    NodeActivity,
    NodeCreated,
    NodeStatus,
    NodeUsage,
)
from .ids import InvalidIdError, make_id

#: The concrete members of the ``ActivityEvent`` union. ``isinstance`` needs a
#: tuple of classes, and §8's envelope check is the reason this exists: anything
#: that is not one of these is not an event, whatever it claims to be.
_EVENT_TYPES = (NodeCreated, NodeStatus, NodeActivity, NodeUsage, NodeAction)

__all__ = [
    "ACTIVITY_MAX",
    "ActivityConfig",
    "ActivityNode",
    "ActivityRegistry",
    "DETAIL_MAX",
    "ERROR_MAX",
    "TITLE_MAX",
    "sanitize_text",
]

# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------
# Titles, details and activity lines originate from tool arguments and child
# agent output. They are rendered into a persistent line under the input box, so
# a subagent that emits a cursor-movement escape could redraw the rail or forge
# a second node's line. Neutralize on ingest, not on render: the registry sits
# upstream of every consumer, so doing it here means no subscriber, journal line
# or projection can ever observe the hostile form.

_ANSI_RE = re.compile(
    "\x1b\\[[0-9;?]*[ -/]*[@-~]"              # CSI ... final byte
    "|\x1b\\][^\x07\x1b]*(?:\x07|\x1b\\\\)"   # OSC ... BEL / ST
    "|\x1b[@-Z\\\\-_]"                        # two-character escapes
)
# C0 minus the whitespace folded below, plus DEL and the whole C1 block.
_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# Zero-width characters, bidi embeddings/overrides/isolates, and the line and
# paragraph separators — all invisible, all able to reorder or hide text.
_INVISIBLE_RE = re.compile(
    "[\\u200b-\\u200f\\u2028\\u2029\\u202a-\\u202e"
    "\\u2060-\\u2064\\u2066-\\u2069\\ufeff]"
)
_WHITESPACE_RE = re.compile(r"\s+")

TITLE_MAX = 80  # §5: "one-line human label, ≤ 80 chars"
DETAIL_MAX = 160
ACTIVITY_MAX = 200
ERROR_MAX = 500
_TOKEN_MAX = 32  # action / actor names


def sanitize_text(text: Any, limit: int = ACTIVITY_MAX) -> str:
    """Make untrusted text safe to put on a shared terminal line.

    Strips ANSI sequences, C0/C1 control characters, bidi overrides and
    zero-width characters, collapses every whitespace run (newlines included)
    to a single space, then truncates to ``limit`` with an ellipsis.
    """

    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if not s:
        return ""
    s = _ANSI_RE.sub("", s)
    s = _INVISIBLE_RE.sub("", s)
    s = _CONTROL_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    if 0 < limit < len(s):
        s = s[: max(1, limit - 1)].rstrip() + "…"
    return s


# ---------------------------------------------------------------------------
# Configuration and nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivityConfig:
    """Every bound in §7, with defaults that carry their own rationale.

    Frozen because a live registry's budgets must not drift underneath it;
    build a new one with ``dataclasses.replace`` when you need other limits.
    """

    max_live_nodes: int = 2000
    #: Generalizes ``workflow._RECENT_CAP = 5`` and ``Job.events`` (maxlen 40).
    max_recent_per_node: int = 8
    #: Generalizes ``jobs._MAX_RETAINED_JOBS = 100``.
    terminal_node_retention: int = 200
    #: A watch already batches at 0.2 s; this is the registry's second pass.
    max_events_per_second_per_node: int = 50
    orphan_buffer_size: int = 256
    #: An orphan older than this will never find its node — drop and count it.
    orphan_ttl_s: float = 30.0
    #: Failures a subscriber gets before it is detached for good.
    subscriber_error_threshold: int = 3


@dataclass
class ActivityNode:
    """One unit of work, whichever engine happens to be running it.

    ``status`` is what a viewer displays; ``intrinsic_status`` is the last
    explicit :class:`~mantis_agent.activity.events.NodeStatus` this node
    received. For engine-owned kinds the two are always equal. For container
    kinds (``phase``, ``swarm``, ...) the displayed value rolls up from
    children, and keeping both means the container's own verdict — a cancelled
    phase — is never lost behind its children's.
    """

    id: str
    parent_id: str | None = None
    kind: str = ""
    title: str = ""
    detail: str = ""
    status: str = statuses.PENDING
    intrinsic_status: str = statuses.PENDING
    created_at: float = 0.0
    started_at: float | None = None
    ended_at: float | None = None
    model: str = ""
    provider: str = ""
    effort: str = ""
    usage: Usage | None = None
    cost_usd: float | None = None
    activity: str = ""
    recent: tuple[str, ...] = ()
    transcript_ref: str | None = None
    permission_mode: str = ""
    isolation: str = "none"
    source: str = "model"
    actions: frozenset = frozenset()
    error: str | None = None
    #: Creation ``seq``. Eviction is oldest-first and this is the total order.
    created_seq: int = 0

    def elapsed_s(self, now: float) -> float:
        """Wall-clock seconds this node has been (or was) running."""

        if self.started_at is None:
            return 0.0
        end = self.ended_at if self.ended_at is not None else now
        return max(0.0, end - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        """Plain-data snapshot, in the shape ``Job``/``AgentRun`` already use."""

        u = self.usage
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "status": self.status,
            "intrinsic_status": self.intrinsic_status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "model": self.model,
            "provider": self.provider,
            "effort": self.effort,
            "usage": None
            if u is None
            else {
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cache_creation_input_tokens": u.cache_creation_input_tokens,
                "cache_read_input_tokens": u.cache_read_input_tokens,
            },
            "cost_usd": self.cost_usd,
            "activity": self.activity,
            "recent": list(self.recent),
            "transcript_ref": self.transcript_ref,
            "permission_mode": self.permission_mode,
            "isolation": self.isolation,
            "source": self.source,
            "actions": sorted(self.actions),
            "error": self.error,
        }


@dataclass
class _Subscription:
    """A subscriber plus its error budget."""

    fn: Callable[[ActivityEvent], None]
    errors: int = 0


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class ActivityRegistry:
    """Per-session activity tree.

    Construction is explicit — there is no module-level singleton — so tests,
    SDK embedders and future multi-session hosts can run isolated registries
    concurrently::

        registry = ActivityRegistry(session_id=session.id)
        node_id = registry.create_node("job", 3, title="pytest -q")
        registry.set_status(node_id, "running")

    ``clock`` and ``monotonic`` are injectable for the same reason ``Workflow``
    takes ``_clock``: several bounds in here are functions of time, and a test
    must be able to control it.
    """

    def __init__(
        self,
        *,
        session_id: str = "",
        config: ActivityConfig | None = None,
        clock: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.session_id = session_id
        self.config = config or ActivityConfig()
        self._clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        # The origin pair, captured once and together, so the two clocks stay
        # relatable for the life of the session even though only one of them
        # survives a restart.
        self.wallclock_origin = self._clock()
        self.monotonic_origin = self._monotonic()

        #: node id -> node, in creation order (which is also eviction order).
        self.nodes: OrderedDict[str, ActivityNode] = OrderedDict()
        self._children: dict[str, list[str]] = {}
        self._orphans: OrderedDict[str, list[ActivityEvent]] = OrderedDict()
        self._orphan_count = 0
        self._subs: list[_Subscription] = []
        self._seq = 0
        self._counts: dict[str, int] = {s: 0 for s in statuses.STATUSES}
        # node id -> [window_start_monotonic, events_in_window]
        self._rate: dict[str, list[float]] = {}
        #: Diagnostics. Every rejection is counted rather than raised, so this
        #: is the only evidence a bound fired — tests and diagnostics read it.
        self.stats: dict[str, int] = {
            "events": 0,
            "applied": 0,
            "coalesced": 0,
            "dropped_duplicate": 0,
            "dropped_malformed": 0,
            "dropped_node_limit": 0,
            "dropped_orphan": 0,
            "dropped_parent_link": 0,
            "evicted": 0,
            "orphans_attached": 0,
            "orphans_buffered": 0,
            "rejected_parent": 0,
            "subscriber_detached": 0,
            "subscriber_errors": 0,
            "unknown_status": 0,
        }

    # ------------------------------------------------------------------
    # Clocks
    # ------------------------------------------------------------------

    def now(self) -> float:
        """Current wall-clock epoch seconds."""

        return self._clock()

    def to_wallclock(self, monotonic_s: float) -> float:
        """Convert a ``time.monotonic()`` reading to epoch seconds."""

        return self.wallclock_origin + (monotonic_s - self.monotonic_origin)

    def to_wallclock_ms(self, monotonic_ms: float) -> float:
        """Convert a workflow-style monotonic *milliseconds* reading."""

        return self.to_wallclock(monotonic_ms / 1000.0)

    @property
    def origin(self) -> tuple[float, float]:
        """``(monotonic_origin, wallclock_origin)`` — the journal header pair."""

        return (self.monotonic_origin, self.wallclock_origin)

    @property
    def last_seq(self) -> int:
        """The highest ``seq`` assigned so far."""

        return self._seq

    # ------------------------------------------------------------------
    # Subscribers
    # ------------------------------------------------------------------

    def subscribe(self, fn: Any) -> Callable[[], None]:
        """Register a synchronous subscriber; returns an unsubscribe callable.

        ``fn`` may be a plain callable or an object exposing ``on_event`` (the
        ``ActivitySubscriber`` protocol). Subscribers must be fast and must not
        block — anything slow belongs in a queue the subscriber owns.
        """

        cb = fn
        if not callable(cb):
            cb = getattr(fn, "on_event", None)
            if not callable(cb):
                raise TypeError("subscriber must be callable or expose on_event()")
        sub = _Subscription(fn=cb)
        self._subs.append(sub)

        def _unsubscribe() -> None:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass  # already detached, by us or by the error threshold

        return _unsubscribe

    def unsubscribe(self, fn: Any) -> bool:
        """Remove a previously registered subscriber. True if one was removed."""

        target = fn if callable(fn) else getattr(fn, "on_event", None)
        for sub in list(self._subs):
            # ``==`` rather than ``is``: ``obj.on_event`` builds a fresh bound
            # method on every attribute access, and two bound methods of the
            # same instance compare equal but are never identical.
            if sub.fn is target or sub.fn == target:
                self._subs.remove(sub)
                return True
        return False

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def _fire(self, ev: ActivityEvent) -> None:
        """Fan out one applied event.

        Errors are swallowed exactly as ``JobManager._fire_on_event`` swallows
        them — a broken notifier must never affect the work it describes — and
        additionally counted, so a subscriber that keeps failing gets detached
        instead of being called forever.
        """

        for sub in list(self._subs):
            try:
                res = sub.fn(ev)
                if inspect.isawaitable(res):
                    # Subscribers are synchronous by contract. Close the stray
                    # coroutine so it cannot resurface as a "never awaited"
                    # warning somewhere unrelated, and count it as a failure so
                    # the offender is eventually detached.
                    close = getattr(res, "close", None)
                    if close is not None:
                        close()
                    raise TypeError("activity subscribers must be synchronous")
            except Exception:  # noqa: BLE001 — see docstring
                sub.errors += 1
                self.stats["subscriber_errors"] += 1
                if sub.errors >= self.config.subscriber_error_threshold:
                    self.stats["subscriber_detached"] += 1
                    try:
                        self._subs.remove(sub)
                    except ValueError:
                        pass

    # ------------------------------------------------------------------
    # Emission helpers
    # ------------------------------------------------------------------
    # Thin constructors, so a caller never has to know that ``seq`` and ``ts``
    # are registry-assigned. ``activity/emit.py`` will wrap these with the
    # ``reg is None`` guard that keeps the engine diffs to one line each.

    def create_node(
        self,
        kind: str,
        local: Any,
        *,
        title: str = "",
        parent_id: str | None = None,
        detail: str = "",
        source: str = "model",
        model: str = "",
        provider: str = "",
        isolation: str = "none",
        effort: str = "",
        permission_mode: str = "",
        transcript_ref: str | None = None,
        actions: Iterable[str] = (),
        ts: float | None = None,
    ) -> str:
        """Create a node from an engine-local id. Returns the namespaced id.

        The id comes back even when a bound rejected the creation; call
        :meth:`node` if you need to know whether it landed. An unknown kind or a
        local part that normalizes to nothing yields ``""`` — the one id that
        can never name a node — because an emitter calling this from inside a
        ``finally`` must not be handed an exception for a bad label.
        """

        try:
            node_id = make_id(kind, local)
        except InvalidIdError:
            # ``make_id`` is being made total elsewhere; until every path
            # through it is, an unusable id must still leave as a counter.
            node_id = ""
        if not isinstance(node_id, str):
            node_id = ""
        self.apply(
            NodeCreated(
                seq=0,
                ts=ts or 0.0,
                node_id=node_id,
                parent_id=parent_id,
                kind=kind,
                title=title,
                detail=detail,
                source=source,
                model=model,
                provider=provider,
                isolation=isolation,
                effort=effort,
                permission_mode=permission_mode,
                transcript_ref=transcript_ref,
                actions=tuple(actions),
            )
        )
        return node_id

    def set_status(
        self,
        node_id: str,
        status: str,
        *,
        error: str | None = None,
        ts: float | None = None,
    ) -> ActivityEvent | None:
        """Record a node's own status (already in the unified vocabulary)."""

        return self.apply(
            NodeStatus(seq=0, ts=ts or 0.0, node_id=node_id, status=status, error=error)
        )

    def add_activity(
        self, node_id: str, text: str, *, ts: float | None = None
    ) -> ActivityEvent | None:
        """Record one "what it is doing right now" line."""

        return self.apply(NodeActivity(seq=0, ts=ts or 0.0, node_id=node_id, text=text))

    def add_usage(
        self,
        node_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cost_usd: float = 0.0,
        ts: float | None = None,
    ) -> ActivityEvent | None:
        """Add a usage *delta* to a node."""

        return self.apply(
            NodeUsage(
                seq=0,
                ts=ts or 0.0,
                node_id=node_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cost_usd=cost_usd,
            )
        )

    def record_action(
        self,
        node_id: str,
        action: str,
        actor: str,
        *,
        detail: str = "",
        ts: float | None = None,
    ) -> ActivityEvent | None:
        """Record that somebody asked this node to do something."""

        return self.apply(
            NodeAction(
                seq=0,
                ts=ts or 0.0,
                node_id=node_id,
                action=action,
                actor=actor,
                detail=detail,
            )
        )

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def apply(self, ev: Any) -> ActivityEvent | None:
        """Ingest one event.

        Returns the canonical (stamped, sanitized) event when it took effect, or
        ``None`` when it was buffered as an orphan, coalesced by the rate limit,
        or dropped by a bound — or when it was not a well-formed event at all.

        The argument is typed ``Any`` on purpose. ``apply`` is the registry's
        only ingest point and the module's promise is that nothing in here may
        raise into an engine, so a malformed envelope has to leave by the same
        door every other rejection uses: a counter, not an exception.
        """

        self.stats["events"] += 1
        if not self._valid_envelope(ev):
            self.stats["dropped_malformed"] += 1
            return None
        try:
            ev = self._normalize(ev)
            applied = self._dispatch(ev)
        except Exception:  # noqa: BLE001 — see docstring
            # The validator above rejects every malformed shape we know how to
            # name. This is the backstop for the ones we do not: a registry
            # failure must never turn a completed job into a failed one.
            self.stats["dropped_malformed"] += 1
            return None
        if applied is not None:
            self.stats["applied"] += 1
            self._fire(applied)
            if isinstance(applied, NodeCreated):
                # Only now, after the creation has been announced: a subscriber
                # must never see a node's activity before the node itself.
                node = self.nodes.get(applied.node_id)
                if node is not None:
                    self._attach_orphans(node)
        return applied

    def _valid_envelope(self, ev: Any) -> bool:
        """§8's envelope check: is this an event this registry can apply?

        Only the fields the registry does *arithmetic* or *lookups* on are
        checked. Everything model-authored (titles, activity lines, actions)
        goes through :func:`sanitize_text`, which already accepts anything —
        rejecting a node because its title was an ``int`` would lose real work
        over a cosmetic problem.
        """

        if not isinstance(ev, _EVENT_TYPES):
            return False
        # ``seq`` is compared and ``ts`` is arithmetic, in the orphan sweep and
        # in every elapsed-time calculation downstream. A bool is an int and is
        # harmless here; a string or a NaN is not.
        if not isinstance(ev.seq, int) or isinstance(ev.seq, bool):
            return False
        if not isinstance(ev.ts, (int, float)) or isinstance(ev.ts, bool):
            return False
        if not math.isfinite(float(ev.ts)):
            return False
        # The id is a dict key, and an empty one would make a node that no
        # engine can ever address again.
        if not isinstance(ev.node_id, str) or not ev.node_id:
            return False
        if isinstance(ev, NodeCreated):
            if not isinstance(ev.kind, str):  # looked up in the rollup tables
                return False
            if ev.parent_id is not None and not isinstance(ev.parent_id, str):
                return False
            if not isinstance(ev.actions, (list, tuple)):
                return False
        elif isinstance(ev, NodeUsage):
            # Deltas are accumulated; a non-number would poison the running
            # total for the whole session, not just this event.
            for value in (
                ev.input_tokens,
                ev.output_tokens,
                ev.cache_read_tokens,
                ev.cost_usd,
            ):
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    return False
                if not math.isfinite(float(value)):
                    return False
        return True

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _normalize(self, ev: ActivityEvent) -> ActivityEvent:
        """Stamp ``seq``/``ts`` and sanitize every model-authored string."""

        changes: dict[str, Any] = {}
        if ev.seq > 0:
            # A replayed event carries its own ordering. Keep it, but never let
            # the live counter fall behind it.
            self._seq = max(self._seq, ev.seq)
        else:
            changes["seq"] = self._next_seq()
        if ev.ts <= 0:
            changes["ts"] = self.now()

        if isinstance(ev, NodeCreated):
            changes["title"] = sanitize_text(ev.title, TITLE_MAX)
            changes["detail"] = sanitize_text(ev.detail, DETAIL_MAX)
            changes["actions"] = tuple(
                a for a in (sanitize_text(x, _TOKEN_MAX) for x in ev.actions) if a
            )
        elif isinstance(ev, NodeStatus):
            mapped = statuses.normalize(ev.status)
            if mapped != ev.status:
                # An unknown status means "we do not know"; ``normalize`` fails
                # toward pending so a parent's rollup cannot claim completion.
                self.stats["unknown_status"] += 1
                changes["status"] = mapped
            if ev.error is not None:
                changes["error"] = sanitize_text(ev.error, ERROR_MAX)
        elif isinstance(ev, NodeActivity):
            changes["text"] = sanitize_text(ev.text, ACTIVITY_MAX)
        elif isinstance(ev, NodeUsage):
            # Deltas accumulate; a negative one would corrupt the running total.
            changes["input_tokens"] = max(0, ev.input_tokens)
            changes["output_tokens"] = max(0, ev.output_tokens)
            changes["cache_read_tokens"] = max(0, ev.cache_read_tokens)
            changes["cost_usd"] = max(0.0, ev.cost_usd)
        elif isinstance(ev, NodeAction):
            changes["action"] = sanitize_text(ev.action, _TOKEN_MAX)
            changes["actor"] = sanitize_text(ev.actor, _TOKEN_MAX)
            changes["detail"] = sanitize_text(ev.detail, DETAIL_MAX)

        return msgspec.structs.replace(ev, **changes) if changes else ev

    def _dispatch(self, ev: ActivityEvent) -> ActivityEvent | None:
        """Apply a normalized event to the tree. No fan-out, no re-stamping."""

        if isinstance(ev, NodeCreated):
            return self._on_created(ev)
        node = self.nodes.get(ev.node_id)
        if node is None:
            # The node has not been announced yet. Engines emit progress
            # eagerly, so this is expected rather than exceptional.
            return self._buffer_orphan(ev)
        if isinstance(ev, NodeStatus):
            return self._on_status(node, ev)
        if isinstance(ev, NodeActivity):
            return self._on_activity(node, ev)
        if isinstance(ev, NodeUsage):
            return self._on_usage(node, ev)
        if isinstance(ev, NodeAction):
            # An action mutates nothing — the resulting NodeStatus does. Applying
            # one is purely "record it, and tell the subscribers".
            return ev
        return None

    # ------------------------------------------------------------------
    # Per-event handlers
    # ------------------------------------------------------------------

    def _on_created(self, ev: NodeCreated) -> ActivityEvent | None:
        if ev.node_id in self.nodes:
            # Idempotent by construction: an engine that re-announces a node is
            # retrying, not erring. Keep the original.
            self.stats["dropped_duplicate"] += 1
            return None

        if len(self.nodes) >= self.config.max_live_nodes:
            # Make room by discarding the oldest fully-terminal subtrees, then
            # give up rather than exceed the budget.
            self._evict_terminal(1)
            if len(self.nodes) >= self.config.max_live_nodes:
                self.stats["dropped_node_limit"] += 1
                return None

        # An empty parent id is not a link, it is a missing one — and keying
        # ``_children`` on "" would collide every such node into one bucket.
        parent_id = ev.parent_id or None
        if parent_id is not None and self._would_cycle(ev.node_id, parent_id):
            self.stats["rejected_parent"] += 1
            parent_id = None

        node = ActivityNode(
            id=ev.node_id,
            parent_id=parent_id,
            kind=ev.kind,
            title=ev.title,
            detail=ev.detail,
            created_at=ev.ts,
            model=ev.model,
            provider=ev.provider,
            effort=ev.effort,
            transcript_ref=ev.transcript_ref,
            permission_mode=ev.permission_mode,
            isolation=ev.isolation,
            source=ev.source,
            actions=frozenset(ev.actions),
            created_seq=ev.seq,
        )
        self.nodes[node.id] = node
        self._counts[node.status] += 1
        if parent_id is not None:
            # Link even when the parent does not exist yet: engines discover
            # structure out of order, and the link is what lets a late parent
            # inherit its children for free.
            self._children.setdefault(parent_id, []).append(node.id)
        # A container announced after its children already has a rollup.
        if self._children.get(node.id):
            self._recompute(node)
        if parent_id is not None:
            self._roll_up_from(parent_id)
        self._prune_parent_links()
        # Buffered events for this node are replayed by ``apply`` once the
        # creation itself has been fanned out.
        return ev

    def _on_status(self, node: ActivityNode, ev: NodeStatus) -> ActivityEvent | None:
        node.intrinsic_status = ev.status
        if ev.status == statuses.RUNNING and node.started_at is None:
            node.started_at = ev.ts
        if statuses.is_terminal(ev.status):
            if node.ended_at is None:
                node.ended_at = ev.ts
        else:
            # A retried agent goes back to pending — it has no end any more.
            node.ended_at = None
        if ev.error is not None:
            node.error = ev.error
        self._recompute(node)
        self._roll_up_from(node.parent_id)
        self._enforce_retention()
        return ev

    def _on_activity(self, node: ActivityNode, ev: NodeActivity) -> ActivityEvent | None:
        node.activity = ev.text
        if self._rate_limited(node.id):
            # Over budget: the freshest line still wins (the rail stays live),
            # but the ring and every subscriber are spared the flood.
            self.stats["coalesced"] += 1
            return None
        cap = self.config.max_recent_per_node
        if cap > 0 and ev.text:
            node.recent = (node.recent + (ev.text,))[-cap:]
        return ev

    def _on_usage(self, node: ActivityNode, ev: NodeUsage) -> ActivityEvent | None:
        cur = node.usage or Usage()
        node.usage = Usage(
            input_tokens=cur.input_tokens + ev.input_tokens,
            output_tokens=cur.output_tokens + ev.output_tokens,
            cache_creation_input_tokens=cur.cache_creation_input_tokens,
            cache_read_input_tokens=cur.cache_read_input_tokens + ev.cache_read_tokens,
        )
        if ev.cost_usd:
            node.cost_usd = (node.cost_usd or 0.0) + ev.cost_usd
        return ev

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _rate_limited(self, node_id: str) -> bool:
        """Fixed one-second window per node.

        Cheap and deliberately unsophisticated: the point is to cap a flood, not
        to be a precise traffic shaper.
        """

        budget = self.config.max_events_per_second_per_node
        if budget <= 0:
            return False
        now = self._monotonic()
        window = self._rate.get(node_id)
        if window is None or (now - window[0]) >= 1.0:
            self._rate[node_id] = [now, 1]
            return False
        window[1] += 1
        return window[1] > budget

    # ------------------------------------------------------------------
    # Orphans
    # ------------------------------------------------------------------

    def _buffer_orphan(self, ev: ActivityEvent) -> ActivityEvent | None:
        """Hold an event whose node has not been announced yet.

        Always returns ``None``: a buffered event has not taken effect, so it
        must not be fanned out. It is replayed — and fanned out then — if and
        when its ``NodeCreated`` arrives.
        """

        self._sweep_orphans(ev.ts)
        bucket = self._orphans.get(ev.node_id)
        if bucket is None:
            bucket = []
            self._orphans[ev.node_id] = bucket
        bucket.append(ev)
        self._orphan_count += 1
        self.stats["orphans_buffered"] += 1
        while self._orphan_count > self.config.orphan_buffer_size:
            # Drop from the oldest group first: the buffer is a bet that a node
            # is about to appear, and the oldest bet is the worst one.
            oldest_id = next(iter(self._orphans))
            oldest = self._orphans[oldest_id]
            oldest.pop(0)
            self._orphan_count -= 1
            self.stats["dropped_orphan"] += 1
            if not oldest:
                del self._orphans[oldest_id]
        return None

    def _sweep_orphans(self, now: float) -> None:
        """Discard orphans past their TTL, with a counted diagnostic."""

        ttl = self.config.orphan_ttl_s
        if ttl <= 0:
            return
        cutoff = now - ttl
        for node_id in list(self._orphans):
            bucket = self._orphans[node_id]
            keep = [e for e in bucket if e.ts >= cutoff]
            dropped = len(bucket) - len(keep)
            if not dropped:
                continue
            self._orphan_count -= dropped
            self.stats["dropped_orphan"] += dropped
            if keep:
                self._orphans[node_id] = keep
            else:
                del self._orphans[node_id]

    def _attach_orphans(self, node: ActivityNode) -> None:
        """Replay the events that arrived before ``node`` existed."""

        bucket = self._orphans.pop(node.id, None)
        if not bucket:
            return
        self._orphan_count -= len(bucket)
        for ev in sorted(bucket, key=lambda e: e.seq):
            self.stats["orphans_attached"] += 1
            applied = self._dispatch(ev)
            if applied is not None:
                self.stats["applied"] += 1
                self._fire(applied)

    # ------------------------------------------------------------------
    # Structure
    # ------------------------------------------------------------------

    def _would_cycle(self, node_id: str, parent_id: str) -> bool:
        """True if parenting ``node_id`` under ``parent_id`` closes a loop."""

        if parent_id == node_id:
            return True
        seen: set[str] = set()
        cur: str | None = parent_id
        # Bounded walk: a corrupt link must not spin here.
        for _ in range(len(self.nodes) + 1):
            if cur is None or cur in seen:
                return False
            if cur == node_id:
                return True
            seen.add(cur)
            parent = self.nodes.get(cur)
            cur = parent.parent_id if parent is not None else None
        return False

    def _child_statuses(self, node_id: str) -> list[str]:
        out: list[str] = []
        for cid in self._children.get(node_id, ()):
            child = self.nodes.get(cid)
            if child is not None:
                out.append(child.status)
        return out

    def _recompute(self, node: ActivityNode) -> bool:
        """Recompute one node's displayed status. True when it changed."""

        new = statuses.resolve_status(
            node.kind, node.intrinsic_status, self._child_statuses(node.id)
        )
        if new == node.status:
            return False
        self._counts[node.status] -= 1
        self._counts[new] += 1
        node.status = new
        return True

    def _roll_up_from(self, node_id: str | None) -> None:
        """Recompute the ancestor chain above a node that just changed.

        Stops at the first ancestor whose displayed status did not move: if it
        did not change, nothing above it can have changed either.
        """

        seen: set[str] = set()
        cur = node_id
        while cur is not None and cur not in seen:
            seen.add(cur)
            node = self.nodes.get(cur)
            if node is None:
                return
            if not self._recompute(node):
                return
            cur = node.parent_id

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    def _prune_parent_links(self) -> None:
        """Keep ``_children`` bounded by the live node count.

        A child may name a parent that has not been announced yet — that is an
        explicitly supported case, and the pending link is what lets a late
        parent inherit its children for free. But the bet that a parent is
        coming is the same bet the orphan buffer makes about a node, so it gets
        the same bound rather than a new one: at most ``orphan_buffer_size``
        parents-in-waiting, oldest bet dropped first.

        Without this, a stream of nodes whose parents never arrive grew the map
        forever — measured at ``max_live_nodes=50``: 5 live nodes, 4000 entries
        in ``_children`` — which contradicts the whole "memory is provably
        capped by the node and event budgets" claim.

        Dropping a link is not dropping a node: the child keeps its
        ``parent_id``, so :meth:`roots` and :meth:`tree` still reach it. All it
        forfeits is the chance to be re-adopted if that parent shows up.
        """

        limit = max(0, self.config.orphan_buffer_size)
        # Every key is either a live node or a parent-in-waiting, so this bound
        # is the whole invariant — and checking it is O(1), which is why the
        # scan below only runs when it is actually violated.
        if len(self._children) <= len(self.nodes) + limit:
            return
        dangling: list[str] = []
        for pid, kids in list(self._children.items()):
            if not kids:
                del self._children[pid]
            elif pid not in self.nodes:
                dangling.append(pid)
        # Insertion order is arrival order: a dict key appears when the first
        # child claims that parent, which is the moment the bet was placed.
        for pid in dangling[: max(0, len(dangling) - limit)]:
            del self._children[pid]
            self.stats["dropped_parent_link"] += 1

    def _has_live_descendant(self, node_id: str) -> bool:
        stack = list(self._children.get(node_id, ()))
        seen: set[str] = set()
        while stack:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            child = self.nodes.get(cid)
            if child is None:
                continue
            if not statuses.is_terminal(child.status):
                return True
            stack.extend(self._children.get(cid, ()))
        return False

    def _remove_subtree(self, node_id: str) -> int:
        """Delete a node and everything under it. Returns how many went."""

        order: list[str] = []
        stack = [node_id]
        seen: set[str] = set()
        while stack:
            nid = stack.pop()
            if nid in seen or nid not in self.nodes:
                continue
            seen.add(nid)
            order.append(nid)
            stack.extend(self._children.get(nid, ()))
        for nid in order:
            node = self.nodes.pop(nid, None)
            if node is None:
                continue
            self._counts[node.status] -= 1
            self._children.pop(nid, None)
            self._rate.pop(nid, None)
            bucket = self._orphans.pop(nid, None)
            if bucket:
                self._orphan_count -= len(bucket)
            if node.parent_id:
                siblings = self._children.get(node.parent_id)
                if siblings is not None:
                    if nid in siblings:
                        siblings.remove(nid)
                    if not siblings:
                        # An emptied list is not free: leaving it behind is how
                        # ``_children`` outgrew the node budget it is supposed
                        # to be capped by.
                        self._children.pop(node.parent_id, None)
        self.stats["evicted"] += len(order)
        return len(order)

    def _evict_terminal(self, needed: int) -> int:
        """Discard up to ``needed`` nodes, oldest fully-terminal subtree first.

        A subtree is evictable only when *nothing* in it is still live: a
        finished parent with a running child is the shape of a phase that has
        handed off, and dropping it would orphan live work.
        """

        if needed <= 0:
            return 0

        # Order matters, and getting it wrong is catastrophic rather than
        # untidy. Walking ``self.nodes`` in creation order put the SESSION ROOT
        # first: it is a rollup kind, so once its children are all terminal it is
        # terminal too and has no live descendant — and `_remove_subtree` then
        # took the entire session in one call (measured: 201 nodes -> 0, with
        # every later node parented to an id that no longer existed).
        #
        # Two rules fix it. Never evict a root: it *is* the tree, and dropping it
        # orphans everything under it. And peel the DEEPEST nodes first, so a
        # terminal leaf goes before its parent — that is what actually keeps "the
        # most recent results stay inspectable" true, instead of trimming whole
        # branches from the top.
        depth: dict[str, int] = {}

        def _depth(nid: str) -> int:
            if nid in depth:
                return depth[nid]
            seen: set[str] = set()
            cur, d = nid, 0
            while True:
                node = self.nodes.get(cur)
                if node is None or node.parent_id is None or cur in seen:
                    break
                seen.add(cur)
                cur = node.parent_id
                d += 1
                if cur in depth:
                    d += depth[cur]
                    break
            depth[nid] = d
            return d

        # Insertion order is the age ordering — ``nodes`` is a dict and every
        # node is inserted once, on creation. Using it rather than ``created_at``
        # keeps the sort total and deterministic even when several nodes share a
        # normalized timestamp.
        order = {nid: i for i, nid in enumerate(self.nodes)}
        candidates = [
            n for n in self.nodes.values()
            # The SESSION node specifically, not every parentless node: plenty of
            # nodes are legitimately roots (an engine that emits before a session
            # node exists), and those must still age out normally. It is the
            # session — the thing every later node hangs off — that must outlive
            # its own children.
            if n.kind != "session" and n.id != self.session_id
            and statuses.is_terminal(n.status)
            and not self._has_live_descendant(n.id)
        ]
        # Deepest first, then oldest first within a depth.
        candidates.sort(key=lambda n: (-_depth(n.id), order[n.id]))

        removed = 0
        for node in candidates:
            if removed >= needed:
                break
            if node.id not in self.nodes:  # already taken with an earlier subtree
                continue
            removed += self._remove_subtree(node.id)
        if removed:
            # Eviction turns live parents into vanished ones, so the links that
            # named them are pending bets again and fall under the same bound.
            self._prune_parent_links()
        return removed

    def _enforce_retention(self) -> None:
        """Keep at most ``terminal_node_retention`` finished nodes.

        The generalization of ``JobManager._prune``: running work is never
        evicted and the most recent results stay inspectable.
        """

        cap = self.config.terminal_node_retention
        if cap < 0:
            return
        terminal = sum(1 for n in self.nodes.values() if statuses.is_terminal(n.status))
        excess = terminal - cap
        if excess > 0:
            self._evict_terminal(excess)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def node(self, node_id: str) -> ActivityNode | None:
        return self.nodes.get(node_id)

    def children(self, node_id: str) -> list[ActivityNode]:
        return [self.nodes[c] for c in self._children.get(node_id, ()) if c in self.nodes]

    def descendants(self, node_id: str) -> list[ActivityNode]:
        """Every node below ``node_id``, depth-first, excluding itself."""

        out: list[ActivityNode] = []
        seen = {node_id}
        stack = list(reversed(self._children.get(node_id, ())))
        while stack:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            child = self.nodes.get(cid)
            if child is None:
                continue
            out.append(child)
            stack.extend(reversed(self._children.get(cid, ())))
        return out

    def ancestors(self, node_id: str) -> list[ActivityNode]:
        """The parent chain, nearest first."""

        out: list[ActivityNode] = []
        seen = {node_id}
        node = self.nodes.get(node_id)
        while node is not None and node.parent_id is not None:
            if node.parent_id in seen:
                break
            seen.add(node.parent_id)
            parent = self.nodes.get(node.parent_id)
            if parent is None:
                break
            out.append(parent)
            node = parent
        return out

    def roots(self) -> list[ActivityNode]:
        """Nodes with no parent — including those whose parent never arrived,
        because a dangling link must still be reachable from somewhere."""

        return [
            n
            for n in self.nodes.values()
            if n.parent_id is None or n.parent_id not in self.nodes
        ]

    def tree(self, root_id: str | None = None) -> list[tuple[int, ActivityNode]]:
        """Depth-first ``(depth, node)`` pairs — the shape a renderer wants.

        Iterative, like :meth:`descendants` and ``_remove_subtree``, and for the
        same reason: nesting is bounded by ``max_live_nodes`` (2000 by default),
        not by anything smaller, so a legal tree can be far deeper than CPython's
        ~1000-frame recursion limit. A recursive walk here blew the stack at a
        depth of about 997 — well inside the node budget — and this is the
        renderer's entry point, so that ``RecursionError`` landed in the middle
        of drawing a frame.
        """

        out: list[tuple[int, ActivityNode]] = []
        if root_id is not None:
            start = self.nodes.get(root_id)
            if start is None:
                return out
            roots = [start]
        else:
            roots = self.roots()
        seen: set[str] = set()
        for r in roots:
            # Children are pushed in reverse so they pop in declaration order —
            # the traversal order the recursive version produced.
            stack: list[tuple[ActivityNode, int]] = [(r, 0)]
            while stack:
                node, depth = stack.pop()
                if node.id in seen:
                    continue
                seen.add(node.id)
                out.append((depth, node))
                for cid in reversed(self._children.get(node.id, ())):
                    child = self.nodes.get(cid)
                    if child is not None:
                        stack.append((child, depth + 1))
        return out

    def filter(self, pred: Callable[[ActivityNode], bool]) -> list[ActivityNode]:
        """Nodes matching ``pred``, in creation order."""

        return [n for n in self.nodes.values() if pred(n)]

    def counts(self) -> dict[str, int]:
        """Live node counts by status, maintained incrementally.

        Every status key is present (zeros included) so the rail can format
        without branching on absence, and so the call costs nothing per frame
        instead of walking the tree.
        """

        return dict(self._counts)

    def active(self) -> list[ActivityNode]:
        """Every node that is not in a terminal state."""

        return [n for n in self.nodes.values() if statuses.is_active(n.status)]

    def __len__(self) -> int:
        return len(self.nodes)

    def __contains__(self, node_id: object) -> bool:
        return node_id in self.nodes
