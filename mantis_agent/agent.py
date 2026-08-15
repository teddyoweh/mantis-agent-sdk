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

import inspect
import logging
import os
import re
import time
import uuid as _uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import anyio
import msgspec

from .budget import Budget, BudgetTracker
from .capabilities import (
    BackendCapability,
    ModelCapability,
    hosted_profile_from_url,
    lookup_model,
)
from .errors import StreamProtocolError
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
from .hooks import HookContext, HookDispatcher, Hooks, _env_forces_fail_closed
from .permissions import (
    Allow,
    Deny,
    PermissionContext,
    check_permission,
    recheck_mutated_input,
)
from .compact import Compactor, SimpleCompactor
from .providers.base import Provider, detect_provider, resolve
from .streaming.executor import StreamingToolExecutor
from .tools import ToolRegistry
from .tracing import Span, Tracer, maybe_start_span
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
_DEFAULT_MAX_TOKENS = 1024  # the conservative field default; a caller who left this
#                                gets bumped to the model's output budget in __post_init__
# Marker stuffed into a tool_use block's input when the model's tool-call
# arguments were not parseable JSON (even leniently). The run loop detects it
# and returns an is_error tool_result asking the model to re-emit valid JSON,
# instead of crashing the whole run or executing the tool with garbage input.
_MALFORMED_TOOL_JSON_KEY = "__mantis_malformed_tool_json__"

# Default sampling temperature ceiling when the model has tools registered and
# the caller didn't pin one. Weak OSS models emit far more malformed/partial
# tool-call JSON at the table's default 0.7 than near-greedy; clamping the
# default down makes structured tool calls markedly more reliable without
# overriding an explicit user temperature.
_TOOL_TEMPERATURE_CAP = 0.2


def _rejects_default_temperature(provider: Any) -> bool:
    """True where sending an UNREQUESTED temperature is worse than sending none.

    The capability table's ``recommended_temperature`` is a default we invent,
    not something the user asked for. Anthropic's newer models reject an explicit
    temperature outright — ``400: `temperature` is deprecated for this model`` —
    so injecting one turns a perfectly good request into a failed one, on a value
    nobody chose.

    Scoped to the provider rather than a model list on purpose: a hardcoded set
    of model ids rots the moment a new one ships, and the failure mode is a hard
    400 on first use. An explicit ``--temperature`` is still honoured — the user
    then owns the outcome.
    """

    return type(provider).__name__ == "AnthropicPassthroughProvider"


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


def _looks_truncated(raw: Any) -> bool:
    """Did this tool-argument JSON get CUT OFF rather than written wrong?

    Truncation leaves a well-formed *prefix*: every structure that opened is
    still open at the end. A genuine syntax error (trailing comma, unquoted
    key, smart quotes) is balanced but wrong. The two need opposite advice —
    "write less" vs "fix your syntax" — so guessing costs the model a whole
    generation per retry, and it retries into the same wall.

    Walks the string once, tracking string state and escapes so a brace inside
    a quoted value doesn't count.
    """
    if not isinstance(raw, str) or not raw.strip():
        return False
    depth = 0
    in_string = False
    escaped = False
    for ch in raw:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth < 0:
                return False          # closed more than it opened: malformed
    # Ends inside a string, or with structures still open → cut off.
    return in_string or depth > 0


async def aclose_stream(stream: Any) -> None:
    """Finalize a ``run_iter`` generator in the task that was consuming it.

    ``run_iter`` deliberately holds the streaming tool executor's task group
    open ACROSS its yields, so a consumer can render "tool running…" while the
    tools drain. The cost is that the generator must not be left for the event
    loop to finalize: abandoning it (``break``, an exception, or Esc cancelling
    the consuming task) hands teardown to the asyncgen shutdown hook, which
    runs in a DIFFERENT task, and anyio answers that with

        RuntimeError: Attempted to exit cancel scope in a different task
                      than it was entered in

    raised straight into the event loop, killing the session. Closing here
    keeps the exit in the task that entered the scope.

    Shielded, because the common trigger IS cancellation: an unshielded await
    in an already-cancelled task re-raises before the cleanup can run.
    """
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    try:
        with anyio.CancelScope(shield=True):
            await aclose()
    except (RuntimeError, StopAsyncIteration):
        pass          # already finalized, or closed from elsewhere first
    except Exception:  # noqa: BLE001 — teardown must not mask the real error
        pass


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


_TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


def _retry_delay(
    err: BaseException, attempt: int, *, base: float = 0.5, cap: float = 8.0,
    retry_after_cap: float = 60.0,
) -> float:
    """How long to wait before the next retry. Honors a server-supplied
    ``Retry-After`` (on a RateLimitError) — capped so a huge value can't hang the
    agent — otherwise exponential backoff (``base * 2**attempt``, capped)."""
    ra = getattr(err, "retry_after_s", None)
    if isinstance(ra, (int, float)) and ra > 0:
        return min(float(ra), retry_after_cap)
    return min(base * (2 ** attempt), cap)


_OVERFLOW_MARKERS = (
    "context length", "context window", "maximum context", "too many tokens",
    "context_length_exceeded", "reduce the length", "prompt is too long",
    "input is too long", "maximum context length", "input tokens exceed",
    "configured limit of",
)


def _is_context_overflow(err: BaseException) -> bool:
    """Whether a failure is the model rejecting an over-long prompt — which we can
    recover from by compacting and retrying, rather than failing the turn."""
    low = str(err).lower()
    return any(p in low for p in _OVERFLOW_MARKERS)


def _missing_tool_result(call: ToolUseBlock) -> ToolResultBlock:
    return ToolResultBlock(
        tool_use_id=call.id,
        content=f"tool result missing for {call.name}; previous turn was interrupted",
        is_error=True,
    )


def _repair_tool_call_history(messages: list[Message]) -> list[Message]:
    repaired: list[Message] = []
    pending: list[ToolUseBlock] = []
    for msg in messages:
        if pending:
            if isinstance(msg, UserMessage) and isinstance(msg.content, list):
                result_ids = {
                    b.tool_use_id for b in msg.content if isinstance(b, ToolResultBlock)
                }
                missing = [call for call in pending if call.id not in result_ids]
                pending = []
                if missing:
                    # Keep EVERY real block in this message (text + valid
                    # tool_results) and synthesize an error result ONLY for the
                    # genuinely orphaned tool_uses — appended into the SAME
                    # message so all results directly follow the assistant's
                    # tool_use call. Emitting the synthetic results as a separate
                    # preceding message (the old behavior) stranded the real
                    # results in a message that no longer immediately followed the
                    # call, which providers reject — losing valid results and
                    # fabricating failures.
                    repaired.append(msgspec.structs.replace(
                        msg,
                        content=[
                            *msg.content,
                            *(_missing_tool_result(call) for call in missing),
                        ],
                    ))
                else:
                    repaired.append(msg)
                continue
            repaired.append(UserMessage(
                content=[_missing_tool_result(call) for call in pending],
                isMeta=True,
            ))
            pending = []
        if isinstance(msg, UserMessage) and isinstance(msg.content, list):
            cleaned = [b for b in msg.content if not isinstance(b, ToolResultBlock)]
            if len(cleaned) != len(msg.content):
                if cleaned:
                    repaired.append(UserMessage(content=cleaned, isMeta=msg.isMeta))
                continue
        repaired.append(msg)
        if isinstance(msg, AssistantMessage):
            pending = [b for b in msg.content if isinstance(b, ToolUseBlock)]
    if pending:
        repaired.append(UserMessage(
            content=[_missing_tool_result(call) for call in pending],
            isMeta=True,
        ))
    return repaired


def _is_transient(err: BaseException) -> bool:
    """Whether a provider failure is worth retrying: rate limits, 5xx / overload,
    and transport blips (connection reset, read timeout). Auth failures and
    client (4xx other than throttle) errors are NOT retried — they won't fix
    themselves."""
    from .errors import AuthError, ProviderError, RateLimitError  # noqa: PLC0415

    if isinstance(err, AuthError):
        return False
    if isinstance(err, RateLimitError):
        return True
    # Malformed / incomplete stream — a cold-start / scaledown / proxy blip on
    # serverless GPU backends: a 2xx body closed before any event ("without
    # message_start"), an empty turn ("no content blocks"), or one cut off
    # mid-block ("truncated response"). No complete output was produced, so a
    # retry is safe and usually succeeds once the container is warm.
    if isinstance(err, StreamProtocolError):
        low = str(err)
        if (
            "without message_start" in low
            or "no content blocks" in low
            or "truncated response" in low
        ):
            return True
    if isinstance(err, ProviderError):
        return err.status_code in _TRANSIENT_STATUS
    try:
        import httpx  # noqa: PLC0415
        return isinstance(err, httpx.TransportError)  # Connect/Read/Timeout/Protocol
    except ImportError:
        return False


def close_open_tool_calls(
    messages: list[Message], *, note: str = "[interrupted by user]"
) -> int:
    """Ensure every assistant ``tool_use`` is answered by a ``tool_result`` in the
    IMMEDIATELY following user message — inserting synthetic ``[interrupted]``
    results in the correct position when the tools never ran. Keeps the history
    well-formed (providers require every tool_use be answered, right after it)
    WITHOUT discarding the work already done. Returns how many results were added.

    Position-aware, so it heals a malformed tail from any source: a cancelled
    turn, a session saved mid-tool then resumed, or a hand-built message list —
    including the case where a new user message was already appended after the
    open tool_use (the result is slotted BETWEEN them, not tacked on the end).
    Idempotent.
    """
    added = 0
    i = 0
    while i < len(messages):
        m = messages[i]
        content = getattr(m, "content", None)
        if getattr(m, "role", "") == "assistant" and isinstance(content, list):
            use_ids = [b.id for b in content if isinstance(b, ToolUseBlock)]
            if use_ids:
                nxt = messages[i + 1] if i + 1 < len(messages) else None
                nxt_content = getattr(nxt, "content", None) if nxt is not None else None
                answered = (
                    {b.tool_use_id for b in nxt_content if isinstance(b, ToolResultBlock)}
                    if isinstance(nxt_content, list) else set()
                )
                missing = [tid for tid in use_ids if tid not in answered]
                if missing:
                    results: list[ContentBlock] = [
                        ToolResultBlock(tool_use_id=tid, content=note, is_error=True)
                        for tid in missing
                    ]
                    if isinstance(nxt_content, list) and answered:
                        # partial results already there → augment that message
                        messages[i + 1] = msgspec.structs.replace(
                            nxt, content=[*nxt_content, *results]
                        )
                    else:
                        messages.insert(i + 1, UserMessage(content=results))
                    added += len(missing)
        i += 1
    return added


def _final_turn_reminder(reason: str = "turn limit") -> "UserMessage":
    """A one-shot reminder injected as a run approaches a hard stop (its turn
    limit or budget) so it ends with a summary instead of a half-finished tool
    call. ``reason`` names the limit for the model."""
    from .system_reminder import wrap_system_reminder  # noqa: PLC0415

    return UserMessage(
        content=wrap_system_reminder(
            f"You are reaching your {reason} — wrap up NOW. Do not start new tool "
            "calls you can't finish. Instead, give the user a concise summary of "
            "what you accomplished, what's still left, and the clear next step, so "
            "they can pick up from here."
        ),
        isMeta=True,
    )


# ---------------------------------------------------------------------------
# Persistence / completion-contract knobs
# ---------------------------------------------------------------------------
#
# Mirrors Claude Code's gated-stop design (checkTokenBudget / queryLoop): a
# no-tool-use turn is NOT automatically the end of the run. When there is a
# real unfinished-work signal (open todos or an unmet spend target) and
# progress isn't diminishing, persist mode nudges the model to keep going —
# under a hard cap so "persistence on by default" can never become "never
# stop". The gate lives in ``Agent._should_continue_at_natural_stop``.

# Hard ceiling on how many times one run may re-drive a natural stop.
_MAX_CONTINUATIONS = 8
# ``>=`` this many consecutive near-zero-progress continuations => allow stop.
_DIMINISHING_RETURNS_STREAK = 2
# All-error tool turns in a row before persist mode forces a REPLAN.
_REPLAN_AT_ERROR_STREAK = 3
# How many times a persist-mode run may extend past ``max_steps`` when budget
# runway remains and work is unfinished.
_MAX_STEP_EXTENSIONS = 2

# effort -> universal thinking config. "medium" maps to None (provider
# default) so a default agent's requests are byte-for-byte unchanged; only an
# explicit non-medium effort, an explicit ``Agent.thinking``, a keyword
# escalation, or a failure streak sends a thinking block.
_EFFORT_LADDER: tuple[str, ...] = ("low", "medium", "high", "max")
_EFFORT_TO_THINKING: dict[str, dict[str, Any] | None] = {
    "low": {"type": "enabled", "budget_tokens": 2048},
    "medium": None,
    "high": {"type": "enabled", "budget_tokens": 12288},
    "max": {"type": "enabled", "budget_tokens": 24576},
}
_ULTRATHINK_KEYWORDS = ("ultrathink",)
_THINK_HARD_KEYWORDS = (
    "think harder", "think hard", "think more", "think deeply",
    "think step by step", "think longer",
)


def _persist_nudge() -> "UserMessage":
    """Meta nudge appended when persist mode continues a natural stop: keep the
    model working instead of prematurely summarizing, while making the stop
    condition explicit so a genuinely-finished task still ends."""
    from .system_reminder import wrap_system_reminder  # noqa: PLC0415

    return UserMessage(
        content=wrap_system_reminder(
            "Keep working — do not summarize. There is still open work (unchecked "
            "todos or an unmet target). Take the next concrete action now. If the "
            "task is truly complete, say so explicitly and stop."
        ),
        isMeta=True,
    )


def _replan_nudge() -> "UserMessage":
    """Meta message injected after a tool-error streak (persist mode): step back
    and rethink rather than repeating the failing approach. Mirrors the spirit
    of ``tui.goal_replan_prompt`` without needing a goal string."""
    from .system_reminder import wrap_system_reminder  # noqa: PLC0415

    return UserMessage(
        content=wrap_system_reminder(
            "[replan] Repeated tool failures. STOP and step back: what is actually "
            "failing, and why? Discard the stalled plan and re-decompose the task "
            "into concrete, independently checkable steps, then try a DIFFERENT "
            "method — do not repeat the calls that just failed."
        ),
        isMeta=True,
    )


def _remaining_work_summary(todos: list[dict[str, Any]] | None) -> "UserMessage":
    """A structured "what's left" note emitted when a persist run ends with the
    step budget exhausted but work still open, so the caller/next session can
    pick up cleanly instead of getting a silent max-steps cutoff."""
    from .system_reminder import wrap_system_reminder  # noqa: PLC0415

    open_items = [
        t for t in (todos or []) if str(t.get("status", "pending")) != "completed"
    ]
    lines = [
        "Step budget exhausted before the task was complete. Remaining open work:",
    ]
    if open_items:
        for t in open_items:
            lines.append(f"- [ ] {t.get('content', '')}")
    else:
        lines.append("- (no open todos, but a spend target was still unmet)")
    lines.append("Resume from here to finish it.")
    return UserMessage(content=wrap_system_reminder("\n".join(lines)), isMeta=True)


_SHELL_FENCE_LANGS = {"bash", "sh", "shell", "zsh", "console", "shellsession"}
_FENCE_RE = re.compile(r"```([a-zA-Z]*)[ \t]*\n(.*?)```", re.DOTALL)
# Llama-family emit ``<function=NAME>{json args}</function>`` (or with a colon /
# ``<function_call name="NAME">``) instead of the OpenAI/Anthropic tool-call shape.
_FUNC_TAG_RE = re.compile(
    r"<function(?:_call)?[=:\s]+(?:name\s*=\s*[\"']?)?([A-Za-z_][\w.-]*)[\"']?\s*>(.*?)</function(?:_call)?\s*>",
    re.DOTALL | re.IGNORECASE,
)


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

    # 0. Llama-style <function=NAME>{json}</function>. Resolve the name (tolerant
    #    of Claude-name/case drift) so the salvaged call maps to a real tool.
    for m in _FUNC_TAG_RE.finditer(s):
        t = registry.resolve(m.group(1))
        if t is None:
            continue
        body = m.group(2).strip()
        args = _loads_lenient(body) if body else {}
        if isinstance(args, dict):
            calls.append(_mk(t.name, args))
    if calls:
        return calls

    # 1. JSON tool-call object(s), tolerant of the malformed JSON small models
    #    emit. Accept both "arguments" (OpenAI/Anthropic) and "parameters" (the
    #    shape llama3.x tends to print). Resolve names for Claude-name/case drift.
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
        t = registry.resolve(name) if isinstance(name, str) else None
        if t is not None and isinstance(args, dict):
            calls.append(_mk(t.name, args))
    if calls:
        return calls

    # 2. Shell code fences -> bash(command=...). Only explicit shell langs, so we
    #    never mistake an output dump or a python snippet for a command to run.
    #    AND only when the message is essentially *just* the fence(s): a model
    #    answering in prose with an illustrative ```bash``` snippet is NOT asking
    #    to run it, so salvaging that would execute a command it merely described.
    #    Two fail-closed gates: (a) little surrounding prose overall, and
    #    (b) NO prose *before* the first fence — a lead-in like "To fix this,
    #    run:" is a description of the command, not a request to execute it, so
    #    a genuine text-channel tool call must open with the fence itself.
    non_fence = _FENCE_RE.sub("", s).strip()
    first = _FENCE_RE.search(s)
    leading = s[: first.start()].strip() if first is not None else s.strip()
    if registry.get("bash") is not None and len(non_fence) <= 40 and not leading:
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


@dataclass
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
    #: Accepted alias for ``backend``. Every OpenAI-compatible SDK on earth
    #: calls this ``base_url``, and both doc trees documented it as an option
    #: long before anything read it, so honor the name rather than correcting
    #: the reader. Folded into ``backend`` in ``__post_init__``; passing both
    #: with different values is an error rather than a silent precedence rule.
    base_url: str | None = None
    #: Explicit provider credential. ``None`` means "discover from the
    #: environment" (``MANTIS_AGENT_API_KEY``, then the provider-specific
    #: chain in each adapter); ``""`` means "send no auth at all". Kept out of
    #: ``repr`` so a stray ``print(agent)`` can't leak a key into a log.
    api_key: str | None = field(default=None, repr=False)
    #: Working directory for the built-in file and shell tools. Relative paths
    #: the model emits resolve against this, and ``bash`` starts here.
    #:
    #: This value is already reported to the model in its env context block
    #: ("Working directory: …"), so before it scoped the tools, the model was
    #: told one directory while ``write_file`` used the host process's — files
    #: landed outside the intended tree and the agent's own ``ls`` disagreed
    #: with its own writes. ``None`` keeps the process cwd, unchanged.
    cwd: str | None = None
    provider: Provider | None = None
    system: str | None = None
    tools: ToolRegistry | list = field(default_factory=ToolRegistry)
    max_tokens: int = 1024  # conservative default; __post_init__ raises it to the
    # model's max_output_tokens (≤8192) so large writes/edits don't truncate
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
    # Persistence — the completion contract. When True (default), a no-tool-use
    # turn is not automatically the end of the run: if there is a real
    # unfinished-work signal (open todos or an unmet ``Budget`` spend target)
    # and progress isn't diminishing, the loop nudges the model to keep going,
    # under ``_MAX_CONTINUATIONS``. It ALWAYS stops on a final answer with no
    # open work, on diminishing returns, at the hard cap, or on budget/step
    # ceilings — so a plain ``query()`` with no todos and no target behaves
    # exactly as it did before persistence existed. Set False to restore the
    # strict "stop at the first natural stop" behavior.
    persist: bool = True
    # Reasoning effort — "low"|"medium"|"high"|"max". Derives the adaptive
    # thinking config passed to providers each turn (see ``thinking``). "medium"
    # is the neutral default: it maps to the provider default (no thinking
    # block), so a default agent's requests are unchanged.
    effort: str = "medium"
    # Explicit thinking config override — a dict
    # ``{"type": "adaptive"|"enabled"|"disabled", "budget_tokens": int|None}``.
    # ``None`` (default) derives the config from ``effort`` + per-turn
    # escalation (an "ultrathink"/"think harder" keyword in the user's message,
    # or a tool-error / repeated-call streak). When set, it's the base the
    # escalation ladder builds on.
    thinking: dict[str, Any] | None = None
    # Retry a model call that fails with a TRANSIENT error (rate limit, 5xx,
    # connection blip) BEFORE any output, with exponential backoff, this many
    # times before falling back / raising. 0 disables. Non-transient errors
    # (auth, 4xx) are never retried.
    max_retries: int = 2
    extra: dict[str, Any] | None = None

    # Capability + safety surface (M0.1 / M2)
    model_capability: ModelCapability | None = None
    backend_capability: BackendCapability | None = None

    # Safety + budget knobs — None means "no enforcement".
    hooks: Hooks | None = None
    # What a *crashing* veto hook means. Default False = today's behaviour: a
    # PreToolUse guard that raises is logged and IGNORED, and the tool call it
    # existed to deny proceeds (fail-open). True turns "the guard did not
    # answer" into a denial for the events in ``hooks.BLOCKING_EVENTS``.
    # ``MANTIS_HOOKS_FAIL_CLOSED=1`` forces it on for operators who want the
    # safe behaviour without touching code; it never turns an explicit
    # ``hooks_fail_closed=True`` back off.
    hooks_fail_closed: bool = False
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
    # Original model/capability, captured the first time we fall back so a later
    # run can restore them — fallback is scoped per-run, not a permanent downgrade.
    _primary_model: str | None = field(default=None, init=False)
    _primary_model_capability: ModelCapability | None = field(default=None, init=False)
    _refusal_retried: bool = field(default=False, init=False)
    _budget_wrapup_done: bool = field(default=False, init=False)
    # Absolute paths of memory files already surfaced this session, so recall
    # doesn't re-inject the same note every turn.
    _surfaced: set[str] = field(default_factory=set, init=False)
    # Path-scoped conditional rules already injected this session (dedup by path).
    _rules_surfaced: set[str] = field(default_factory=set, init=False)
    # Auto-loaded skill bodies already injected this session (dedup by skill name).
    _skills_surfaced: set[str] = field(default_factory=set, init=False)
    # Which SKILL.md files this agent may use.
    #   None    — off (the default): a library caller does not inherit skills
    #             from the developer's home directory.
    #   "auto"  — discover + inject the ones matching each turn (what the
    #             mantis terminal uses).
    #   "all"   — every discovered skill.
    #   [names] — exactly these.
    skills: list[str] | str | None = None
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
    # Persistence / escalation counters — all reset at run start.
    # How many times persist mode has re-driven a natural stop this run.
    _continuation_count: int = field(default=0, init=False)
    # Consecutive near-zero-progress continuations (diminishing-returns guard).
    _near_zero_streak: int = field(default=0, init=False)
    # Progress marker (completed-todo count) at the last natural-stop check.
    _last_progress: int = field(default=0, init=False)
    # Consecutive all-error tool turns — drives thinking escalation + replan.
    _tool_error_streak: int = field(default=0, init=False)
    # Set once the repeated-call guard trips this run — escalates thinking.
    _repeat_tripped: bool = field(default=False, init=False)
    # How many times the step budget was extended in persist mode this run.
    _step_extensions: int = field(default=0, init=False)
    # Unique per-agent key for isolating process-global tool state (bash cwd,
    # background shells, read-before-write guard) so a subagent's ``cd`` /
    # shells / reads never bleed into a concurrently-running parent or sibling.
    # See ``builtin_tools.fs.TOOL_SCOPE``. Set on the ContextVar for the run.
    _tool_scope: str = field(
        default_factory=lambda: f"agent-{_uuid.uuid4().hex}", init=False
    )
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

        # base_url is an alias for backend, resolved before any provider is
        # built. Conflicting values raise: quietly preferring one would send
        # requests to a URL the caller can see they didn't ask for, which is
        # the exact failure mode this alias exists to end.
        if self.base_url is not None:
            if self.backend is not None and self.backend != self.base_url:
                raise ValueError(
                    "Agent(backend=..., base_url=...) got two different URLs "
                    f"({self.backend!r} vs {self.base_url!r}) — they are aliases "
                    "for the same thing, so pass only one."
                )
            self.backend = self.base_url
        else:
            self.base_url = self.backend

        # Resolve model capability if not given explicitly.
        if self.model_capability is None:
            self.model_capability = lookup_model(self.model)

        # Give the model its full output budget by default. 1024 tokens (~4k
        # chars, ~100 lines) truncates a large file write/edit mid-output; when
        # the caller leaves the conservative 1024 default and the model can do
        # more, use its ``max_output_tokens`` (capped at 8192 to stay sane). An
        # explicitly-higher ``max_tokens`` is always respected, but never let the
        # completion reservation eat half+ of a small context window.
        cap = self.model_capability
        default_max_tokens = self.max_tokens == _DEFAULT_MAX_TOKENS
        if cap is not None:
            model_max = getattr(cap, "max_output_tokens", 0) or 0
            ctx_window = getattr(cap, "context_window", 0) or 0
            if default_max_tokens and model_max > 0:
                self.max_tokens = min(model_max, 8192)
            if default_max_tokens and ctx_window > 0:
                self.max_tokens = min(self.max_tokens, max(512, ctx_window // 4))

        # Resolve backend capability if not given explicitly.
        if self.backend_capability is None and self.backend:
            self.backend_capability = hosted_profile_from_url(self.backend)

        # Build the provider if not given.
        if self.provider is None:
            backend_str = self.backend or self.model
            backend_kind = detect_provider(backend_str)
            ProviderCls = resolve(backend_kind)
            self.provider = self._build_provider(ProviderCls, backend_kind)

        # Propagate temperature from capability if user didn't set one. When
        # tools are registered, clamp the default down for tool-call reliability
        # (see ``_TOOL_TEMPERATURE_CAP``) — never overrides an explicit value.
        if self.temperature is None and not _rejects_default_temperature(self.provider):
            rec = self.model_capability.recommended_temperature
            if self.tools:
                rec = min(rec, _TOOL_TEMPERATURE_CAP)
            self.temperature = rec

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

        # Wire safety surface. The env var is an escalation switch only —
        # it promotes fail-open to fail-closed, never the reverse.
        if not self.hooks_fail_closed and _env_forces_fail_closed():
            self.hooks_fail_closed = True
        self._dispatcher = HookDispatcher(
            self.hooks or Hooks(), fail_closed=self.hooks_fail_closed
        )

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

    # ------------------------------------------------------------------
    # Persistence / completion contract
    # ------------------------------------------------------------------

    def _progress_value(self) -> int:
        """The observable progress signal the diminishing-returns guard tracks:
        the number of completed todos. A run with no todos has a constant 0,
        which is exactly why a no-todo natural stop is never continued (the
        unfinished-work gate returns False first)."""
        return sum(
            1 for t in (self.todos or [])
            if str(t.get("status", "pending")) == "completed"
        )

    def _has_unfinished_work(self) -> bool:
        """A REAL unfinished-work signal: an open (non-completed) todo, or a
        spend FLOOR (``Budget.target_*``) the run hasn't reached yet. This is
        the hard precondition for continuing a natural stop — no signal means
        the model's final answer is the end, exactly as before persistence."""
        if self.todos and any(
            str(t.get("status", "pending")) != "completed" for t in self.todos
        ):
            return True
        if self._budget_tracker is not None and self._budget_tracker.target_unmet():
            return True
        return False

    def _has_runway(self) -> bool:
        """Whether any configured budget ceiling still has room. No budget =>
        unbounded runway. Exhausted (runway 0.0) => brake."""
        if self._budget_tracker is None:
            return True
        r = self._budget_tracker.runway()
        return r is None or r > 0.0

    def _should_continue_at_natural_stop(self) -> bool:
        """The gate at the heart of the completion contract. Returns True only
        when persist mode should re-drive a no-tool-use turn instead of ending.

        CONTINUES iff ALL hold: persist is on; there is a real unfinished-work
        signal; the hard continuation cap isn't hit; budget runway remains; and
        progress is not diminishing. Otherwise STOPS. Mutates the
        diminishing-returns streak, so call it exactly once per natural stop.
        """
        if not self.persist:
            return False
        if not self._has_unfinished_work():
            return False
        if self._continuation_count >= _MAX_CONTINUATIONS:
            return False
        if not self._has_runway():
            return False
        # Diminishing returns: did completed-todo count advance since the last
        # natural stop? Two consecutive near-zero-progress stalls => give up.
        prog = self._progress_value()
        if prog > self._last_progress:
            self._near_zero_streak = 0
        else:
            self._near_zero_streak += 1
        self._last_progress = prog
        if self._near_zero_streak >= _DIMINISHING_RETURNS_STREAK:
            return False
        return True

    def _escalate_effort(self, effort: str, steps: int) -> str:
        """Bump ``effort`` up the ladder by ``steps``, clamped to the top."""
        try:
            i = _EFFORT_LADDER.index(effort)
        except ValueError:
            i = _EFFORT_LADDER.index("medium")
        return _EFFORT_LADDER[min(len(_EFFORT_LADDER) - 1, i + max(0, steps))]

    def _thinking_config_for_turn(
        self, messages: list[Message]
    ) -> dict[str, Any] | None:
        """Resolve the adaptive thinking config for THIS turn from ``effort`` +
        per-turn escalation. Escalates on an "ultrathink"/"think harder" keyword
        in the latest user message, and on a tool-error / repeated-call streak.
        Returns a ``{"type", "budget_tokens"}`` dict, or ``None`` for the
        provider default (which is what a default medium-effort agent yields —
        keeping its requests unchanged)."""
        base = (self.effort or "medium").lower()
        if base not in _EFFORT_LADDER:
            base = "medium"

        text = self._latest_user_text(messages).lower()
        escalated = base
        if any(k in text for k in _ULTRATHINK_KEYWORDS):
            escalated = "max"
        elif any(k in text for k in _THINK_HARD_KEYWORDS):
            # At least "high" for an explicit "think harder".
            if _EFFORT_LADDER.index(escalated) < _EFFORT_LADDER.index("high"):
                escalated = "high"

        bump = (1 if self._tool_error_streak >= 2 else 0) + (
            1 if self._repeat_tripped else 0
        )
        if bump:
            escalated = self._escalate_effort(escalated, bump)

        # An explicit ``thinking`` override is the base config when nothing
        # escalated us past the configured effort; otherwise the escalated
        # effort's mapping wins so failures actually deepen reasoning.
        if self.thinking is not None and escalated == base:
            return dict(self.thinking)
        cfg = _EFFORT_TO_THINKING.get(escalated)
        if cfg is None and self.thinking is not None:
            return dict(self.thinking)
        return dict(cfg) if cfg is not None else None

    def _agent_cwd(self) -> str:
        """The agent's effective working directory — the directory its bash tool
        is currently in (tracked per-agent under ``self._tool_scope`` as it
        ``cd``s around), falling back to the process cwd before any ``cd``.

        Path-scoped features (conditional rule discovery) must resolve against
        where THIS agent is working, not ``os.getcwd()``: a subagent — or a
        reused session that already ``cd``'d elsewhere — has an agent cwd that
        diverges from the process cwd, so using the process cwd matches/misses
        rules against the wrong tree."""
        try:
            from .builtin_tools.fs import _BASH_CWD_BY_SCOPE  # noqa: PLC0415
            tracked = _BASH_CWD_BY_SCOPE.get(self._tool_scope, {}).get("cwd")
            if tracked:
                return tracked
        except Exception:  # noqa: BLE001 — never let cwd resolution break a turn
            pass
        return os.getcwd()

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
                    self._env_context = render_environment_context(
                        model=self.model,
                        backend=getattr(self.provider, "name", None) or self.backend,
                    ).strip()
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
                all_skills = discover_skills()
                if self.skills == "all":
                    catalog_skills = all_skills
                elif self.skills == "auto":
                    catalog_skills = all_skills
                elif isinstance(self.skills, list):
                    wanted = set(self.skills)
                    catalog_skills = [s for s in all_skills if s.name in wanted]
                else:
                    # ``None`` means OFF. A library caller's agent should not
                    # silently inherit whatever SKILL.md files happen to sit in
                    # the developer's home directory — that made behavior depend
                    # on the machine it ran on. The terminal opts in with
                    # ``skills="auto"``.
                    catalog_skills = []
                catalog = render_skill_catalog(catalog_skills).strip()
                if catalog:
                    ctx["skills"] = catalog
                    registry: ToolRegistry = self.tools  # type: ignore[assignment]
                    if registry.get("load_skill") is None:
                        from .builtin_tools.skill_tool import load_skill
                        registry.add(load_skill)
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
            # Explicit api_key wins over the environment; ``None`` leaves the
            # adapter's own env chain (MANTIS_AGENT_API_KEY, then
            # OPENAI_API_KEY / TOGETHER_API_KEY / … ) in charge, and ``""``
            # deliberately sends no auth at all.
            kw["api_key"] = (
                self.api_key if self.api_key is not None
                else os.environ.get("MANTIS_AGENT_API_KEY")
            )
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
            # With no explicit key, api_key is read from $ANTHROPIC_API_KEY /
            # $ANTHROPIC_AUTH_TOKEN inside the provider — surfacing it here
            # too would shadow that resolution path.
        elif backend_kind == "mock":
            pass  # mock takes its own kwargs from `extra`

        # An explicit key is honored on every adapter that has somewhere to
        # put it, not just openai_compat — the caller said "use this key",
        # and silently dropping it is how the docs came to describe an option
        # that did nothing.
        if self.api_key is not None:
            kw.setdefault("api_key", self.api_key)

        # Drop kwargs this adapter doesn't take, rather than losing *all* of
        # them to a blanket TypeError fallback (which is how a real base_url
        # could vanish and leave the provider pointing at its own default).
        try:
            accepted = {
                p.name
                for p in inspect.signature(ProviderCls).parameters.values()
                if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
            }
        except (TypeError, ValueError):
            accepted = set()
        if accepted:
            kw = {k: v for k, v in kw.items() if k in accepted}
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

        # Self-heal a malformed tail before doing anything: if the history ends
        # with an assistant tool_use that was never answered (a prior run was
        # cancelled mid-tool, or a session was saved mid-turn and resumed), close
        # it with a synthetic result so this run's very first provider request is
        # well-formed instead of erroring.
        close_open_tool_calls(messages, note="[previous turn interrupted]")

        # UserPromptSubmit hook — fires once as the user's turn begins, BEFORE any
        # model call. A hook may inject extra context (its ``note``, wrapped as a
        # system-reminder) or BLOCK the prompt entirely (``block=True``) — a
        # guardrail integrators asked for. No hook configured → skipped. This runs
        # before the run span opens, so a block returns cleanly.
        if self._dispatcher.has("UserPromptSubmit"):
            ups = await self._dispatcher.dispatch(
                "UserPromptSubmit",
                HookContext(event="UserPromptSubmit", messages_snapshot=messages),
            )
            if ups.block:
                _log.info("prompt blocked by UserPromptSubmit hook: %s", ups.note)
                if ups.note:
                    blocked = AssistantMessage(content=[TextBlock(text=ups.note)])
                    messages.append(blocked)
                    yield blocked
                return
            if ups.note:
                from .system_reminder import wrap_system_reminder  # noqa: PLC0415
                extra = UserMessage(content=wrap_system_reminder(ups.note), isMeta=True)
                messages.append(extra)
                yield extra

        # Inject persistent user-context (memory + custom) as a synthetic
        # ``<system-reminder>``-wrapped UserMessage at the head of the
        # conversation. Matches Claude SDK 1:1. No-op when context is
        # empty. We do this once per run_iter() call; subsequent turns
        # reuse the already-injected message. Yield it so streaming-mode
        # consumers can see what context the agent saw (and skip it via
        # the ``isMeta`` flag if they're rendering to a UI).
        if not self._has_user_context_message(messages):
            # _build_user_context shells out to git and scans disk (memory,
            # skills, rules); run it in a worker thread so the first turn doesn't
            # block the shared event loop (freezing background jobs / sibling
            # subagents) for the up-to-several-seconds it can take.
            import anyio  # noqa: PLC0415
            user_ctx = await anyio.to_thread.run_sync(self._build_user_context)
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
        query = self._latest_user_text(messages)
        if self.include_recall and os.environ.get("MANTIS_AGENT_NO_CONTEXT") != "1":
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

        # Skills auto-relevance — the catalog stays in the stable context head,
        # but matching skill bodies ride the current turn like Claude Code's
        # progressive disclosure. Explicit Claude-SDK-style skills preload their
        # bodies here; "all" loads every discovered skill. Dedup by skill name
        # across the session so a long task does not keep re-paying.
        if query and os.environ.get("MANTIS_AGENT_NO_CONTEXT") != "1":
            try:
                from .skills import discover_skills, match_skills, render_relevant_skills

                all_skills = discover_skills()
                if self.skills == "all":
                    selected = all_skills
                elif self.skills == "auto":
                    selected = match_skills(query, all_skills)
                elif isinstance(self.skills, list):
                    wanted = set(self.skills)
                    selected = [s for s in all_skills if s.name in wanted]
                else:
                    selected = []  # ``None`` means off — see the catalog site.
                matches = [s for s in selected if s.name not in self._skills_surfaced]
                if matches:
                    self._skills_surfaced.update(s.name for s in matches)
                    skill_msg = UserMessage(
                        content=render_relevant_skills(matches),
                        isMeta=True,
                    )
                    messages.append(skill_msg)
                    yield skill_msg
            except Exception:  # noqa: BLE001 — broken SKILL.md should not break turns
                _log.debug("skill auto-relevance skipped", exc_info=True)

        # Path-scoped conditional rules — inject a ``.mantis/rules/*.md`` rule
        # only when a file matching its globs is active in the conversation (an
        # @-mention or a file just read/edited). Deduped by path across the
        # session. Keeps project instructions lean: SQL rules ride only SQL work.
        if os.environ.get("MANTIS_AGENT_NO_CONTEXT") != "1":
            try:
                from .rules import (
                    active_files_from_messages,
                    discover_conditional_rules,
                    render_rules_reminder,
                    select_matching_rules,
                )
                all_rules = discover_conditional_rules(self._agent_cwd())
                if all_rules:
                    active = active_files_from_messages(messages)
                    fresh = [
                        (p, body) for p, body in select_matching_rules(all_rules, active)
                        if str(p) not in self._rules_surfaced
                    ]
                    if fresh:
                        self._rules_surfaced.update(str(p) for p, _ in fresh)
                        rules_msg = UserMessage(
                            content=render_rules_reminder([b for _, b in fresh]),
                            isMeta=True,
                        )
                        messages.append(rules_msg)
                        yield rules_msg
            except Exception:  # noqa: BLE001 — rules are best-effort
                _log.debug("conditional rules skipped", exc_info=True)

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

        def _close_run_span(error: BaseException | None = None) -> None:
            # Idempotent: end_ns is set once ended, so a belated call from the
            # finally-guard after a normal-exit close is a no-op (no double-append).
            if run_span is None or self.tracer is None or run_span.end_ns is not None:
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

        def _close_span(span: Span | None, error: BaseException | None = None) -> None:
            """End + file a turn/llm span into the finished list. Idempotent —
            skips a span that was already closed on a normal path."""
            if span is None or self.tracer is None or span.end_ns is not None:
                return
            if error is not None:
                span.end(status="error", exception=error)
            else:
                span.end()
            mirror = getattr(self.tracer, "_mirror", None) or self.tracer
            close_fn = getattr(mirror, "_close", None)
            if callable(close_fn):
                close_fn(span)

        # Auto-compaction bookkeeping: the most recent turn's reported usage
        # (drives should_compact) and a per-run cap on how many times we
        # summarize — the circuit breaker against a summary that itself stays
        # over threshold and would otherwise re-compact every turn.
        last_usage: Usage | None = None
        compactions = 0
        _MAX_COMPACTIONS = 5
        self._refusal_retried = False
        self._budget_wrapup_done = False
        # Reset persistence / escalation counters for this run.
        self._continuation_count = 0
        self._near_zero_streak = 0
        self._last_progress = self._progress_value()
        self._tool_error_streak = 0
        self._repeat_tripped = False
        self._step_extensions = 0
        # Fallback is per-run: if a prior run fell back to the fallback model,
        # restore the primary here so a recovered backend is used again (and a
        # fresh fallback is available), instead of staying permanently downgraded.
        if self._fallback_used and self._primary_model is not None:
            self.model = self._primary_model
            self.model_capability = self._primary_model_capability
            self._env_context = None  # env block referenced the fallback model
        self._fallback_used = False

        # Isolate this run's process-global tool state (bash cwd, background
        # shells, read-guard) under a per-agent scope so concurrent agents /
        # subagents don't share it. Reset in the finally to restore the parent's
        # scope when a subagent's run (in the same task) returns.
        from .builtin_tools.fs import AGENT_CWD, TOOL_SCOPE  # noqa: PLC0415
        _scope_token = TOOL_SCOPE.set(self._tool_scope)
        # Scope the file/shell tools to this agent's working directory for the
        # run, so relative paths land where the model was told they would.
        _cwd_token = AGENT_CWD.set(self.cwd)

        # Spans are hoisted so the exception guard below can close whichever
        # is still open. The loop's normal-exit paths close them explicitly.
        turn_span: Span | None = None
        llm_span: Span | None = None
        # ``effective_max`` starts at ``max_steps`` but persist mode may grant a
        # bounded extension (below) when budget runway remains and work is
        # unfinished — a handoff that beats a silent max-steps cutoff.
        effective_max = self.max_steps
        step = 0
        try:
            while True:
                if step >= effective_max:
                    if (
                        self.persist
                        and self._step_extensions < _MAX_STEP_EXTENSIONS
                        and self._has_unfinished_work()
                        and self._has_runway()
                    ):
                        self._step_extensions += 1
                        effective_max += max(2, self.max_steps // 2)
                        _log.info(
                            "persist: extending step budget to %d (extension %d/%d)",
                            effective_max, self._step_extensions, _MAX_STEP_EXTENSIONS,
                        )
                    else:
                        break
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

                # Final-turn wrap-up: on the last allowed step, tell the model to
                # summarize instead of starting work it can't finish — so a run that
                # hits the turn limit ends with a coherent answer, not a dangling
                # tool result. Soft nudge (isMeta), injected once.
                if effective_max > 1 and step == effective_max - 1:
                    final_msg = _final_turn_reminder()
                    messages.append(final_msg)
                    yield final_msg

                # Budget wrap-up: once we're within ~85% of a configured USD/token/turn
                # budget, nudge the model to summarize BEFORE the hard cap raises
                # BudgetExceededError — so a budget-limited run ends coherently too.
                elif (
                    not self._budget_wrapup_done
                    and self._budget_tracker is not None
                    and self._budget_tracker.should_use_fallback(0.75)  # leave runway to summarize
                ):
                    self._budget_wrapup_done = True
                    budget_msg = _final_turn_reminder("budget limit")
                    messages.append(budget_msg)
                    yield budget_msg

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
                    ctx_window = self._message_budget()
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
                        # PreCompact hook — fires just before the (lossy) summarization
                        # so integrators can snapshot/persist the full transcript before
                        # it's compressed, or block it to handle compaction themselves.
                        skip_compact = False
                        if self._dispatcher.has("PreCompact"):
                            pc = await self._dispatcher.dispatch(
                                "PreCompact",
                                HookContext(event="PreCompact", messages_snapshot=messages),
                            )
                            skip_compact = pc.block
                        if not skip_compact:
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
                            step += 1
                            continue
                    if not tool_uses:
                        # Completion contract. A no-tool-use turn is a candidate
                        # stop — but persist mode re-drives it when there's a
                        # real unfinished-work signal (open todos / unmet target)
                        # and progress isn't diminishing, under a hard cap. The
                        # gate ALWAYS stops on a final answer with no open work,
                        # so a plain query() with no todos behaves as before.
                        if self._should_continue_at_natural_stop():
                            self._continuation_count += 1
                            nudge = _persist_nudge()
                            messages.append(nudge)
                            yield nudge
                            if turn_span is not None and self.tracer is not None:
                                turn_span.set_attributes({"turn.persist_continued": True})
                                turn_span.end()
                                mirror = getattr(self.tracer, "_mirror", None) or self.tracer
                                close_fn = getattr(mirror, "_close", None)
                                if callable(close_fn):
                                    close_fn(turn_span)
                            step += 1
                            continue
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
                    tool = registry.resolve(call.name)
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
                # A tool_use materialized by finalize() but never dispatched (e.g. a
                # non-conforming provider emits no ContentBlockStop, so
                # _maybe_dispatch_closed_block never ran) is absent from by_id — feed
                # back a synthetic error result instead of KeyError-crashing the turn.
                results_in_order: list[ToolResultBlock] = []
                for b in tool_uses:
                    r = by_id.get(b.id)
                    if r is None:
                        r = ToolResultBlock(
                            tool_use_id=b.id,
                            content="tool call was truncated and never executed; re-issue it if needed",
                            is_error=True,
                        )
                    results_in_order.append(r)

                # Tool-error-streak tally (escalation ladder). A turn whose tool
                # calls ALL errored bumps the streak (which deepens thinking next
                # turn via ``_thinking_config_for_turn``); any success resets it.
                # A mixed turn leaves it unchanged.
                if results_in_order:
                    n_err = sum(1 for r in results_in_order if r.is_error)
                    if n_err == 0:
                        self._tool_error_streak = 0
                    elif n_err == len(results_in_order):
                        self._tool_error_streak += 1

                tool_result_msg = UserMessage(content=list(results_in_order))
                messages.append(tool_result_msg)
                yield tool_result_msg

                # Escalation rung 3: after a sustained tool-error streak, persist
                # mode injects a REPLAN meta message (rising-edge, fires once per
                # streak) so a stuck run rethinks its approach instead of grinding
                # the same failing plan. Rung 1 is the repeated-call nudge; rung 2
                # is the deepened thinking budget above.
                if self.persist and self._tool_error_streak == _REPLAN_AT_ERROR_STREAK:
                    replan = _replan_nudge()
                    messages.append(replan)
                    yield replan

                # End the turn span now that this turn (model call + tools +
                # post-tool hooks) is fully wrapped up. Tool spans live as
                # children of this turn via the executor's trace_parent.
                if turn_span is not None and self.tracer is not None:
                    turn_span.end()
                    mirror = getattr(self.tracer, "_mirror", None) or self.tracer
                    close_fn = getattr(mirror, "_close", None)
                    if callable(close_fn):
                        close_fn(turn_span)

                step += 1

            # Step budget exhausted (including any persist-mode extensions)
            # without a natural stop. Under persist mode with work still open,
            # emit a structured "remaining work" handoff so the run ends with a
            # clear pick-up point instead of a silent cutoff.
            _log.warning("agent hit max_steps=%d without natural stop", effective_max)
            if self.persist and self._has_unfinished_work():
                remaining = _remaining_work_summary(self.todos)
                messages.append(remaining)
                yield remaining
            _close_run_span()
        except BaseException as _run_exc:
            # Any error (budget cap, provider failure past retries/fallback,
            # a hook/permission bug) — or a consumer abandoning the generator —
            # must still close the open spans and stamp the error, instead of
            # leaking them. Idempotent closers make already-closed spans no-ops.
            _close_span(llm_span, error=_run_exc)
            _close_span(turn_span, error=_run_exc)
            _close_run_span(error=_run_exc)
            raise
        finally:
            # Restore the enclosing scope (parent agent, or global). Best-effort:
            # a reset can raise only if the token is from a different context,
            # which can't happen here (set + reset in the same frame).
            try:
                TOOL_SCOPE.reset(_scope_token)
            except (ValueError, LookupError):  # pragma: no cover — defensive
                pass
            try:
                AGENT_CWD.reset(_cwd_token)
            except (ValueError, LookupError):  # pragma: no cover — defensive
                pass

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

        # Malformed tool-call JSON the assembler couldn't parse (even leniently).
        # Fail CLOSED: never execute the tool with garbage/empty input — return
        # an is_error result so the model re-emits a well-formed call, instead of
        # crashing the run.
        if isinstance(call.input, dict) and _MALFORMED_TOOL_JSON_KEY in call.input:
            raw = call.input.get(_MALFORMED_TOOL_JSON_KEY)
            # Distinguish CUT OFF from MALFORMED. They need opposite fixes, and
            # telling the model the wrong one costs a full generation per retry:
            # "re-issue valid JSON" makes it re-emit the same oversized call,
            # which truncates at the identical point. That loop is what a big
            # write_file looks like when the content doesn't fit under the cap.
            if _looks_truncated(raw):
                return call, ToolResultBlock(
                    tool_use_id=call.id,
                    content=(
                        f"The arguments for `{call.name}` were CUT OFF mid-call — "
                        f"the response hit its output limit (max_tokens="
                        f"{self.max_tokens}), so the JSON ended part-way through. "
                        f"Your syntax was fine; there was just no room left.\n"
                        f"Re-issue it SMALLER: send less in one call (for a file, "
                        f"write the first part now and append the rest with "
                        f"follow-up calls). Repeating the same call unchanged will "
                        f"truncate at the same place."
                    ),
                    is_error=True,
                )
            return call, ToolResultBlock(
                tool_use_id=call.id,
                content=(
                    f"The arguments for `{call.name}` were not valid JSON and "
                    f"could not be parsed. Re-issue the tool call with a single "
                    f"well-formed JSON object as the arguments."
                ),
                is_error=True,
            )

        tool = registry.resolve(call.name)
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
                    # Escalation rung 1 (the nudge) fires here; record the trip so
                    # ``_thinking_config_for_turn`` deepens reasoning next turn.
                    self._repeat_tripped = True
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

            if isinstance(decision, Allow) and decision.updated_input is not None:
                # A can_use_tool / PreToolUse callback APPROVED some input but
                # handed back a REWRITTEN one. That rewrite was never vetted —
                # re-run the mandatory guards (deny rules + dangerous-shell gate)
                # against the MUTATED input before dispatch so an approval of
                # `ls` can't be smuggled out as an unreviewed `rm -rf`. Fail
                # closed: a denied/blocked rewrite is denied, not run.
                recheck = _normalize_permission_decision(
                    await recheck_mutated_input(
                        tool, decision.updated_input, self.permissions
                    )
                )
                if isinstance(recheck, Deny):
                    decision = recheck
                else:
                    call = ToolUseBlock(
                        id=call.id,
                        name=call.name,
                        input=decision.updated_input,
                    )

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
        partial output). Before falling back, a TRANSIENT failure (rate limit,
        5xx, connection blip) is retried up to ``max_retries`` times with
        exponential backoff — a single throttle shouldn't kill the turn."""
        import anyio  # noqa: PLC0415

        attempt = 0
        overflow_retried = False
        while True:
            produced = False
            produced_content = False
            try:
                async for ev in self._provider_stream(messages):
                    produced = True
                    if isinstance(ev, ContentBlockStart):
                        produced_content = True
                    yield ev
                if not produced:
                    # A 2xx whose body closed before a single event — an empty
                    # stream (no message_start). Classic serverless cold-start /
                    # idle-scaledown / proxy blip (e.g. a Modal or RunPod GPU
                    # endpoint still booting the model). This is otherwise raised
                    # later by the assembler, OUTSIDE any retry; raise it here so
                    # a warm retry (below) can recover instead of failing the turn.
                    raise StreamProtocolError(
                        "stream ended without message_start (empty response — "
                        "backend cold start or scaledown?)")
                if not produced_content:
                    # message_start arrived but the stream closed without a single
                    # content block — a wholly empty assistant turn. Nothing usable
                    # was streamed downstream (only message envelope events, safe to
                    # replay), so retry rather than accept an empty turn as done.
                    raise StreamProtocolError(
                        "stream produced no content blocks (empty response)")
                return
            except Exception as err:  # noqa: BLE001
                # Once a CONTENT block has streamed downstream we can't safely
                # retry (the consumer's assembler already holds partial blocks);
                # re-raise. Failures before any content — including the empty /
                # cold-start cases above — are still retryable.
                if produced_content:
                    raise  # can't retry partial output
                # Context-overflow: the prompt is too long. Emergency-compact
                # (clear old tool results + summarize) and retry ONCE, rather than
                # failing the turn — a last-resort safety net when auto-compaction
                # didn't fire in time (e.g. a sudden huge input).
                if (
                    not overflow_retried
                    and self._compactor is not None
                    and _is_context_overflow(err)
                ):
                    overflow_retried = True
                    # Learn BEFORE compacting. The refusal states the real
                    # ceiling, and compacting against our (too large) guess is
                    # exactly how this used to fail: the retry re-sent a prompt
                    # the endpoint had already refused, and every later turn
                    # overflowed the same way.
                    self._learn_context_limit(err)
                    if await self._emergency_compact(messages):
                        _log.warning("context overflow (%r); compacted and retrying", err)
                        continue
                # Same-model retry on a transient error, with backoff.
                if _is_transient(err) and attempt < self.max_retries:
                    delay = _retry_delay(err, attempt)
                    # Same UI treatment as the transport layer: with a TUI hook
                    # installed, surface as an in-place spinner note instead of
                    # a raw WARNING line torn through the prompt frame.
                    from . import retry as _retry_mod  # noqa: PLC0415
                    _cb = _retry_mod.notify
                    if _cb is not None:
                        try:
                            _cb({"host": self.model,
                                 "reason": f"model error ({type(err).__name__})",
                                 "attempt": attempt + 1,
                                 "attempts": self.max_retries, "sleep_s": delay})
                        except Exception:  # noqa: BLE001
                            pass
                        _log.debug("transient model error (%r); retry %d/%d in %.1fs",
                                   err, attempt + 1, self.max_retries, delay)
                    else:
                        _log.warning(
                            "transient model error (%r); retry %d/%d in %.1fs",
                            err, attempt + 1, self.max_retries, delay,
                        )
                    await anyio.sleep(delay)
                    attempt += 1
                    continue
                # Exhausted / non-transient → try model fallback, else raise.
                if self.fallback_model and not self._fallback_used:
                    self._activate_fallback(err)
                    break
                raise
        # Retry once on the fallback (outside the loop so its own errors
        # propagate normally).
        async for ev in self._provider_stream(messages):
            yield ev

    def _endpoint(self) -> str | None:
        """The endpoint this agent actually talks to.

        ``backend`` is only set when the caller passed a URL; the TUI builds a
        provider object instead and leaves it None. Reading the provider's own
        base_url keeps the key used to RECORD a learned limit identical to the
        one used to READ it back — they diverged once, and the limit was
        written under a bare model name and then never found again.
        """

        if self.backend:
            return self.backend
        base = getattr(self.provider, "base_url", None)
        return str(base) if base else None

    def _effective_context_window(self) -> int:
        """The context window to plan against.

        Our capability table is a guess, and a guess that is too large disables
        compaction entirely — the compactor only fires at a fraction of the
        window it is told about. Anything we have watched the endpoint actually
        reject lowers it.
        """

        cap = self.model_capability
        declared = (getattr(cap, "context_window", 0) or 0) if cap is not None else 0
        try:
            from .context_limits import effective_window  # noqa: PLC0415
            return effective_window(self.model, declared, self._endpoint())
        except Exception:  # noqa: BLE001 — never let bookkeeping break a turn
            return declared

    def _prompt_overhead_tokens(self) -> int:
        """Tokens every request spends before a single message: the system
        prompt and the tool schemas.

        The compaction estimator counted messages only. With a large tool set
        the real prompt therefore ran thousands of tokens above what we planned
        with — a session showing "7k used" was sending 13140. On a 128k model
        that slack is invisible; on an 8k one it is the whole bug, because
        compaction happily "succeeds" at a target the request can never meet.
        """

        total = 0
        if self.system:
            total += max(1, len(str(self.system)) // 4)
        try:
            if self.tools:
                import json as _json  # noqa: PLC0415
                total += len(_json.dumps(self.tools.to_wire())) // 4
        except Exception:  # noqa: BLE001 — an estimate, never a hard failure
            pass
        return total

    def _message_budget(self) -> int:
        """How much of the context window the conversation may actually use.

        Zero when the fixed overhead alone does not fit: that is not a
        compaction problem and no amount of summarizing fixes it.
        """

        window = self._effective_context_window()
        if window <= 0:
            return 0
        return max(0, window - self._prompt_overhead_tokens())

    def _learn_context_limit(self, err: BaseException) -> bool:
        """Record the ceiling a provider just told us it enforces.

        The refusal carries the real number ("…while limit is 8192"), which is
        the only trustworthy source: our table said 128k for this same model,
        and the provider's own catalog advertised 131k. Returns True when this
        taught us something new, meaning the retry is worth attempting against
        a budget that has actually changed.
        """

        try:
            from .context_limits import learned_limit, parse_limit, record_limit  # noqa: PLC0415

            limit = parse_limit(err)
            if limit is None:
                return False
            endpoint = self._endpoint()
            before = learned_limit(self.model, endpoint)
            if not record_limit(self.model, limit, endpoint):
                return False
            _log.warning(
                "learned real context limit for %r: %d tokens (was planning against %s); "
                "compaction now targets the true ceiling",
                self.model, limit,
                before or (getattr(self.model_capability, "context_window", 0) or "unknown"),
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _emergency_compact(self, messages: list[Message]) -> bool:
        """Shrink ``messages`` in place as much as possible after a context-
        overflow error: clear old tool-result bodies (microcompaction, no model
        call) AND summarize older turns. Returns True if anything shrank."""
        if self._compactor is None:
            return False
        before = len(messages)
        before_chars = sum(len(str(getattr(m, "content", ""))) for m in messages)
        micro = getattr(self._compactor, "microcompact", None)
        if micro is not None:
            micro(messages)
        try:
            compacted = await self._compactor.compact(messages)
            if len(compacted) < len(messages):
                messages[:] = compacted
        except Exception:  # noqa: BLE001 — summarizer failed; microcompaction may still help
            _log.debug("emergency summarize failed", exc_info=True)
        after_chars = sum(len(str(getattr(m, "content", ""))) for m in messages)
        # Escalate when the normal path barely dented it. Both microcompaction
        # and summarization protect the RECENT window — which is exactly where a
        # sudden oversized turn (a screenshot, a whole-page dump) lands, so the
        # retry would re-send the same rejected prompt and the session wedges:
        # every subsequent message, including a manual /compact, overflows too.
        # The provider has already refused this transcript; a degraded run beats
        # a dead one.
        # Escalate when the result is still over the ceiling the provider just
        # told us about, not only when compaction "barely dented it". Shrinking
        # 14789 tokens by 40% still overflows an 8192 limit, and the old
        # proportional test called that a success — so the retry re-sent a
        # prompt that could not fit and the session wedged.
        if after_chars > before_chars * 0.5 or self._still_over_limit(messages):
            clear = getattr(self._compactor, "emergency_clear", None)
            if clear is not None and clear(messages, keep_last=0):
                _log.warning(
                    "context overflow persisted after compaction; cleared recent "
                    "tool-result payloads to recover the session")
                after_chars = sum(len(str(getattr(m, "content", ""))) for m in messages)
        return len(messages) < before or after_chars < before_chars

    def _still_over_limit(self, messages: list[Message]) -> bool:
        """Whether ``messages`` would still be refused by the known ceiling.

        Uses the same estimator the compactor plans with, and reserves the room
        the request itself needs, so "it fits" means it fits with the reply.
        """

        window = self._effective_context_window()
        if window <= 0:
            return False
        try:
            from .compact import _message_token_estimate  # noqa: PLC0415
            used = sum(_message_token_estimate(m) for m in messages)
            used += self._prompt_overhead_tokens()
        except Exception:  # noqa: BLE001
            return False
        return used >= window * 0.9

    def _activate_fallback(self, error: BaseException) -> None:
        _log.warning(
            "model %r failed (%r) before output; falling back to %r",
            self.model, error, self.fallback_model,
        )
        # Remember the primary model once so run_iter can restore it next run;
        # a single transient blip must not permanently downgrade the agent.
        if self._primary_model is None:
            self._primary_model = self.model
            self._primary_model_capability = self.model_capability
        self._fallback_used = True
        self.model = self.fallback_model  # type: ignore[assignment]
        self.model_capability = lookup_model(self.model)
        self._env_context = None  # env block referenced the old model; let it rebuild

    def _provider_stream(self, messages: list[Message]) -> AsyncIterator[StreamEvent]:
        provider_messages = _repair_tool_call_history(messages)
        # Hoist the system prompt: prefer explicit Agent.system, else look at
        # messages[0] if it's a SystemMessage. Provider adapters expect system
        # as a top-level field (Anthropic, OpenAI, Ollama all do).
        system = self.system
        if system is None:
            # Scan for the first SystemMessage anywhere — not just index 0.
            # run_iter may prepend a synthetic isMeta UserMessage (memory/env
            # context) ahead of a caller-supplied leading SystemMessage, so the
            # system prompt is no longer guaranteed to sit at index 0.
            for _m in provider_messages:
                if isinstance(_m, SystemMessage):
                    system = _m.content if isinstance(_m.content, str) else None
                    break

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
        max_tokens = self.max_tokens
        # Effective, not declared: with a learned 8k ceiling the reply
        # reservation has to shrink too, or input+output overflows even though
        # the transcript alone fits.
        ctx_window = self._effective_context_window()
        if ctx_window > 0:
            try:
                from .compact import _message_token_estimate  # noqa: PLC0415
                estimated_input = sum(_message_token_estimate(m) for m in provider_messages)
                # Tool schemas ride on every request too — omitting them made
                # the reservation optimistic by thousands of tokens.
                estimated_input += self._prompt_overhead_tokens()
                if system and not self.system:
                    estimated_input += max(1, len(system) // 4)
                room = ctx_window - estimated_input - 512
                if room > 0:
                    max_tokens = min(max_tokens, max(512, room))
            except Exception:  # noqa: BLE001
                pass
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": provider_messages,
            "system": system,
            "tools": self.tools.to_wire() if self.tools else None,
            "max_tokens": max_tokens,
            "temperature": self.temperature,
            "extra": provider_extra,
        }
        # Adaptive thinking config for this turn (effort + per-turn escalation).
        # ``None`` (the default medium-effort case) is NOT passed, so a default
        # agent's request is byte-for-byte unchanged.
        thinking_cfg = self._thinking_config_for_turn(provider_messages)
        return self._call_provider_stream(kwargs, thinking_cfg)

    def _call_provider_stream(
        self, kwargs: dict[str, Any], thinking_cfg: dict[str, Any] | None
    ) -> AsyncIterator[StreamEvent]:
        """Invoke ``provider.stream`` with graceful degradation for adapters
        that don't accept ``model_capability`` and/or ``thinking``. Argument
        binding for an async-generator function raises ``TypeError`` at call
        time (before iteration), so we can probe the richest signature and fall
        back — a provider that can't use ``thinking`` just runs without it
        (accepting-and-ignoring is fine per the interface contract)."""
        assert self.provider is not None  # post-init guarantees this
        base = dict(kwargs)
        attempts: list[dict[str, Any]] = []
        if thinking_cfg is not None:
            attempts.append(
                {**base, "thinking": thinking_cfg, "model_capability": self.model_capability}
            )
            attempts.append({**base, "thinking": thinking_cfg})
        attempts.append({**base, "model_capability": self.model_capability})
        attempts.append(base)
        last_exc: TypeError | None = None
        for kw in attempts:
            try:
                return self.provider.stream(**kw)
            except TypeError as exc:
                last_exc = exc
        raise last_exc  # pragma: no cover — base attempt has the minimal kwargs

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
        import anyio  # noqa: PLC0415

        sig = self.cancellation_signal
        if sig is not None and sig.is_set():
            return ""
        assert self.provider is not None
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
        # Retry the summarization on a transient failure (rate limit / 5xx /
        # connection blip) the same way a normal turn does — a throttle during
        # compaction shouldn't kill the whole run.
        attempt = 0
        while True:
            asm = _AssistantAssembler()
            produced = False
            try:
                try:
                    stream = self.provider.stream(model_capability=self.model_capability, **kwargs)
                except TypeError:
                    stream = self.provider.stream(**kwargs)
                async for ev in stream:
                    produced = True
                    asm.feed(ev)
                if not produced:
                    # Empty stream — same cold-start / scaledown blip as a normal
                    # turn; raise into the retry below rather than letting
                    # finalize() fail outside it.
                    raise StreamProtocolError(
                        "stream ended without message_start (empty response)")
                break
            except Exception as err:  # noqa: BLE001
                if produced or not (_is_transient(err) and attempt < self.max_retries):
                    raise
                await anyio.sleep(_retry_delay(err, attempt))
                attempt += 1
        msg = asm.finalize()
        if msg.usage is not None and self._budget_tracker is not None:
            self._budget_tracker.add_usage(
                msg.usage, self.model, backend_hint=self._provider_hint
            )
        return "".join(b.text for b in msg.content if isinstance(b, TextBlock))


# ---------------------------------------------------------------------------
# AssistantAssembler — turn event stream into AssistantMessage
# ---------------------------------------------------------------------------


@dataclass
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
                raw = "".join(self.json_parts)
                try:
                    input_obj = _JSON_DECODER.decode(raw)
                except msgspec.DecodeError:
                    # Weak models routinely emit invalid tool-arg JSON (trailing
                    # commas, unquoted keys) or truncate it at the token cap. Try
                    # the lenient parser the salvage path uses; accept it only if
                    # it yields a JSON object. If even that fails, the input is
                    # genuinely unparseable — DON'T crash the run: flag it with
                    # the malformed-JSON marker so ``_preflight_call`` returns an
                    # is_error "re-issue valid JSON" reminder and the model can
                    # recover, instead of raising ``StreamProtocolError`` and
                    # killing the whole turn.
                    from .streaming.text_tool_parser import _loads_lenient  # noqa: PLC0415
                    recovered = _loads_lenient(raw)
                    if not isinstance(recovered, dict):
                        input_obj = {_MALFORMED_TOOL_JSON_KEY: raw}
                    else:
                        input_obj = recovered
            else:
                input_obj = self.tool_initial_input or {}
            if not isinstance(input_obj, dict):
                # Tool arguments must be a JSON object; anything else is malformed.
                input_obj = {_MALFORMED_TOOL_JSON_KEY: str(input_obj)}
            return ToolUseBlock(id=self.tool_id, name=self.tool_name, input=input_obj)
        # Unknown / passthrough — return whatever the start event gave us.
        if self.raw_block is None:
            raise StreamProtocolError(f"no block payload for kind {self.kind!r}")
        return self.raw_block


class _AssistantAssembler:
    """Folds a stream of events into a single ``AssistantMessage``.

    Holds builders by block index, plus message-level metadata.
    """

    __slots__ = ("blocks", "stop_reason", "usage", "_seen_start", "_closed")

    def __init__(self) -> None:
        self.blocks: dict[int, _BlockBuilder] = {}
        self.stop_reason: str | None = None
        self.usage: Usage | None = None
        self._seen_start = False
        # Indices whose ContentBlockStop we've seen — so finalize() can tell a
        # cleanly-closed block from one the stream was cut off mid-way through.
        self._closed: set[int] = set()

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
            # Builder stays as-is; we materialize at finalize(). Record the
            # close so a block started-but-never-stopped (truncated stream)
            # is detectable.
            self._closed.add(ev.index)
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
        # Truncated stream: a content block was opened but its ContentBlockStop
        # never arrived — the provider cut off mid-block (dropped connection,
        # proxy truncation). The partial block is not a real completion; treat
        # it as a transient protocol failure so the turn is retried, not
        # accepted as a half-written success.
        unclosed = [i for i in self.blocks if i not in self._closed]
        if unclosed:
            raise StreamProtocolError(
                "stream ended mid-block (truncated response — "
                f"{len(unclosed)} block(s) never closed)"
            )
        # Sorted by index so block order matches what the provider emitted.
        ordered = [self.blocks[i].to_block() for i in sorted(self.blocks)]
        if not ordered:
            # message_start (and possibly a stop reason) but zero content blocks
            # — a wholly empty assistant turn. Nothing usable was produced;
            # treat it as a transient failure to retry rather than emitting an
            # empty message the loop would silently accept as complete.
            raise StreamProtocolError(
                "stream produced no content blocks (empty response)"
            )
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
