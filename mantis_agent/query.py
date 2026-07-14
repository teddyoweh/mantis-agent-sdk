"""``query()`` — the drop-in wrapper.

Mirrors the Claude Agent SDK's public surface verbatim so a caller can
swap ``from claude_agent_sdk import query`` for ``from mantis_agent import
query`` and the rest of their code keeps working::

    from mantis_agent import query

    async for msg in query(
        prompt="What is Spawn Labs?",
        options={"model": "qwen2.5-72b-instruct", "backend": "https://api.together.xyz/v1"},
    ):
        if msg["type"] == "result":
            print(msg["result"], "$", msg["total_cost_usd"])

SDK message schema
------------------
We follow Claude SDK's wire shape 1:1 (audited against the upstream zip,
specifically ``entrypoints/sdk/coreSchemas.ts``):

  * ``SDKAssistantMessage(type='assistant', message=APIAssistantMessage,
                          parent_tool_use_id, uuid, session_id, error?)``
  * ``SDKUserMessage(type='user', message=APIUserMessage,
                     parent_tool_use_id, uuid?, session_id?)``
  * ``SDKSystemMessage(type='system', subtype='init', ...)``
  * ``SDKCompactBoundaryMessage(type='system', subtype='compact_boundary', ...)``
  * ``SDKStatusMessage(type='system', subtype='status', status, permissionMode?)``
  * ``SDKResultMessage`` (union of Success + Error subtypes) with:
        type='result', subtype, duration_ms, duration_api_ms, is_error,
        num_turns, result (success only) / errors (error only), stop_reason,
        total_cost_usd, usage, modelUsage, permission_denials,
        uuid, session_id

The ``message`` field on assistant/user mirrors Anthropic's API message
shape: ``{id, type, role, content, model, stop_reason, stop_sequence,
usage}`` for assistants; ``{role, content}`` for users. This matches what
upstream sees from the Anthropic SDK and what every Claude-SDK consumer
expects.

Options dict
------------
Accepts snake_case AND camelCase keys for full Claude SDK parity:
``max_turns`` / ``maxTurns``, ``allowed_tools`` / ``allowedTools``, etc.
Normalized once via ``_to_snake``.
"""

from __future__ import annotations

import re
import time
import uuid as _uuid
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any, Literal, Union

import msgspec

from .agent import Agent
from .budget import lookup_pricing
from .capabilities import lookup_model
from .tools import Tool, ToolRegistry
from .transcripts import JsonlTranscript
from .types import (
    AssistantMessage,
    ContentBlock,
    Message,
    ModelUsage,
    SystemMessage,
    TextBlock,
    Usage,
    UserMessage,
)

# ---------------------------------------------------------------------------
# API-shape message objects (mirror Anthropic SDK message shapes)
# ---------------------------------------------------------------------------


class APIAssistantMessage(msgspec.Struct):
    """The ``message`` field of an SDKAssistantMessage. Matches the
    ``anthropic.types.Message`` shape that Claude SDK forwards verbatim.

    Wire format always includes ``role``, ``type``, ``content`` — JS
    consumers check those fields directly, so we do NOT omit defaults.
    """

    id: str
    role: Literal["assistant"] = "assistant"
    type: Literal["message"] = "message"
    content: list[ContentBlock] = msgspec.field(default_factory=list)
    model: str = ""
    stop_reason: str | None = None
    stop_sequence: str | None = None
    usage: Usage | None = None


class APIUserMessage(msgspec.Struct):
    """The ``message`` field of an SDKUserMessage. ``role`` is always
    emitted on the wire — see APIAssistantMessage."""

    role: Literal["user"] = "user"
    content: str | list[ContentBlock] = ""


# ---------------------------------------------------------------------------
# SDK message variants
# ---------------------------------------------------------------------------


def _tag_type(cls):
    """Decorator: expose msgspec's tag value as a runtime ``.type`` property.

    msgspec stores ``tag`` as serialization metadata only — it's not a Python
    attribute on the instance. Consumers expect ``msg.type == "assistant"``
    (Claude SDK parity), so we synthesize a property that reads
    ``__struct_tag__`` (set by msgspec at class build time). For ``system``
    subclasses, the property returns ``"system"`` and ``msg.subtype`` carries
    the discriminator (``init`` / ``compact_boundary`` / ``status``), again
    matching Claude SDK.
    """

    tag = cls.__struct_config__.tag  # set by msgspec when ``tag=...`` is used

    @property
    def _type(self) -> str:
        return tag

    cls.type = _type
    return cls


@_tag_type
class SDKAssistantMessage(
    msgspec.Struct,
    tag="assistant",
    tag_field="type",
):
    """One assistant turn. Wraps an ``APIAssistantMessage`` under ``message``,
    same as Claude SDK."""

    message: APIAssistantMessage
    parent_tool_use_id: str | None = None
    uuid: str = ""
    session_id: str = ""
    error: str | None = None


@_tag_type
class SDKUserMessage(
    msgspec.Struct,
    tag="user",
    tag_field="type",
):
    """One user turn (or tool-result-bearing turn appended by the agent
    loop). Wraps an ``APIUserMessage`` under ``message``."""

    message: APIUserMessage
    parent_tool_use_id: str | None = None
    uuid: str = ""
    session_id: str = ""
    isSynthetic: bool = False
    tool_use_result: Any = None


@_tag_type
class SDKSystemMessage(
    msgspec.Struct,
    tag="system",
    tag_field="type",
):
    """Session-init banner. Fired once at the start of a stream.

    Mirrors Claude SDK's ``SDKSystemMessage`` with ``subtype='init'``:
    surfaces the active model, configured tools, mcp servers, permission
    mode, and cwd so consumers can render a session header.
    """

    subtype: Literal["init"] = "init"
    apiKeySource: str = "user"
    cwd: str = ""
    tools: list[str] = msgspec.field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = msgspec.field(default_factory=list)
    model: str = ""
    permissionMode: str = "default"
    slug: str = "mantis-agent-sdk"
    output_style: str | None = None
    agents: list[str] = msgspec.field(default_factory=list)
    uuid: str = ""
    session_id: str = ""


@_tag_type
class SDKCompactBoundaryMessage(
    msgspec.Struct,
    tag="system",
    tag_field="type",
):
    """Fired by the compactor when it summarizes older turns into a single
    boundary message. Mirrors Claude SDK's ``SDKCompactBoundaryMessage``."""

    subtype: Literal["compact_boundary"] = "compact_boundary"
    compact_metadata: dict[str, Any] = msgspec.field(default_factory=dict)
    uuid: str = ""
    session_id: str = ""


@_tag_type
class SDKStatusMessage(
    msgspec.Struct,
    tag="system",
    tag_field="type",
):
    """Mid-stream status nudge — ``status='compacting'`` while the compactor
    runs, ``None`` when idle."""

    subtype: Literal["status"] = "status"
    status: str | None = None
    permissionMode: str | None = None
    uuid: str = ""
    session_id: str = ""


class SDKPermissionDenial(msgspec.Struct, omit_defaults=True):
    """Carried on SDKResultMessage. Matches upstream field-for-field."""

    tool_name: str
    tool_use_id: str
    tool_input: dict[str, Any] = msgspec.field(default_factory=dict)


@_tag_type
class SDKResultMessage(
    msgspec.Struct,
    tag="result",
    tag_field="type",
):
    """Terminal message. ``subtype='success'`` for natural completion,
    ``error_during_execution`` / ``error_max_turns`` / ``error_max_budget_usd``
    / ``error_max_structured_output_retries`` for the error paths — same
    vocabulary as Claude SDK so error-handling code ports without diffs.

    ``result`` carries the final assistant text on success; ``errors`` carries
    a list of human-readable error strings on the error subtypes. Both
    fields are optional on the struct (omit_defaults=True) so consumers see
    the upstream-shape wire output.

    ``usage`` is the aggregate Usage for the whole run; ``modelUsage`` is a
    per-model dict so multi-model runs report each model's contribution.
    """

    subtype: Literal[
        "success",
        "error_during_execution",
        "error_max_turns",
        "error_max_budget_usd",
        "error_max_structured_output_retries",
    ] = "success"
    duration_ms: int = 0
    duration_api_ms: int = 0
    is_error: bool = False
    num_turns: int = 0
    result: str = ""
    stop_reason: str | None = None
    total_cost_usd: float = 0.0
    usage: Usage = msgspec.field(default_factory=Usage)
    modelUsage: dict[str, ModelUsage] = msgspec.field(default_factory=dict)
    permission_denials: list[SDKPermissionDenial] = msgspec.field(default_factory=list)
    errors: list[str] = msgspec.field(default_factory=list)
    uuid: str = ""
    session_id: str = ""


SDKMessage = Union[
    SDKAssistantMessage,
    SDKUserMessage,
    SDKSystemMessage,
    SDKCompactBoundaryMessage,
    SDKStatusMessage,
    SDKResultMessage,
]


# ---------------------------------------------------------------------------
# Options normalization
# ---------------------------------------------------------------------------


_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _to_snake(s: str) -> str:
    """``maxTurns`` → ``max_turns``. Idempotent on already-snake input."""

    return _CAMEL_RE.sub("_", s).lower()


def _normalize_options(opts: dict[str, Any] | None) -> dict[str, Any]:
    """Apply :func:`_to_snake` to every top-level key so the rest of the
    wrapper only ever sees snake_case names. Inner dicts are left alone."""

    if not opts:
        return {}
    return {_to_snake(k): v for k, v in opts.items()}


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------


def _build_registry(tools: Any) -> ToolRegistry:
    """Accept a list / ToolRegistry / None and always return a registry."""

    if tools is None:
        return ToolRegistry()
    if isinstance(tools, ToolRegistry):
        return tools
    reg = ToolRegistry()
    for t in tools:
        if isinstance(t, Tool):
            reg.add(t)
        else:
            raise TypeError(
                f"query(options.tools): expected Tool or ToolRegistry, got {type(t).__name__}"
            )
    return reg


def _agent_from_options(opts: dict[str, Any]) -> Agent:
    """Construct an :class:`Agent` from a normalized options dict.

    Recognized keys map 1:1 to :class:`Agent` constructor params; unknown
    keys flow through to ``Agent.extra`` so adapters / hooks /
    downstream features can read them without us growing constructor
    parameters every release.
    """

    model = opts.get("model")
    if not model:
        raise ValueError("query(options): 'model' is required")

    # Recognized keys map to Agent constructor params.
    agent_kwargs: dict[str, Any] = {
        "model": model,
        "backend": opts.get("backend"),
        "system": opts.get("system"),
        "tools": _build_registry(opts.get("tools")),
        "max_tokens": opts.get("max_tokens", 1024),
        "temperature": opts.get("temperature"),
        "max_steps": opts.get("max_turns", opts.get("max_steps", 20)),
        "max_usd": opts.get("max_usd"),
    }

    # Optional pass-throughs — only forward when set so we don't override
    # the dataclass defaults with None.
    for key in (
        "hooks",
        "permissions",
        "budget",
        "include_memory",
        "model_capability",
        "backend_capability",
        "provider",
        "response_format",
        "tracer",
        "skills",
    ):
        if key in opts:
            agent_kwargs[key] = opts[key]

    # Everything else goes on Agent.extra for adapter-specific knobs.
    consumed = set(agent_kwargs.keys()) | {
        "api_key",
        "max_turns",
        "max_steps",
        "persist",
        "session_id",
        "cwd",
        "permission_mode",
        "mcp_servers",
        "agents",
    }
    extra = {k: v for k, v in opts.items() if k not in consumed}
    if isinstance(extra.get("extra"), dict):
        nested = dict(extra.pop("extra"))
        nested.update(extra)
        extra = nested
    if extra:
        agent_kwargs["extra"] = extra

    return Agent(**agent_kwargs)


# ---------------------------------------------------------------------------
# Prompt → seed messages
# ---------------------------------------------------------------------------


async def _collect_prompt(
    prompt: str | AsyncIterable[Any],
) -> list[Message]:
    if isinstance(prompt, str):
        return [UserMessage(content=prompt)]

    messages: list[Message] = []
    async for item in prompt:
        messages.append(_to_internal_user(item))
    if not messages:
        raise ValueError("query(prompt): async iterable yielded no messages")
    return messages


def _to_internal_user(item: Any) -> UserMessage:
    if isinstance(item, UserMessage):
        return item
    if isinstance(item, SDKUserMessage):
        return UserMessage(content=item.message.content)
    if isinstance(item, dict):
        if "message" in item and isinstance(item["message"], dict):
            return UserMessage(content=item["message"].get("content", ""))
        if "content" in item:
            return UserMessage(content=item["content"])
    if isinstance(item, str):
        return UserMessage(content=item)
    raise TypeError(f"query(prompt): unsupported item type {type(item).__name__}")


def _extract_text(blocks: list[ContentBlock]) -> str:
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, TextBlock):
            parts.append(b.text)
    return "".join(parts)


def _new_uuid() -> str:
    return str(_uuid.uuid4())


# ---------------------------------------------------------------------------
# Usage aggregation
# ---------------------------------------------------------------------------


def _accumulate_usage(
    agg_usage: Usage,
    msg_usage: Usage,
) -> Usage:
    """Sum tokens across turns, including the cache fields.

    Each turn reports its own cache_read / cache_creation counts, so the
    run-total must accumulate them like input/output tokens; taking only the
    latest turn's value undercounts a multi-turn run."""

    return Usage(
        input_tokens=agg_usage.input_tokens + msg_usage.input_tokens,
        output_tokens=agg_usage.output_tokens + msg_usage.output_tokens,
        cache_creation_input_tokens=(
            agg_usage.cache_creation_input_tokens
            + msg_usage.cache_creation_input_tokens
        ),
        cache_read_input_tokens=(
            agg_usage.cache_read_input_tokens
            + msg_usage.cache_read_input_tokens
        ),
    )


def _to_model_usage(
    model: str,
    usage: Usage,
    *,
    backend_hint: str | None,
) -> ModelUsage:
    """Build a per-model ModelUsage record. Costs are computed from the
    same per-model pricing table the BudgetTracker uses, so the
    SDKResultMessage's modelUsage values agree with the budget tracker."""

    pricing = lookup_pricing(model, backend_hint)
    cost = pricing.cost(usage) if pricing is not None else 0.0
    caps = lookup_model(model)
    return ModelUsage(
        inputTokens=usage.input_tokens,
        outputTokens=usage.output_tokens,
        cacheReadInputTokens=usage.cache_read_input_tokens,
        cacheCreationInputTokens=usage.cache_creation_input_tokens,
        webSearchRequests=0,  # the WebSearch built-in updates this externally
        costUSD=cost,
        contextWindow=caps.context_window,
        maxOutputTokens=caps.max_output_tokens,
    )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def query(
    *,
    prompt: str | AsyncIterable[Any],
    options: Any = None,
) -> AsyncIterator[Any]:
    """Run an agent and yield SDK-shaped messages.

    Two output modes share this entry point:

    1. **Claude Python SDK mode** (default for ``MantisAgentOptions`` or
       ``options=None``): yields flat-shape ``AssistantMessage`` /
       ``UserMessage`` / ``SystemMessage`` / ``ResultMessage`` matching
       https://github.com/anthropics/claude-agent-sdk-python verbatim.

    2. **TS SDK wire mode** (when ``options`` is a plain ``dict``): yields
       ``SDKAssistantMessage`` / ``SDKResultMessage`` etc. with nested
       ``.message`` for byte-perfect TS SDK wire compatibility.

    Pass a :class:`~mantis_agent.MantisAgentOptions` (preferred) or a
    dict. With no options, default model is ``$MANTIS_AGENT_MODEL`` else
    ``"qwen2.5-7b-instruct"`` and default backend is ``$MANTIS_AGENT_BASE_URL``
    else ``http://localhost:11434``.

    Lifecycle: the underlying ``Agent``'s HTTP client is closed *before*
    the final result message is yielded so consumers don't need to manage
    cleanup.
    """

    # Claude Python SDK shape detection — MantisAgentOptions OR no options.
    # When options is a plain dict, fall through to legacy TS-SDK shape so
    # existing dict-shaped callers don't break.
    _is_claude_compat = options is None or not isinstance(options, dict)
    if _is_claude_compat:
        from .compat_query import query as _compat_query

        async for msg in _compat_query(prompt=prompt, options=options):
            yield msg
        return

    opts = _normalize_options(options)
    agent = _agent_from_options(opts)

    session_id = opts.get("session_id") or _new_uuid()
    seeds = await _collect_prompt(prompt)
    started_at = time.monotonic()

    # Persist to ~/.mantis-agent/sessions/{session_id}.jsonl when requested.
    # Default: off — opt-in via options={"persist": True}. When on, every
    # yielded SDKMessage is appended to the transcript before being
    # yielded to the caller (so a crashed consumer doesn't lose history).
    persist = bool(opts.get("persist", False))
    transcript: JsonlTranscript | None = None
    if persist:
        transcript = JsonlTranscript(session_id)
        transcript.open()

    def _persist(msg: SDKMessage) -> SDKMessage:
        """Persist-before-yield. Returns the message so call sites read naturally."""
        if transcript is not None:
            transcript.write(msg)
        return msg

    # 1) Session-init banner.
    yield _persist(SDKSystemMessage(
        model=agent.model,
        tools=[t.name for t in agent.tools],
        cwd=opts.get("cwd", ""),
        permissionMode=opts.get("permission_mode", "default"),
        agents=opts.get("agents", []),
        mcp_servers=opts.get("mcp_servers", []),
        uuid=_new_uuid(),
        session_id=session_id,
    ))

    # 2) Seed user messages.
    for seed in seeds:
        if isinstance(seed, UserMessage):
            yield _persist(SDKUserMessage(
                message=APIUserMessage(content=seed.content),
                uuid=_new_uuid(),
                session_id=session_id,
            ))

    # 3) Run the agent loop and translate each emitted message.
    is_error = False
    error_subtype: Literal[
        "success",
        "error_during_execution",
        "error_max_turns",
        "error_max_budget_usd",
    ] = "success"
    error_strings: list[str] = []
    num_turns = 0
    last_stop_reason: str | None = None
    agg_usage = Usage()
    model_usage: dict[str, ModelUsage] = {}
    final_text = ""
    backend_hint = (
        agent.backend_capability.provider_hint
        if agent.backend_capability is not None
        else None
    )

    # Skip-set: ``agent.run_iter`` mutates ``running`` and re-yields the
    # seed UserMessages we already echoed in step 2. Identity (``id(...)``)
    # is the cheapest test that "this is the EXACT instance we passed in",
    # so we don't accidentally drop a freshly-built duplicate.
    seed_ids = {id(s) for s in seeds}
    running: list[Message] = list(seeds)

    try:
        # Streaming-mode dispatch: yield each SDK-shape message as
        # ``run_iter`` produces it, not after the whole multi-turn loop
        # returns. This matches the Claude Agent SDK contract — consumers
        # see each AssistantMessage the instant its turn's stream
        # finalizes (StreamingToolExecutor is already dispatching tool
        # calls in parallel by that point), and the tool-result
        # UserMessage lands as soon as the executor batch finishes,
        # BEFORE the next assistant turn streams. The compat path
        # already did this; the dict-options TS-SDK path was buffering
        # via ``await agent.run(...)`` until this rewrite.
        async for msg in agent.run_iter(running):
            if id(msg) in seed_ids:
                # Already echoed in step 2 — don't re-yield seeds.
                continue
            if isinstance(msg, AssistantMessage):
                num_turns += 1
                last_stop_reason = msg.stop_reason or last_stop_reason
                if msg.usage is not None:
                    agg_usage = _accumulate_usage(agg_usage, msg.usage)
                    model_usage[agent.model] = _to_model_usage(
                        agent.model, agg_usage, backend_hint=backend_hint
                    )
                api_msg = APIAssistantMessage(
                    id=_new_uuid(),
                    content=list(msg.content),
                    model=agent.model,
                    stop_reason=msg.stop_reason,
                    usage=msg.usage,
                )
                yield _persist(SDKAssistantMessage(
                    message=api_msg,
                    uuid=_new_uuid(),
                    session_id=session_id,
                ))
                text = _extract_text(msg.content)
                if text:
                    final_text = text
            elif isinstance(msg, UserMessage):
                # Two flavors flow through this branch:
                #   1. The synthetic ``<system-reminder>``-wrapped meta
                #      UserMessage that ``run_iter`` prepends to surface
                #      user context (memory etc.). ``isMeta=True``.
                #   2. A tool-result-bearing UserMessage appended by the
                #      agent loop after a tool batch finishes.
                # Both map to ``isSynthetic=True`` in the SDK shape so
                # downstream renderers can tell them apart from
                # user-typed messages.
                yield _persist(SDKUserMessage(
                    message=APIUserMessage(content=msg.content),
                    uuid=_new_uuid(),
                    session_id=session_id,
                    isSynthetic=True,
                ))
            elif isinstance(msg, SystemMessage):
                content = msg.content if isinstance(msg.content, str) else ""
                yield _persist(SDKSystemMessage(
                    model=agent.model,
                    cwd=opts.get("cwd", ""),
                    tools=[t.name for t in agent.tools],
                    permissionMode=opts.get("permission_mode", "default"),
                    output_style=content or None,
                    uuid=_new_uuid(),
                    session_id=session_id,
                ))
    except Exception as e:  # noqa: BLE001 — wrap into result.errors
        is_error = True
        # Map our typed exceptions to upstream subtypes. BudgetExceededError
        # carries one of {"turns", "input_tokens", "output_tokens",
        # "total_tokens", "usd"} on ``.kind``. Anything turn-shaped maps to
        # ``error_max_turns``; every $/token overrun maps to
        # ``error_max_budget_usd`` since that's the only non-turn subtype
        # Claude Agent SDK exposes.
        from .errors import BudgetExceededError

        if isinstance(e, BudgetExceededError):
            error_subtype = (
                "error_max_turns" if e.kind == "turns" else "error_max_budget_usd"
            )
        else:
            error_subtype = "error_during_execution"
        error_strings.append(f"{type(e).__name__}: {e}")
    finally:
        await agent.aclose()

    duration_ms = int((time.monotonic() - started_at) * 1000)

    # Pull total cost from the agent's tracker (if one exists).
    total_cost_usd = 0.0
    if agent._budget_tracker is not None:
        total_cost_usd = agent._budget_tracker.total_usd

    # If we never populated model_usage (e.g. no usage events emitted by the
    # backend), fall back to the aggregate usage we tallied.
    if not model_usage and (agg_usage.input_tokens or agg_usage.output_tokens):
        model_usage[agent.model] = _to_model_usage(
            agent.model, agg_usage, backend_hint=backend_hint
        )

    result_msg = SDKResultMessage(
        subtype=error_subtype,
        duration_ms=duration_ms,
        duration_api_ms=duration_ms,  # we don't separately track API time yet
        is_error=is_error,
        num_turns=num_turns,
        result=final_text if not is_error else "",
        errors=error_strings,
        stop_reason=last_stop_reason,
        total_cost_usd=total_cost_usd,
        usage=agg_usage,
        modelUsage=model_usage,
        permission_denials=[
            SDKPermissionDenial(
                tool_name=d["tool_name"],
                tool_use_id=d["tool_use_id"],
                tool_input=d.get("tool_input") or {},
            )
            for d in getattr(agent, "_permission_denials", []) or []
        ],
        uuid=_new_uuid(),
        session_id=session_id,
    )
    if transcript is not None:
        transcript.write(result_msg)
        transcript.close()
    yield result_msg


__all__ = [
    "APIAssistantMessage",
    "APIUserMessage",
    "SDKAssistantMessage",
    "SDKCompactBoundaryMessage",
    "SDKMessage",
    "SDKPermissionDenial",
    "SDKResultMessage",
    "SDKStatusMessage",
    "SDKSystemMessage",
    "SDKUserMessage",
    "query",
]
