"""Tools — declaration, registry, dispatch.

Design
------
* ``@tool`` decorator turns an async function into a ``Tool``. Schema is
  derived from the function's type hints + docstring; we don't ship a
  separate schema-building layer.
* ``ToolRegistry`` holds the live set. Lookup is O(1) by name.
* Dispatch runs tool calls **in parallel by default** via ``anyio.create_task_group``,
  with single-flight enforcement on tools declared ``parallel_safe=False``.
* Exceptions in tool bodies are caught and surfaced as ``ToolResultBlock``
  with ``is_error=True`` — the agent loop never crashes because of a
  user tool throwing.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, get_type_hints

import anyio
import msgspec

from .errors import ToolExecutionError
from .types import ToolResultBlock, ToolUseBlock

ToolFn = Callable[..., Awaitable[Any]]


# ---------------------------------------------------------------------------
# Tool model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Tool:
    """Runtime representation of a tool the agent can call.

    Fields
    ------
    name, description, input_schema, fn:
        The four things every tool needs.
    is_concurrency_safe:
        ``True`` (default), ``False``, or a callable ``(input: dict) -> bool``.
        Function-of-input form matches Claude Code's upstream model — two ``bash``
        calls writing to different files can parallelize; same file can't.
    abort_siblings_on_error:
        When ``True`` and this tool errors inside a concurrent batch, the
        ``StreamingToolExecutor`` cancels its sibling tasks via the batch
        ``CancelScope`` so subprocesses die fast. Default ``False``.
    is_read_only:
        Hint to the permission system + concurrency partitioner. Read-only
        tools are auto-allowed under ``mode="auto"``.
    timeout_s:
        Soft per-call timeout via ``anyio.fail_after``. ``None`` for no timeout.
    parallel_safe (deprecated):
        Kept for backwards compat with v0 callers. Reads/writes through to
        ``is_concurrency_safe``.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    fn: ToolFn
    is_concurrency_safe: Callable[[dict], bool] | bool = True
    abort_siblings_on_error: bool = False
    is_read_only: bool = False
    timeout_s: float | None = None

    def to_wire(self) -> dict[str, Any]:
        """JSON-Schema tool definition (Anthropic-shaped). Other providers
        convert from this — e.g. OpenAI wraps it under ``function``."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    # Backwards-compat shim. v0 used a static ``parallel_safe`` bool.
    @property
    def parallel_safe(self) -> bool:
        return self.is_concurrency_safe is True

    @parallel_safe.setter
    def parallel_safe(self, value: bool) -> None:
        self.is_concurrency_safe = bool(value)


def tool(
    fn_or_name: ToolFn | str | None = None,
    description: str | None = None,
    input_schema: dict[str, Any] | dict[str, type] | None = None,
    *,
    name: str | None = None,
    is_concurrency_safe: Callable[[dict], bool] | bool = True,
    abort_siblings_on_error: bool = False,
    is_read_only: bool = False,
    timeout_s: float | None = None,
    # Deprecated alias, accepted for backwards compat.
    parallel_safe: bool | None = None,
) -> Tool | Callable[[ToolFn], Tool]:
    """Decorator: turn an async function into a ``Tool``.

    **Two signatures are supported** for verbatim Claude SDK parity:

    Pythonic (auto-derived schema)::

        @tool
        async def get_weather(city: str) -> str:
            \"\"\"Get current weather for a city.\"\"\"
            ...

    Claude SDK form (positional name + description + schema, single
    ``args: dict`` parameter, returns ``{"content": [...], "is_error"?}``)::

        @tool("add", "Add two numbers", {"a": float, "b": float})
        async def add_numbers(args: dict[str, Any]) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}

    Both forms register the same ``Tool`` shape internally. The Claude
    form's ``args: dict``-in, ``dict``-out signature is wrapped so that
    when the agent dispatcher calls the tool with ``**kwargs``, we
    repackage them into the ``args`` dict the Claude-style function
    expects, then extract the ``content[0].text`` (or stringify the
    whole result) as the result block content.

    Schema for the Pythonic form is auto-derived from type hints. For the
    Claude form, the ``{"a": float}`` dict is mapped to a JSON schema.
    """

    # Resolve deprecated parallel_safe alias once.
    if parallel_safe is not None and is_concurrency_safe is True:
        is_concurrency_safe = bool(parallel_safe)

    # Disambiguate the three valid first-arg shapes:
    #   @tool                  (no-call, fn_or_name is the function)
    #   @tool(name="x")        (kw-only, fn_or_name is None)
    #   @tool("x", "desc", {}) (Claude positional, fn_or_name is a str)
    claude_positional = (
        isinstance(fn_or_name, str) or description is not None or input_schema is not None
    )

    if claude_positional:
        tool_name = name or (fn_or_name if isinstance(fn_or_name, str) else None)
        tool_desc = description or ""
        raw_schema = input_schema or {}

        def _wrap_claude(fn: ToolFn) -> Tool:
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(f"@tool requires async def, got {fn!r}")
            schema = (
                _python_type_schema(raw_schema)
                if raw_schema and not _looks_like_json_schema(raw_schema)
                else (raw_schema or _derive_schema(fn))
            )
            wrapped = _wrap_claude_style_fn(fn)
            return Tool(
                name=tool_name or fn.__name__,
                description=tool_desc or (inspect.getdoc(fn) or "").strip(),
                input_schema=schema,
                fn=wrapped,
                is_concurrency_safe=is_concurrency_safe,
                abort_siblings_on_error=abort_siblings_on_error,
                is_read_only=is_read_only,
                timeout_s=timeout_s,
            )

        return _wrap_claude

    # Pythonic form (existing behavior).
    fn = fn_or_name if callable(fn_or_name) else None

    def _wrap(fn: ToolFn) -> Tool:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"@tool requires async def, got {fn!r}")
        return Tool(
            name=name or fn.__name__,
            description=description or (inspect.getdoc(fn) or "").strip(),
            input_schema=input_schema or _derive_schema(fn),
            fn=fn,
            is_concurrency_safe=is_concurrency_safe,
            abort_siblings_on_error=abort_siblings_on_error,
            is_read_only=is_read_only,
            timeout_s=timeout_s,
        )

    if fn is not None:
        return _wrap(fn)
    return _wrap


# ---------------------------------------------------------------------------
# Claude-style helpers
# ---------------------------------------------------------------------------


def _looks_like_json_schema(d: dict[str, Any]) -> bool:
    """Heuristic: does ``d`` look like a JSON Schema (vs. Claude's
    type-dict shorthand)?"""

    return "type" in d and d.get("type") == "object"


def _python_type_schema(d: dict[str, type]) -> dict[str, Any]:
    """Convert Claude's shorthand ``{"a": float, "b": float}`` to a JSON
    Schema ``{"type": "object", "properties": {...}, "required": [...]}``.
    """

    props: dict[str, Any] = {}
    required: list[str] = []
    for k, t in d.items():
        props[k] = _type_to_schema(t)
        required.append(k)
    return {"type": "object", "properties": props, "required": required}


def _wrap_claude_style_fn(fn: ToolFn) -> ToolFn:
    """Wrap a Claude-style ``async def f(args: dict) -> dict`` so the
    dispatcher's ``**kwargs`` call shape works.

    Also unwraps the returned ``{"content": [{"type":"text","text":"…"}], …}``
    dict into a plain string (which the dispatcher then puts into the
    ``ToolResultBlock.content`` field).
    """

    async def _wrapper(**kwargs: Any) -> Any:
        result = await fn(kwargs)
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list) and content:
                # Concatenate text blocks; ignore image/resource for now.
                texts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(str(block.get("text", "")))
                if texts:
                    return "\n".join(texts)
            if "text" in result:
                return str(result["text"])
        return result

    _wrapper.__name__ = getattr(fn, "__name__", "claude_tool")
    _wrapper.__doc__ = fn.__doc__
    return _wrapper


def _derive_schema(fn: ToolFn) -> dict[str, Any]:
    """Build a JSON Schema from the function's annotations.

    Keeps it simple: supports str/int/float/bool/list/dict primitives and
    treats anything else as a generic object. For richer schemas, pass
    ``input_schema=`` explicitly to ``@tool``.
    """

    hints = get_type_hints(fn)
    sig = inspect.signature(fn)
    props: dict[str, Any] = {}
    required: list[str] = []
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        ann = hints.get(param_name, str)
        props[param_name] = _type_to_schema(ann)
        if param.default is inspect.Parameter.empty:
            required.append(param_name)
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


_SCALAR_MAP = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
}


def _type_to_schema(t: Any) -> dict[str, Any]:
    if t in _SCALAR_MAP:
        return _SCALAR_MAP[t]
    origin = getattr(t, "__origin__", None)
    if origin is list:
        args = getattr(t, "__args__", ())
        inner = _type_to_schema(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": inner}
    if origin is dict:
        return {"type": "object"}
    return {"type": "object"}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolRegistry:
    """Holds tools by name. Cheap to construct per ``Agent``."""

    _by_name: dict[str, Tool] = field(default_factory=dict)

    def add(self, *tools: Tool) -> None:
        for t in tools:
            if t.name in self._by_name:
                raise ValueError(f"duplicate tool name {t.name!r}")
            self._by_name[t.name] = t

    def get(self, name: str) -> Tool | None:
        return self._by_name.get(name)

    def to_wire(self) -> list[dict[str, Any]]:
        return [t.to_wire() for t in self._by_name.values()]

    def __bool__(self) -> bool:
        return bool(self._by_name)

    def __iter__(self):
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_ENC = msgspec.json.Encoder()


async def dispatch_tool_calls(
    registry: ToolRegistry,
    calls: list[ToolUseBlock],
) -> list[ToolResultBlock]:
    """Run every tool call in ``calls`` and return result blocks in order.

    Parallel by default; non-parallel-safe tools are serialized by tool name.

    Order of returned results matches the order of ``calls`` — callers can
    pair them by ``tool_use_id``, but stable ordering keeps debugging sane.
    """

    if not calls:
        return []

    results: list[ToolResultBlock | None] = [None] * len(calls)
    # Per-name locks for non-parallel-safe tools.
    locks: dict[str, anyio.Lock] = {}

    async def run_one(idx: int, call: ToolUseBlock) -> None:
        t = registry.get(call.name)
        if t is None:
            results[idx] = ToolResultBlock(
                tool_use_id=call.id,
                content=f"tool {call.name!r} not found",
                is_error=True,
            )
            return

        lock = None
        if not t.parallel_safe:
            lock = locks.setdefault(t.name, anyio.Lock())

        try:
            if lock is not None:
                async with lock:
                    out = await t.fn(**call.input)
            else:
                out = await t.fn(**call.input)
        except Exception as e:  # noqa: BLE001 — user code, must not crash the loop
            # Wrap for typed handling upstream, but still produce a result block.
            err = ToolExecutionError(call.name, call.id, e)
            results[idx] = ToolResultBlock(
                tool_use_id=call.id,
                content=str(err),
                is_error=True,
            )
            return

        results[idx] = ToolResultBlock(
            tool_use_id=call.id,
            content=_stringify_result(out),
        )

    async with anyio.create_task_group() as tg:
        for i, c in enumerate(calls):
            tg.start_soon(run_one, i, c)

    # All slots are guaranteed filled by the task group exit.
    return [r for r in results if r is not None]


def _stringify_result(out: Any) -> str:
    """Coerce a tool return to a string suitable for the wire.

    Strings pass through. msgspec-encodable objects get JSON-encoded.
    Anything else falls back to ``str()``.
    """

    if isinstance(out, str):
        return out
    try:
        return _ENC.encode(out).decode()
    except (TypeError, msgspec.EncodeError):
        return str(out)
