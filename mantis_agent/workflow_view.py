"""Shared, dependency-light render helpers for the workflow engine.

These functions are pure: they take plain data (an :class:`AgentRun`-shaped
object, numbers, strings) and return plain ``str`` (optionally ANSI-colored).
They import NOTHING from ``prompt_toolkit`` so both the fullscreen TUI and the
classic REPL can call them, and so they stay trivially unit-testable.

The row/rail models mirror Claude Code's ``CoordinatorAgentStatus`` layout:

    {caret}{viewed} {label}: {summary} {run/pause} {duration} · {arrow}{tok} tok · {model}

Glyphs follow ``constants/figures.ts``:
    ◇ running/queued · ◆ done/awaiting · ✓ success · ✗ error
    ❯ selection caret · ● viewed · ◦ unviewed · ▶ running · ⏸ paused
"""

from __future__ import annotations

from typing import Any

# ANSI palette — matches the constants at the top of tui_fullscreen.py so rows
# rendered here drop straight into the fullscreen overlay without a reskin.
_GREEN = "\033[38;5;113m"
_DIM = "\033[38;5;240m"
_GREY = "\033[90m"
_RED = "\033[38;5;203m"
_YELLOW = "\033[38;5;179m"
_RESET = "\033[0m"
_SELECT = "\033[30;48;5;113m"  # black-on-green selected-row highlight

# Status glyphs.
GLYPH_QUEUED = "◇"
GLYPH_RUNNING = "◇"
GLYPH_DONE = "✓"
GLYPH_ERROR = "✗"
GLYPH_PAUSED = "⏸"
GLYPH_CANCELLED = "✗"

# Spinner frames for the running state (braille dots, same family the TUI uses).
_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

# Row decoration glyphs.
_CARET = "❯"
_VIEWED = "●"
_UNVIEWED = "◦"
_RUN_MARK = "▶"
_PAUSE_MARK = "⏸"


def status_glyph(status: str, *, frame: int = 0, color: bool = False) -> str:
    """Glyph for a run status.

    ``running`` returns a spinner frame (advance ``frame`` on each redraw tick);
    everything else is a static glyph. Pass ``color=True`` to wrap in ANSI.
    """

    status = (status or "").lower()
    if status == "running":
        g = _SPINNER[frame % len(_SPINNER)]
        return f"{_GREEN}{g}{_RESET}" if color else g
    if status == "done":
        return f"{_GREEN}{GLYPH_DONE}{_RESET}" if color else GLYPH_DONE
    if status == "error":
        return f"{_RED}{GLYPH_ERROR}{_RESET}" if color else GLYPH_ERROR
    if status == "cancelled":
        return f"{_DIM}{GLYPH_CANCELLED}{_RESET}" if color else GLYPH_CANCELLED
    if status == "paused":
        return f"{_YELLOW}{GLYPH_PAUSED}{_RESET}" if color else GLYPH_PAUSED
    # queued / unknown
    return f"{_DIM}{GLYPH_QUEUED}{_RESET}" if color else GLYPH_QUEUED


def format_duration(ms: float) -> str:
    """Human duration from milliseconds.

    ``<1min`` → ``"Ns"``; otherwise ``"Xm Ys"``. Negative inputs clamp to 0.
    """

    total_s = int(max(0.0, ms) / 1000.0)
    if total_s < 60:
        return f"{total_s}s"
    return f"{total_s // 60}m {total_s % 60}s"


def format_number(n: float) -> str:
    """Compact token count: ``1321`` → ``"1.3k"``, ``2_500_000`` → ``"2.5m"``.

    Values under 1000 render as the plain integer.
    """

    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        s = f"{n / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{s}k"
    s = f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".")
    return f"{s}m"


# ---------------------------------------------------------------------------
# Token accounting
# ---------------------------------------------------------------------------


def accumulate_usage(prev: Any, usage: Any) -> Any:
    """Fold one turn's :class:`Usage` into a running :class:`ModelUsage`.

    Claude's token accounting rule (tasks/LocalAgentTask):

    * ``input_tokens`` is *cumulative per turn* (each turn re-sends the whole
      context) — so we keep the LATEST turn's input, cache-read and
      cache-creation counts, not a sum.
    * ``output_tokens`` is *per turn* — so we SUM it across turns.

    Returns a fresh :class:`ModelUsage` (the mantis frozen struct). ``prev`` may
    be ``None`` (treated as an empty accumulator).
    """

    from .types import ModelUsage  # noqa: PLC0415 — keep module import-light

    prev_out = getattr(prev, "outputTokens", 0) if prev is not None else 0
    prev_web = getattr(prev, "webSearchRequests", 0) if prev is not None else 0
    prev_cost = getattr(prev, "costUSD", 0.0) if prev is not None else 0.0
    return ModelUsage(
        inputTokens=int(getattr(usage, "input_tokens", 0)),
        outputTokens=int(prev_out + getattr(usage, "output_tokens", 0)),
        cacheReadInputTokens=int(getattr(usage, "cache_read_input_tokens", 0)),
        cacheCreationInputTokens=int(getattr(usage, "cache_creation_input_tokens", 0)),
        webSearchRequests=int(prev_web),
        costUSD=float(prev_cost),
    )


def total_tokens(usage: Any) -> int:
    """Effective total for display: latest_input (+cache) + cumulative_output.

    ``total = (inputTokens + cacheCreationInputTokens + cacheReadInputTokens)
    + outputTokens`` — the same figure Claude prints in the row's ``tok`` field.
    """

    if usage is None:
        return 0
    return int(
        getattr(usage, "inputTokens", 0)
        + getattr(usage, "cacheCreationInputTokens", 0)
        + getattr(usage, "cacheReadInputTokens", 0)
        + getattr(usage, "outputTokens", 0)
    )


# ---------------------------------------------------------------------------
# Row + rail rendering
# ---------------------------------------------------------------------------


def _elapsed_ms(agent_run: Any, now: float) -> float:
    """``now - started - total_paused_ms`` in ms, clamped at 0. All three inputs
    share the workflow clock's unit (ms)."""

    started = getattr(agent_run, "started", None)
    if started is None:
        return 0.0
    ended = getattr(agent_run, "ended", None)
    end = ended if ended is not None else now
    return max(0.0, end - started - getattr(agent_run, "total_paused_ms", 0.0))


def format_agent_row(
    agent_run: Any,
    *,
    selected: bool = False,
    viewed: bool = True,
    width: int = 80,
    now: float = 0.0,
    frame: int = 0,
    show_model: bool = False,
    color: bool = True,
) -> str:
    """Render one agent as a Claude coordinator row.

    ``{caret}{viewed} {label}: {summary} {run/pause} {dur} · {arrow}{tok} tok``
    with the model appended only when ``show_model`` (the list row keeps it off;
    the detail pane turns it on). ``now`` is the current clock reading so the
    duration recomputes live; ``frame`` advances the running spinner.
    """

    status = (getattr(agent_run, "status", "queued") or "queued").lower()
    paused = bool(getattr(agent_run, "_paused", False)) or status == "paused"

    caret = _CARET if selected else " "
    seen = _VIEWED if viewed else _UNVIEWED
    label = getattr(agent_run, "label", "") or getattr(agent_run, "id", "?")
    summary = (getattr(agent_run, "summary", "") or "").strip()
    summary = summary.replace("\n", " ")

    glyph = status_glyph("paused" if paused else status, frame=frame, color=color)

    # run/pause activity marker
    if paused:
        mark = _PAUSE_MARK
    elif status == "running":
        mark = _RUN_MARK
    else:
        mark = " "

    dur = format_duration(_elapsed_ms(agent_run, now))
    tok = total_tokens(getattr(agent_run, "usage", None))
    # ↓ active (accruing output) vs ↑ idle — mirror the coordinator arrow.
    arrow = "↓" if status == "running" and not paused else "↑"
    tok_str = f"{arrow}{format_number(tok)} tok"

    body = summary if summary else ""
    core = f"{caret}{seen} {glyph} {label}"
    if body:
        core += f": {body}"

    tail = f"{mark} {dur} · {tok_str}"
    if show_model:
        model = getattr(agent_run, "model", "") or ""
        if model:
            tail += f" · {model}"

    if color:
        if selected:
            line = f"{_SELECT} {core}  {tail} {_RESET}"
        else:
            line = f"{core}  {_DIM}{tail}{_RESET}"
    else:
        line = f"{core}  {tail}"

    # Truncate on the *visible* text length (ignore ANSI) to respect width.
    return _truncate_visible(line, width)


def format_phase_rail(phases: Any, sel: int = -1, *, color: bool = True) -> str:
    """A one-line breadcrumb of phase titles, e.g. ``Research › Build › Ship``.

    The selected phase (index ``sel``) is highlighted; done phases get a ✓,
    the running phase a ◇.
    """

    segs: list[str] = []
    for i, ph in enumerate(phases):
        title = getattr(ph, "title", str(ph))
        st = (getattr(ph, "status", "queued") or "queued").lower()
        g = status_glyph(st, color=False)
        seg = f"{g} {title}"
        if color:
            if i == sel:
                seg = f"{_SELECT} {seg} {_RESET}"
            elif st == "done":
                seg = f"{_GREEN}{seg}{_RESET}"
            else:
                seg = f"{_DIM}{seg}{_RESET}"
        segs.append(seg)
    joiner = f" {_GREY}›{_RESET} " if color else " › "
    return joiner.join(segs)


def _visible_len(s: str) -> int:
    """Length of ``s`` ignoring ANSI SGR escape sequences."""

    out = 0
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "\033":
            j = s.find("m", i)
            if j == -1:
                break
            i = j + 1
            continue
        out += 1
        i += 1
    return out


def _truncate_visible(s: str, width: int) -> str:
    """Truncate to ``width`` visible chars, preserving ANSI and appending reset."""

    if width <= 0 or _visible_len(s) <= width:
        return s
    out: list[str] = []
    shown = 0
    i = 0
    n = len(s)
    while i < n and shown < width - 1:
        if s[i] == "\033":
            j = s.find("m", i)
            if j == -1:
                break
            out.append(s[i : j + 1])
            i = j + 1
            continue
        out.append(s[i])
        shown += 1
        i += 1
    out.append("…")
    out.append(_RESET)
    return "".join(out)


__all__ = [
    "status_glyph",
    "format_duration",
    "format_number",
    "accumulate_usage",
    "total_tokens",
    "format_agent_row",
    "format_phase_rail",
]
