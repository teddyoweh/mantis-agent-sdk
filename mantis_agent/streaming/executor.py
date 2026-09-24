"""``StreamingToolExecutor`` — kick off tool calls as soon as their JSON
finalizes mid-stream, run them with the right concurrency rules, and produce
``ToolResultBlock``s as they complete (or in insertion order via
``wait_all`` when the turn is done).

This is the *speed unlock* described in ``docs/plan.md`` §5. The naive
"wait until message_stop, then dispatch" loop pays a per-tool serialization
tax even when tools could overlap. By starting each tool the moment its
``</tool_call>`` / native ``tool_use.stop`` arrives, multi-tool turns finish
in roughly ``max(tool_durations)`` rather than ``sum(tool_durations)``.

Two ways to observe results
---------------------------
* ``await ex.wait_all()`` — block until every dispatched tool finishes
  and return a list of ``ToolResultBlock``s in **insertion order**
  (same order as ``add_tool_call`` calls). Use this when downstream
  needs the canonical result list for the assistant turn.
* ``async for idx, result in ex.iter_completions(): ...`` — yield
  ``(idx, ToolResultBlock)`` pairs in **completion order** as each tool
  finishes. ``idx`` is the insertion index so callers can correlate. Use
  this for live UIs that want to render "tool #2 done (120ms)" the
  moment it completes, without waiting on slower siblings.
* ``await ex.wait_one()`` — single-shot variant of ``iter_completions``.
  Returns the next completion as ``(idx, ToolResultBlock)`` or ``None``
  if the executor has closed with no further work pending.

Behavior summary
----------------
* **Concurrency-safe tools** run in parallel, bounded by
  ``MANTIS_AGENT_MAX_TOOL_CONCURRENCY`` (env, default 10) or the
  ``max_concurrency`` constructor arg.
* **Non-concurrency-safe tools** are ordering barriers (Claude Code's
  semantics, by insertion index): an unsafe call waits for *every* earlier
  call to finish, and every later call waits for the most recent earlier
  unsafe call. Consecutive safe calls still overlap, so ``edit_file`` then
  ``read_file`` in one turn can never see pre-edit state, while three
  ``grep``s run in parallel. A barrier releases however its call ends
  (result, error, denial, timeout, cancellation), so waiters never hang.
* ``Tool.is_concurrency_safe`` may be a bool *or* a callable
  ``(input) -> bool``. Callable form lets ``bash`` say "safe iff paths
  don't conflict" etc.
* ``Tool.abort_siblings_on_error`` — when a tool with this flag errors,
  every in-flight sibling under the executor is cancelled via a shared
  ``CancelScope`` and produces a ``ToolResultBlock(is_error=True)``.
* ``Tool.timeout_s`` — wraps the call in ``anyio.fail_after``; on timeout
  the result is ``is_error=True``.
* User-tool exceptions never propagate. They become ``is_error=True``
  result blocks. The executor is bulletproof against bad tool code.
* Missing tools become ``is_error=True`` immediately.
* ``can_use_tool(tool, input, ctx)`` — if supplied, gates every call.
  Denials become ``is_error=True`` with the reason as the body.

Anyio, not asyncio
------------------
We use ``anyio`` primitives throughout: ``create_task_group``,
``CancelScope``, ``Semaphore``, ``Lock``, ``fail_after``,
``create_memory_object_stream``. The agent loop must be driven by an
anyio backend (default trio or asyncio under ``anyio.run``). Never
reach for ``asyncio.*`` here.
"""

from __future__ import annotations

import contextvars
import logging
import math
import os
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import nullcontext
from copy import deepcopy
from typing import Any, Optional

try:
    BaseExceptionGroup
except NameError:  # pragma: no cover - Python 3.10 only
    from exceptiongroup import BaseExceptionGroup

import anyio
import anyio.abc
import msgspec

from ..errors import ToolExecutionError
from ..tools import Tool, ToolRegistry
from ..tools import unknown_tool_message as _unknown_tool_message
from ..types import ImageBlock, TextBlock, ToolResultBlock, ToolUseBlock

__all__ = [
    "CanUseToolFn",
    "StreamingToolExecutor",
    "normalize_tool_input",
    "result_char_budget_for",
    "tool_result_char_cap",
    "truncate_middle",
]

_LOG = logging.getLogger("mantis_agent.streaming.executor")
_ENC = msgspec.json.Encoder()
_DEFAULT_MAX_CONCURRENCY = 10
_ENV_VAR = "MANTIS_AGENT_MAX_TOOL_CONCURRENCY"


# Signature: (tool, input, ctx) -> (allowed, reason_if_denied)
CanUseToolFn = Callable[
    [Tool, dict[str, Any], dict[str, Any]],
    Awaitable[tuple[bool, Optional[str]]],
]


def _resolve_max_concurrency(override: int | None) -> int:
    if override is not None and override > 0:
        return override
    raw = os.environ.get(_ENV_VAR)
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            _LOG.warning("ignoring invalid %s=%r", _ENV_VAR, raw)
    return _DEFAULT_MAX_CONCURRENCY


def _is_safe(tool: Tool, tool_input: dict[str, Any]) -> bool:
    """Resolve ``Tool.is_concurrency_safe`` against this input.

    Accepts:
      * a static ``bool`` (the common case)
      * a callable ``(input) -> bool``
      * the legacy ``parallel_safe: bool`` attribute (current tools.py)

    The callable path lets ``bash``-class tools decide per-input —
    e.g. two writes to disjoint paths can parallelize."""
    flag = getattr(tool, "is_concurrency_safe", None)
    if flag is None:
        # Fall back to the older static attribute on Tool. v0 of tools.py uses
        # ``parallel_safe: bool``; the M0.1 refactor renames + extends it.
        flag = getattr(tool, "parallel_safe", True)
    if callable(flag):
        try:
            return bool(flag(tool_input))
        except Exception:  # noqa: BLE001 — predicate must never crash dispatch
            _LOG.exception("is_concurrency_safe predicate raised; assuming UNSAFE")
            return False
    return bool(flag)


def _stringify(out: Any) -> str:
    if isinstance(out, str):
        return out
    try:
        return _ENC.encode(out).decode()
    except (TypeError, msgspec.EncodeError):
        return str(out)


# Tool-result truncation backstop. A single huge tool result (a `cat bigfile`,
# a noisy build log, an MCP tool dumping JSON) can blow the whole context window
# in one turn — most builtin tools self-cap, but this guards custom / MCP tools
# and full-file reads. Head+tail are kept (the ends usually carry the signal);
# the middle is elided with a note. Caps are tool-aware (reads/shell get more
# room than a grep). Override the default via MANTIS_AGENT_MAX_TOOL_RESULT.
try:
    _DEFAULT_TOOL_RESULT_CAP = max(2000, int(os.environ.get("MANTIS_AGENT_MAX_TOOL_RESULT", "30000")))
except ValueError:
    _DEFAULT_TOOL_RESULT_CAP = 30000

_TOOL_RESULT_CAPS = {
    "read_file": 60_000,
    "bash": 40_000,
    "web_fetch": 40_000,
}

# The static caps above are sized for a 128k+ model. On an 8k/32k local model a
# single 60k-char read is the whole window, so when the loop knows its budget
# the per-result cap shrinks to a slice of it (``result_char_budget_for``) —
# never below this floor, never above the static cap.
_MIN_RESULT_CAP = 4_000
# Share of the conversation's token budget one tool result may take, and the
# chars-per-token rule of thumb used to turn that into a char cap.
_RESULT_BUDGET_SHARE = 0.15
_CHARS_PER_TOKEN = 4

# The effective cap for the tool currently running, set by the executor around
# ``tool.fn`` so self-limiting tools (read_file, bash) can cut on a clean line
# boundary with a precise "continue at offset N" notice instead of having the
# backstop elide their middle. ``None`` outside an executor-dispatched call.
_RESULT_CHAR_CAP: "contextvars.ContextVar[int | None]" = contextvars.ContextVar(
    "mantis_tool_result_char_cap", default=None
)


def tool_result_char_cap() -> int | None:
    """Char cap the executor will apply to the running tool's result, or
    ``None`` when the tool is called directly (no executor)."""
    return _RESULT_CHAR_CAP.get()


def result_char_budget_for(message_budget_tokens: int | None) -> int | None:
    """Per-result char budget for a conversation that may use
    ``message_budget_tokens`` tokens (``Agent._message_budget()``): ~15% of it
    at ~4 chars/token, floored at 4k chars. ``None`` (= static caps) when the
    budget is unknown."""
    if not message_budget_tokens or message_budget_tokens <= 0:
        return None
    return max(
        _MIN_RESULT_CAP,
        int(message_budget_tokens * _RESULT_BUDGET_SHARE * _CHARS_PER_TOKEN),
    )


def _result_cap(tool_name: str, budget: int | None) -> int:
    static = _TOOL_RESULT_CAPS.get(tool_name, _DEFAULT_TOOL_RESULT_CAP)
    if budget is None or budget <= 0:
        return static
    return min(static, max(_MIN_RESULT_CAP, budget))


# How the model gets at what a truncation dropped, per tool.
_TRUNCATION_HINTS = {
    "read_file": "Read the elided range with read_file(path, offset=<line>, limit=<n>).",
    "bash": (
        "Re-run with a filter (grep / head / tail), or redirect the output to a "
        "file and read it in ranges."
    ),
    "bash_output": "Filter the command's output (grep / tail) instead of dumping it.",
}
_DEFAULT_TRUNCATION_HINT = (
    "Re-run with a narrower query, a line range, or a filter to see the middle."
)


# WEAK-keyed: an ``id(fn)``-keyed dict poisoned this cache — when a tool
# closure was GC'd (mantis rebuilds tool closures on every model switch), a
# NEW function allocated at the recycled address inherited the OLD signature,
# and the executor then silently dropped the new tool's real arguments. A weak
# key dies with its function, so a recycled address can never alias.
_ACCEPTED_PARAMS_CACHE: "weakref.WeakKeyDictionary[Any, frozenset[str] | None]" = (
    weakref.WeakKeyDictionary()
)


def _accepted_params(fn: Any) -> frozenset[str] | None:
    """Parameter names ``fn`` accepts, or ``None`` if it takes ``**kwargs`` (=
    accept anything). Weakly cached on the function object itself."""
    import inspect  # noqa: PLC0415

    try:
        return _ACCEPTED_PARAMS_CACHE[fn]
    except (KeyError, TypeError):  # TypeError: unhashable/unweakrefable fn
        pass
    try:
        params = inspect.signature(fn).parameters
    except (ValueError, TypeError):
        result: frozenset[str] | None = None
    else:
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            result = None
        else:
            result = frozenset(params)
    try:
        _ACCEPTED_PARAMS_CACHE[fn] = result
    except TypeError:
        pass  # can't weakref this callable — just recompute next time
    return result


_TRUE_STRS = frozenset({"true", "yes", "1", "on", "y", "t"})
_FALSE_STRS = frozenset({"false", "no", "0", "off", "n", "f", ""})


def _coerce_value(v: Any, jtype: str | None) -> Any:
    """Coerce a single value to the JSON-schema type — models pass numbers/bools
    as strings constantly. Best-effort: leaves the value unchanged if it can't
    convert cleanly (so the tool still sees SOMETHING rather than a wrong guess)."""
    if jtype == "integer" and isinstance(v, str):
        try:
            return int(float(v.strip()))
        except ValueError:
            return v
    if jtype == "number" and isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return v
    if jtype == "boolean":
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in _TRUE_STRS:
                return True
            if s in _FALSE_STRS:
                return False
    if jtype in ("array", "object") and isinstance(v, str):
        try:
            parsed = msgspec.json.decode(v.encode())
        except (msgspec.DecodeError, ValueError):
            return v
        if (jtype == "array" and isinstance(parsed, list)) or (
            jtype == "object" and isinstance(parsed, dict)
        ):
            return parsed
    return v


def _coerce_to_schema(input: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce each arg to the type its ``input_schema`` property declares, so a
    model that passes ``"10"`` for an int or ``"true"`` for a bool still gets a
    working call. No-op when there's no typed schema."""
    props = (schema or {}).get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not input:
        return input
    out: dict[str, Any] = {}
    changed = False
    for k, v in input.items():
        spec = props.get(k)
        jtype = spec.get("type") if isinstance(spec, dict) else None
        nv = _coerce_value(v, jtype) if jtype else v
        out[k] = nv
        changed = changed or (nv is not v)
    return out if changed else input


def _filter_tool_input(fn: Any, input: dict[str, Any]) -> dict[str, Any]:
    """Drop keys ``fn`` won't accept so a model's hallucinated extra arg doesn't
    TypeError the call. Pass-through when ``fn`` takes ``**kwargs`` or ``input``
    is already clean."""
    accepted = _accepted_params(fn)
    if accepted is None or not input:
        return input
    if all(k in accepted for k in input):
        return input
    return {k: v for k, v in input.items() if k in accepted}


# Argument-name aliases: canonical param → the spellings models trained on other
# harnesses (Claude Code's ``file_path``, SWE-agent's ``old_str``, rg's ``-i``)
# reach for. Applied only when the tool's signature takes the canonical name, the
# canonical key is absent, and the alias is NOT itself a real param — so a present
# key is never overwritten and tools with ``**kwargs`` (MCP, explicit schemas)
# are never touched. First alias present (in tuple order) wins.
_ARG_ALIASES: dict[str, tuple[str, ...]] = {
    "path": ("file_path", "filepath", "filename", "file", "notebook_path",
             "dir", "directory", "dir_path"),
    "command": ("cmd", "command_line", "shell_command"),
    "old_string": ("old_str", "old_text", "search", "find"),
    "new_string": ("new_str", "new_text", "replace", "replacement"),
    "content": ("text", "contents", "file_text", "data"),
    "pattern": ("query", "regex", "search_pattern"),
    "ignore_case": ("-i", "case_insensitive", "insensitive"),
    "context_lines": ("-C", "context", "-A", "-B"),
    "file_type": ("type",),
}

# Per-tool aliases whose VALUE needs a unit change: tool → {alias: (canonical,
# transform)}. Claude Code's Bash ``timeout`` is milliseconds; ours is seconds, so
# only the unambiguous ``timeout_ms`` spelling is converted (a bare ``timeout`` is
# already our canonical name and is left alone).
_ARG_TRANSFORMS: dict[str, dict[str, tuple[str, Any]]] = {
    "bash": {"timeout_ms": ("timeout", lambda ms: max(1, -(-int(float(ms)) // 1000)))},
}


def _apply_arg_aliases(
    tool_name: str, fn: Any, input: dict[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Rename aliased argument keys to the tool's canonical params. Returns the
    (possibly new) input and the ``(alias, canonical)`` pairs that fired."""
    accepted = _accepted_params(fn)
    if accepted is None or not input or all(k in accepted for k in input):
        return input, []
    out = dict(input)
    fired: list[tuple[str, str]] = []
    for alias, (canonical, transform) in _ARG_TRANSFORMS.get(tool_name, {}).items():
        if canonical in accepted and canonical not in out and alias in out \
                and alias not in accepted:
            try:
                out[canonical] = transform(out[alias])
            except (TypeError, ValueError, OverflowError):
                continue  # unconvertible — leave it for the filter to drop
            del out[alias]
            fired.append((alias, canonical))
    for canonical, aliases in _ARG_ALIASES.items():
        if canonical not in accepted or canonical in out:
            continue
        present = [a for a in aliases if a in out and a not in accepted]
        if not present:
            continue
        value = out.pop(present[0])
        fired.append((present[0], canonical))
        if canonical == "context_lines":
            # ``-A`` / ``-B`` / ``-C`` together: one symmetric window that
            # covers the widest the model asked for, not whichever came first.
            for extra in present[1:]:
                other = out.pop(extra)
                fired.append((extra, canonical))
                try:
                    value = max(int(value), int(other))
                except (TypeError, ValueError):
                    pass
        out[canonical] = value
    return (out, fired) if fired else (input, [])


def normalize_tool_input(
    tool: Any, input: Any,
) -> tuple[Any, list[tuple[str, str]]]:
    """Canonicalize a call's argument NAMES for ``tool`` (``file_path`` → ``path``,
    ``cmd`` → ``command``, ``timeout_ms`` → seconds). Returns ``(input, fired)``;
    ``input`` is returned unchanged (same object) when nothing fired.

    The agent loop calls this BEFORE the PreToolUse hook and the permission
    check, so hooks, deny rules (``Edit(secrets/**)`` binds to ``path``;
    ``Bash(rm:*)`` to ``command``) and tracing all see exactly the keys the
    executor will run with — an alias can't slip a call past a rule keyed on
    the canonical name. Idempotent: an already-canonical input is a no-op, so
    the executor re-applying it is safe."""
    if not isinstance(input, dict) or tool is None:
        return input, []
    return _apply_arg_aliases(
        getattr(tool, "name", ""), getattr(tool, "fn", None), input,
    )


def _missing_required(fn: Any, call_input: dict[str, Any]) -> list[str]:
    """Params ``fn`` requires (no default) that ``call_input`` lacks. Empty for
    ``**kwargs`` tools — their schema is validated by whoever implements them."""
    import inspect  # noqa: PLC0415

    if _accepted_params(fn) is None:
        return []
    try:
        params = inspect.signature(fn).parameters
    except (ValueError, TypeError):
        return []
    return [
        n for n, p in params.items()
        if p.default is inspect.Parameter.empty
        and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                       inspect.Parameter.KEYWORD_ONLY)
        and n not in call_input
    ]


def _expected_params(tool: Any) -> str:
    """``path (string, required), offset (integer, optional)`` — from the tool's
    input_schema, else its signature."""
    schema = getattr(tool, "input_schema", None)
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict) and props:
        required = set(schema.get("required") or ())
        parts = []
        for name, spec in props.items():
            jtype = spec.get("type") if isinstance(spec, dict) else None
            bits = ([jtype] if isinstance(jtype, str) else []) + [
                "required" if name in required else "optional"
            ]
            parts.append(f"{name} ({', '.join(bits)})")
        return ", ".join(parts)
    accepted = _accepted_params(getattr(tool, "fn", None))
    return ", ".join(sorted(accepted)) if accepted else "(unknown)"


def _arg_error_message(
    tool: Any, received: dict[str, Any], call_input: dict[str, Any],
    missing: list[str], fired: list[tuple[str, str]],
) -> str:
    """A tool-result error that teaches: what was missing, what was ignored, what
    the tool actually takes — so the model's retry is different from its call."""
    renamed = {a: c for a, c in fired}
    notes = []
    for k in received:
        if k in renamed:
            notes.append(f"{k} (used as {renamed[k]})")
        elif k not in call_input:
            notes.append(f"{k} (ignored — unknown)")
        else:
            notes.append(k)
    head = (
        f"{tool.name}: missing required argument"
        f"{'s' if len(missing) > 1 else ''} {', '.join(repr(m) for m in missing)}."
        if missing else f"{tool.name}: invalid arguments."
    )
    return (
        f"{head} Received: {', '.join(notes) or '(none)'}. "
        f"Expected: {_expected_params(tool)}."
    )


def _as_block_content(out: Any) -> list[Any] | None:
    """If a tool returned rich content (an ImageBlock / TextBlock, or a list of
    them — e.g. a multimodal ``read_file`` handing back an image), pass it
    through as the tool_result content instead of stringifying it. Otherwise
    return None so the caller falls back to the text path."""
    if isinstance(out, (ImageBlock, TextBlock)):
        return [out]
    if isinstance(out, list) and out and all(isinstance(b, (ImageBlock, TextBlock)) for b in out):
        return list(out)
    return None


def truncate_middle(
    text: str,
    cap: int,
    *,
    head_ratio: float = 2 / 3,
    hint: str = _DEFAULT_TRUNCATION_HINT,
) -> str:
    """Keep the head and tail of ``text`` within ``cap`` chars (note included),
    eliding the middle with a note that says how to get it back. ``head_ratio``
    is the share of the kept body given to the head — build logs want more
    tail (the failing summary is at the end)."""
    if len(text) <= cap:
        return text
    note_fmt = "\n\n… [{dropped:,} characters elided (output truncated) — {total:,} total. {hint}] …\n\n"
    # Size the body so body + note fits the cap (the note's own digits are
    # bounded by len(text), so measure with that).
    note_len = len(note_fmt.format(dropped=len(text), total=len(text), hint=hint))
    if cap <= note_len:
        # No room for the note: a hard cut is the only way to honour the cap.
        return text[: max(0, cap)]
    body = cap - note_len
    head = int(body * head_ratio)
    tail = body - head
    dropped = len(text) - head - tail
    note = note_fmt.format(dropped=dropped, total=len(text), hint=hint)
    return text[:head] + note + (text[-tail:] if tail > 0 else "")


def _truncate_tool_result(text: str, tool_name: str, budget: int | None = None) -> str:
    return truncate_middle(
        text,
        _result_cap(tool_name, budget),
        hint=_TRUNCATION_HINTS.get(tool_name, _DEFAULT_TRUNCATION_HINT),
    )


def _truncate_block_content(blocks: list[Any], tool_name: str, budget: int | None) -> list[Any]:
    """Apply the text cap to the TextBlocks of a rich result (typical MCP
    output). Images pass through untouched; over budget, each text block keeps
    a share of the cap proportional to its size, drawn from one running budget
    so the text total stays within the cap however many blocks there are. Once
    the budget is spent, the remaining text blocks collapse into one note."""
    texts = [b for b in blocks if isinstance(b, TextBlock) and b.text]
    total = sum(len(b.text) for b in texts)
    cap = _result_cap(tool_name, budget)
    if total <= cap:
        return blocks
    hint = _TRUNCATION_HINTS.get(tool_name, _DEFAULT_TRUNCATION_HINT)
    note_fmt = "[… {n:,} more text block(s) ({chars:,} characters) elided (output truncated). {hint}]"
    # Reserve room for that note up front (digits bounded by ``total``).
    remaining = cap - len(note_fmt.format(n=len(texts), chars=total, hint=hint))
    # A per-block floor keeps small blocks readable — but only when every
    # block's floor fits; 60 floored blocks would otherwise blow the cap 15x.
    floor = _MIN_RESULT_CAP // 4
    use_floor = len(texts) * floor <= cap
    out: list[Any] = []
    elided_n = elided_chars = 0
    for b in blocks:
        if isinstance(b, TextBlock) and b.text:
            if remaining < 200:  # spent — a sliver of a block helps nobody
                elided_n += 1
                elided_chars += len(b.text)
                continue
            share = max(floor if use_floor else 200, cap * len(b.text) // total)
            share = min(share, remaining)
            b = msgspec.structs.replace(b, text=truncate_middle(b.text, share, hint=hint))
            remaining -= len(b.text)
        out.append(b)
    if elided_n:
        out.append(TextBlock(text=note_fmt.format(n=elided_n, chars=elided_chars, hint=hint)))
    return out


def _flatten_exception_group(eg: BaseExceptionGroup) -> list[BaseException]:
    """Walk a (possibly nested) ``BaseExceptionGroup`` and return the leaf
    exceptions. anyio 4.x can nest groups arbitrarily — a single-leaf
    group nested two deep should still unwrap cleanly."""
    out: list[BaseException] = []
    for sub in eg.exceptions:
        if isinstance(sub, BaseExceptionGroup):
            out.extend(_flatten_exception_group(sub))
        else:
            out.append(sub)
    return out


class StreamingToolExecutor:
    """Dispatch tool calls as they arrive on the stream.

    Lifecycle::

        async with StreamingToolExecutor(registry) as ex:
            # in the stream consumer, for every ToolUseBlock that finalizes:
            ex.add_tool_call(block)
            # ...after message_stop:
            results = await ex.wait_all()

    The context-manager exit awaits every still-in-flight task (so leaving
    the ``async with`` block is a safe join point even if you forget to
    call ``wait_all``)."""

    __slots__ = (
        "_registry",
        "_can_use_tool",
        "_max_concurrency",
        "_sem",
        "_serial_lock",
        "_done_events",
        "_last_unsafe_idx",
        "_calls",
        "_executed_calls",
        "_results",
        "_pending",
        "_idle_event",
        "_tg",
        "_scopes",
        "_aborted",
        "_signal_cancelled",
        "_cancellation_signal",
        "_watcher_scope",
        "_closed",
        # Completion-streaming channel — every ``_results[idx] = result``
        # assignment goes through ``_record_result`` which pushes
        # ``(idx, result)`` here. Consumers iterate via ``iter_completions``
        # (completion order) or ``wait_all`` (insertion order).
        "_completion_send",
        "_completion_recv",
        "_completion_closed",
        "_tracer",
        "_trace_parent",
        "_result_char_budget",
    )

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_concurrency: int | None = None,
        can_use_tool: CanUseToolFn | None = None,
        cancellation_signal: "anyio.Event | None" = None,
        tracer: "Any | None" = None,
        trace_parent: "Any | None" = None,
        result_char_budget: int | None = None,
    ) -> None:
        self._registry = registry
        # Per-result char budget derived from the model's context window (see
        # ``result_char_budget_for``). ``None`` keeps the static caps — the
        # right default when the window is unknown.
        self._result_char_budget = result_char_budget
        self._can_use_tool = can_use_tool
        # Tracing — set by the agent loop when ``Agent.tracer`` is on. When
        # both are ``None`` the executor pays zero overhead. Each tool
        # dispatch opens a ``tool.call`` span nested under ``trace_parent``
        # (the per-turn span) — see ``_run_one`` for the wiring.
        self._tracer = tracer
        self._trace_parent = trace_parent
        self._max_concurrency = _resolve_max_concurrency(max_concurrency)
        # Parallel slots for concurrency-safe tools.
        self._sem = anyio.Semaphore(self._max_concurrency)
        # One-at-a-time lock for non-concurrency-safe tools.
        self._serial_lock = anyio.Lock()
        # Ordering barriers: ``_done_events[i]`` fires when call ``i`` has
        # finished (any outcome). ``_last_unsafe_idx`` is the newest unsafe
        # call so later calls know which barrier to wait behind.
        self._done_events: list[anyio.Event] = []
        self._last_unsafe_idx: int | None = None
        # Track calls in insertion order.
        self._calls: list[ToolUseBlock] = []
        self._executed_calls: list[ToolUseBlock] = []
        self._results: list[ToolResultBlock | None] = []
        # Number of dispatched-but-not-yet-finished tasks. ``wait_all`` blocks
        # on ``_idle_event`` until this reaches zero. Reset whenever a new
        # call is added so callers can interleave dispatch and waiting.
        self._pending = 0
        self._idle_event = anyio.Event()
        self._idle_event.set()  # start "idle" — no pending work
        # Set in __aenter__.
        self._tg: anyio.abc.TaskGroup | None = None
        # Per-task cancel scopes, kept so sibling-abort can cancel every
        # in-flight task at once. Indexed in insertion order.
        self._scopes: list[anyio.CancelScope] = []
        self._aborted = False
        # Distinct from ``_aborted``: that flag covers
        # ``abort_siblings_on_error`` (one tool crashed, kill the rest).
        # ``_signal_cancelled`` covers external cancellation via the
        # cancellation_signal Event (``Agent.cancel()``, budget overrun,
        # any future abort path). They produce different
        # ToolResultBlock messages so callers can distinguish.
        self._signal_cancelled = False
        # Shared event the agent fires to cancel the whole run. We
        # observe it via a watcher task started in ``__aenter__``; when
        # it fires we mark ``_signal_cancelled`` and cancel every
        # per-task CancelScope so in-flight tools die fast.
        self._cancellation_signal = cancellation_signal
        # Scope wrapping the watcher task so ``__aexit__`` can wake it
        # up when the executor closes cleanly (signal never fired).
        self._watcher_scope: anyio.CancelScope | None = None
        self._closed = False
        # Completion-stream wiring. Unbounded buffer so result-recording
        # paths never block; the buffer holds tiny ``(int, ToolResultBlock)``
        # tuples. The send side is closed in ``__aexit__`` so consumers of
        # ``iter_completions`` terminate cleanly. We must allocate the
        # streams here (cheap) so callers can grab a receive handle BEFORE
        # entering the ``async with`` block — useful for spinning up a
        # consumer task in the same task group as the executor.
        send, recv = anyio.create_memory_object_stream[
            tuple[int, ToolResultBlock]
        ](max_buffer_size=math.inf)
        self._completion_send: anyio.abc.ObjectSendStream[
            tuple[int, ToolResultBlock]
        ] = send
        self._completion_recv: anyio.abc.ObjectReceiveStream[
            tuple[int, ToolResultBlock]
        ] = recv
        self._completion_closed = False

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "StreamingToolExecutor":
        # We do NOT enter the task group as ``async with`` here because that
        # would block on every task at exit. Instead we manage it manually so
        # ``add_tool_call`` can ``start_soon`` into it across the lifetime.
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        # Wire cancellation_signal handling. Two cases:
        #   1. Signal is already set (caller fired ``cancel()`` BEFORE
        #      entering this executor). Mark ourselves cancelled now so
        #      ``add_tool_call`` short-circuits every call — no watcher
        #      needed.
        #   2. Signal is not set. Spawn a watcher task in the executor's
        #      task group that awaits the signal. When it fires, we
        #      cancel every in-flight per-task scope and mark
        #      ``_signal_cancelled`` so future ``add_tool_call``s
        #      short-circuit too. The watcher lives in its own
        #      ``CancelScope`` so ``__aexit__`` can wake it on clean
        #      shutdown (signal never fired).
        if self._cancellation_signal is not None:
            if self._cancellation_signal.is_set():
                self._signal_cancelled = True
            else:
                self._watcher_scope = anyio.CancelScope()
                self._tg.start_soon(self._watch_cancellation_signal)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._closed = True
        # Closing the task group joins all in-flight tasks.
        assert self._tg is not None
        # Wake the watcher if it's still parked on the Event. Doing this
        # before joining the task group prevents the task group's
        # ``__aexit__`` from blocking forever on a watcher that never
        # observed the signal during this run.
        if self._watcher_scope is not None:
            self._watcher_scope.cancel()
        unwrap_exc: BaseException | None = None
        try:
            await self._tg.__aexit__(exc_type, exc, tb)
        except BaseExceptionGroup as eg:
            # anyio 4.x ALWAYS wraps body-raised exceptions in a
            # ``BaseExceptionGroup`` at task-group exit, even when no child
            # task raised. That makes plain exception flow (e.g. a
            # ``BudgetExceededError`` raised in the body of
            # ``async with StreamingToolExecutor(...)``) bubble up as a
            # group to our caller — which is brittle and not what callers
            # expect from a "dispatch tools and run them" helper. Unwrap
            # singleton groups so callers see the original exception.
            flat = _flatten_exception_group(eg)
            if len(flat) == 1:
                unwrap_exc = flat[0]
            else:
                # Multiple distinct exceptions — keep the group so the
                # caller can inspect every failure.
                raise
        finally:
            # Fill any leftover None slots — shouldn't happen if the TG joined
            # cleanly, but a stray cancellation could leave a hole. Route
            # through ``_record_result`` so any active ``iter_completions``
            # consumer sees these backfills too before the stream closes.
            fallback_reason = (
                "cancelled by signal"
                if self._signal_cancelled
                else "tool execution cancelled"
            )
            for i, r in enumerate(self._results):
                if r is None:
                    call = self._calls[i]
                    self._record_result(
                        i,
                        ToolResultBlock(
                            tool_use_id=call.id,
                            content=fallback_reason,
                            is_error=True,
                        ),
                    )
            # Close the completion channel so iter_completions / wait_one
            # consumers see EndOfStream and break their loops. Done AFTER
            # the backfill so they observe every result first.
            self._close_completion_stream()
            # Releases any wait_all() blocked on us.
            self._pending = 0
            if not self._idle_event.is_set():
                self._idle_event.set()
        if unwrap_exc is not None:
            raise unwrap_exc

    async def _watch_cancellation_signal(self) -> None:
        """Background task: wait for ``self._cancellation_signal`` to fire,
        then yank every in-flight tool task via its CancelScope.

        Lives in the executor's task group. On clean executor shutdown
        (signal never fired) ``__aexit__`` calls ``self._watcher_scope.cancel()``
        to wake us out of the indefinite ``await``. The CancelledError
        is caught here and swallowed — clean exit, no propagation.
        """
        assert self._cancellation_signal is not None
        assert self._watcher_scope is not None
        try:
            with self._watcher_scope:
                await self._cancellation_signal.wait()
                # Signal fired. Flip the flag so any subsequent
                # ``add_tool_call`` short-circuits, and cancel every
                # in-flight per-task scope so the running bodies bail.
                self._signal_cancelled = True
                # Snapshot the list — new scopes appended after the
                # snapshot are caught by the ``_signal_cancelled`` flag
                # check in ``add_tool_call``.
                for sc in list(self._scopes):
                    sc.cancel()
        except anyio.get_cancelled_exc_class():
            # Clean shutdown — executor exited before signal fired.
            return

    # ------------------------------------------------------------------
    # Internal: result recording (drives both wait_all + iter_completions)
    # ------------------------------------------------------------------

    def _record_result(self, idx: int, result: ToolResultBlock) -> None:
        """Write the result for ``idx`` and notify completion subscribers.

        Idempotent: only the FIRST call for a given index pushes to the
        completion channel. Subsequent calls (race-window backfills in
        ``__aexit__`` / ``wait_all``) are silently dropped. This is what
        lets us reuse the same recording path for fast-fail short circuits
        AND post-invoke completions AND exit-time cleanup without ever
        double-emitting a result for the same tool_use_id."""
        if self._results[idx] is not None:
            return
        self._results[idx] = result
        if self._completion_closed:
            return
        try:
            self._completion_send.send_nowait((idx, result))
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            # Receive side already aborted (consumer crashed) — drop the
            # event. The result is still recorded so wait_all sees it.
            pass

    def _close_completion_stream(self) -> None:
        """Mark the completion channel closed and close the send side so
        ``iter_completions`` consumers see ``EndOfStream`` and terminate.

        Idempotent — safe to call multiple times. Called from ``__aexit__``
        as part of teardown."""
        if self._completion_closed:
            return
        self._completion_closed = True
        try:
            self._completion_send.close()
        except Exception:  # noqa: BLE001 — close must never raise out
            _LOG.debug("completion send-stream close raised", exc_info=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_tool_call(self, block: ToolUseBlock) -> None:
        """Register a freshly-finalized tool_use block and dispatch it.

        Non-blocking — returns as soon as the task is scheduled. Order of
        ``add_tool_call`` calls is the order of the returned results."""
        if self._closed:
            raise RuntimeError("add_tool_call after executor exit")
        if self._tg is None:
            raise RuntimeError("StreamingToolExecutor used outside async with")
        idx = len(self._calls)
        self._calls.append(block)
        self._results.append(None)
        self._scopes.append(anyio.CancelScope())
        done = anyio.Event()
        self._done_events.append(done)
        # Fast-fail paths — return BEFORE spawning a task. Two flavors,
        # each producing a distinct error message so callers can tell
        # what happened from the ToolResultBlock alone.
        if self._signal_cancelled:
            self._record_result(
                idx,
                ToolResultBlock(
                    tool_use_id=block.id,
                    content="cancelled by signal",
                    is_error=True,
                ),
            )
            done.set()
            return
        if self._aborted:
            self._record_result(
                idx,
                ToolResultBlock(
                    tool_use_id=block.id,
                    content="aborted by sibling tool error",
                    is_error=True,
                ),
            )
            done.set()
            return
        # Ordering: resolve safety now (insertion time) so the barrier set
        # is fixed by call index, not by which task happens to run first.
        # Unknown tools error immediately without touching anything — safe.
        tool = self._registry.resolve(block.name)
        safe = tool is None or _is_safe(tool, block.input)
        if safe:
            wait_for = (
                [] if self._last_unsafe_idx is None
                else [self._done_events[self._last_unsafe_idx]]
            )
        else:
            # Every earlier event — waiting on one already set is free.
            wait_for = list(self._done_events[:idx])
            self._last_unsafe_idx = idx
        # Bump pending and clear the idle gate.
        self._pending += 1
        if self._idle_event.is_set():
            self._idle_event = anyio.Event()
        self._tg.start_soon(self._run_one, idx, block, safe, wait_for)

    async def wait_all(self) -> list[ToolResultBlock]:
        """Block until every dispatched tool finishes and return result blocks
        in the order ``add_tool_call`` was called.

        Safe to call from inside ``async with``. The context exit will also
        flush — but most callers will await this explicitly to get the list
        before leaving the block."""
        if self._tg is None:
            raise RuntimeError("StreamingToolExecutor used outside async with")
        while self._pending > 0:
            await self._idle_event.wait()
        # Fill any leftover Nones (cancellation races).
        fallback_reason = (
            "cancelled by signal"
            if self._signal_cancelled
            else "tool execution cancelled"
        )
        out: list[ToolResultBlock] = []
        for i, r in enumerate(self._results):
            if r is None:
                call = self._calls[i]
                self._record_result(
                    i,
                    ToolResultBlock(
                        tool_use_id=call.id,
                        content=fallback_reason,
                        is_error=True,
                    ),
                )
                r = self._results[i]
            out.append(r)  # type: ignore[arg-type]
        return out

    def iter_completions(
        self,
    ) -> AsyncIterator[tuple[int, ToolResultBlock]]:
        """Yield ``(idx, ToolResultBlock)`` pairs as tools complete.

        Completion order — **not** insertion order. The ``idx`` is the
        insertion index (``add_tool_call`` ordinal, 0-based) so callers
        can correlate with ``self.calls`` if they need original-order
        positioning.

        Terminates when the executor exits (the completion send-stream
        is closed in ``__aexit__``). Safe to consume from inside the
        ``async with`` block in parallel with ``add_tool_call`` calls —
        the consumer will block on ``receive()`` whenever the queue is
        empty and wake the instant the next tool finishes.

        At-most-once delivery. Only one consumer can iterate at a time
        (memory streams have a single receive side). For multiple
        consumers, fan out via a downstream broadcast — the executor
        does not duplicate."""
        if self._tg is None and not self._closed:
            raise RuntimeError("StreamingToolExecutor used outside async with")
        return self._iter_completions()

    async def _iter_completions(
        self,
    ) -> AsyncIterator[tuple[int, ToolResultBlock]]:
        try:
            async for item in self._completion_recv:
                yield item
        except anyio.EndOfStream:
            return
        except anyio.ClosedResourceError:
            return

    async def wait_one(self) -> tuple[int, ToolResultBlock] | None:
        """Block until the next tool completion, return ``(idx, result)``.

        Returns ``None`` if the executor has closed and no further
        completions will arrive. Use ``iter_completions`` for a clean
        ``async for`` loop; ``wait_one`` is for callers that want to
        interleave manual control flow between completions."""
        if self._tg is None and not self._closed:
            raise RuntimeError("StreamingToolExecutor used outside async with")
        try:
            return await self._completion_recv.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            return None

    # ------------------------------------------------------------------
    # Introspection — read-only views for callers iterating completions
    # ------------------------------------------------------------------

    @property
    def calls(self) -> tuple[ToolUseBlock, ...]:
        """Snapshot of every ``ToolUseBlock`` registered so far, in
        insertion order. Lets ``iter_completions`` consumers map
        ``idx`` → originating call without poking at internals."""
        return tuple(self._calls)

    @property
    def executed_calls(self) -> list[ToolUseBlock]:
        """Detached snapshot of invocations in start order, not dispatch order.

        Names are canonical and inputs are captured after coercion/filtering,
        before tool code can mutate them. Includes calls that fail or time out,
        but excludes unknown, denied, and cancelled-before-invocation calls.
        """
        return deepcopy(self._executed_calls)

    @property
    def pending(self) -> int:
        """How many dispatched tools are still in flight. Reaches 0
        when every tool has either completed or short-circuited."""
        return self._pending

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run_one(
        self,
        idx: int,
        block: ToolUseBlock,
        safe: bool = True,
        wait_for: "list[anyio.Event] | None" = None,
    ) -> None:
        """One tool's lifetime: lookup → permission → concurrency gate →
        invoke → record result. Never raises out (all errors become
        ``is_error=True`` result blocks).

        The body runs under its own ``CancelScope`` so a sibling that calls
        ``_maybe_abort`` can yank in-flight work via ``scope.cancel()`` —
        this is how ``abort_siblings_on_error`` kills subprocess-bearing
        bash tools the moment a peer fails."""
        scope = self._scopes[idx]
        # Open a tool.call span (no-op when no tracer). Attributes capture
        # the input KEYS only (never values) to avoid leaking secrets into
        # observability backends.
        tool_span = None
        if self._tracer is not None:
            try:
                tool_span = self._tracer.start_span(
                    "tool.call",
                    parent=self._trace_parent,
                    attributes={
                        "tool.name": block.name,
                        "tool.id": block.id,
                        "tool.input.keys": sorted(
                            list((block.input or {}).keys())
                        ),
                    },
                )
            except Exception:  # noqa: BLE001 — tracer must never crash the run
                _LOG.debug("tracer.start_span failed", exc_info=True)
                tool_span = None
        try:
            with scope:
                await self._run_one_inner(idx, block, safe, wait_for or ())
            if scope.cancelled_caught and self._results[idx] is None:
                self._record_result(idx, self._cancelled_result(block))
        finally:
            # Release this call's ordering barrier first, whatever happened —
            # an errored / cancelled call must never strand later waiters.
            self._done_events[idx].set()
            self._pending -= 1
            if self._pending <= 0:
                self._pending = 0
                if not self._idle_event.is_set():
                    self._idle_event.set()
            # Stamp result attributes + close the span.
            if tool_span is not None and self._tracer is not None:
                try:
                    result = self._results[idx]
                    if result is not None:
                        tool_span.set_attributes({
                            "tool.is_error": bool(result.is_error),
                            "tool.result.len": (
                                len(result.content)
                                if isinstance(result.content, (str, list)) else 0
                            ),
                        })
                    tool_span.end(
                        status="error" if (
                            result is not None and result.is_error
                        ) else "ok"
                    )
                    mirror = getattr(
                        self._tracer, "_mirror", None
                    ) or self._tracer
                    close_fn = getattr(mirror, "_close", None)
                    if callable(close_fn):
                        close_fn(tool_span)
                except Exception:  # noqa: BLE001
                    _LOG.debug("tracer span end failed", exc_info=True)

    async def _run_one_inner(
        self,
        idx: int,
        block: ToolUseBlock,
        safe: bool,
        wait_for: "Any" = (),
    ) -> None:
        tool = self._registry.resolve(block.name)
        if tool is None:
            self._record_result(
                idx,
                ToolResultBlock(
                    tool_use_id=block.id,
                    content=_unknown_tool_message(block.name, self._registry),
                    is_error=True,
                ),
            )
            return

        # Permission gate first — cheap and may short-circuit.
        if self._can_use_tool is not None:
            try:
                allowed, reason = await self._can_use_tool(tool, block.input, {})
            except Exception as e:  # noqa: BLE001 — caller code must not crash us
                _LOG.exception("can_use_tool raised")
                self._record_result(
                    idx,
                    ToolResultBlock(
                        tool_use_id=block.id,
                        content=f"permission check error: {e!r}",
                        is_error=True,
                    ),
                )
                return
            if not allowed:
                self._record_result(
                    idx,
                    ToolResultBlock(
                        tool_use_id=block.id,
                        content=f"permission denied: {reason or 'no reason given'}",
                        is_error=True,
                    ),
                )
                return

        try:
            # Ordering barrier (see module docstring). Waited outside the
            # semaphore so a parked call never holds a parallel slot.
            for ev in wait_for:
                await ev.wait()
            if safe:
                async with self._sem:
                    if self._aborted:
                        self._record_result(idx, self._cancelled_result(block))
                        return
                    await self._invoke(idx, tool, block)
            else:
                async with self._serial_lock:
                    if self._aborted:
                        self._record_result(idx, self._cancelled_result(block))
                        return
                    await self._invoke(idx, tool, block)
        except anyio.get_cancelled_exc_class():
            # Cooperatively cancelled by sibling abort. Surface as error.
            if self._results[idx] is None:
                self._record_result(idx, self._cancelled_result(block))
            raise  # propagate so the TG records the cancellation

    async def _invoke(self, idx: int, tool: Tool, block: ToolUseBlock) -> None:
        """Run the tool body with optional timeout. Captures every exception
        and stores a ``ToolResultBlock``. May trigger sibling abort."""
        timeout = getattr(tool, "timeout_s", None)
        # Repair a model's loose args before calling: coerce typed values passed
        # as strings ("10" → 10, "true" → True) to the schema type, then drop
        # kwargs the tool doesn't accept (hallucinated extras) — either would
        # otherwise error the call and burn a turn. Aliased arg names
        # (``file_path`` → ``path``) are renamed first so they aren't dropped.
        received = block.input or {}
        aliased, fired = _apply_arg_aliases(tool.name, tool.fn, received)
        if fired:
            _LOG.debug(
                "tool %r: argument aliases applied: %s", tool.name,
                ", ".join(f"{a}→{c}" for a, c in fired),
            )
        coerced = _coerce_to_schema(aliased, getattr(tool, "input_schema", None))
        call_input = _filter_tool_input(tool.fn, coerced)
        missing = _missing_required(tool.fn, call_input)
        if missing:
            # Not executed — no evidence recorded, no sibling abort (no side
            # effects happened). The message names what to change.
            self._record_result(
                idx,
                ToolResultBlock(
                    tool_use_id=block.id,
                    content=_arg_error_message(tool, received, call_input, missing, fired),
                    is_error=True,
                ),
            )
            return
        try:
            with anyio.fail_after(timeout) if timeout is not None else nullcontext():
                # Record only after timeout setup succeeds, immediately before
                # invocation; synthetic results are not execution evidence.
                self._executed_calls.append(
                    ToolUseBlock(id=block.id, name=tool.name, input=deepcopy(call_input))
                )
                cap_token = _RESULT_CHAR_CAP.set(
                    _result_cap(tool.name, self._result_char_budget)
                )
                try:
                    out = await tool.fn(**call_input)
                finally:
                    _RESULT_CHAR_CAP.reset(cap_token)
        except TimeoutError:
            self._record_result(
                idx,
                ToolResultBlock(
                    tool_use_id=block.id,
                    content=f"tool {tool.name!r} timed out after {timeout}s",
                    is_error=True,
                ),
            )
            self._maybe_abort(tool)
            return
        except anyio.get_cancelled_exc_class():
            # Bubbled up — let _run_one handle it.
            raise
        except Exception as e:  # noqa: BLE001 — user code must not crash us
            err = ToolExecutionError(tool.name, block.id, e)
            content = str(err)
            renamed = {a for a, _ in fired}
            if isinstance(e, TypeError) and any(
                k not in call_input and k not in renamed for k in received
            ):
                # Likely an argument-shape error — say what was dropped and
                # what the tool takes, so the retry isn't identical.
                content += "\n" + _arg_error_message(tool, received, call_input, [], fired)
            self._record_result(
                idx,
                ToolResultBlock(
                    tool_use_id=block.id,
                    content=content,
                    is_error=True,
                ),
            )
            self._maybe_abort(tool)
            return

        rich = _as_block_content(out)
        budget = self._result_char_budget
        content: Any = (
            _truncate_block_content(rich, tool.name, budget)
            if rich is not None
            else _truncate_tool_result(_stringify(out), tool.name, budget)
        )
        self._record_result(
            idx,
            ToolResultBlock(tool_use_id=block.id, content=content),
        )

    def _maybe_abort(self, tool: Tool) -> None:
        """Trigger sibling-abort if this tool requested it.

        Cancels every per-task ``CancelScope`` so in-flight peers die fast
        (this is the whole point — kill subprocesses, abort file writes).
        Already-recorded results are kept; the rest become cancellation
        error blocks. Subsequent ``add_tool_call`` invocations short-circuit
        via the ``_aborted`` flag."""
        if not getattr(tool, "abort_siblings_on_error", False):
            return
        if self._aborted:
            return
        self._aborted = True
        # Cancel every in-flight task; their scope's cancelled_caught path
        # will write a cancelled result block.
        for sc in self._scopes:
            sc.cancel()

    def _cancelled_result(self, block: ToolUseBlock) -> ToolResultBlock:
        """Build the cancellation-error result block for ``block``.

        The reason string depends on *why* the cancel scope fired —
        signal-driven cancellation produces ``"cancelled by signal"``
        so a user-facing UI can distinguish abort-on-sibling-error
        (recover-and-retry) from agent-cancel (user wants out).
        """
        reason = (
            "cancelled by signal"
            if self._signal_cancelled
            else "aborted by sibling tool error"
        )
        return ToolResultBlock(
            tool_use_id=block.id,
            content=reason,
            is_error=True,
        )
