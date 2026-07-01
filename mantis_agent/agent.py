"""Agent — the run loop.

This module owns the multi-turn dance:

  1. Send messages → provider.stream (model + backend resolved from capability)
  2. Drive a StreamingToolExecutor on the event stream: tool calls dispatch
     as soon as their input JSON closes, not after the assistant finalizes.
  3. Run hooks (PreToolUse, PostToolUse, Stop) at the right moments.
  4. Check permissions before each tool call.
  5. Track budget (turns + USD + tokens) and raise BudgetExceededError when hit.
  6. If the assistant emits tool calls, append results + loop; otherwise stop.

The streaming variant (``Agent.stream``) yields the *normalized* event
stream so user UIs can render token-by-token. The non-streaming
``Agent.run`` consumes the stream internally and returns the final messages.

The agent is *backend-agnostic* — model + backend URL drive provider choice
via ``capabilities.lookup_model`` + ``providers.base.detect_provider``. Pass
``provider=`` directly to override.

Performance notes
-----------------
* Text deltas are *not* concatenated until the block stops (O(n) join once).
* Tool input JSON deltas are buffered and parsed once at block stop.
* Conversation list is appended-to in place, never copied.
* Capability lookups are O(1) and frozen onto the agent at init.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any

import msgspec

from .budget import Budget, BudgetTracker
from .capabilities import (
    BackendCapability,
    ModelCapability,
    hosted_profile_from_url,
    lookup_model,
)
from .errors import BudgetExceededError, StreamProtocolError
from .events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    ErrorEvent,
    InputJsonDelta,
    MessageDelta,
    MessageStart,
    MessageStop,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
)
from .hooks import HookContext, HookDispatcher, Hooks
from .permissions import (
    Allow,
    Deny,
    PermissionContext,
    check_permission,
)
from .compact import Compactor, SimpleCompactor
from .providers.base import Provider, detect_provider, resolve
from .streaming.executor import StreamingToolExecutor
from .tools import ToolRegistry
from .tracing import Span, Tracer, maybe_span, maybe_start_span
from .types import (
    AssistantMessage,
    ContentBlock,
    Message,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    Usage,
)

_log = logging.getLogger("mantis_agent.agent")
_JSON_DECODER = msgspec.json.Decoder()


# ---------------------------------------------------------------------------
# Text tool-call salvage
# ---------------------------------------------------------------------------
#
# Local OSS models (even 7B coder tunes) routinely "call" a tool by printing it
# as TEXT instead of using the structured tool-call channel — either a JSON
# object ``{"name": "bash", "arguments"/"parameters": {...}}`` or a shell code
# fence `````bash\n<cmd>\n`````. Without recovery the turn has no
# tool_use block, the agent loop treats it as a natural stop, and the model's
# fabricated output is shown as the answer. We salvage these into real
# ToolUseBlocks so the loop actually runs the command and feeds back the result.

_TODO_SENTINEL = "[Current todo list]"
_TODO_GLYPH = {"completed": "[x]", "in_progress": "[→]", "pending": "[ ]"}


def _render_todo_reminder(todos: list[dict[str, Any]]) -> str:
    """A ``<system-reminder>`` reflecting the live todo list so the model keeps
    its plan in view. Marked with ``_TODO_SENTINEL`` so the loop can find and
    replace a stale copy instead of accumulating them."""
    from .system_reminder import wrap_system_reminder  # noqa: PLC0415

    lines = [f"{_TODO_SENTINEL} — keep it updated with todo_write as you complete items:"]
    for t in todos:
        g = _TODO_GLYPH.get(t.get("status", "pending"), "[ ]")
        lines.append(f"{g} {t.get('content', '')}")
    return wrap_system_reminder("\n".join(lines))


_REFUSAL_RE = re.compile(
    r"\b("
    r"i'?m sorry,?\s+but\s+i\s+(can'?t|cannot)"
    r"|i\s+(can'?t|cannot)\s+(help|assist|comply|complete|continue|do that|fulf-?ill|proceed)"
    r"|i'?m\s+(not able|unable)\s+to\s+(help|assist|complete|comply|proceed|do)"
    r"|i\s+(won'?t|will not)\s+(be able to\s+)?(help|assist|comply|complete)"
    r"|i\s+(must|have to)\s+decline"
    r"|i\s+can'?t\s+assist\s+with\s+that"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_refusal(text: str) -> bool:
    """A short assistant turn that reads as a canned refusal. Length-capped so a
    long, substantive answer that merely contains a hedge isn't misread."""
    t = (text or "").strip()
    if not t or len(t) > 600:
        return False
    return bool(_REFUSAL_RE.search(t))


def _refusal_nudge() -> "UserMessage":
    """A one-shot system-reminder that re-frames the authorized context, so a
    spurious over-refusal doesn't dead-end a legitimate local task."""
    from .system_reminder import wrap_system_reminder  # noqa: PLC0415

    return UserMessage(
        content=wrap_system_reminder(
            "You just declined — but you are the user's own coding agent running "
            "inside THEIR authorized local environment, at their request. Routine "
            "development and sysadmin on the user's own machine — listing "
            "processes/ports, reading local files, running builds/tests/scripts, "
            "inspecting the user's own services — is legitimate and expected, not "
            "something to refuse. Reconsider and actually complete the task. Only if "
            "a request is genuinely harmful, name the specific concern instead of a "
            "generic refusal."
        ),
        isMeta=True,
    )


_SHELL_FENCE_LANGS = {"bash", "sh", "shell", "zsh", "console", "shellsession"}
_FENCE_RE = re.compile(r"```([a-zA-Z]*)[ \t]*\n(.*?)```", re.DOTALL)


def _salvage_text_tool_calls(text: str, registry: ToolRegistry) -> list[ToolUseBlock]:
    """Recover tool calls a model emitted as prose. Returns ToolUseBlocks whose
    ``name`` is a real registered tool (empty list if nothing salvageable)."""
    from .streaming.text_tool_parser import _loads_lenient  # noqa: PLC0415

    s = text.strip()
    if not s:
        return []
    calls: list[ToolUseBlock] = []
    n = 0

    def _mk(name: str, args: dict) -> ToolUseBlock:
        nonlocal n
        n += 1
        return ToolUseBlock(id=f"salvage_{n}_{abs(hash(name)) % 100000}", name=name, input=args)

    # 1. JSON tool-call object(s), tolerant of the malformed JSON small models
    #    emit. Accept both "arguments" (OpenAI/Anthropic) and "parameters" (the
    #    shape llama3.x tends to print).
    cleaned = s.replace("<tool_call>", " ").replace("</tool_call>", " ")
    parsed = _loads_lenient(cleaned)
    candidates = parsed if isinstance(parsed, list) else [parsed]
    for obj in candidates:
        if not isinstance(obj, dict):
            continue
        name = obj.get("name")
        args = obj.get("arguments")
        if args is None:
            args = obj.get("parameters")
        if isinstance(name, str) and registry.get(name) is not None and isinstance(args, dict):
            calls.append(_mk(name, args))
    if calls:
        return calls

    # 2. Shell code fences -> bash(command=...). Only explicit shell langs, so we
    #    never mistake an output dump or a python snippet for a command to run.
    if registry.get("bash") is not None:
        for m in _FENCE_RE.finditer(s):
            lang = m.group(1).lower()
            body = m.group(2).strip()
            if body and lang in _SHELL_FENCE_LANGS:
                cmd = "\n".join(
                    re.sub(r"^\s*\$\s?", "", ln) for ln in body.splitlines()
                ).strip()
                if cmd:
                    calls.append(_mk("bash", {"command": cmd}))
    return calls


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Agent:
    """Multi-turn agent over any OSS model on any compatible backend.

    Construction
    ------------
    The minimal form is ``Agent(model="qwen2.5-72b-instruct")`` — but most
    users will also pass ``backend="http://localhost:11434"`` (Ollama),
    ``"https://api.together.xyz/v1"`` (Together), etc.

    ``provider`` overrides the auto-construction completely. Useful for
    tests (pass a ``MockProvider``) or exotic deployments.

    ``model_capability`` overrides the looked-up capability — useful when
    you know your custom-finetuned model supports tool calling but the
    family heuristic doesn't.
    """

    model: str
    backend: str | None = None
    provider: Provider | None = None
    system: str | None = None
    tools: ToolRegistry | list = field(default_factory=ToolRegistry)
    max_tokens: int = 1024
    temperature: float | None = None
    max_steps: int = 20
    max_turns: int | None = None  # alias for max_steps
    # Anti-runaway: if the model makes the *same* tool call (same name + input)
    # this many times in one run, further repeats are short-circuited with a
    # nudge instead of executed — so a stuck model can't burn the whole
    # ``max_steps`` budget (and minutes of wall-clock) re-running an identical
    # failing command. 0 disables the guard.
    max_repeated_tool_calls: int = 3
    # Refusal recovery: if the model ends a turn with a bare, no-tool-call
    # refusal ("I'm sorry, but I can't complete that request"), nudge it ONCE
    # with a reminder that it's the user's own authorized environment and let it
    # retry, instead of dead-ending the task on a spurious over-refusal. A
    # genuinely harmful request just gets refused again and stops. 0/False off.
    recover_refusals: bool = True
    extra: dict[str, Any] | None = None

    # Capability + safety surface (M0.1 / M2)
    model_capability: ModelCapability | None = None
    backend_capability: BackendCapability | None = None

    # Safety + budget knobs — None means "no enforcement".
    hooks: Hooks | None = None
    permissions: PermissionContext | None = None
    budget: Budget | None = None
    max_usd: float | None = None  # shortcut: sets budget.max_usd if budget is None

    # Memory — auto-loads ``~/.mantis-agent/MEMORY.md`` and prepends it to the
    # system prompt. Matches Claude Code's behavior (the index is always in
    # context; individual entries are loaded on demand via the memory tool).
    # Set to False for tests / containerized runs where you don't want disk I/O.
    include_memory: bool = True

    # Environment context — injects a session-start ``<env>`` + git snapshot
    # (cwd, platform, OS, date, branch, status, recent commits) into the same
    # isMeta context head as memory, so the model is oriented in the repo.
    # Built once and memoized (``_env_context``) to keep the prompt-cache prefix
    # stable across turns. Set False to disable (tests / non-repo runs).
    include_env: bool = True

    # Memory recall — before each turn, surface the ``~/.mantis-agent/memory/``
    # topic files most relevant to the latest user message (keyword-scored,
    # offline) as a ``<system-reminder>``, deduped across the session via
    # ``_surfaced``. The read side of the memory system (write side: the
    # ``remember`` tool / ``memory.save_memory_entry``). Set False to disable.
    include_recall: bool = True

    # Fallback model — if the primary model call fails *before producing any
    # output* (overload, model-not-found, connection drop), the turn is retried
    # once on this model (same provider/backend). ``None`` disables it.
    fallback_model: str | None = None

    # Live todo list (the same list a ``todo_write`` tool mutates). When set,
    # the current state is re-injected as a ``<system-reminder>`` at the top of
    # each turn so the model keeps its plan in view over a long task instead of
    # losing it as the ``todo_write`` result scrolls out of the window.
    todos: list[dict[str, Any]] | None = None

    # Structured output — see ``response_format.py``. ``None`` means "model
    # is free to emit any text"; a dict in OpenAI ``response_format`` shape
    # is translated per backend at provider-stream time. Normalized once at
    # __post_init__ so a bad value blows up at construction (where the user
    # can fix it) rather than at first turn.
    response_format: dict[str, Any] | None = None

    # Tracing — see ``tracing.py``. ``None`` means "do nothing" — the agent
    # loop pays zero overhead. Pass an ``InMemoryTracer()`` for local
    # inspection / tests, or an ``OTelTracer()`` to ship spans to your
    # OpenTelemetry pipeline (Datadog, Honeycomb, Tempo, Jaeger, ...). The
    # tracer is shared with sub-agents so their spans nest under the parent
    # ``agent.run`` span.
    tracer: Tracer | None = None
    # Auto-compaction. When ``auto_compact`` is True (default) and no explicit
    # ``compactor`` is given, a default ``SimpleCompactor`` is built that uses
    # THIS agent's model as the summarizer. Pass a ``compactor`` to override the
    # strategy, or ``auto_compact=False`` to disable. Compaction runs at the top
    # of each turn (a safe boundary) when the history approaches the model's
    # context window, summarizing older turns so long sessions don't 413.
    compactor: Compactor | None = None
    auto_compact: bool = True
    # Parent span — set when this agent is being run as a sub-agent so its
    # ``agent.run`` span nests under the parent's ``tool.call`` span. Users
    # rarely set this directly; the sub-agent runner wires it.
    _trace_parent: Span | None = None

    # Internal state populated in __post_init__ / run loop.
    _dispatcher: HookDispatcher | None = field(default=None, init=False)
    _budget_tracker: BudgetTracker | None = field(default=None, init=False)
    _provider_hint: str | None = field(default=None, init=False)
    # Memoized ``<env>`` + git snapshot (built once, reused every turn so the
    # prompt-cache prefix stays stable). Populated lazily in _build_user_context.
    _env_context: str | None = field(default=None, init=False)
    # Set once the fallback model has been activated, so we don't loop.
    _fallback_used: bool = field(default=False, init=False)
    _refusal_retried: bool = field(default=False, init=False)
    # Absolute paths of memory files already surfaced this session, so recall
    # doesn't re-inject the same note every turn.
    _surfaced: set[str] = field(default_factory=set, init=False)
    # Resolved compactor (built from ``compactor``/``auto_compact`` in post-init).
    _compactor: Compactor | None = field(default=None, init=False)
    # AbortSignal-like cancellation event — see __post_init__ for wiring.
    cancellation_signal: Any = field(default=None, init=False)
    # Permission denials accumulated across the run. Each entry is a
    # ``{tool_name, tool_use_id, tool_input}`` dict matching the Claude
    # SDK's ``SDKPermissionDenial`` shape. ``query()`` reads this after
    # ``run()`` returns and populates ``SDKResultMessage.permission_denials``.
    _permission_denials: list = field(default_factory=list, init=False)
    # Per-run count of identical (name, input) tool calls — drives the
    # ``max_repeated_tool_calls`` anti-runaway guard. Reset at run start.
    _run_call_sigs: dict = field(default_factory=dict, init=False)
    # Optional sink for raw stream events during ``run_iter`` (token deltas,
    # block start/stop) so a UI can render text live. ``None`` = no overhead.
    on_event: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        # Normalize tools input — accept list[Tool] or pre-built ToolRegistry.
        if not isinstance(self.tools, ToolRegistry):
            registry = ToolRegistry()
            if self.tools:
                registry.add(*self.tools)
            self.tools = registry

        # max_turns is a friendly alias for max_steps (Claude SDK parity).
        if self.max_turns is not None:
            self.max_steps = self.max_turns

        # Resolve model capability if not given explicitly.
        if self.model_capability is None:
            self.model_capability = lookup_model(self.model)

        # Resolve backend capability if not given explicitly.
        if self.backend_capability is None and self.backend:
            self.backend_capability = hosted_profile_from_url(self.backend)

        # Build the provider if not given.
        if self.provider is None:
            backend_str = self.backend or self.model
            backend_kind = detect_provider(backend_str)
            ProviderCls = resolve(backend_kind)
            self.provider = self._build_provider(ProviderCls, backend_kind)

        # Propagate temperature from capability if user didn't set one.
        if self.temperature is None:
            self.temperature = self.model_capability.recommended_temperature

        # Normalize ``response_format`` early so a malformed value fails at
        # ``Agent(...)`` time, not on the first ``run()`` call. We don't
        # store the canonicalized form — translate_response_format() needs
        # to re-canonicalize per call anyway (cheap, dict-only) and
        # round-tripping the raw input lets tests inspect it as-given.
        if self.response_format is not None:
            from .response_format import normalize_response_format
            normalize_response_format(self.response_format)

        # Memory + user-context will be injected at run() time as a
        # synthetic <system-reminder>-wrapped UserMessage with
        # isMeta=True, NOT prepended to the system prompt. This matches
        # Claude Code's mechanism (see system_reminder.py for the audit).
        # The actual content is resolved lazily so a session that doesn't
        # call run() pays nothing for it.

        # Wire safety surface.
        self._dispatcher = HookDispatcher(self.hooks or Hooks())

        # Resolve budget — accept either an explicit Budget or shortcut kwargs.
        if self.budget is None and self.max_usd is not None:
            self.budget = Budget(max_usd=self.max_usd, max_turns=self.max_steps)
        if self.budget is not None:
            self._budget_tracker = BudgetTracker(budget=self.budget)

        # Provider hint for pricing lookups (e.g. "together", "fireworks").
        if self.backend_capability is not None:
            self._provider_hint = self.backend_capability.provider_hint or None

        # Auto-compaction: use the supplied compactor, else build a default
        # SimpleCompactor wired to this agent's own model as the summarizer.
        if self.compactor is not None:
            self._compactor = self.compactor
        elif self.auto_compact:
            self._compactor = SimpleCompactor(self._summarize)

        # AbortSignal-like cancellation event. Fires when ``Agent.cancel()``
        # is called. Surfaces on ``ToolPermissionContext.signal`` so
        # ``can_use_tool`` callbacks (and, post streaming-dispatch rewrite,
        # tool bodies themselves) can observe the abort and bail.
        #
        # Lazy-import to keep top-of-file imports tight; instantiate inside
        # an event-loop-aware context. anyio.Event() works without a
        # running loop, so eager init is safe.
        import anyio  # noqa: PLC0415
        self.cancellation_signal = anyio.Event()

        # Thread the signal into PermissionContext so check_permission
        # passes it to the user's can_use_tool callback.
        if self.permissions is not None and getattr(self.permissions, "signal", None) is None:
            self.permissions.signal = self.cancellation_signal

    def cancel(self) -> None:
        """Signal cancellation. Idempotent.

        Fires the agent's ``cancellation_signal`` so:

          * Any ``can_use_tool`` callback inspecting
            ``ToolPermissionContext.signal`` sees ``signal.is_set() is True``
            on the next check.
          * The :class:`StreamingToolExecutor` cancels every in-flight tool
            task via its per-task ``CancelScope`` — running tool bodies see
            ``anyio.get_cancelled_exc_class()`` raised at the next ``await``
            and unwind cooperatively.
          * Any ``tool_use`` block that arrives *after* cancel short-circuits
            to a ``ToolResultBlock(content="cancelled by signal", is_error=True)``
            without dispatching.
          * The agent's run-loop exits at the next turn boundary — no more
            model calls are issued after ``cancel()`` fires.

        Cooperating tool bodies that don't want to rely on the implicit
        CancelScope can still ``await ctx.signal.wait()`` from a background
        task or peek ``ctx.signal.is_set()`` periodically and bail
        themselves.
        """

        if not self.cancellation_signal.is_set():
            self.cancellation_signal.set()

    @staticmethod
    def _latest_user_text(messages: list[Message]) -> str:
        """Text of the most recent real (non-meta) user message — the query
        recall keys off. Skips the synthetic isMeta context head and
        tool-result user messages (list content)."""
        for m in reversed(messages):
            if (
                isinstance(m, UserMessage)
                and not getattr(m, "isMeta", False)
                and isinstance(m.content, str)
            ):
                return m.content
        return ""

    @staticmethod
    def _has_user_context_message(messages: list[Message]) -> bool:
        """True if the first message is already a meta user-context message
        — so re-calling ``run()`` doesn't double-inject."""

        if not messages:
            return False
        first = messages[0]
        if not isinstance(first, UserMessage):
            return False
        return getattr(first, "isMeta", False) is True

    def _build_user_context(self) -> dict[str, str]:
        """Resolve the user-context dict that gets wrapped in a
        ``<system-reminder>`` and prepended to the conversation.

        Matches Claude SDK's ``getUserContext()`` shape — a flat
        ``{key: value}`` dict. Currently populates one key:

          * ``memory`` — contents of ``~/.mantis-agent/MEMORY.md`` if
            ``include_memory`` is True

        Extension point for future keys: ``claudeMd`` (project-local
        ``CLAUDE.md`` walk), ``skills`` (always-loaded skills bundle),
        custom keys via ``extra={"user_context": {...}}``.
        """

        ctx: dict[str, str] = {}

        # Global opt-out for ALL persistent context (env + memory + project
        # memory). Mirrors Claude Code skipping context injection under
        # NODE_ENV=test; the test suite sets it so agents don't shell out to
        # git or pick up the ambient repo's AGENTS.md/MANTIS.md.
        if os.environ.get("MANTIS_AGENT_NO_CONTEXT") == "1":
            return ctx

        # Environment first — <env> + git snapshot, memoized so the cache prefix
        # stays stable across turns.
        if self.include_env:
            try:
                if self._env_context is None:
                    from .system_reminder import render_environment_context
                    self._env_context = render_environment_context().strip()
                if self._env_context:
                    ctx["environment"] = self._env_context
            except Exception:  # noqa: BLE001 — subprocess / I/O can fail
                _log.debug("env context skipped", exc_info=True)

        if self.include_memory:
            try:
                from .memory import load_memory_index
                index = load_memory_index().strip()
                if index:
                    ctx["memory"] = index
            except Exception:  # noqa: BLE001 — disk I/O can fail in containers
                _log.debug("memory load skipped (I/O error)", exc_info=True)

            # MANTIS.md instruction-memory hierarchy (managed → user → project →
            # local, with @imports). The mantis analogue of CLAUDE.md.
            try:
                from .project_memory import render_memory_prompt
                md = render_memory_prompt().strip()
                if md:
                    ctx["mantis_md"] = md
            except Exception:  # noqa: BLE001
                _log.debug("MANTIS.md load skipped (I/O error)", exc_info=True)

            # Skills catalog — frontmatter only (name + description). The model
            # sees what's available and calls load_skill for the body on demand.
            try:
                from .skills import discover_skills, render_skill_catalog
                catalog = render_skill_catalog(discover_skills()).strip()
                if catalog:
                    ctx["skills"] = catalog
            except Exception:  # noqa: BLE001
                _log.debug("skills catalog skipped", exc_info=True)

        # User-supplied extras flow through ``extra={"user_context": {...}}``.
        if isinstance(self.extra, dict):
            extra_ctx = self.extra.get("user_context")
            if isinstance(extra_ctx, dict):
                for k, v in extra_ctx.items():
                    if isinstance(v, str) and v:
                        ctx[k] = v

        return ctx

    def _build_provider(self, ProviderCls: type[Provider], backend_kind: str) -> Provider:
        """Construct a provider with sensible defaults per backend kind."""

        kw: dict[str, Any] = {}
        if backend_kind == "openai_compat":
            kw["base_url"] = self.backend or os.environ.get(
                "MANTIS_AGENT_BASE_URL", "http://localhost:8000/v1"
            )
            kw["api_key"] = os.environ.get("MANTIS_AGENT_API_KEY")
            if self.backend_capability is not None:
                kw["backend_capability"] = self.backend_capability
        elif backend_kind == "ollama":
            kw["base_url"] = self.backend or "http://localhost:11434"
        elif backend_kind == "llamacpp":
            kw["base_url"] = self.backend or "http://localhost:8080"
        elif backend_kind == "tgi":
            kw["base_url"] = self.backend or "http://localhost:3000/v1"
        elif backend_kind == "anthropic_passthrough":
            # Pass an explicit base_url only when the caller gave us a real
            # URL (skip the literal ``"anthropic"`` sentinel — letting the
            # provider use its own default is the whole point of the sentinel).
            if self.backend and self.backend.lower().startswith(("http://", "https://")):
                kw["base_url"] = self.backend
            if self.backend_capability is not None:
                kw["backend_capability"] = self.backend_capability
            # api_key is read from $ANTHROPIC_API_KEY inside the provider —
            # surfacing it here too would shadow that resolution path.
        elif backend_kind == "mock":
            pass  # mock takes its own kwargs from `extra`
        # Best-effort construction — adapters that don't accept some keys
        # will tell us at instantiation.
        try:
            return ProviderCls(**kw)
        except TypeError:
            # Fall back to no-kwarg construction.
            return ProviderCls()  # type: ignore[call-arg]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, messages: list[Message]) -> list[Message]:
        """Run the full multi-turn loop and return the updated message list.

        Buffered wrapper around :meth:`run_iter`. ``messages`` is mutated in
        place; the same list is returned for chaining. For mid-stream
        consumption (yield each assistant turn + tool-result UserMessage
        AS they finalize), use :meth:`run_iter` directly.
        """

        async for _msg in self.run_iter(messages):
            # run_iter mutates `messages` in place; we just drain it.
            pass
        return messages

    async def run_iter(self, messages: list[Message]) -> AsyncIterator[Message]:
        """Streaming-mode run loop: yield each new Message as it finalizes.

        Yields, in order:
          * The synthetic user-context ``UserMessage`` (``isMeta=True``) if
            one is injected at the head of the conversation.
          * One :class:`AssistantMessage` per turn, the moment that turn's
            stream finishes assembling. By the time the yield happens, the
            :class:`StreamingToolExecutor` has *already* been dispatching its
            tool calls — each tool's body was kicked off the instant its
            ``tool_use`` block's input JSON closed mid-stream, NOT after
            ``MessageStop``. Long-running tools may already be finished, or
            may still be in flight when the yield happens; the next yield
            (the tool-result ``UserMessage``) blocks until every dispatched
            tool has produced a result block.
          * One :class:`UserMessage` carrying the batch of
            :class:`ToolResultBlock` s for each turn that requested tools.
          * Nothing after the final natural-stop turn — callers use the
            yielded AssistantMessage's ``stop_reason`` to detect end.

        ``messages`` is mutated in place — every yielded item is also
        appended to the list — so the caller can inspect the running
        conversation between iterations.

        Drives the full integration: PreToolUse / PostToolUse hooks,
        permission checks (including ``PermissionResultAllow.updated_input``
        rewrites), ``BudgetExceededError`` on turn / usd / token overruns,
        and ``Stop`` hook fired on natural turn-end.

        Mid-stream dispatch contract
        ----------------------------
        For each ``ContentBlockStop`` event that closes a ``tool_use``
        block, the agent immediately:

          1. Materializes the ``ToolUseBlock`` (parses the closed input
             JSON; malformed JSON defers to ``finalize()`` which raises
             ``StreamProtocolError`` — same as the buffered path).
          2. Runs the ``PreToolUse`` hook with a snapshot of the
             conversation *before* this assistant turn (the partial
             assistant message in progress is not yet appended).
          3. Runs the permission check; ``Allow.updated_input`` rewrites
             the call, ``Deny`` short-circuits to an ``is_error`` result
             block (and is recorded for ``ResultMessage.permission_denials``).
          4. Hands the surviving call to the live ``StreamingToolExecutor``
             via ``add_tool_call`` — the body starts running concurrently
             with the remainder of the stream.

        Tool result blocks are returned in the same order as the model
        emitted the tool_use blocks — ``StreamingToolExecutor`` preserves
        insertion order regardless of which tool finished first.
        """

        assert self._dispatcher is not None  # set in __post_init__

        # Inject persistent user-context (memory + custom) as a synthetic
        # ``<system-reminder>``-wrapped UserMessage at the head of the
        # conversation. Matches Claude SDK 1:1. No-op when context is
        # empty. We do this once per run_iter() call; subsequent turns
        # reuse the already-injected message. Yield it so streaming-mode
        # consumers can see what context the agent saw (and skip it via
        # the ``isMeta`` flag if they're rendering to a UI).
        if not self._has_user_context_message(messages):
            user_ctx = self._build_user_context()
            if user_ctx:
                from .system_reminder import prepend_user_context  # local import
                prepend_user_context(messages, user_ctx, in_place=True)
                # The first message is now the synthetic meta UserMessage.
                if messages and isinstance(messages[0], UserMessage) and getattr(messages[0], "isMeta", False):
                    yield messages[0]

        # Memory recall — surface the topic files most relevant to THIS turn's
        # user message (query-specific, so it rides the current turn rather than
        # the cached head), deduped across the session. Appended after the user
        # message so it's the freshest context the model sees before replying.
        if self.include_recall and os.environ.get("MANTIS_AGENT_NO_CONTEXT") != "1":
            query = self._latest_user_text(messages)
            if query:
                try:
                    from .memory_recall import recall_block
                    text, paths = recall_block(
                        query, already_surfaced=frozenset(self._surfaced)
                    )
                    if text:
                        self._surfaced.update(paths)
                        reminder = UserMessage(content=text, isMeta=True)
                        messages.append(reminder)
                        yield reminder
                except Exception:  # noqa: BLE001 — recall is best-effort
                    _log.debug("memory recall skipped", exc_info=True)

        # Todo state — keep the model's plan in view over a long task. Refresh
        # rather than accumulate: drop any prior todo reminder, append the
        # current one (isMeta, so UIs filter it from the visible transcript).
        if self.todos:
            messages[:] = [
                m for m in messages
                if not (isinstance(m, UserMessage) and getattr(m, "isMeta", False)
                        and isinstance(m.content, str) and _TODO_SENTINEL in m.content)
            ]
            todo_msg = UserMessage(content=_render_todo_reminder(self.todos), isMeta=True)
            messages.append(todo_msg)
            yield todo_msg

        registry: ToolRegistry = self.tools  # type: ignore[assignment]

        # Open the root ``agent.run`` span — covers the entire multi-turn
        # loop. We deliberately open the span outside the for-loop so
        # ``agent.turn`` children nest under it correctly. The span is
        # closed in the ``finally`` below regardless of natural-stop /
        # max-steps / exception path.
        run_span = maybe_start_span(
            self.tracer,
            "agent.run",
            parent=self._trace_parent,
            attributes={
                "agent.model": self.model,
                "agent.backend": self.backend or "",
                "agent.max_steps": self.max_steps,
                "agent.tools.count": len(registry) if registry is not None else 0,
                "agent.system.len": len(self.system) if self.system else 0,
            },
        )
        # Track aggregate run-totals so we can stamp them on the run span
        # at finalize-time. ``Usage`` fields default to 0 so a no-cost run
        # still gets zeros (not None) on the span — easier dashboarding.
        run_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "cost_usd": 0.0,
            "turns": 0,
        }
        self._run_call_sigs = {}  # reset anti-runaway counters for this run
        run_error: BaseException | None = None

        def _close_run_span(error: BaseException | None = None) -> None:
            if run_span is None or self.tracer is None:
                return
            run_span.set_attributes({
                "agent.turns": run_totals["turns"],
                "agent.total_input_tokens": run_totals["input_tokens"],
                "agent.total_output_tokens": run_totals["output_tokens"],
                "agent.total_cache_read_tokens": run_totals["cache_read_tokens"],
                "agent.total_cache_creation_tokens": run_totals["cache_creation_tokens"],
                "agent.total_cost_usd": round(run_totals["cost_usd"], 8),
            })
            if error is not None:
                run_span.end(status="error", exception=error)
            else:
                run_span.end()
            # Move into the finished-spans list for in-memory inspection.
            mirror = getattr(self.tracer, "_mirror", None) or self.tracer
            close_fn = getattr(mirror, "_close", None)
            if callable(close_fn):
                close_fn(run_span)

        # Auto-compaction bookkeeping: the most recent turn's reported usage
        # (drives should_compact) and a per-run cap on how many times we
        # summarize — the circuit breaker against a summary that itself stays
        # over threshold and would otherwise re-compact every turn.
        last_usage: Usage | None = None
        compactions = 0
        _MAX_COMPACTIONS = 5
        self._refusal_retried = False

        for _ in range(self.max_steps):
            # If the cancellation signal already fired BEFORE this turn
            # starts, bail without burning another model round-trip. The
            # signal could be set externally (``Agent.cancel()``), or by
            # the prior turn's tool cancellation cascade. Either way the
            # contract is: don't ask the model again after cancel.
            if self.cancellation_signal.is_set():
                await self._dispatcher.dispatch(
                    "Stop",
                    HookContext(event="Stop", messages_snapshot=messages),
                )
                _close_run_span()
                return

            # ----------------------------------------------------------------
            # Auto-compaction — at the TOP of the turn (before the model call),
            # the one safe boundary: the prior iteration ended by appending this
            # turn's tool_result UserMessage, so every tool_use is matched and
            # `messages` never ends on a dangling assistant tool_use.
            # Summarizing older turns now keeps the NEXT model call inside the
            # context window. ``messages[:]`` replaces in place so the caller's
            # list reference sees the shrunk history too.
            # ----------------------------------------------------------------
            if (
                self._compactor is not None
                and compactions < _MAX_COMPACTIONS
                and self._is_safe_compaction_point(messages)
            ):
                ctx_window = (
                    self.model_capability.context_window
                    if self.model_capability is not None
                    else 0
                )
                usage_now = last_usage or Usage()
                # Cheap first line: clear old tool-result bodies (no model call).
                micro = getattr(self._compactor, "microcompact", None)
                should_micro = getattr(self._compactor, "should_microcompact", None)
                if micro is not None and should_micro is not None and should_micro(
                    messages, usage_now, ctx_window
                ):
                    micro(messages)
                # Fallback: full summarizing compaction when still over threshold.
                if await self._compactor.should_compact(messages, usage_now, ctx_window):
                    before_len = len(messages)
                    compacted = await self._compactor.compact(messages)
                    if len(compacted) < before_len:
                        messages[:] = compacted
                        compactions += 1

            # Per-turn span — nests under agent.run when tracing is on.
            turn_span = maybe_start_span(
                self.tracer,
                "agent.turn",
                parent=run_span,
                attributes={"turn.index": run_totals["turns"]},
            )
            llm_span: Span | None = None
            llm_start_ns = time.monotonic_ns()
            llm_first_token_ns: int | None = None
            # --------------------------------------------------------------
            # One turn, with mid-stream tool dispatch.
            #
            # The executor stays open across the *entire* provider stream
            # so it can accept ``add_tool_call`` invocations the moment a
            # tool_use block's input JSON finalizes. Tool bodies start
            # running concurrently with subsequent text/tool deltas — the
            # cost saved is the per-turn ``max(tool_dur) - 0`` rather than
            # ``sum(tool_dur)`` we paid pre-streaming.
            # --------------------------------------------------------------
            assembler = _AssistantAssembler()
            # Block indices we've already dispatched (so a duplicate
            # ContentBlockStop on the same index doesn't re-fire).
            dispatched_indices: set[int] = set()
            # Calls in the order the model emitted them (matches the order
            # of ToolUseBlocks in the finalized assistant.content).
            ordered_calls: list[ToolUseBlock] = []
            # Calls that were short-circuited by hooks or permissions.
            # Their entries in ``ordered_calls`` are the ORIGINAL blocks;
            # the result lives in ``short_circuit`` keyed by call id.
            short_circuit: dict[str, ToolResultBlock] = {}
            # Snapshot of the conversation BEFORE this assistant turn —
            # passed to hooks so they see the same context the model saw.
            messages_snapshot = list(messages)

            async with StreamingToolExecutor(
                registry,
                cancellation_signal=self.cancellation_signal,
                tracer=self.tracer,
                trace_parent=turn_span,
            ) as executor:
                # llm.call span covers just the provider stream — start →
                # MessageStop. ``first_token_ms`` is filled at the first
                # ContentBlockDelta (TextDelta or ThinkingDelta) so users
                # can dashboard TTFB independently of total latency.
                llm_span = maybe_start_span(
                    self.tracer,
                    "llm.call",
                    parent=turn_span,
                    attributes={
                        "llm.model": self.model,
                        "llm.provider": getattr(self.provider, "name", "")
                        or self._provider_hint or "",
                    },
                )
                async for ev in self._stream_with_fallback(messages):
                    assembler.feed(ev)
                    # Surface raw stream events (token deltas, block start/stop)
                    # to an optional consumer so a UI can render text live as it
                    # streams — run_iter itself only yields finalized messages.
                    if self.on_event is not None:
                        try:
                            self.on_event(ev)
                        except Exception:  # noqa: BLE001 — a UI callback must never break the loop
                            _log.debug("on_event callback raised", exc_info=True)
                    if llm_first_token_ns is None and isinstance(
                        ev, ContentBlockDelta
                    ):
                        llm_first_token_ns = time.monotonic_ns()
                    if isinstance(ev, ContentBlockStop):
                        await self._maybe_dispatch_closed_block(
                            ev,
                            assembler,
                            dispatched_indices,
                            ordered_calls,
                            short_circuit,
                            messages_snapshot,
                            executor,
                        )

                # Stream consumed. Finalize the assistant message.
                assistant = assembler.finalize()

                # Salvage tool calls the model emitted as TEXT (JSON object or a
                # shell code fence) instead of via the structured channel — the
                # dominant failure mode for local OSS models. Convert them to
                # real tool_use blocks and dispatch through this same executor so
                # the loop continues exactly as if they'd been native calls.
                if self.tools and not any(
                    isinstance(b, ToolUseBlock) for b in assistant.content
                ):
                    salvaged = _salvage_text_tool_calls(
                        "".join(
                            b.text for b in assistant.content
                            if isinstance(b, TextBlock)
                        ),
                        registry,
                    )
                    if salvaged:
                        assistant = AssistantMessage(
                            content=list(salvaged),
                            stop_reason=assistant.stop_reason,
                            usage=assistant.usage,
                        )
                        for call in salvaged:
                            approved, sc_result = await self._preflight_call(
                                call, messages_snapshot
                            )
                            if sc_result is not None:
                                short_circuit[call.id] = sc_result
                                ordered_calls.append(call)
                            else:
                                ordered_calls.append(approved)
                                executor.add_tool_call(approved)

                messages.append(assistant)

                # Close the llm.call span now that the provider stream is
                # done — *before* tool execution drains. ``llm.call`` is
                # specifically "time the model spent generating," not
                # "time the turn took including downstream tools." Tool
                # latency lives on tool.call children.
                if llm_span is not None and self.tracer is not None:
                    llm_span.set_attributes({
                        "llm.input_tokens": (
                            assistant.usage.input_tokens
                            if assistant.usage is not None else 0
                        ),
                        "llm.output_tokens": (
                            assistant.usage.output_tokens
                            if assistant.usage is not None else 0
                        ),
                        "llm.cache_read_tokens": (
                            assistant.usage.cache_read_input_tokens or 0
                            if assistant.usage is not None else 0
                        ),
                        "llm.cache_creation_tokens": (
                            assistant.usage.cache_creation_input_tokens or 0
                            if assistant.usage is not None else 0
                        ),
                        "llm.stop_reason": assistant.stop_reason or "",
                        "llm.first_token_ms": (
                            (llm_first_token_ns - llm_start_ns) / 1_000_000.0
                            if llm_first_token_ns is not None else 0.0
                        ),
                    })
                    llm_span.end()
                    mirror = getattr(self.tracer, "_mirror", None) or self.tracer
                    close_fn = getattr(mirror, "_close", None)
                    if callable(close_fn):
                        close_fn(llm_span)

                # Remember this turn's usage so the NEXT turn's compaction
                # check sees the real prompt size, not a stale/empty estimate.
                last_usage = assistant.usage

                # Bump turn + cost AFTER the assistant message materializes.
                # Tools may already be running — that's fine, we still
                # enforce budget on the turn that just finalized.
                if self._budget_tracker is not None:
                    self._budget_tracker.add_turn()
                    if assistant.usage is not None:
                        self._budget_tracker.add_usage(
                            assistant.usage,
                            self.model,
                            backend_hint=self._provider_hint,
                        )
                    self._budget_tracker.check()

                # Update run-totals + turn-span attrs from this turn's usage.
                if assistant.usage is not None:
                    run_totals["input_tokens"] += assistant.usage.input_tokens or 0
                    run_totals["output_tokens"] += assistant.usage.output_tokens or 0
                    run_totals["cache_read_tokens"] += assistant.usage.cache_read_input_tokens or 0
                    run_totals["cache_creation_tokens"] += assistant.usage.cache_creation_input_tokens or 0
                    if self._budget_tracker is not None:
                        run_totals["cost_usd"] = self._budget_tracker.total_usd
                run_totals["turns"] += 1

                if turn_span is not None:
                    turn_span.set_attributes({
                        "turn.stop_reason": assistant.stop_reason or "",
                        "turn.input_tokens": (
                            assistant.usage.input_tokens
                            if assistant.usage is not None else 0
                        ),
                        "turn.output_tokens": (
                            assistant.usage.output_tokens
                            if assistant.usage is not None else 0
                        ),
                        "turn.tool_uses": sum(
                            1 for b in assistant.content
                            if isinstance(b, ToolUseBlock)
                        ),
                    })

                # Yield the assistant turn the moment it's complete — BEFORE
                # blocking on tool execution. Consumers see the tool_use
                # blocks immediately and can render "tool running…" state
                # while ``wait_all`` drains the executor.
                yield assistant

                tool_uses = [
                    b for b in assistant.content if isinstance(b, ToolUseBlock)
                ]
                if not tool_uses and self.recover_refusals and not self._refusal_retried:
                    # Bare, no-tool-call refusal? Nudge ONCE with the authorized-
                    # context reminder and re-prompt instead of dead-ending. A
                    # ``continue`` exits this turn's ``async with executor`` cleanly
                    # (no tools were dispatched) and re-streams with the nudge.
                    _text = "".join(
                        b.text for b in assistant.content if isinstance(b, TextBlock)
                    )
                    if _looks_like_refusal(_text):
                        self._refusal_retried = True
                        messages.append(_refusal_nudge())
                        if turn_span is not None and self.tracer is not None:
                            turn_span.set_attributes({"turn.refusal_recovered": True})
                            turn_span.end()
                            mirror = getattr(self.tracer, "_mirror", None) or self.tracer
                            close_fn = getattr(mirror, "_close", None)
                            if callable(close_fn):
                                close_fn(turn_span)
                        continue
                if not tool_uses:
                    # Natural turn-end. Fire Stop hook and exit cleanly —
                    # the executor's ``__aexit__`` releases its task group
                    # (no tasks were ever started because no tool_use blocks
                    # arrived).
                    await self._dispatcher.dispatch(
                        "Stop",
                        HookContext(event="Stop", messages_snapshot=messages),
                    )
                    if turn_span is not None and self.tracer is not None:
                        turn_span.end()
                        mirror = getattr(self.tracer, "_mirror", None) or self.tracer
                        close_fn = getattr(mirror, "_close", None)
                        if callable(close_fn):
                            close_fn(turn_span)
                    _close_run_span()
                    return

                # Drain every dispatched tool. ``wait_all`` returns results
                # in insertion order (= stream order = assistant.content
                # tool_use order). Tools that errored produce
                # ``is_error=True`` blocks; tools that haven't been
                # dispatched (short-circuited) aren't represented here.
                executor_results = await executor.wait_all()

            # PostToolUse hooks for each tool that actually executed.
            by_id: dict[str, ToolResultBlock] = {
                r.tool_use_id: r for r in executor_results
            }
            by_id.update(short_circuit)

            for call in ordered_calls:
                if call.id in short_circuit:
                    continue
                tool = registry.get(call.name)
                if tool is None:
                    continue
                result = by_id.get(call.id)
                if result is None:
                    continue
                event_name = (
                    "PostToolUseFailure" if result.is_error else "PostToolUse"
                )
                await self._dispatcher.dispatch(
                    event_name,
                    HookContext(
                        event=event_name,
                        tool=tool,
                        input=call.input,
                        output=result.content,
                    ),
                )

            # Align results with assistant.content tool_use blocks in order.
            results_in_order = [by_id[b.id] for b in tool_uses]
            tool_result_msg = UserMessage(content=list(results_in_order))
            messages.append(tool_result_msg)
            yield tool_result_msg

            # End the turn span now that this turn (model call + tools +
            # post-tool hooks) is fully wrapped up. Tool spans live as
            # children of this turn via the executor's trace_parent.
            if turn_span is not None and self.tracer is not None:
                turn_span.end()
                mirror = getattr(self.tracer, "_mirror", None) or self.tracer
                close_fn = getattr(mirror, "_close", None)
                if callable(close_fn):
                    close_fn(turn_span)

        _log.warning("agent hit max_steps=%d without natural stop", self.max_steps)
        _close_run_span()

    async def _maybe_dispatch_closed_block(
        self,
        ev: ContentBlockStop,
        assembler: "_AssistantAssembler",
        dispatched_indices: set[int],
        ordered_calls: list[ToolUseBlock],
        short_circuit: dict[str, ToolResultBlock],
        messages_snapshot: list[Message],
        executor: StreamingToolExecutor,
    ) -> None:
        """If the block at ``ev.index`` is a freshly-closed tool_use,
        preflight it (PreToolUse hook + permission) and either dispatch it
        to the live ``StreamingToolExecutor`` or record a short-circuit
        result.

        Idempotent: ``dispatched_indices`` guards against duplicate
        ``ContentBlockStop`` events on the same index. Malformed input JSON
        is *skipped* here and re-raised at ``assembler.finalize()`` so the
        whole turn errors out — matching the pre-streaming behavior.
        """

        idx = ev.index
        if idx in dispatched_indices:
            return
        builder = assembler.blocks.get(idx)
        if builder is None or builder.kind != "tool_use":
            return
        dispatched_indices.add(idx)

        try:
            block = builder.to_block()
        except StreamProtocolError:
            # Malformed input JSON. Let ``finalize()`` re-raise the
            # canonical error — don't dispatch a broken call.
            return
        assert isinstance(block, ToolUseBlock)

        approved_call, sc_result = await self._preflight_call(
            block, messages_snapshot
        )
        if sc_result is not None:
            short_circuit[block.id] = sc_result
            ordered_calls.append(block)
            return

        ordered_calls.append(approved_call)
        executor.add_tool_call(approved_call)

    async def _preflight_call(
        self,
        call: ToolUseBlock,
        messages_snapshot: list[Message],
    ) -> tuple[ToolUseBlock, ToolResultBlock | None]:
        """PreToolUse hook + permission check for one call.

        Returns ``(call_to_dispatch, short_circuit_result)``. The
        second element is ``None`` when the call survives; otherwise the
        caller records the error result and skips dispatch. The returned
        call may have a different ``input`` than the original if a hook
        or ``PermissionResultAllow.updated_input`` rewrote it.

        Unknown tools (not in the registry) skip the hook/permission
        chain entirely and dispatch as-is — the executor produces the
        canonical "tool not found" error block.
        """

        assert self._dispatcher is not None
        registry: ToolRegistry = self.tools  # type: ignore[assignment]
        tool = registry.get(call.name)
        if tool is None:
            return call, None

        # Anti-runaway: short-circuit the Nth+ identical call so a stuck model
        # can't re-run the same failing command until it exhausts max_steps.
        if self.max_repeated_tool_calls:
            try:
                sig = (call.name, msgspec.json.encode(call.input))
            except Exception:  # noqa: BLE001 — unencodable input: skip the guard
                sig = None
            if sig is not None:
                seen = self._run_call_sigs.get(sig, 0) + 1
                self._run_call_sigs[sig] = seen
                if seen > self.max_repeated_tool_calls:
                    return call, ToolResultBlock(
                        tool_use_id=call.id,
                        content=(
                            f"Stopping: this exact `{call.name}` call has already "
                            f"run {self.max_repeated_tool_calls} times with the same "
                            f"arguments and no new result. Do not repeat it — change "
                            f"the arguments, try a different approach, or give your "
                            f"final answer to the user now."
                        ),
                        is_error=True,
                    )

        # PreToolUse hook.
        hr = await self._dispatcher.dispatch(
            "PreToolUse",
            HookContext(
                event="PreToolUse",
                tool=tool,
                input=call.input,
                messages_snapshot=messages_snapshot,
            ),
        )
        if hr.block:
            return call, ToolResultBlock(
                tool_use_id=call.id,
                content=f"blocked by hook: {hr.note or 'no reason given'}",
                is_error=True,
            )

        # Apply hook-mutated input. ToolUseBlock is frozen — rebuild it.
        payload = hr.mutated_input if hr.mutated_input is not None else call.input
        if payload is not call.input:
            call = ToolUseBlock(id=call.id, name=call.name, input=payload)

        # Permission check.
        if self.permissions is not None:
            decision = await check_permission(tool, call.input, self.permissions)
            decision = _normalize_permission_decision(decision)

            if isinstance(decision, Deny):
                await self._dispatcher.dispatch(
                    "PermissionDenied",
                    HookContext(
                        event="PermissionDenied",
                        tool=tool,
                        input=call.input,
                        arbitrary={"reason": decision.reason},
                    ),
                )
                self._permission_denials.append(
                    {
                        "tool_name": call.name,
                        "tool_use_id": call.id,
                        "tool_input": dict(call.input or {}),
                    }
                )
                return call, ToolResultBlock(
                    tool_use_id=call.id,
                    content=f"permission denied: {decision.reason}",
                    is_error=True,
                )

            if isinstance(decision, Allow) and decision.updated_input is not None:
                call = ToolUseBlock(
                    id=call.id,
                    name=call.name,
                    input=decision.updated_input,
                )
            # Ask → treated as Allow at the loop layer (integrators can
            # convert Ask to Deny via the can_use_tool callback).

        return call, None

    async def stream(self, messages: list[Message]) -> AsyncIterator[StreamEvent]:
        """Stream the next assistant turn as normalized events.

        Does *not* run the multi-turn loop — the caller is responsible for
        appending the resulting assistant message and (if it contains tool
        calls) calling ``stream`` again with appended tool results.
        """

        async for ev in self._provider_stream(messages):
            yield ev

    async def aclose(self) -> None:
        if self.provider is not None:
            await self.provider.aclose()
        # Built-in tools (WebSearch/WebFetch) hold a long-lived HTTP client;
        # close it when the agent shuts down. Safe to call when unused —
        # lazily-constructed.
        try:
            from .builtin_tools import aclose_builtin_clients
            await aclose_builtin_clients()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass

    # Async context manager sugar — `async with Agent(...) as a: ...`
    async def __aenter__(self) -> Agent:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _stream_with_fallback(self, messages: list[Message]) -> AsyncIterator[StreamEvent]:
        """Wrap the provider stream with one-shot model fallback. If the model
        call fails BEFORE producing any event (open/first-token failure) and a
        ``fallback_model`` is configured, switch to it and retry the turn. A
        failure *after* events have streamed is re-raised (can't safely retry
        partial output)."""
        produced = False
        try:
            async for ev in self._provider_stream(messages):
                produced = True
                yield ev
            return
        except Exception as err:  # noqa: BLE001
            if not (not produced and self.fallback_model and not self._fallback_used):
                raise
            self._activate_fallback(err)
        # Retry once on the fallback (outside the except so its own errors
        # propagate normally).
        async for ev in self._provider_stream(messages):
            yield ev

    def _activate_fallback(self, error: BaseException) -> None:
        _log.warning(
            "model %r failed (%r) before output; falling back to %r",
            self.model, error, self.fallback_model,
        )
        self._fallback_used = True
        self.model = self.fallback_model  # type: ignore[assignment]
        self.model_capability = lookup_model(self.model)
        self._env_context = None  # env block referenced the old model; let it rebuild

    def _provider_stream(self, messages: list[Message]) -> AsyncIterator[StreamEvent]:
        # Hoist the system prompt: prefer explicit Agent.system, else look at
        # messages[0] if it's a SystemMessage. Provider adapters expect system
        # as a top-level field (Anthropic, OpenAI, Ollama all do).
        system = self.system
        if system is None and messages and isinstance(messages[0], SystemMessage):
            sys_msg = messages[0]
            system = sys_msg.content if isinstance(sys_msg.content, str) else None

        assert self.provider is not None  # post-init guarantees this

        # Build the ``extra`` dict for the provider. We may layer a
        # ``response_format`` translation on top of the user-supplied extras.
        # Merge order: translator output FIRST, then user extras on top —
        # so an explicit ``Agent.extra={"response_format": {...}}`` overrides
        # the high-level ``response_format`` field. That's the escape hatch
        # for backends with quirky wire shapes the translator doesn't know
        # about yet. For nested dicts (``parameters``, ``options``) we
        # shallow-merge per inner key so we don't clobber e.g. a user-set
        # ``parameters.seed`` when the translator only emitted
        # ``parameters.grammar``.
        provider_extra: dict[str, Any] | None
        if self.response_format is not None:
            from .response_format import translate_response_format
            provider_name = getattr(self.provider, "name", "") or ""
            rf_extra = translate_response_format(
                self.response_format, provider_name
            )
            if self.extra:
                merged = dict(rf_extra)
                for k, v in self.extra.items():
                    existing = merged.get(k)
                    if isinstance(existing, dict) and isinstance(v, dict):
                        # Inner-key shallow merge: user wins on inner-key
                        # collisions but keeps any keys only set by the
                        # translator (e.g. ``parameters.grammar``).
                        merged[k] = {**existing, **v}
                    else:
                        merged[k] = v
                provider_extra = merged
            else:
                provider_extra = dict(rf_extra)
        else:
            provider_extra = self.extra

        # Pass the resolved capability through so the provider can pick the
        # right tool-use path (A/B/C) without re-doing lookup.
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "system": system,
            "tools": self.tools.to_wire() if self.tools else None,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "extra": provider_extra,
        }
        # Some legacy adapters don't accept model_capability; gate it.
        try:
            return self.provider.stream(model_capability=self.model_capability, **kwargs)
        except TypeError:
            return self.provider.stream(**kwargs)

    @staticmethod
    def _is_safe_compaction_point(messages: list[Message]) -> bool:
        """True when no tool_use is awaiting its tool_result. Compacting is only
        safe when the conversation does NOT end on an AssistantMessage carrying
        unmatched tool_use blocks — otherwise summarizing/dropping history
        orphans a tool_use and the next provider call 400s."""
        if not messages:
            return False
        last = messages[-1]
        if isinstance(last, AssistantMessage):
            return not any(isinstance(b, ToolUseBlock) for b in last.content)
        return True

    async def _summarize(self, prompt: str) -> str:
        """One-shot, tools-less summarization call for the compactor.

        Issues a bare provider stream (no ``tools[]`` so the summarizer can't be
        coerced into a tool call, no ``response_format``), drains text + usage,
        and bills the summary's tokens through the budget tracker (via
        ``add_usage`` only — never ``check()``, so compaction can't raise
        ``BudgetExceededError`` mid-summary). Returns "" on cancellation."""
        sig = self.cancellation_signal
        if sig is not None and sig.is_set():
            return ""
        assert self.provider is not None
        asm = _AssistantAssembler()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [UserMessage(content=prompt)],
            "system": (
                "You compress agent conversations faithfully and concisely. "
                "Respond with the summary text only — never call a tool."
            ),
            "tools": None,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "extra": None,
        }
        try:
            stream = self.provider.stream(model_capability=self.model_capability, **kwargs)
        except TypeError:
            stream = self.provider.stream(**kwargs)
        async for ev in stream:
            asm.feed(ev)
        msg = asm.finalize()
        if msg.usage is not None and self._budget_tracker is not None:
            self._budget_tracker.add_usage(
                msg.usage, self.model, backend_hint=self._provider_hint
            )
        return "".join(b.text for b in msg.content if isinstance(b, TextBlock))


# ---------------------------------------------------------------------------
# AssistantAssembler — turn event stream into AssistantMessage
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _BlockBuilder:
    """In-progress content block, mutated as deltas arrive."""

    kind: str  # "text" | "thinking" | "tool_use" | other
    # For text and thinking: accumulated chunks (joined once at stop).
    text_parts: list[str] = field(default_factory=list)
    # For thinking only: signature carried from start.
    signature: str | None = None
    # For tool_use: name + id from start, JSON deltas accumulated.
    tool_id: str = ""
    tool_name: str = ""
    tool_initial_input: dict[str, Any] | None = None
    json_parts: list[str] = field(default_factory=list)
    # Original block payload (for unknown / passthrough types).
    raw_block: ContentBlock | None = None

    def to_block(self) -> ContentBlock:
        if self.kind == "text":
            return TextBlock(text="".join(self.text_parts))
        if self.kind == "thinking":
            return ThinkingBlock(
                thinking="".join(self.text_parts),
                signature=self.signature,
            )
        if self.kind == "tool_use":
            if self.json_parts:
                try:
                    input_obj = _JSON_DECODER.decode("".join(self.json_parts))
                except msgspec.DecodeError as e:
                    raise StreamProtocolError(
                        f"tool_use {self.tool_name!r} sent malformed input JSON"
                    ) from e
            else:
                input_obj = self.tool_initial_input or {}
            return ToolUseBlock(id=self.tool_id, name=self.tool_name, input=input_obj)
        # Unknown / passthrough — return whatever the start event gave us.
        if self.raw_block is None:
            raise StreamProtocolError(f"no block payload for kind {self.kind!r}")
        return self.raw_block


class _AssistantAssembler:
    """Folds a stream of events into a single ``AssistantMessage``.

    Holds builders by block index, plus message-level metadata.
    """

    __slots__ = ("blocks", "stop_reason", "usage", "_seen_start")

    def __init__(self) -> None:
        self.blocks: dict[int, _BlockBuilder] = {}
        self.stop_reason: str | None = None
        self.usage: Usage | None = None
        self._seen_start = False

    def feed(self, ev: StreamEvent) -> None:
        if isinstance(ev, MessageStart):
            self._seen_start = True
            return
        if isinstance(ev, ContentBlockStart):
            self._on_block_start(ev)
            return
        if isinstance(ev, ContentBlockDelta):
            self._on_delta(ev)
            return
        if isinstance(ev, ContentBlockStop):
            # Builder stays as-is; we materialize at finalize().
            return
        if isinstance(ev, MessageDelta):
            if ev.stop_reason is not None:
                self.stop_reason = ev.stop_reason
            if ev.usage is not None:
                self.usage = _merge_usage(self.usage, ev.usage)
            return
        if isinstance(ev, MessageStop):
            return
        if isinstance(ev, ErrorEvent):
            raise StreamProtocolError(f"provider error event: {ev.message}")

    def finalize(self) -> AssistantMessage:
        if not self._seen_start:
            raise StreamProtocolError("stream ended without message_start")
        # Sorted by index so block order matches what the provider emitted.
        ordered = [self.blocks[i].to_block() for i in sorted(self.blocks)]
        return AssistantMessage(
            content=ordered,
            stop_reason=self.stop_reason,
            usage=self.usage,
        )

    # --- per-event handlers --------------------------------------------------

    def _on_block_start(self, ev: ContentBlockStart) -> None:
        block = ev.block
        if isinstance(block, TextBlock):
            self.blocks[ev.index] = _BlockBuilder(kind="text", text_parts=[block.text])
            return
        if isinstance(block, ThinkingBlock):
            self.blocks[ev.index] = _BlockBuilder(
                kind="thinking",
                text_parts=[block.thinking],
                signature=block.signature,
            )
            return
        if isinstance(block, ToolUseBlock):
            self.blocks[ev.index] = _BlockBuilder(
                kind="tool_use",
                tool_id=block.id,
                tool_name=block.name,
                tool_initial_input=dict(block.input) if block.input else None,
            )
            return
        # Unknown / passthrough — keep the raw block so finalize can return it.
        self.blocks[ev.index] = _BlockBuilder(kind="passthrough", raw_block=block)

    def _on_delta(self, ev: ContentBlockDelta) -> None:
        b = self.blocks.get(ev.index)
        if b is None:
            raise StreamProtocolError(
                f"delta for index {ev.index} before content_block_start"
            )
        d = ev.delta
        if isinstance(d, TextDelta):
            b.text_parts.append(d.text)
            return
        if isinstance(d, ThinkingDelta):
            b.text_parts.append(d.thinking)
            return
        if isinstance(d, InputJsonDelta):
            b.json_parts.append(d.partial_json)
            return
        # Unknown delta: ignore for forward-compat.


def _normalize_permission_decision(decision: Any) -> Any:
    """Bridge Claude-shape PermissionResultAllow/Deny → internal Allow/Deny.

    Users passing a ``can_use_tool`` callback via ``MantisAgentOptions``
    typically return Claude SDK's dataclasses (``PermissionResultAllow``
    /``PermissionResultDeny``). Our ``check_permission`` returns the
    internal msgspec ``Allow``/``Deny``/``Ask``. This normalizer makes
    the agent loop indifferent to which shape it got.

    Duck-types on ``.behavior`` so we don't have to import the Claude
    compat classes here and create a cycle.
    """

    if isinstance(decision, (Allow, Deny)):
        return decision
    behavior = getattr(decision, "behavior", None)
    if behavior == "allow":
        return Allow(updated_input=getattr(decision, "updated_input", None))
    if behavior == "deny":
        return Deny(reason=getattr(decision, "message", "denied"))
    return decision


def _merge_usage(prev: Usage | None, new: Usage) -> Usage:
    """Merge incremental usage updates. Output tokens accumulate; input tokens
    typically arrive once on message_start so we prefer the latest non-zero."""

    if prev is None:
        return new
    return Usage(
        input_tokens=new.input_tokens or prev.input_tokens,
        output_tokens=prev.output_tokens + new.output_tokens,
        cache_creation_input_tokens=new.cache_creation_input_tokens
        or prev.cache_creation_input_tokens,
        cache_read_input_tokens=new.cache_read_input_tokens or prev.cache_read_input_tokens,
    )
