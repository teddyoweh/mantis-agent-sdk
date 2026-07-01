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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, overload

from .agent import Agent
from .providers.base import Provider
from .tools import Tool, ToolRegistry, tool
from .types import AssistantMessage, Message, TextBlock, UserMessage

IsolationMode = Literal["asyncio_task", "subprocess", "remote"]


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


_TASK_SUBAGENT_SYSTEM = (
    "You are a read-only exploration subagent, launched by a parent coding agent "
    "to investigate a focused question autonomously. You have search/read tools "
    "(read_file, grep, glob, ls, lsp, web) but CANNOT edit files, run shell "
    "commands, or ask the user anything — work entirely from what you can read. "
    "Investigate thoroughly, then return ONE concise report: the concrete answer "
    "with exact file:line references and any facts the parent needs to act. No "
    "preamble — return only the findings."
)

_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "A short (3-5 word) description of the task.",
        },
        "prompt": {
            "type": "string",
            "description": (
                "The investigation for the subagent to perform. Be detailed and "
                "self-contained — it starts fresh with NO memory of this "
                "conversation, so include every path, name, and constraint it needs."
            ),
        },
    },
    "required": ["prompt"],
}


def make_task_tool(
    *,
    model: str,
    tools: list[Tool],
    provider: Provider | None = None,
    backend: str | None = None,
    max_steps: int = 20,
) -> Tool:
    """Build the general-purpose ``task`` tool: the parent delegates a focused,
    multi-step investigation to a fresh read-only subagent that runs to completion
    and returns just its findings — keeping the parent's context clean. The
    subagent shares the parent's provider/model but gets only ``tools`` (a
    read-only kit) and cannot recurse, edit, or prompt the user."""

    @tool(name="task", is_read_only=True, is_concurrency_safe=True, input_schema=_TASK_SCHEMA)
    async def task(args: dict) -> str:
        prompt = (args or {}).get("prompt", "")
        if not isinstance(prompt, str) or not prompt.strip():
            return "task: a non-empty 'prompt' is required."
        registry = ToolRegistry()
        if tools:
            registry.add(*tools)
        child = Agent(
            model=model,
            provider=provider,
            backend=backend,
            system=_TASK_SUBAGENT_SYSTEM,
            tools=registry,
            max_steps=max_steps,
            include_recall=False,   # subagent is stateless; no session memory
            include_env=False,
        )
        messages: list[Message] = [UserMessage(content=prompt)]
        await child.run(messages)
        return _extract_final_text(messages) or "(subagent produced no output)"

    return task


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
