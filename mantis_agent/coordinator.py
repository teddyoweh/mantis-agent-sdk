"""Coordinator tool — expose the workflow engine to the MODEL.

``task`` (see :mod:`mantis_agent.subagent`) delegates ONE focused sub-task and
returns its report. That is the wrong shape for a hard, decomposable problem:
the model ends up firing several blind parallel ``task`` calls and then
hand-stitching the results with no verification pass.

``coordinate`` closes that gap. It hands the model the dormant
:class:`~mantis_agent.workflow.Workflow` engine as a single tool: give it a
high-level ``objective`` plus an optional decomposition into ``subtasks`` and it
runs the canonical **Research → Synthesis → Verification** DAG —

* **Research** — fan out one worker per subtask *in parallel* (the engine's
  :meth:`Workflow.parallel` barrier, capped at the workflow concurrency limit).
* **Synthesis** — collect every worker's report into structured ``findings``.
* **Verification** — unless disabled or the budget is spent, run one adversarial
  ``verify`` agent over the synthesized findings and parse its ``VERDICT``.

The result is a single structured report (findings + which agents ran + the
verdict) returned as the tool result, so the parent's context stays clean.

Design mirrors :func:`mantis_agent.subagent.make_task_tool`: the real child
agents are built by :func:`mantis_agent.workflow.make_agent_runner` (same
:class:`AgentType` personas the ``task`` tool uses), and every child tool
call / turn is streamed through ``on_progress`` in the SAME event shape the
``task`` tool emits, so the ``/workflows`` live viewer (``_live_subagents``)
renders coordinate runs with no extra wiring. Tests inject a fake
``agent_runner`` so nothing hits a model.
"""

from __future__ import annotations

import itertools
from typing import Any, AsyncIterator

from .tools import Tool, tool
from .types import AssistantMessage, ToolUseBlock
from .workflow import Workflow, make_agent_runner
from .workflow_view import accumulate_usage, total_tokens

__all__ = ["make_coordinate_tool"]


# ---------------------------------------------------------------------------
# Progress streaming — reuse the task tool's event shape
# ---------------------------------------------------------------------------


def _emit(on_progress: Any, ev: dict[str, Any]) -> None:
    """Fire one progress event, swallowing observer errors (garnish, not logic)."""

    if on_progress is None:
        return
    try:
        on_progress(ev)
    except Exception:  # noqa: BLE001 — a bad observer never breaks the run
        pass


def _short(text: str, limit: int = 80) -> str:
    """Collapse whitespace and clip — a one-line desc for the live viewer."""

    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


async def _aiter_out(out: Any) -> AsyncIterator[Any]:
    """Normalize a runner's return (async iterator or awaitable) into a stream."""

    if hasattr(out, "__aiter__"):
        async for msg in out:
            yield msg
        return
    if hasattr(out, "__await__"):
        res = await out
        if hasattr(res, "__aiter__"):
            async for msg in res:
                yield msg


def _with_progress(base: Any, on_progress: Any, counter: "itertools.count[int]") -> Any:
    """Wrap an ``agent_runner`` so each child run streams start/tool/turn/end
    events in the exact shape ``make_task_tool`` emits (so the TUI's
    ``_subagent_progress`` sink and ``/workflows`` viewer light up unchanged).

    A no-op passthrough when ``on_progress`` is ``None``."""

    if on_progress is None:
        return base

    def runner(
        prompt: str,
        *,
        model: str = "",
        agent_type: str = "general-purpose",
        schema: dict | None = None,
    ) -> AsyncIterator[Any]:
        rid = next(counter)

        async def gen() -> AsyncIterator[Any]:
            _emit(on_progress, {"id": rid, "phase": "start", "type": agent_type,
                                "desc": _short(prompt), "model": model})
            acc: Any = None
            try:
                out = base(prompt, model=model, agent_type=agent_type, schema=schema)
                async for msg in _aiter_out(out):
                    if isinstance(msg, AssistantMessage):
                        for block in getattr(msg, "content", None) or []:
                            if isinstance(block, ToolUseBlock):
                                _emit(on_progress, {"id": rid, "phase": "tool",
                                                    "tool": block.name})
                        if getattr(msg, "usage", None) is not None:
                            acc = accumulate_usage(acc, msg.usage)
                            _emit(on_progress, {"id": rid, "phase": "turn",
                                                "model": model, "usage": acc,
                                                "tokens": total_tokens(acc)})
                    yield msg
            finally:
                _emit(on_progress, {"id": rid, "phase": "end"})

        return gen()

    return runner


# ---------------------------------------------------------------------------
# Decomposition — objective (+ optional subtasks) → parallel workers
# ---------------------------------------------------------------------------


def _build_workers(
    objective: str,
    raw_subtasks: Any,
    valid_types: set[str],
) -> list[dict[str, Any]]:
    """Turn the model's ``objective`` + optional ``subtasks`` into worker specs.

    With no subtasks we still fan out: an ``explore`` worker (find the relevant
    code/facts) and a ``plan`` worker (design the approach) both scoped to the
    objective — a real parallel research phase rather than a single agent."""

    workers: list[dict[str, Any]] = []
    items = raw_subtasks if isinstance(raw_subtasks, list) else []
    for i, item in enumerate(items):
        if isinstance(item, str):
            task_text, at, label = item, "explore", None
        elif isinstance(item, dict):
            task_text = str(item.get("task") or item.get("prompt") or "").strip()
            at = str(item.get("subagent_type") or item.get("agent_type") or "explore").strip()
            label = item.get("label")
        else:
            continue
        if not task_text:
            continue
        if at not in valid_types:
            at = "explore"
        workers.append({
            "label": (label or _short(task_text, 24) or f"task-{i + 1}"),
            "agent_type": at,
            # Self-contained prompt: the worker starts fresh with no memory of
            # this conversation, so fold the overall objective into every task.
            "prompt": f"{task_text}\n\nThis is part of a larger objective: {objective}",
        })

    if not workers:
        workers = [
            {
                "label": "investigate",
                "agent_type": "explore" if "explore" in valid_types else "general-purpose",
                "prompt": (
                    "Investigate everything relevant to the objective below and "
                    "report concrete findings with file:line citations.\n\n"
                    f"OBJECTIVE: {objective}"
                ),
            },
            {
                "label": "plan",
                "agent_type": "plan" if "plan" in valid_types else "general-purpose",
                "prompt": (
                    "Design a decisive, step-by-step implementation plan for the "
                    "objective below, with exact file targets and trade-offs.\n\n"
                    f"OBJECTIVE: {objective}"
                ),
            },
        ]
    return workers


# ---------------------------------------------------------------------------
# Report formatting — structured synthesis → text for the parent model
# ---------------------------------------------------------------------------


def _format_report(result: dict[str, Any]) -> str:
    """Render the :meth:`Workflow.coordinate` synthesis dict as a text report."""

    lines: list[str] = []
    objective = result.get("objective") or ""
    lines.append(f"# Coordination report — {_short(objective, 120)}")
    agents = result.get("agents") or []
    ran = [a for a in agents if a.get("status") == "done"]
    tok = sum(int(a.get("tokens") or 0) for a in agents)
    lines.append(
        f"workflow {result.get('workflow_id', '?')} · status {result.get('status', '?')} "
        f"· {len(ran)}/{len(agents)} agents completed · ~{tok} tok"
    )
    if result.get("over_budget"):
        lines.append(
            "NOTE: budget was exhausted during research — verification was skipped."
        )
    if result.get("status") == "cancelled":
        lines.append("NOTE: the coordination was cancelled before completing.")

    lines.append("\n## Findings")
    findings = result.get("findings") or []
    if not findings:
        lines.append("(no research workers ran)")
    for f in findings:
        label = f.get("label") or "worker"
        at = f.get("agent_type") or ""
        report = (f.get("report") or "").strip() or "(no output)"
        lines.append(f"\n### {label} ({at})\n{report}")

    verification = result.get("verification")
    if verification:
        verdict = result.get("verdict") or "UNKNOWN"
        lines.append(f"\n## Verification — VERDICT: {verdict}")
        lines.append(verification.strip())

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Schema + factory
# ---------------------------------------------------------------------------


def _coordinate_schema(type_names: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "description": (
                    "The high-level, multi-part problem to solve. Be specific and "
                    "self-contained — include the paths, names, and constraints the "
                    "workers need, since each starts fresh with no memory of this chat."
                ),
            },
            "subtasks": {
                "type": "array",
                "description": (
                    "Optional decomposition into independent workers that run in "
                    "PARALLEL during the research phase. Omit to let the coordinator "
                    "fan out a default explore + plan pair over the objective."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "Self-contained prompt for this one worker.",
                        },
                        "subagent_type": {
                            "type": "string",
                            "enum": type_names,
                            "description": "Which agent persona runs this task (default: explore).",
                        },
                        "label": {
                            "type": "string",
                            "description": "Short label for this worker in the report.",
                        },
                    },
                    "required": ["task"],
                },
            },
            "verify": {
                "type": "boolean",
                "description": (
                    "Run an adversarial verification pass over the synthesized "
                    "findings and return a PASS/FAIL/PARTIAL verdict (default true)."
                ),
            },
        },
        "required": ["objective"],
    }


_COORDINATE_DESCRIPTION = (
    "Decompose a HARD, multi-part problem into a real phased workflow and run it "
    "end-to-end: fan out research/plan workers in PARALLEL, synthesize their "
    "reports, then run an adversarial verification pass — returning one "
    "structured report (findings + which agents ran + a PASS/FAIL/PARTIAL "
    "verdict).\n"
    "\n"
    "Use coordinate (NOT task) when the objective is big and DECOMPOSABLE — it "
    "needs several independent investigations that must then be reconciled and "
    "checked (e.g. 'audit the auth flow across these 4 modules and propose a "
    "fix', 'compare three designs and recommend one'). Use task for a SINGLE "
    "focused delegation, and pair to converse with a twin. Give a clear "
    "objective; optionally pre-split it into subtasks (each runs as its own "
    "parallel worker), or omit them to let the coordinator split it for you."
)


def make_coordinate_tool(
    *,
    model: str,
    provider: Any = None,
    backend: str | None = None,
    tools: Any = None,
    permissions: Any = None,
    budget: Any = None,
    agent_types: Any = None,
    on_progress: Any = None,
    jobs: Any = None,
    agent_runner: Any = None,
    concurrency: int | None = None,
) -> Tool:
    """Build the ``coordinate`` tool — the model-facing entry to the workflow
    engine (see the module docstring for the Research → Synthesis → Verification
    shape).

    ``tools`` is the parent's FULL kit; each worker carves its own subset via its
    :class:`AgentType` policy exactly as ``task`` does. ``permissions`` and
    ``budget`` are inherited so workers stay gated and under the same spend cap,
    and the workflow's budget rollup skips verification once the cap is hit.
    ``on_progress`` receives the same ``{id, phase, ...}`` events ``task`` emits
    so the ``/workflows`` live viewer renders coordinate runs.

    ``agent_runner`` overrides the default child-building runner (used by tests
    to inject a fake so nothing hits a model); ``jobs`` is accepted for signature
    parity with ``make_task_tool`` (coordinate always runs synchronously)."""

    # Resolve personas once (same source the task tool uses) for schema + defaults.
    from .subagent import discover_agent_types  # noqa: PLC0415 — avoid import cycle

    types = list(agent_types) if agent_types is not None else discover_agent_types()
    type_names = [t.name for t in types]
    valid = set(type_names)

    @tool(name="coordinate", is_read_only=True, is_concurrency_safe=True,
          input_schema=_coordinate_schema(type_names))
    async def coordinate(args: dict) -> str:
        objective = (args or {}).get("objective", "")
        if not isinstance(objective, str) or not objective.strip():
            return "coordinate: a non-empty 'objective' is required."
        objective = objective.strip()

        workers = _build_workers(objective, (args or {}).get("subtasks"), valid)
        verify_arg = (args or {}).get("verify")
        verify = True if verify_arg is None else bool(verify_arg)

        base = agent_runner or make_agent_runner(
            model=model,
            tools=list(tools) if tools else None,
            provider=provider,
            backend=backend,
            permissions=permissions,
            budget=budget,
            agent_types=types,
        )
        runner = _with_progress(base, on_progress, itertools.count(1))

        wf = Workflow(
            name=f"coordinate: {_short(objective, 48)}",
            agent_runner=runner,
            budget=budget,
            model=model,
            concurrency=concurrency,
        )
        result = await wf.coordinate(objective, workers, verify=verify)
        return _format_report(result)

    coordinate.description = _COORDINATE_DESCRIPTION
    return coordinate
