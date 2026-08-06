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


# ---------------------------------------------------------------------------
# Agent drill-down
# ---------------------------------------------------------------------------


def format_agent_detail(
    run: Any,
    agent_run: Any,
    *,
    now: float = 0.0,
    job: Any = None,
    prompt_chars: int = 600,
    activity_limit: int = 12,
    result_chars: int = 2000,
    color: bool = True,
) -> list[str]:
    """Everything known about ONE agent, as plain lines.

    Identity (workflow · phase · type · model), lifecycle (status · timing),
    accounting (turns · tools · tokens · cost), the brief it was given, its
    recent activity, and its result or error. Shared by the fullscreen overlay
    and the classic REPL so the two can never drift.

    Only observable facts appear here — tool names, turn counts, final text.
    A model's hidden reasoning is never surfaced, by construction: the engine
    records tool calls and visible text and nothing else.
    """

    dim = _DIM if color else ""
    grey = _GREY if color else ""
    red = _RED if color else ""
    reset = _RESET if color else ""

    label = getattr(agent_run, "label", "") or getattr(agent_run, "id", "?")
    lines = [f"Workflow agent · {label}"]

    wf_name = getattr(run, "name", "") or "?"
    ident = f"{dim}workflow: {wf_name}"
    run_id = getattr(run, "id", "")
    if run_id:
        ident += f" ({run_id})"
    job_id = getattr(run, "job_id", None)
    if job_id is None and job is not None:
        job_id = getattr(job, "id", None)
    if job_id is not None:
        ident += f" · job #{job_id}"
    lines.append(ident + reset)

    meta = " · ".join(x for x in (
        getattr(agent_run, "phase", "") or "",
        getattr(agent_run, "agent_type", "") or "",
        getattr(agent_run, "model", "") or "",
    ) if x)
    if meta:
        lines.append(f"{dim}phase: {meta}{reset}")

    status = getattr(agent_run, "status", "?")
    if getattr(agent_run, "replayed", False):
        status += " (replayed)"
    dur = format_duration(_elapsed_ms(agent_run, now))
    lines.append(f"{dim}status: {status} · {dur}{reset}")

    tok = total_tokens(getattr(agent_run, "usage", None))
    counts = (f"{getattr(agent_run, 'turns', 0)} turns · "
              f"{getattr(agent_run, 'tool_count', 0)} tools · {format_number(tok)} tok")
    cost = getattr(agent_run, "cost_usd", 0.0) or 0.0
    if cost:
        counts += f" · ${cost:.4f}" if cost < 0.01 else f" · ${cost:.2f}"
    lines.append(f"{dim}progress: {counts}{reset}")

    prompt = (getattr(agent_run, "prompt", "") or "").strip()
    if prompt:
        lines.append(f"{grey}prompt{reset}")
        lines += [f"{grey}  {ln}{reset}" for ln in _clip_lines(prompt, prompt_chars)]

    acts = list(getattr(agent_run, "recent_activities", []) or [])[-activity_limit:]
    if acts:
        lines.append(f"{grey}recent activity{reset}")
        lines += [f"{grey}  - {a}{reset}" for a in acts]

    err = getattr(agent_run, "error", None)
    if err:
        lines.append(f"{red}error{reset}")
        lines += [f"{red}  {ln}{reset}" for ln in _clip_lines(str(err), 800)]

    result = (getattr(agent_run, "result", "") or "").strip()
    if result:
        lines.append(f"{grey}result{reset}")
        lines += [f"  {ln}" for ln in _clip_lines(result, result_chars)]
    elif not err and status.startswith("running"):
        lines.append(f"{dim}(still working — no result yet){reset}")
    return lines


def _clip_lines(text: str, limit: int) -> list[str]:
    """Clip to ``limit`` chars and split into display lines, marking truncation."""

    body = text if len(text) <= limit else text[:limit].rstrip() + " …(truncated)"
    return body.splitlines() or [""]


# ---------------------------------------------------------------------------
# Control plane — one place that decides what is possible and says why not
# ---------------------------------------------------------------------------

# Every control, and what it needs to be legal. The viewer renders the footer
# from this, and both TUIs route their keys through :func:`apply_control`, so
# an action can never crash the UI or silently do nothing.
CONTROLS: tuple[tuple[str, str, str], ...] = (
    ("stop", "x", "stop the whole run"),
    ("pause", "p", "pause / resume the run"),
    ("cancel", "c", "cancel the selected agent"),
    ("skip", "k", "skip the selected agent"),
    ("retry", "r", "retry the selected agent"),
    ("save", "s", "save the run to disk"),
    ("inspect", "enter", "inspect the selected agent"),
)


def control_footer(*, detail: bool = False) -> str:
    """The hint line. Controls are only discoverable if they are written down."""

    if detail:
        return "←/esc back · r retry agent · c cancel agent · s save"
    return ("↑↓ select · enter/→ inspect · ←/esc back · x stop · p pause/resume · "
            "c cancel · k skip · r retry · s save")


def apply_control(handle: Any, run: Any, agent_run: Any, action: str) -> str:
    """Run one control-plane action and return a one-line human result.

    Never raises. A control that is not eligible explains WHY (the run already
    finished, the agent is not running, this run is history not a live handle)
    instead of failing silently — an orchestration UI that quietly ignores keys
    is worse than one with fewer keys.

    ``handle`` is the live :class:`~mantis_agent.workflow.Workflow`, or ``None``
    for a run loaded from history."""

    name = getattr(run, "name", None) or getattr(run, "id", "workflow")
    if action == "save":
        return _do_save(handle, run)
    if handle is None:
        return f"{name} is not live (loaded from history) — only 'save' works here"

    from .workflow import WorkflowError  # noqa: PLC0415

    try:
        if action == "stop":
            handle.stop()
            return f"stopping {name}"
        if action == "pause":
            if getattr(handle, "_paused", False):
                handle.resume()
                return f"resumed {name}"
            handle.pause()
            return f"paused {name} — new agents wait at their phase boundary"
        if action in ("cancel", "skip", "retry"):
            if agent_run is None:
                return f"no agent selected to {action}"
            agent_id = getattr(agent_run, "id", "")
            label = getattr(agent_run, "label", "") or agent_id
            if action == "cancel":
                handle.cancel(agent_id)
                return f"cancelling {label}"
            if action == "skip":
                handle.skip_agent(agent_id)
                return f"skipping {label}"
            return f"retry-{agent_id}"  # caller schedules the coroutine
    except WorkflowError as e:
        return f"cannot {action}: {e}"
    except Exception as e:  # noqa: BLE001 — a control must never take down the UI
        return f"cannot {action}: {type(e).__name__}: {e}"
    return f"unknown action {action!r}"


def _do_save(handle: Any, run: Any) -> str:
    """Snapshot a run into the durable store.

    Deliberately the SAME destination a finished run auto-persists to, live or
    not: saving mid-run should put the artifact where ``/workflows history``
    will look for it, not somewhere else the user then has to remember."""

    from .workflow_store import save_run  # noqa: PLC0415

    try:
        path = save_run(
            run,
            definition=getattr(run, "definition", ""),
            job_id=getattr(run, "job_id", None),
        )
    except Exception as e:  # noqa: BLE001
        return f"could not save: {type(e).__name__}: {e}"
    return f"saved → {path}"


def empty_state_lines(definitions: Any = None) -> list[str]:
    """What ``/workflows`` shows with nothing running — a lesson, not a shrug.

    An empty state that only says "nothing here" teaches the user nothing; this
    one names the exact command that starts a run and lists what is available."""

    lines = [
        "No workflows in this session yet.",
        "Start one:  /workflows run <name> key=value …",
    ]
    defs = list(definitions or [])
    if defs:
        lines.append("Available:")
        for d in defs[:8]:
            req = ""
            names = getattr(d, "required_input_names", None)
            if callable(names):
                got = names()
                if got:
                    req = f"  (needs {', '.join(got)})"
            lines.append(f"  {getattr(d, 'name', '?')} — "
                         f"{getattr(d, 'description', '')}{req}")
        if len(defs) > 8:
            lines.append(f"  … and {len(defs) - 8} more — /workflows list")
    lines.append("Past runs:  /workflows history · resume one with "
                 "/workflows resume <run-id>")
    return lines


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
    "CONTROLS",
    "status_glyph",
    "format_duration",
    "format_number",
    "accumulate_usage",
    "total_tokens",
    "apply_control",
    "control_footer",
    "empty_state_lines",
    "format_agent_detail",
    "format_agent_row",
    "format_phase_rail",
]
