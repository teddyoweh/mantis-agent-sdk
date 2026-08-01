"""Sub-agents — orchestration via the tool channel.

A sub-agent is just another tool from the parent's point of view. When the
parent's model decides to call it, we instantiate (or reuse) a child
``Agent`` with the sub-agent's system prompt + tool kit, run it to
completion, and surface the child's final assistant text as the tool
result.

This means the parent's agent loop in ``agent.py`` does **not** need to know
sub-agents exist — they look like any other ``Tool``. All the orchestration
lives in this file.

Two ergonomic shapes
--------------------
``as_subagent_tool`` accepts **either**:

1. A :class:`SubAgentSpec` — pure declaration; we mint a fresh ``Agent`` per
   invocation. Good when the sub-agent should start clean every call and
   you want the registry / provider plumbing handled for you.

2. An already-built :class:`~mantis_agent.Agent` instance plus a ``name=``
   kwarg — we wrap that exact agent. Reuses its provider, tools, system
   prompt, budget, hooks. Good when the sub-agent has expensive resources
   (open HTTP pool, MCP servers, custom permissions) that you don't want
   to re-create per call.

Both paths produce a real :class:`~mantis_agent.tools.Tool` that drops
straight into a :class:`~mantis_agent.tools.ToolRegistry`.

Isolation modes
---------------
* ``asyncio_task`` (v0 default) — child runs in the same event loop, shares
  the parent's HTTP client pool via the inherited provider. Cheap. The only
  one fully implemented in M3.
* ``subprocess`` — fork a Python child, talk to it over stdio. Hard isolation;
  ~80 ms spawn tax. Stubbed (raises ``NotImplementedError``); lands in M4.
* ``remote`` — submit to a worker node via the SDK's own protocol. Used in
  distributed deployments. Stubbed too; lands in M4.

The parent passes its provider into the child by default so the child reuses
the open HTTP connection pool — that's the single biggest perf win for the
common asyncio_task case, and it's why we don't make people wire it up by
hand.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, overload

from .agent import Agent
from .providers.base import Provider
from .tools import Tool, ToolRegistry, tool
from .types import AssistantMessage, Message, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

IsolationMode = Literal["asyncio_task", "subprocess", "remote"]

_RUN_COUNTER = itertools.count(1)  # live-progress ids for task-tool runs


def _job_log(job: Any, text: str) -> None:
    try:
        if hasattr(job, "record_event"):
            job.record_event(text)
        else:
            job.last_event = text
            job.events.append((time.monotonic(), text))
    except Exception:  # noqa: BLE001
        pass


def _short_text(s: str, limit: int = 90) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _update_job_progress(job: Any, msg: Message) -> None:
    if isinstance(msg, AssistantMessage):
        try:
            job.turn_count += 1
        except Exception:  # noqa: BLE001
            pass
        text = _short_text(" ".join(
            b.text for b in msg.content if isinstance(b, TextBlock)
        ))
        if text:
            _job_log(job, f"assistant: {text}")
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                try:
                    job.tool_count += 1
                    job.last_tool = block.name
                except Exception:  # noqa: BLE001
                    pass
                desc = ""
                if isinstance(block.input, dict):
                    desc = str(block.input.get("description") or block.input.get("path")
                               or block.input.get("pattern") or block.input.get("command") or "")
                _job_log(job, f"tool {block.name}{(': ' + _short_text(desc, 70)) if desc else ''}")
    elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
        for block in msg.content:
            if isinstance(block, ToolResultBlock):
                status = "error" if block.is_error else "result"
                content = block.content if isinstance(block.content, str) else ""
                _job_log(job, f"{status}: {_short_text(content, 90)}")


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SubAgentSpec:
    """Describes a sub-agent the parent can invoke as a tool.

    ``name`` is what the model sees and calls.
    ``system_prompt`` defines the sub-agent's persona / scope.
    ``model`` overrides the parent's model (often a cheaper / faster one).
    ``tools`` is the kit the sub-agent has access to — usually a *subset* of
        the parent's, locked down to its responsibility.
    ``max_turns`` caps the child's loop independently of the parent's.
    ``isolation`` picks the execution mode (see module docstring).
    ``description`` is shown to the parent model; defaults to a generic line.
    """

    name: str
    system_prompt: str
    model: str
    tools: list[Tool] = field(default_factory=list)
    max_turns: int = 10
    isolation: IsolationMode = "asyncio_task"
    description: str | None = None
    # Inherited by the child Agent so a write-capable sub-agent still routes
    # mutating calls through the parent's gate, and stays under the parent's
    # spend cap. ``None`` leaves the child ungated / uncapped (v0 behaviour).
    permissions: Any = None
    budget: Any = None


# ---------------------------------------------------------------------------
# SubAgentTool — the bridge object
# ---------------------------------------------------------------------------


def _spec_to_input_schema() -> dict[str, Any]:
    """Every sub-agent takes a single ``prompt`` string. Keep it dumb on purpose
    — richer arg shapes are easier to design once we have real usage."""

    return {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Task the sub-agent should accomplish.",
            },
        },
        "required": ["prompt"],
    }


class SubAgentTool(Tool):
    """A ``Tool`` whose body spawns a child ``Agent`` and runs it to completion.

    The result returned to the parent is the child's final assistant text,
    concatenated from any ``TextBlock``s in the last message. Tool calls
    happen inside the child loop; the parent never sees them.

    We extend ``Tool`` so this drops straight into a ``ToolRegistry`` without
    any special-casing in the agent loop — that's the whole design.
    """

    __slots__ = ("_spec", "_parent_provider")

    def __init__(
        self,
        spec: SubAgentSpec,
        *,
        parent_provider: Provider | None = None,
    ) -> None:
        # Tool is a slotted dataclass — initialize via its fields. We then
        # capture our spec/provider on the instance.
        super().__init__(
            name=spec.name,
            description=spec.description or f"Delegate to the {spec.name} sub-agent.",
            input_schema=_spec_to_input_schema(),
            fn=self._invoke,  # type: ignore[arg-type]
            # Sub-agents involve LLM calls; serializing same-name invocations
            # is safer than racing them through the parent's tool dispatcher.
            is_concurrency_safe=False,
        )
        self._spec = spec
        self._parent_provider = parent_provider

    # ------------------------------------------------------------------
    # The tool body — called by ``dispatch_tool_calls``.
    # ------------------------------------------------------------------

    async def _invoke(self, prompt: str) -> str:
        if self._spec.isolation == "asyncio_task":
            return await self._run_inproc(prompt)
        if self._spec.isolation == "subprocess":
            raise NotImplementedError(
                "subprocess isolation lands in M4 — see plan.md §4.10"
            )
        if self._spec.isolation == "remote":
            raise NotImplementedError(
                "remote isolation lands in M4 — see plan.md §4.10"
            )
        raise ValueError(f"unknown isolation mode: {self._spec.isolation!r}")

    async def _run_inproc(self, prompt: str) -> str:
        """asyncio_task mode: instantiate a child Agent in this loop."""

        registry = ToolRegistry()
        if self._spec.tools:
            registry.add(*self._spec.tools)

        child = Agent(
            model=self._spec.model,
            provider=self._parent_provider,  # share the parent's HTTP pool
            system=self._spec.system_prompt,
            tools=registry,
            max_steps=self._spec.max_turns,
            permissions=self._spec.permissions,
            budget=self._spec.budget,
        )

        # The child sees a single user turn: the prompt the parent passed in.
        messages: list[Message] = [UserMessage(content=prompt)]
        await child.run(messages)
        return _extract_final_text(messages)


class WrappedAgentTool(Tool):
    """A ``Tool`` that delegates to an already-instantiated ``Agent``.

    Unlike :class:`SubAgentTool`, this does NOT mint a fresh child per
    invocation. The wrapped agent's tools, provider, system prompt, budget,
    hooks, and permissions are reused as-is across calls. The wrapped agent
    is run with a *fresh, single-turn message list* per invocation so calls
    don't leak conversational state into each other — but if you genuinely
    want stateful chained sub-agent calls, pass an ``agent_factory`` to
    :func:`as_subagent_tool` and capture state yourself.

    Concurrency: same-name parallel dispatch is disabled (``parallel_safe=False``)
    because a single Agent instance is not safe to run twice concurrently —
    the provider stream, hook dispatcher, and budget tracker all expect a
    single in-flight run at a time.
    """

    __slots__ = ("_agent",)

    def __init__(
        self,
        agent: Agent,
        *,
        name: str,
        description: str | None = None,
    ) -> None:
        super().__init__(
            name=name,
            description=description or f"Delegate to the {name} sub-agent.",
            input_schema=_spec_to_input_schema(),
            fn=self._invoke,  # type: ignore[arg-type]
            is_concurrency_safe=False,
        )
        self._agent = agent

    async def _invoke(self, prompt: str) -> str:
        messages: list[Message] = [UserMessage(content=prompt)]
        await self._agent.run(messages)
        return _extract_final_text(messages)


# ---------------------------------------------------------------------------
# Public factory — accepts either shape
# ---------------------------------------------------------------------------


@overload
def as_subagent_tool(
    spec: SubAgentSpec,
    *,
    parent_provider: Provider | None = ...,
) -> Tool: ...


@overload
def as_subagent_tool(
    agent: Agent,
    *,
    name: str,
    description: str | None = ...,
) -> Tool: ...


def as_subagent_tool(
    spec_or_agent: SubAgentSpec | Agent,
    *,
    name: str | None = None,
    description: str | None = None,
    parent_provider: Provider | None = None,
) -> Tool:
    """Turn a :class:`SubAgentSpec` (or an already-built :class:`Agent`) into
    a :class:`~mantis_agent.tools.Tool` the parent agent can register.

    Two shapes:

    * ``as_subagent_tool(spec)`` — declaration form. We mint a fresh
      :class:`Agent` per invocation using the spec's model + system prompt
      + tools. Pass ``parent_provider=`` to share an HTTP pool with the
      parent (recommended in ``asyncio_task`` mode).

    * ``as_subagent_tool(agent, name="research", description="...")`` —
      wrap an existing :class:`Agent` instance directly. Reuses the agent's
      provider, tools, system, budget, hooks. ``name`` is what the parent
      model sees and calls; ``description`` defaults to a generic line.

    Raises
    ------
    TypeError
        If the first argument is neither a :class:`SubAgentSpec` nor an
        :class:`Agent`, or if an ``Agent`` is passed without ``name``.
    """

    if isinstance(spec_or_agent, SubAgentSpec):
        if name is not None or description is not None:
            raise TypeError(
                "as_subagent_tool(SubAgentSpec) — pass name/description via "
                "the spec, not as kwargs."
            )
        return SubAgentTool(spec_or_agent, parent_provider=parent_provider)

    if isinstance(spec_or_agent, Agent):
        if not name:
            raise TypeError(
                "as_subagent_tool(Agent, name='...') requires an explicit "
                "name kwarg — the wrapped agent has no externally-visible "
                "identifier of its own."
            )
        if parent_provider is not None:
            raise TypeError(
                "as_subagent_tool(Agent) does not take parent_provider — "
                "the wrapped agent already owns its provider."
            )
        return WrappedAgentTool(
            spec_or_agent, name=name, description=description
        )

    raise TypeError(
        f"as_subagent_tool: first argument must be SubAgentSpec or Agent, "
        f"got {type(spec_or_agent).__name__}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Agent types — selectable personas for the ``task`` tool
# ---------------------------------------------------------------------------
#
# Claude Code's Agent tool takes a ``subagent_type``; mantis mirrors that. A
# type bundles a system prompt + a tool policy + step budget. Three built-ins
# ship (explore / plan / general-purpose), and users add their own as markdown
# files in ``~/.mantis-agent/agents/*.md`` (user) or ``./.mantis/agents/*.md``
# (project, wins on name collision):
#
#     ---
#     name: code-reviewer
#     description: Reviews a diff for bugs and style problems.
#     tools: read_file, grep, glob        # or "read-only" / "all" (default)
#     model: gpt-5.4-mini                 # optional; default inherits parent
#     max_steps: 30                       # optional
#     ---
#     You are a meticulous code reviewer... (the system prompt)


@dataclass(frozen=True, slots=True)
class AgentType:
    """A selectable subagent persona for the ``task`` tool.

    ``tools`` is a *policy*, resolved against the parent's kit at spawn time:
    ``"read-only"`` (only ``is_read_only`` tools), ``"all"`` (everything except
    the excluded interactive/recursive tools), or an explicit tuple of tool
    names. ``model`` of ``None`` (or ``"inherit"`` in frontmatter) uses the
    parent's model."""

    name: str
    description: str
    system_prompt: str
    tools: str | tuple[str, ...] = "read-only"
    model: str | None = None
    max_steps: int = 20
    source: str = "builtin"  # "builtin" | "user" | "project"


_EXPLORE_SYSTEM = (
    "You are a file-search specialist — a read-only exploration subagent "
    "launched by a parent coding agent to navigate a codebase and answer ONE "
    "focused question. You excel at thoroughly exploring code.\n"
    "\n"
    "=== CRITICAL: READ-ONLY MODE ===\n"
    "You are STRICTLY PROHIBITED from changing anything. Do NOT create or modify "
    "files, and NEVER use shell redirect operators (>, >>, |), heredocs (<<), or "
    "any other state-changing command. You cannot ask the user anything — work "
    "entirely from what you can read.\n"
    "\n"
    "Tool choice:\n"
    "- glob for file-name/path patterns (e.g. **/*.py, src/**/test_*).\n"
    "- grep for content across files (symbols, strings, call sites).\n"
    "- read_file when you already know the path — read generously.\n"
    "- ls / lsp / web for structure, definitions, and outside facts.\n"
    "- bash ONLY for read-only inspection: ls, cat, head, tail, find, and "
    "git status / git log / git diff. NEVER mkdir, touch, rm, cp, mv, git add, "
    "git commit, or any install.\n"
    "\n"
    "Be FAST: spawn multiple grep/read calls in parallel wherever the searches "
    "are independent instead of going one at a time. Adapt your breadth to the "
    "THOROUGHNESS the caller asks for — 'quick' (answer the exact question and "
    "stop), 'medium' (confirm across the obvious files), or 'very thorough' "
    "(trace every relevant path and edge). Default to medium.\n"
    "\n"
    "'Not found' is a claim you must EARN. Before reporting something doesn't "
    "exist, try at least three angles: alternate names and casing, the "
    "abbreviated/plural form, and a content grep for a distinctive substring "
    "rather than the symbol name. Coming back empty-handed after one search is a "
    "failed run — but a wrong guess is worse than an honest gap, so never invent "
    "a path or a line number.\n"
    "\n"
    "Then return ONE concise report: the concrete answer with exact file:line "
    "citations and any facts the parent needs to act. No preamble, and create "
    "no files — communicate everything in your final message."
)

_PLAN_SYSTEM = (
    "You are a software-architect subagent, launched by a parent coding agent to "
    "design an implementation plan. You have read-only tools and you NEVER "
    "implement, edit files, or run state-changing commands — you design.\n"
    "\n"
    "Process:\n"
    "1. Understand — restate the goal and constraints in one line.\n"
    "2. Explore thoroughly — read the files you were pointed at, find existing "
    "patterns with glob/grep/read_file, and trace the real code paths. Never "
    "guess at what exists; verify it.\n"
    "3. Design — weigh the trade-offs, follow the patterns already in the repo, "
    "and be decisive: pick ONE approach rather than listing options.\n"
    "4. Detail — lay out the change step by step with exact file:line targets, "
    "the ordering/dependencies between steps, and the risks or edge cases to "
    "watch.\n"
    "\n"
    "REQUIRED OUTPUT: end your report with a section headed exactly "
    "'### Critical Files' listing the 3-5 files that must change, each an exact "
    "path with a one-line note on what changes there. No preamble."
)

_GENERAL_SYSTEM = (
    "You are a general-purpose subagent, launched by a parent coding agent to "
    "complete a focused multi-step task autonomously. You have the parent's real "
    "tool belt — shell, file edits, search, web — but you CANNOT ask the user "
    "anything: make reasonable decisions yourself and note them in your report.\n"
    "\n"
    "Explore before you act: when you don't know where something lives, search "
    "broadly first, then narrow. Start with a wide grep/glob and drill down. Try "
    "multiple naming conventions and locations before concluding something is "
    "absent, and read the surrounding code so your change matches existing "
    "patterns.\n"
    "\n"
    "Assume the task is completable. If a step fails, read the actual error and "
    "attack it from another angle — a different mechanism, a lower level, a "
    "script you write yourself, a dependency you install. Two failed attempts is "
    "not a blocker; returning 'I couldn't' without having tried several genuine "
    "approaches is a failed run.\n"
    "\n"
    "Verify your work by RUNNING it — tests, the type checker, or the relevant "
    "command — before you finish; do not assume it works because it reads "
    "correctly. Then return ONE concise report: what you did, files you changed, "
    "how you verified it, and anything the parent must know. No preamble."
)

_VERIFY_SYSTEM = (
    "You are an adversarial verification subagent, launched by a parent coding "
    "agent to check whether a change actually works. Your job is NOT to confirm "
    "it works — it is to try to BREAK it. The first 80% is the easy part; your "
    "entire value is in finding the last 20%.\n"
    "\n"
    "Resist verification avoidance. Reading is not verification — RUN it. If you "
    "catch yourself writing an explanation instead of a command, stop and run "
    "the command. Do not be seduced by the first passing case: probe the edges, "
    "the error paths, and the inputs the author probably did not think about.\n"
    "\n"
    "You have shell plus read/search tools. Run tests, invoke the code, and "
    "inspect real output — but do NOT edit files to make a check pass.\n"
    "\n"
    "OUTPUT CONTRACT — report every check in exactly this form:\n"
    "### Check: <what you tested>\n"
    "**Command run:** <the exact command>\n"
    "**Output observed:** <the real output, trimmed>\n"
    "**Result: PASS** (or FAIL)\n"
    "A check with no 'Command run' block is a SKIP, not a PASS. Include at least "
    "one adversarial / edge-case probe. End your report with a single final line "
    "that is literally 'VERDICT: PASS', 'VERDICT: FAIL', or 'VERDICT: PARTIAL'."
)

BUILTIN_AGENT_TYPES: tuple[AgentType, ...] = (
    AgentType(
        name="explore",
        description=(
            "Read-only investigation: find code/files/facts and report back "
            "with file:line references. Cannot edit or run commands."
        ),
        system_prompt=_EXPLORE_SYSTEM,
        tools="read-only",
        max_steps=30,
    ),
    AgentType(
        name="plan",
        description=(
            "Software architect: reads the code and returns a step-by-step "
            "implementation plan with file targets and trade-offs. Read-only."
        ),
        system_prompt=_PLAN_SYSTEM,
        tools="read-only",
        max_steps=25,
    ),
    AgentType(
        name="general-purpose",
        description=(
            "Autonomous multi-step execution with the full tool belt (shell, "
            "edits, web). Use for self-contained subtasks; it cannot ask the "
            "user questions."
        ),
        system_prompt=_GENERAL_SYSTEM,
        tools="all",
        max_steps=100,
    ),
    AgentType(
        name="verify",
        description=(
            "Adversarial verifier: tries to BREAK a change by running tests and "
            "exercising edge cases, then returns a PASS/FAIL/PARTIAL verdict. "
            "Can run commands (bash) but not edit files."
        ),
        system_prompt=_VERIFY_SYSTEM,
        # Needs bash to actually RUN tests/commands, but must not edit files —
        # so an explicit read+search+shell kit rather than "read-only" or "all".
        # Unknown names are dropped by resolve_agent_tools, so listing optional
        # tools (lsp/web) is harmless when the parent lacks them.
        tools=("read_file", "grep", "glob", "ls", "bash", "bash_output",
               "lsp", "web"),
        max_steps=30,
    ),
)


# Tools a subagent must never receive, regardless of policy: recursion
# (``task``), user interaction (a child can't own the input prompt), and
# plan-mode/todo handoffs that belong to the parent session.
_SUBAGENT_EXCLUDED_TOOLS = frozenset({
    "task", "ask_user_question", "exit_plan_mode", "todo_write",
})


def _agent_dirs(cwd: Any = None) -> list[tuple[str, Any]]:
    """``(source_label, dir)`` pairs: user-level then project-level (project
    wins on name). Missing dirs are dropped."""
    from pathlib import Path  # noqa: PLC0415

    from .paths import get_agents_dir  # noqa: PLC0415

    base = Path(cwd) if cwd is not None else Path.cwd()
    pairs = [("user", get_agents_dir()), ("project", base / ".mantis" / "agents")]
    return [(label, d) for label, d in pairs if d.is_dir()]


def _parse_agent_md(text: str, fallback_name: str) -> AgentType | None:
    """One ``agents/*.md`` file → an :class:`AgentType`. Frontmatter keys:
    name, description, tools, model, max_steps; body = system prompt.
    Returns ``None`` for files with no usable body (bad frontmatter is fine —
    defaults kick in — but an empty system prompt is not an agent)."""
    from .skills import _parse_skill_md  # noqa: PLC0415 — same format, one parser

    meta, body = _parse_skill_md(text)
    if not body.strip():
        return None
    name = (meta.get("name") or fallback_name).strip()
    if not name:
        return None
    raw_tools = (meta.get("tools") or "all").strip()
    tools: str | tuple[str, ...]
    if raw_tools.lower() in ("all", "read-only", "readonly"):
        tools = "read-only" if raw_tools.lower().startswith("read") else "all"
    else:
        tools = tuple(t.strip() for t in raw_tools.split(",") if t.strip()) or "all"
    model = (meta.get("model") or "").strip() or None
    if model and model.lower() == "inherit":
        model = None
    try:
        max_steps = max(1, min(int(meta.get("max_steps", 20)), 100))
    except (TypeError, ValueError):
        max_steps = 20
    return AgentType(
        name=name,
        description=meta.get("description", "").strip() or f"User-defined {name} agent.",
        system_prompt=body.strip(),
        tools=tools,
        model=model,
        max_steps=max_steps,
    )


def discover_agent_types(cwd: Any = None) -> list[AgentType]:
    """Built-in agent types + user/project ``agents/*.md`` definitions.

    Later sources win by name (project > user > builtin), so a user can
    override e.g. ``explore``'s prompt. Best-effort: unreadable or malformed
    files are skipped, never fatal."""
    from dataclasses import replace  # noqa: PLC0415

    found: dict[str, AgentType] = {t.name: t for t in BUILTIN_AGENT_TYPES}
    for source, d in _agent_dirs(cwd):
        for md in sorted(d.glob("*.md")):
            try:
                at = _parse_agent_md(md.read_text(encoding="utf-8", errors="replace"), md.stem)
            except OSError:
                continue
            if at is not None:
                found[at.name] = replace(at, source=source)
    return list(found.values())


def resolve_agent_tools(agent_type: AgentType, available: list[Tool]) -> list[Tool]:
    """Apply an agent type's tool policy to the parent's kit. Interactive /
    recursive tools are always excluded (see ``_SUBAGENT_EXCLUDED_TOOLS``)."""
    pool = [t for t in available if t.name not in _SUBAGENT_EXCLUDED_TOOLS]
    if agent_type.tools == "all":
        return pool
    if agent_type.tools == "read-only":
        return [t for t in pool if getattr(t, "is_read_only", False)]
    wanted = set(agent_type.tools)
    return [t for t in pool if t.name in wanted]


def _task_tool_description(types: list[AgentType]) -> str:
    """The ``task`` tool description shown to the parent model — lists every
    available agent type inline (Claude Code style) so the model can pick."""
    lines = [
        "Delegate a focused task to a fresh subagent that runs to completion "
        "and returns only its final report — keeping this context clean. The "
        "subagent starts with NO memory of this conversation: include every "
        "path, name, and constraint in the prompt. Launch multiple task calls "
        "in one message and they run in PARALLEL. For explore/plan you can set a "
        "thoroughness dial in the prompt — 'quick', 'medium', or 'very thorough' "
        "— to trade speed for depth.",
        "",
        "Available agent types (pass as subagent_type):",
    ]
    for t in types:
        lines.append(f"- {t.name}: {t.description}")
    return "\n".join(lines)


def _task_schema(types: list[AgentType]) -> dict[str, Any]:
    names = [t.name for t in types]
    return {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "A short (3-5 word) description of the task.",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "The task for the subagent to perform. Be detailed and "
                    "self-contained — it starts fresh with NO memory of this "
                    "conversation, so include every path, name, and constraint it "
                    "needs, plus what 'done' looks like. State the QUESTION, not "
                    "prescribed steps — a well-briefed subagent out-thinks a "
                    "scripted one."
                ),
            },
            "subagent_type": {
                "type": "string",
                "enum": names,
                "description": "Which agent type to launch (default: explore).",
            },
            "run_in_background": {
                "type": "boolean",
                "description": (
                    "Run as a detached background JOB: returns a job id "
                    "immediately so you can keep working; the result arrives "
                    "as a notification (or fetch it with job_output). Use for "
                    "long tasks that shouldn't block the conversation."
                ),
            },
        },
        "required": ["prompt"],
    }


def make_coordinate_tool(*args: Any, **kwargs: Any) -> Tool:
    """Re-export of :func:`mantis_agent.coordinator.make_coordinate_tool`.

    ``coordinate`` is the model-facing entry to the workflow engine: it
    decomposes a hard, multi-part objective into a phased Research → Synthesis →
    Verification DAG (parallel workers, then an adversarial verify pass) instead
    of the blind parallel ``task`` calls the model would otherwise fire. Exposed
    here so tool-assembly points can register it in the same import that pulls in
    :func:`make_task_tool`. Use ``coordinate`` for big decomposable problems,
    ``task`` for a single focused delegation. Imported lazily to avoid a module
    import cycle (coordinator imports this module's agent types)."""

    from .coordinator import make_coordinate_tool as _impl  # noqa: PLC0415

    return _impl(*args, **kwargs)


def make_task_tool(
    *,
    model: str,
    tools: list[Tool],
    provider: Provider | None = None,
    backend: str | None = None,
    max_steps: int = 20,
    permissions: Any = None,
    budget: Any = None,
    agent_types: list[AgentType] | None = None,
    on_progress: Any = None,
    jobs: Any = None,
) -> Tool:
    """Build the ``task`` tool: the parent delegates a focused, multi-step task
    to a fresh subagent that runs to completion and returns just its findings.

    ``tools`` is the parent's FULL kit — each agent type carves its own subset
    via its tool policy (read-only for explore/plan, everything-but-interactive
    for general-purpose, an explicit list for user-defined agents). Interactive
    and recursive tools are always stripped. ``permissions`` (the parent's
    PermissionContext) is inherited by the child so a write-capable subagent
    still routes mutating calls through the same gate as the parent — without
    it a general-purpose child would edit/execute unchecked. ``budget`` (the
    parent's :class:`Budget`) is likewise inherited so a delegated child stays
    under the same USD/token cap instead of spending unbounded.

    ``max_steps`` is a floor for backward compatibility: an agent type's own
    budget wins when larger."""
    types = agent_types if agent_types is not None else discover_agent_types()
    by_name = {t.name: t for t in types}

    @tool(name="task", is_read_only=True, is_concurrency_safe=True,
          input_schema=_task_schema(types))
    async def task(args: dict) -> str:
        prompt = (args or {}).get("prompt", "")
        if not isinstance(prompt, str) or not prompt.strip():
            return "task: a non-empty 'prompt' is required."
        type_name = str((args or {}).get("subagent_type") or "explore").strip()
        at = by_name.get(type_name)
        if at is None:
            return (f"task: unknown subagent_type {type_name!r} — available: "
                    f"{', '.join(sorted(by_name))}")
        kit = resolve_agent_tools(at, tools)
        # Live progress: wrap this run's kit so every child tool call pings
        # on_progress — the TUI renders "⎿ explore · 6 tools · 42s" under the
        # spinner instead of a silent 90s Delegate line.
        run_id = None
        if on_progress is not None:
            import copy  # noqa: PLC0415
            run_id = next(_RUN_COUNTER)
            try:
                on_progress({"id": run_id, "phase": "start", "type": type_name,
                             "desc": str((args or {}).get("description") or ""),
                             "model": at.model or model})
            except Exception:  # noqa: BLE001
                pass

            def _wrap(t: Tool) -> Tool:
                orig = t.fn

                async def fn(*a: Any, _orig: Any = orig, _name: str = t.name, **kw: Any) -> Any:
                    try:
                        on_progress({"id": run_id, "phase": "tool", "tool": _name})
                    except Exception:  # noqa: BLE001
                        pass
                    return await _orig(*a, **kw)
                # Shallow-copy preserves the concrete type (SubAgentTool /
                # WrappedAgentTool have custom __init__ signatures that
                # ``dataclasses.replace`` can't reconstruct) and its extra
                # slots; we only swap the callable.
                wrapped = copy.copy(t)
                wrapped.fn = fn  # type: ignore[assignment]
                return wrapped
            kit = [_wrap(t) for t in kit]
        registry = ToolRegistry()
        if kit:
            registry.add(*kit)

        async def _execute(job: Any = None) -> str:
            # A type-level model override still uses the PARENT's provider/
            # backend: the common case is a cheaper sibling on the same
            # endpoint. Cross-provider overrides are the parent's job to wire.
            #
            # Read-only investigators (explore/plan + user read-only agents) and
            # the verifier start with a LIGHT env block — cwd, a shallow dir
            # listing, and a git snapshot — so they aren't blind. Write-heavy
            # general-purpose agents stay lean. Recall/memory stay off either
            # way: the subagent is stateless.
            starts_with_env = at.tools == "read-only" or at.name == "verify"
            child = Agent(
                model=at.model or model,
                provider=provider,
                backend=backend,
                system=at.system_prompt,
                tools=registry,
                max_steps=max(max_steps, at.max_steps),
                permissions=permissions,
                budget=budget,   # inherit the parent's USD/token spend cap
                include_recall=False,   # subagent is stateless; no session memory
                include_env=starts_with_env,
            )
            messages: list[Message] = [UserMessage(content=prompt)]
            child_model = at.model or model
            acc_usage: Any = None  # running ModelUsage: latest input + summed output
            try:
                if hasattr(child, "run_iter"):
                    async for msg in child.run_iter(messages):
                        if job is not None:
                            _update_job_progress(job, msg)
                        # Additive per-turn progress: carry the child's model and
                        # accumulated token usage so a viewer (e.g. /workflows)
                        # can show per-agent tokens/model. Existing consumers read
                        # phase/type/desc/tool and ignore these extra keys.
                        if (on_progress is not None and run_id is not None
                                and isinstance(msg, AssistantMessage)
                                and msg.usage is not None):
                            from .workflow_view import (  # noqa: PLC0415
                                accumulate_usage, total_tokens,
                            )
                            acc_usage = accumulate_usage(acc_usage, msg.usage)
                            try:
                                on_progress({"id": run_id, "phase": "turn",
                                             "model": child_model, "usage": acc_usage,
                                             "tokens": total_tokens(acc_usage)})
                            except Exception:  # noqa: BLE001
                                pass
                else:
                    await child.run(messages)
            finally:
                if on_progress is not None and run_id is not None:
                    try:
                        on_progress({"id": run_id, "phase": "end"})
                    except Exception:  # noqa: BLE001
                        pass
            return _extract_final_text(messages) or "(subagent produced no output)"

        wants_bg = bool((args or {}).get("run_in_background"))
        if wants_bg and jobs is not None:
            desc = str((args or {}).get("description") or prompt[:60])
            holder: dict[str, Any] = {}

            async def _bg_execute() -> str:
                return await _execute(holder.get("job"))

            job = jobs.spawn(_bg_execute(), desc=desc, kind=f"task:{type_name}")
            holder["job"] = job
            return (f"Started background job #{job.id} ({type_name}: {desc}). "
                    f"Keep working — the result will arrive as a notification, "
                    f"or fetch it with job_output(job_id={job.id}).")
        result = await _execute()
        if wants_bg:
            # No JobManager wired: we couldn't background this, so it ran
            # inline to completion. Tell the model rather than silently
            # pretending it was backgrounded.
            return (
                "Note: background execution is unavailable here (no job "
                "manager), so this task ran synchronously and its result "
                "is below.\n\n" + result
            )
        return result

    task.description = _task_tool_description(types)
    return task



# ---------------------------------------------------------------------------
# pair — talk WITH a twin, not just delegate TO a worker
# ---------------------------------------------------------------------------
#
# ``task`` is fire-and-forget: prompt in, report out, child forgotten. ``pair``
# is a CONVERSATION: each named peer ("twin") keeps its own running message
# history across calls, so the main agent can propose → get pushback → revise →
# converge over several exchanges — and can keep several twins with different
# stances going at once (a skeptic, a security reviewer, a performance nut).

_TWIN_SYSTEM = (
    "You are {peer}, a twin of the main coding agent — same model, working the "
    "same repo, equal partner in an ongoing conversation. The main agent talks "
    "with you across multiple exchanges; you remember the whole dialogue.\n"
    "Your job is to make the WORK better, not to agree:\n"
    "- Challenge assumptions and look for what the main agent missed.\n"
    "- Verify claims against the actual code with your read tools — never take "
    "an assertion on faith when you can check it.\n"
    "- Propose concrete alternatives with trade-offs; cite file:line.\n"
    "- When you disagree, say exactly why, with evidence. When you're "
    "convinced, say so plainly and move the plan forward.\n"
    "Be direct and concise — this is a working session between peers, not a "
    "report.{persona}"
)

_PAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "description": (
                "What to say to the twin — a proposal to stress-test, a claim "
                "to verify, a question, or a reply in the ongoing exchange. The "
                "twin remembers your whole conversation; don't re-explain."
            ),
        },
        "peer": {
            "type": "string",
            "description": (
                "Which twin to talk to (default 'twin'). Different names are "
                "independent twins with separate memories — e.g. 'skeptic', "
                "'security', 'perf'."
            ),
        },
        "persona": {
            "type": "string",
            "description": (
                "Optional stance for a NEW twin, e.g. 'argue against every "
                "design choice' or 'care only about security'. Ignored once "
                "the twin exists."
            ),
        },
        "reset": {
            "type": "boolean",
            "description": "Forget this twin's conversation and start fresh.",
        },
    },
    "required": ["message"],
}


def make_pair_tool(
    *,
    model: str,
    tools: list[Tool],
    provider: Provider | None = None,
    backend: str | None = None,
    max_steps: int = 15,
    max_history: int = 60,
    conversations: dict[str, list[Message]] | None = None,
    personas: dict[str, str] | None = None,
) -> Tool:
    """Build the ``pair`` tool: converse with persistent same-model twins.

    Each peer's history lives for the session (trimmed to ``max_history``
    messages, oldest exchanges dropped first). Twins get ``tools`` — hand them
    a READ-ONLY kit so their pushback is grounded in the real code but they
    can't race the main agent on writes.

    Pass ``conversations``/``personas`` to own the twin state externally — the
    TUI does this so twins survive agent rebuilds AND so the user's ``/twin``
    command talks to the SAME twins the model's ``pair`` calls do."""
    conversations = conversations if conversations is not None else {}
    personas = personas if personas is not None else {}

    registry = ToolRegistry()
    if tools:
        registry.add(*tools)

    @tool(name="pair", is_read_only=True, is_concurrency_safe=False,
          input_schema=_PAIR_SCHEMA)
    async def pair(args: dict) -> str:
        message = (args or {}).get("message", "")
        if not isinstance(message, str) or not message.strip():
            return "pair: a non-empty 'message' is required."
        peer = str((args or {}).get("peer") or "twin").strip() or "twin"
        if (args or {}).get("reset"):
            conversations.pop(peer, None)
            personas.pop(peer, None)
        history = conversations.setdefault(peer, [])
        if peer not in personas:
            extra = str((args or {}).get("persona") or "").strip()
            personas[peer] = f"\nYour stance: {extra}" if extra else ""
        child = Agent(
            model=model,
            provider=provider,  # share the parent's HTTP pool
            backend=backend,
            system=_TWIN_SYSTEM.format(peer=peer, persona=personas[peer]),
            tools=registry,
            max_steps=max_steps,
            include_recall=False,
            include_env=False,
        )
        rollback_to = len(history)
        history.append(UserMessage(content=message))
        try:
            await child.run(history)
        except BaseException:
            # A failed turn must not poison the persistent history: run()
            # mutates `history` in place, so a mid-turn exception can leave a
            # user message with no assistant reply (or a half-written pair),
            # which corrupts the twin's next turn. Roll back to the pre-turn
            # state so the sliding window stays well-formed.
            del history[rollback_to:]
            raise
        # Trim from the FRONT (oldest exchanges) so the twin's memory is a
        # sliding window; never split a user/assistant pair.
        while len(history) > max_history:
            history.pop(0)
            while history and isinstance(history[0], AssistantMessage):
                history.pop(0)
        reply = _extract_final_text(history)
        return f"[{peer}] {reply}" if reply else f"[{peer}] (no reply)"

    pair.description = (
        "Talk with a TWIN — a persistent same-model peer that remembers your "
        "whole conversation with it. Unlike task (one-shot delegation), pair is "
        "a dialogue: float a plan and get it stress-tested, have a claim "
        "verified against the code, argue until you converge. Name multiple "
        "peers to keep several twins with different stances (peer='skeptic', "
        "peer='security'). Twins have read tools, so their pushback is grounded "
        "in the actual repo. Use it BEFORE committing to a risky design, and "
        "whenever a second pair of eyes would catch what you can't see."
    )
    return pair


_JOB_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "job_id": {"type": "integer", "description": "The background job id."},
        "wait": {
            "type": "boolean",
            "description": "Block up to 120s for the job to finish (default: "
                           "return current status immediately).",
        },
    },
    "required": ["job_id"],
}


def make_job_output_tool(jobs: Any) -> Tool:
    """Build ``job_output``: check on / collect the result of a background job
    started with ``task(run_in_background=true)``."""

    @tool(name="job_output", is_read_only=True, is_concurrency_safe=True,
          input_schema=_JOB_OUTPUT_SCHEMA)
    async def job_output(args: dict) -> str:
        try:
            jid = int((args or {}).get("job_id"))
        except (TypeError, ValueError):
            return "job_output: an integer 'job_id' is required."
        job = jobs.get(jid)
        if job is None:
            known = ", ".join(str(j.id) for j in jobs.all()) or "none"
            return f"no job #{jid} (known jobs: {known})"
        if (args or {}).get("wait") and job.status == "running":
            await jobs.wait(jid, timeout_s=120.0)
        if job.status == "running":
            return (f"job #{jid} still running ({int(job.elapsed_s)}s) — "
                    f"{job.desc[:60]}. Call again with wait=true, or keep working.")
        return f"job #{jid} {job.status} after {int(job.elapsed_s)}s:\n{job.result}"

    job_output.description = (
        "Check a background job started with task(run_in_background=true): "
        "returns its status, or the full result once finished. wait=true "
        "blocks up to 120s for completion."
    )
    return job_output


def _extract_final_text(messages: list[Message]) -> str:
    """Pick the last assistant message and stitch its text blocks together.

    If the child ran out of turns without producing text (e.g. last turn was
    all tool calls), fall back to a stable marker so the parent model can
    still reason about what happened.
    """

    for msg in reversed(messages):
        if isinstance(msg, AssistantMessage):
            parts: list[str] = []
            for blk in msg.content:
                if isinstance(blk, TextBlock):
                    parts.append(blk.text)
            text = "".join(parts).strip()
            if text:
                return text
            # No text in the final assistant turn — surface stop_reason.
            return f"<sub-agent finished with stop_reason={msg.stop_reason!r} and no text>"
    return "<sub-agent produced no assistant message>"


# ---------------------------------------------------------------------------
# Subprocess / remote interface stubs
# ---------------------------------------------------------------------------
# These are documented here so M4 implementers know the contract. The stubs
# raise from ``_invoke`` above; we keep the protocols here as design notes.

SubprocessLauncher = Callable[[SubAgentSpec, str], Awaitable[str]]
"""Future M4 hook: spawn a Python subprocess, hand it the spec + prompt over
stdio, return the child's final text. Implementations will live in
``mantis_agent.runtime.subprocess`` and be wired in via a registry."""

RemoteLauncher = Callable[[SubAgentSpec, str], Awaitable[str]]
"""Future M4 hook: submit to a worker node over the SDK's wire protocol.
Implementations will live in ``mantis_agent.runtime.remote``."""
