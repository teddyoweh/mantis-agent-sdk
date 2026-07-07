"""Claude Python SDK-shaped ``query()``.

The original ``mantis_agent.query.query`` yields TS-SDK-shape messages
(``SDKAssistantMessage`` with nested ``.message.content``). The Python
SDK ships *flat-shape* messages instead — ``AssistantMessage`` directly
has ``.content``, ``ResultMessage`` directly has ``.total_cost_usd``.

This module exposes a ``query()`` that yields those flat-shape messages,
matching the canonical examples at
https://github.com/anthropics/claude-agent-sdk-python/tree/main/examples
verbatim.

Top-level ``mantis_agent.query`` is wired to this when called with a
``MantisAgentOptions`` (or no options) so the Claude SDK examples just
work. When called with a dict it falls through to the existing dict-
options behavior.
"""

from __future__ import annotations

import os
import time
import uuid as _uuid
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

from .agent import Agent
from .budget import lookup_pricing
from .capabilities import lookup_model
from .claude_compat import (
    AssistantMessage,
    MantisAgentOptions,
    Message,
    ResultMessage,
    SystemMessage,
    UserMessage,
)
from .errors import BudgetExceededError
from .tools import Tool, ToolRegistry
from .types import (
    AssistantMessage as _InternalAssistantMessage,
    SystemMessage as _InternalSystemMessage,
    UserMessage as _InternalUserMessage,
    Usage,
)


__all__ = ["query"]


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def query(
    *,
    prompt: str | AsyncIterable[Any],
    options: MantisAgentOptions | dict[str, Any] | None = None,
) -> AsyncIterator[Message]:
    """Drop-in replacement for ``claude_agent_sdk.query``.

    Accepts either a :class:`MantisAgentOptions` instance or a plain
    dict. Yields flat-shape :class:`AssistantMessage`, :class:`UserMessage`,
    :class:`SystemMessage`, and :class:`ResultMessage` matching the
    Claude Python SDK examples 1:1.

    With no ``options``, defaults: model from ``$MANTIS_AGENT_MODEL`` else
    ``"qwen2.5-7b-instruct"``, backend from ``$MANTIS_AGENT_BASE_URL`` else
    ``http://localhost:11434``. So ``async for msg in query(prompt="hi"):``
    runs against local Ollama out of the box.
    """

    opts = _normalize_options(options)
    agent = _build_agent(opts)
    # Claude-SDK parity that used to be silently dropped:
    #   * options.agents={"name": AgentDefinition(...)} → each becomes a real
    #     delegatable subagent tool (named after the agent).
    #   * options.mcp_servers external transports (stdio/sse/http dicts or
    #     typed configs) → connected via MCPManager; tools land namespaced.
    _register_agent_definitions(agent, opts)
    _mcp_mgr = await _connect_external_mcp(agent, opts)

    async def _shutdown() -> None:
        if _mcp_mgr is not None:
            try:
                await _mcp_mgr.stop()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                pass
        await agent.aclose()

    session_id = opts.get("session_id") or _new_uuid()

    # 1) Session-init banner. Claude SDK ships ``SystemMessage(subtype="init",
    #    data={"tools": [...], "mcp_servers": [...], "model": "..."})``.
    yield SystemMessage(
        subtype="init",
        data={
            "tools": _system_tools_list(agent, opts),
            "mcp_servers": opts.get("mcp_servers", []),
            "model": agent.model,
            "permissionMode": opts.get("permission_mode", "default"),
            "cwd": opts.get("cwd", ""),
        },
        session_id=session_id,
    )

    # 2) Echo the user prompt as a UserMessage.
    if isinstance(prompt, str):
        yield UserMessage(content=prompt)
        seed_messages = [_InternalUserMessage(content=prompt)]
    else:
        seed_messages = []
        async for item in prompt:
            content = _extract_content(item)
            yield UserMessage(content=content)
            seed_messages.append(_InternalUserMessage(content=content))

    if not seed_messages:
        # Empty prompt — emit a no-op result and bail.
        yield _build_result(session_id=session_id, num_turns=0, usage=Usage(), backend_hint=None, model=agent.model, started_at=time.monotonic(), final_text="", error_subtype="error_during_execution", is_error=True, total_cost_usd=0.0, last_stop_reason=None, errors=["empty prompt"])
        await _shutdown()
        return

    # 3) Run the agent loop and translate each internal message.
    started_at = time.monotonic()
    num_turns = 0
    last_stop_reason: str | None = None
    agg_usage = Usage()
    final_text = ""
    error_subtype = "success"
    is_error = False
    error_strings: list[str] = []
    backend_hint = (
        agent.backend_capability.provider_hint
        if agent.backend_capability is not None
        else None
    )

    # Track which seed messages have already been echoed to the consumer
    # as UserMessage events above. ``run_iter`` may re-yield them (it can
    # prepend a synthetic <system-reminder> UserMessage) — we want every
    # NEW message it produces, but not duplicates of what we already
    # streamed. The cheapest test: was this message instance already in
    # ``seed_messages`` when we passed the list in?
    seed_set = {id(m) for m in seed_messages}

    try:
        running: list[Message] = list(seed_messages)
        # Stream messages out as the agent produces them — assistant
        # turns yield the moment their stream finalizes (tools are
        # already dispatching in the background under the
        # StreamingToolExecutor), and tool-result UserMessages yield as
        # soon as the executor finishes a batch. This is the streaming-
        # mode ``client.query()`` with mid-stream tool dispatch the
        # roadmap calls for.
        async for msg in agent.run_iter(running):
            if id(msg) in seed_set:
                # We already echoed this seed up front; skip the
                # re-yield from run_iter (it surfaces seeds + injected
                # meta context).
                continue
            if isinstance(msg, _InternalAssistantMessage):
                num_turns += 1
                last_stop_reason = msg.stop_reason or last_stop_reason
                if msg.usage is not None:
                    agg_usage = _accumulate_usage(agg_usage, msg.usage)
                yield AssistantMessage(
                    content=list(msg.content),
                    stop_reason=msg.stop_reason,
                    usage=msg.usage,
                )
                text = _extract_text(msg.content)
                if text:
                    final_text = text
            elif isinstance(msg, _InternalUserMessage):
                yield UserMessage(content=msg.content, isMeta=getattr(msg, "isMeta", False))
            elif isinstance(msg, _InternalSystemMessage):
                content = msg.content if isinstance(msg.content, str) else ""
                yield SystemMessage(
                    subtype="init",
                    data={"text": content},
                    session_id=session_id,
                )
    except BudgetExceededError as e:
        is_error = True
        # ``BudgetExceededError.kind`` is one of {"turns", "input_tokens",
        # "output_tokens", "total_tokens", "usd"} — see budget.py. The
        # Claude SDK only exposes two subtypes here, so anything that
        # isn't ``"turns"`` collapses to ``error_max_budget_usd``.
        error_subtype = (
            "error_max_turns" if e.kind == "turns" else "error_max_budget_usd"
        )
        error_strings.append(f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        is_error = True
        error_subtype = "error_during_execution"
        error_strings.append(f"{type(e).__name__}: {e}")
    finally:
        await _shutdown()

    duration_ms = int((time.monotonic() - started_at) * 1000)

    total_cost_usd = 0.0
    if agent._budget_tracker is not None:
        total_cost_usd = agent._budget_tracker.total_usd
    else:
        pricing = lookup_pricing(agent.model, backend_hint)
        if pricing is not None:
            total_cost_usd = pricing.cost(agg_usage)

    # Snapshot permission denials before _build_result so a closure-
    # mutated list doesn't surprise us. Each entry is a plain dict so
    # it serializes through msgspec cleanly.
    denials = list(getattr(agent, "_permission_denials", []))

    yield _build_result(
        session_id=session_id,
        num_turns=num_turns,
        usage=agg_usage,
        backend_hint=backend_hint,
        model=agent.model,
        started_at=started_at,
        final_text=final_text,
        error_subtype=error_subtype,
        is_error=is_error,
        total_cost_usd=total_cost_usd,
        last_stop_reason=last_stop_reason,
        errors=error_strings,
        permission_denials=denials,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_options(options: MantisAgentOptions | dict[str, Any] | None) -> dict[str, Any]:
    """Coerce options to a plain snake_case dict for ``_build_agent``."""

    if options is None:
        return {}
    if isinstance(options, MantisAgentOptions):
        return options.to_query_options()
    if isinstance(options, dict):
        return options
    raise TypeError(
        f"query(options): expected MantisAgentOptions, dict, or None, got {type(options).__name__}"
    )


def _build_agent(opts: dict[str, Any]) -> Agent:
    """Construct an Agent from the dict-form options with sensible defaults."""

    # Apply on-disk settings layered UNDERNEATH the user's options. The
    # caller's explicit MantisAgentOptions field always wins; settings
    # only fill the blanks. This is what makes
    # ``MantisAgentOptions(setting_sources=["user", "project"])`` actually
    # affect the agent's model / system prompt / allow lists, instead of
    # the field being merely typed-and-ignored.
    sources = opts.get("setting_sources")
    if sources:
        from .settings import apply_settings_to_options, load_settings

        loaded = load_settings(sources, cwd=opts.get("cwd"))
        if loaded:
            opts = apply_settings_to_options(opts, loaded)

    model = opts.get("model") or os.environ.get(
        "MANTIS_AGENT_MODEL", "qwen2.5-7b-instruct"
    )
    # Auto-route from model name when no backend was passed. Precedence:
    # explicit ``backend=`` > ``$MANTIS_AGENT_BASE_URL`` > shape-based
    # inference > Ollama default. This is what makes
    # ``MantisAgentOptions(model="qwen2.5:7b")`` work with no extra
    # kwarg — same two-line drop-in story as the Claude SDK.
    from .routing import resolve_backend
    backend = resolve_backend(model, opts.get("backend"))

    # Tool registry from Tool instances; built-in name strings are stashed.
    registry = ToolRegistry()
    raw_tools = opts.get("tools")
    if raw_tools:
        for t in raw_tools:
            if isinstance(t, Tool):
                registry.add(t)
            elif isinstance(t, ToolRegistry):
                for inner in t:
                    registry.add(inner)

    # Bridge in-process MCP servers (`mcp_servers={"calc": create_sdk_mcp_server(...)}`)
    # into the agent's tool registry under the Claude-SDK-style namespaced
    # names `mcp__{server}__{tool}`. Without this, tools registered on an
    # MCP server never reach the wire-format tools list the provider sends
    # to the model — the model has no idea those tools exist, so it just
    # answers in prose (or, worse, types "Call the multiply tool:" as text).
    #
    # Note on shape: MantisAgentOptions.to_query_options() normalizes
    # `mcp_servers` via `dict.items()`, so what we see here is a list of
    # `(name, config)` tuples — not bare configs. We accept either form.
    mcp_servers_opt = opts.get("mcp_servers") or []
    if isinstance(mcp_servers_opt, dict):
        mcp_servers_opt = list(mcp_servers_opt.items())
    for entry in mcp_servers_opt:
        if isinstance(entry, tuple) and len(entry) == 2:
            server_name, cfg = entry
        else:
            cfg = entry
            server_name = getattr(cfg, "name", None)
        # Only in-process `SdkServerConfig` exposes a directly-walkable
        # tool list. External transports (stdio/sse/http) get their tools
        # via the MCP `list_tools` round-trip, which the agent loop
        # handles separately.
        server = getattr(cfg, "server", None)
        if server is None:
            continue
        inner_registry = getattr(server, "registry", None)
        if not server_name:
            server_name = getattr(server, "name", None)
        if inner_registry is None or not server_name:
            continue
        for inner in inner_registry:
            namespaced = f"mcp__{server_name}__{inner.name}"
            registry.add(
                Tool(
                    name=namespaced,
                    description=inner.description,
                    input_schema=inner.input_schema,
                    fn=inner.fn,
                    is_concurrency_safe=inner.is_concurrency_safe,
                    abort_siblings_on_error=inner.abort_siblings_on_error,
                    is_read_only=inner.is_read_only,
                    timeout_s=inner.timeout_s,
                )
            )

    # `allowed_tools` filters what the model can see. The compat layer
    # stashes it under `opts["extra"]["allowed_tools"]`, not top-level —
    # check both. If set, drop anything not on the list (Claude-SDK parity).
    extra_opt = opts.get("extra") or {}
    allowed = opts.get("allowed_tools") or extra_opt.get("allowed_tools")
    if allowed:
        allowed_set = set(allowed)
        filtered = ToolRegistry()
        for t in registry:
            if t.name in allowed_set:
                filtered.add(t)
        registry = filtered

    kw: dict[str, Any] = {
        "model": model,
        "backend": backend,
        "system": opts.get("system"),
        "tools": registry,
        "max_tokens": opts.get("max_tokens", 1024),
        "temperature": opts.get("temperature"),
        "max_steps": opts.get("max_turns", opts.get("max_steps", 20)),
        "max_usd": opts.get("max_usd"),
    }
    for key in ("hooks", "permissions", "budget", "include_memory", "response_format", "tracer"):
        if key in opts:
            kw[key] = opts[key]

    # ``allowed_tools`` is intentionally NOT in ``consumed`` — it flows
    # through to ``extra`` so ``_system_tools_list`` can read it for the
    # init SystemMessage (Claude-SDK parity: the init message advertises
    # only the allowed names). We've already filtered the registry above.
    consumed = set(kw.keys()) | {
        "model", "backend", "tools", "system", "max_tokens", "temperature",
        "max_turns", "max_steps", "max_usd", "api_key",
        "persist", "session_id", "cwd", "permission_mode",
        "mcp_servers", "agents", "setting_sources", "response_format",
        "tracer",
    }
    extra = {k: v for k, v in opts.items() if k not in consumed}
    if extra:
        kw["extra"] = extra

    return Agent(**kw)


def _register_agent_definitions(agent: Agent, opts: dict[str, Any]) -> None:
    """``options.agents={"reviewer": AgentDefinition(...)}`` → register each as
    a delegatable subagent TOOL named after the agent (Claude SDK parity — this
    field was previously accepted and silently ignored). ``defn.tools`` narrows
    the child's kit by name; empty means the parent's full kit (minus other
    agents, to prevent recursion)."""
    agents_opt = opts.get("agents") or {}
    if not isinstance(agents_opt, dict) or not agents_opt:
        return
    from .subagent import SubAgentSpec, as_subagent_tool  # noqa: PLC0415

    names = set(agents_opt)
    base_kit = [t for t in agent.tools if t.name not in names]
    for name, defn in agents_opt.items():
        wanted = set(getattr(defn, "tools", None) or [])
        kit = [t for t in base_kit if not wanted or t.name in wanted]
        spec = SubAgentSpec(
            name=str(name),
            system_prompt=getattr(defn, "prompt", "") or "",
            model=getattr(defn, "model", None) or agent.model,
            tools=kit,
            description=getattr(defn, "description", None),
        )
        agent.tools.add(as_subagent_tool(spec, parent_provider=agent.provider))


async def _connect_external_mcp(agent: Agent, opts: dict[str, Any]) -> Any:
    """Connect EXTERNAL MCP servers from ``options.mcp_servers`` (stdio/sse/
    http — Claude Code config dicts or typed configs) and fold their tools into
    the agent as ``mcp__{server}__{tool}``. In-process SdkServerConfig entries
    are bridged synchronously in ``_build_agent``; external transports were
    previously dropped on the floor. Returns the manager (caller closes it),
    or None when nothing external is configured."""
    entries = opts.get("mcp_servers") or []
    if isinstance(entries, dict):
        entries = list(entries.items())
    from .mcp.manager import MCPManager, parse_server_entry  # noqa: PLC0415
    from .mcp.types import (  # noqa: PLC0415
        HttpServerConfig,
        SseServerConfig,
        StdioServerConfig,
    )
    configs: dict[str, Any] = {}
    for i, entry in enumerate(entries):
        if isinstance(entry, tuple) and len(entry) == 2:
            name, cfg = entry
        else:
            name, cfg = getattr(entry, "name", None) or f"server{i + 1}", entry
        if cfg is None or getattr(cfg, "server", None) is not None:
            continue  # in-process SDK server — already bridged
        if isinstance(cfg, dict):
            parsed = parse_server_entry(cfg)
        elif isinstance(cfg, (StdioServerConfig, SseServerConfig, HttpServerConfig)):
            parsed = cfg
        else:
            parsed = None
        if parsed is not None and name:
            configs[str(name)] = parsed
    if not configs:
        return None
    mgr = MCPManager(configs)
    tools = await mgr.start()   # dedicated-task lifetime — generator-safe
    if tools:
        agent.tools.add(*tools)
    return mgr


def _system_tools_list(agent: Agent, opts: dict[str, Any]) -> list[str]:
    """Build the ``data.tools`` list for the init SystemMessage.

    Includes:
      * names of real ``Tool`` instances passed in
      * built-in tool-name strings the user passed (``"Read"``, ``"Glob"``…),
        stashed on ``opts["extra"]["builtin_tool_names"]`` by the compat layer
      * MCP-namespaced names from configured ``mcp_servers``
    """

    names: list[str] = [t.name for t in agent.tools]

    extra = opts.get("extra") or {}
    builtin = extra.get("builtin_tool_names") or []
    names.extend(builtin)

    # If user supplied allowed_tools, it overrides everything (Claude SDK
    # surfaces only allowed tool names in the init message when set).
    allowed = extra.get("allowed_tools")
    if allowed:
        return list(allowed)
    return names


def _accumulate_usage(agg: Usage, msg: Usage) -> Usage:
    return Usage(
        input_tokens=agg.input_tokens + msg.input_tokens,
        output_tokens=agg.output_tokens + msg.output_tokens,
        cache_creation_input_tokens=msg.cache_creation_input_tokens or agg.cache_creation_input_tokens,
        cache_read_input_tokens=msg.cache_read_input_tokens or agg.cache_read_input_tokens,
    )


def _extract_text(blocks: Any) -> str:
    from .types import TextBlock as _TB
    if not isinstance(blocks, list):
        return ""
    return "".join(b.text for b in blocks if isinstance(b, _TB))


def _extract_content(item: Any) -> Any:
    if isinstance(item, str):
        return item
    if isinstance(item, UserMessage):
        return item.content
    if isinstance(item, _InternalUserMessage):
        return item.content
    if isinstance(item, dict):
        return item.get("content", "")
    return str(item)


def _new_uuid() -> str:
    return str(_uuid.uuid4())


def _build_result(
    *,
    session_id: str,
    num_turns: int,
    usage: Usage,
    backend_hint: str | None,
    model: str,
    started_at: float,
    final_text: str,
    error_subtype: str,
    is_error: bool,
    total_cost_usd: float,
    last_stop_reason: str | None,
    errors: list[str],
    permission_denials: list | None = None,
) -> ResultMessage:
    duration_ms = int((time.monotonic() - started_at) * 1000)
    # Flatten Usage to a dict for the wire — keeps ResultMessage
    # decoupled from the internal Usage struct and matches Claude SDK.
    usage_dict = (
        {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
        }
        if (usage.input_tokens or usage.output_tokens)
        else {}
    )
    model_usage_dict = (
        {model: dict(usage_dict, costUSD=total_cost_usd)} if usage_dict else {}
    )
    return ResultMessage(
        subtype=error_subtype if is_error else "success",
        duration_ms=duration_ms,
        duration_api_ms=duration_ms,
        is_error=is_error,
        num_turns=num_turns,
        result=final_text if not is_error else None,
        stop_reason=last_stop_reason,
        total_cost_usd=total_cost_usd,
        session_id=session_id,
        permission_denials=list(permission_denials or []),
        usage=usage_dict,
        modelUsage=model_usage_dict,
    )
