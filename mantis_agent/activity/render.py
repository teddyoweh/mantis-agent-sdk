"""Rail and cockpit rendering — the one-line "what is running" strip that sits
under the input, and the drill-down panes behind it.

Split out of the TUI on purpose. Every function here is pure: it takes a list of
:class:`RailItem` and returns a string, so the exact bytes can be asserted in a
test without standing up a prompt_toolkit application — the same reason
``_footer_line`` was split out of ``footer_ft``, and the same reason
``workflow_view`` holds the workflow render helpers.

:class:`RailItem` is deliberately a flat normalized shape rather than a ``Job``
or an ``ActivityNode``. Both engines can project into it, which is what lets the
rail show a workflow run, a background shell job and a watch side by side today
— while the activity registry is wired up underneath later — without this module
learning about either.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..workflow_view import format_duration, format_number, status_glyph
from .status import RUNNING, is_terminal

__all__ = [
    "RailItem",
    "footer_counts",
    "monitor_detail",
    "rail_line",
    "rail_rows",
]


# Plural labels for the footer roll-up. A watch is called a *monitor* in the UI
# because that is what it does from the user's side — the tool is named ``watch``
# and the job kind is ``watch``, but "3 watches" reads as a verb.
_KIND_LABEL = {
    "watch": ("monitor", "monitors"),
    "workflow": ("workflow", "workflows"),
    "task": ("agent", "agents"),
    "subagent": ("agent", "agents"),
    "agent": ("agent", "agents"),
    "shell": ("command", "commands"),
    "job": ("job", "jobs"),
}

# Order the footer lists kinds in. Monitors first: a monitor is the thing most
# likely to be silently still running when the user thinks they are done.
_KIND_ORDER = ("watch", "workflow", "task", "subagent", "agent", "shell", "job")


@dataclass
class RailItem:
    """One row of live work, normalized across engines.

    ``progress`` is a free-form already-formatted fragment (``"10/20 agents
    done"``, ``"2/5 phases"``) rather than a ratio, because what counts as
    progress differs per engine and the rail should not pretend otherwise.
    """

    id: str
    kind: str
    label: str
    detail: str = ""
    status: str = RUNNING
    elapsed_s: float = 0.0
    progress: str = ""
    tokens: int = 0
    script: str = ""
    output: str = ""
    events: int = 0
    actions: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_live(self) -> bool:
        return not is_terminal(self.status)


def rail_item_from_job(job: object) -> RailItem:
    """Project a :class:`~mantis_agent.jobs.Job` into a rail row.

    Duck-typed on purpose — reading attributes rather than importing ``Job``
    keeps this module free of engine imports, so a workflow run, an activity
    node or a future durable job record can be projected the same way without
    this file growing a dependency on each of them.
    """
    get = lambda n, d=None: getattr(job, n, d)  # noqa: E731 — terse by intent
    kind = str(get("kind", "job") or "job")
    events = int(get("stream_count", 0) or 0)
    return RailItem(
        id=f"job:{get('id', '?')}",
        kind=kind,
        label=str(get("desc", "") or ""),
        detail=str(get("last_event", "") or ""),
        status=str(get("status", RUNNING) or RUNNING),
        elapsed_s=float(get("elapsed_s", 0.0) or 0.0),
        tokens=0,
        script=str(get("script", "") or ""),
        output=str(get("result", "") or ""),
        events=events,
        actions=("stop", "open") if kind == "watch" else ("stop",),
    )


def rail_items_from_jobs(jobs: object, *, live_only: bool = True) -> list[RailItem]:
    """Every job the manager knows about, newest first, as rail rows."""
    listing = jobs.all() if hasattr(jobs, "all") else list(jobs)  # type: ignore[union-attr]
    items = [rail_item_from_job(j) for j in listing]
    if live_only:
        items = [i for i in items if i.is_live]
    # Monitors sort above one-shot work: a still-running monitor is the thing a
    # user most often forgets about, so it should never be the row that gets
    # pushed under a `+N more`.
    items.sort(key=lambda i: (i.kind != "watch", -i.elapsed_s))
    return items


def _label_for(kind: str, n: int) -> str:
    one, many = _KIND_LABEL.get(kind, (kind, kind + "s"))
    return one if n == 1 else many


def footer_counts(items: list[RailItem], *, live_only: bool = True) -> str:
    """``"1 monitor · 3 agents"`` — the roll-up that sits in the status line.

    Empty string when nothing is running, so the footer stays clean in an
    ordinary chat session that has never spawned anything.
    """
    pool = [i for i in items if i.is_live] if live_only else list(items)
    if not pool:
        return ""
    counts: dict[str, int] = {}
    for item in pool:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    parts = [
        f"{counts[k]} {_label_for(k, counts[k])}"
        for k in _KIND_ORDER
        if k in counts
    ]
    # Any kind we do not have an explicit order for still gets shown.
    parts += [
        f"{n} {_label_for(k, n)}"
        for k, n in sorted(counts.items())
        if k not in _KIND_ORDER
    ]
    return " · ".join(parts)


def _clip(text: str, width: int) -> str:
    if width <= 1 or len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


def rail_line(
    item: RailItem,
    *,
    width: int = 80,
    color: bool = False,
    frame: int = 0,
) -> str:
    """One rail row: ``○ name  detail  10/20 agents done · 45m 51s · ↓ 245.2k``.

    The right-hand facts are the ones that answer "should I wait for this?", so
    they are never the part that gets clipped — the label and detail absorb the
    truncation instead.
    """
    glyph = status_glyph(item.status, frame=frame, color=color)
    facts: list[str] = []
    if item.progress:
        facts.append(item.progress)
    if item.elapsed_s:
        facts.append(format_duration(item.elapsed_s * 1000.0))
    if item.tokens:
        facts.append(f"↓ {format_number(item.tokens)} tokens")
    if item.events:
        facts.append(f"{item.events} events")
    right = " · ".join(facts)

    head = f"{glyph} {item.label}"
    body = f"  {item.detail}" if item.detail else ""
    # Budget: everything except the facts may shrink; the facts are load-bearing.
    room = max(8, width - len(right) - 3)
    left = _clip(head + body, room)
    if not right:
        return left
    pad = max(1, width - len(left) - len(right))
    return f"{left}{' ' * pad}{right}"


def rail_rows(
    items: list[RailItem],
    *,
    width: int = 80,
    sel: int = -1,
    color: bool = False,
    frame: int = 0,
    limit: int = 0,
) -> list[str]:
    """The rail as a list of rows, with an optional selection marker.

    ``limit`` caps how many rows are drawn and appends a ``+N more`` line, so a
    session with forty live agents does not push the conversation off screen.
    """
    rows: list[str] = []
    shown = items if limit <= 0 else items[:limit]
    for i, item in enumerate(shown):
        marker = "❯ " if i == sel else "  "
        rows.append(marker + rail_line(item, width=width - 2, color=color, frame=frame))
    hidden = len(items) - len(shown)
    if hidden > 0:
        rows.append(f"  +{hidden} more")
    return rows


def monitor_detail(
    item: RailItem,
    *,
    width: int = 80,
    max_script_lines: int = 12,
    max_output_lines: int = 12,
) -> str:
    """The drill-down pane for one monitor/watch.

    Shows the script verbatim. A monitor is usually something the *model* wrote
    — a poll loop it composed on the fly — so "what is this thing actually
    running on my machine" is the first question a user has, and paraphrasing it
    would defeat the point.
    """
    # Title by the USER-facing name, not the internal kind: the tool and the job
    # kind are both ``watch``, but the pane is what a user opens to ask "what is
    # this monitor doing", and "Watch details" reads as an imperative.
    title = _label_for(item.kind, 1).capitalize()
    lines = [f"{title} details", ""]
    lines.append(f"Status:   {item.status}")
    if item.elapsed_s:
        lines.append(f"Runtime:  {format_duration(item.elapsed_s * 1000.0)}")
    if item.detail:
        lines.append(f"Detail:   {_clip(item.detail, max(10, width - 10))}")
    if item.events:
        lines.append(f"Events:   {item.events}")
    if item.tokens:
        lines.append(f"Tokens:   {format_number(item.tokens)}")

    if item.script:
        script = item.script.strip().splitlines()
        # The gutter is 10 columns ("Script:   " / the continuation indent), so
        # the script itself gets whatever is left. Clipping per line keeps a long
        # one-liner — which is what a model-written poll loop usually is — from
        # wrapping and destroying the alignment of everything below it.
        room = max(20, width - 10)
        lines.append("Script:   " + _clip(script[0] if script else "", room))
        for extra in script[1 : max_script_lines + 1]:
            lines.append("          " + _clip(extra, room))
        if len(script) > max_script_lines + 1:
            lines.append(f"          … +{len(script) - max_script_lines - 1} more lines")

    lines.append("")
    lines.append("Output:")
    out = item.output.strip()
    if not out:
        lines.append("No output available")
    else:
        tail = out.splitlines()[-max_output_lines:]
        lines.extend(tail)
    return "\n".join(lines)
