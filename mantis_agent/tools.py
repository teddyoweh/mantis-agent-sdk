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
import itertools
import re
import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

try:
    from eval_type_backport import install_patch
except ImportError:  # pragma: no cover - Python 3.10+
    pass
else:  # pragma: no cover - Python 3.9 only
    install_patch()

import anyio
import msgspec

from .errors import ToolExecutionError
from .types import ToolResultBlock, ToolUseBlock

ToolFn = Callable[..., Awaitable[Any]]
# Monotonic surface order for deferred tools (see ``ToolRegistry.to_wire``).
_SURFACE_SEQ = itertools.count(1)


# ---------------------------------------------------------------------------
# Tool model
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    """Runtime representation of a tool the agent can call.

    Fields
    ------
    name, description, input_schema, fn:
        The four things every tool needs.
    is_concurrency_safe:
        ``True``, ``False``, or a callable ``(input: dict) -> bool``. Left
        unset (``None``) it follows ``is_read_only`` — a tool is only assumed
        safe to overlap other calls when it declares it doesn't write, so a
        forgotten flag can't let a later read race an earlier write.
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
    is_concurrency_safe: Callable[[dict], bool] | bool | None = None
    abort_siblings_on_error: bool = False
    is_read_only: bool = False
    timeout_s: float | None = None
    # Deferred tools are advertised by NAME only — their JSON schema is kept
    # out of every request until the model asks for it with ``tool_search``.
    # A dozen MCP servers otherwise cost thousands of tokens per turn, on every
    # turn, whether or not the model was ever going to use them.
    deferred: bool = False
    # A one- or two-sentence description sent instead of ``description`` when
    # the wire list is built compact (small context window, or a model on the
    # prompt-engineered tool path, where every schema char is prompt text).
    description_short: str | None = None
    # ``(description, description_short)`` as they were paired when the short
    # text was attached. ``dataclasses.replace(tool, description=...)`` copies
    # both the stale short and this pair, so compact mode can tell that the
    # description moved on without the short and fall back to the full text
    # (see :meth:`to_wire`). Filled in by ``__post_init__``.
    _short_for: tuple[str, str] | None = field(default=None, repr=False, compare=False)
    # Load order once a deferred tool is surfaced (0 = live from the start);
    # keeps ``ToolRegistry.to_wire`` append-only.
    _surfaced_seq: int = field(default=0, init=False, repr=False, compare=False)

    def to_wire(self, *, compact: bool = False) -> dict[str, Any]:
        """JSON-Schema tool definition (Anthropic-shaped). Other providers
        convert from this — e.g. OpenAI wraps it under ``function``.

        ``compact=True`` swaps in ``description_short`` when the tool has one;
        parameter docs stay in the schema either way. A short text is skipped
        when it is stale — the description was changed after the short was
        attached (e.g. ``dataclasses.replace(builtin, description=...)``) while
        the short stayed the same — since it describes the old tool."""
        description = self.description
        if compact and self._short_is_current():
            description = self.description_short  # type: ignore[assignment]
        return {
            "name": self.name,
            "description": description,
            "input_schema": self.input_schema,
        }

    def _short_is_current(self) -> bool:
        short = self.description_short
        if not short:
            return False
        pair = self._short_for
        # Unpaired, re-paired or a short changed on its own: it's current. Only
        # a changed description under the SAME short marks it stale.
        return pair is None or short != pair[1] or self.description == pair[0]

    def __post_init__(self) -> None:
        if self.description_short and self._short_for is None:
            self._short_for = (self.description, self.description_short)
        # Unset concurrency safety follows read-only-ness (Claude Code's
        # ``isConcurrencySafe`` default): writers serialize unless they opt in.
        if self.is_concurrency_safe is None:
            self.is_concurrency_safe = bool(self.is_read_only)

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
    is_concurrency_safe: Callable[[dict], bool] | bool | None = None,
    abort_siblings_on_error: bool = False,
    is_read_only: bool = False,
    timeout_s: float | None = None,
    description_short: str | None = None,
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

    A description taken from the docstring drops its ``Args:``/``Returns:``/
    ``Raises:`` sections — the per-parameter docs are already in the schema, so
    sending them in the description too paid for them twice. An explicit
    ``description`` is used verbatim. ``description_short`` is the compact
    variant sent to small-context / prompt-engineered models.
    """

    # Resolve deprecated parallel_safe alias once.
    if parallel_safe is not None and is_concurrency_safe is None:
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
                description=tool_desc or _docstring_description(fn),
                input_schema=schema,
                fn=wrapped,
                description_short=description_short,
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
            description=description or _docstring_description(fn),
            input_schema=input_schema or _derive_schema(fn),
            fn=fn,
            description_short=description_short,
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

    Supports str/int/float/bool/list/dict primitives, unwraps
    ``Optional``/``X | None``/``Union`` to the underlying type, and turns
    ``Literal[...]`` into an ``enum``. Per-parameter ``description``s are
    lifted from the function's ``Args:`` docstring block so weak models — which
    lean on the wire schema more than prose — get field-level guidance. For
    richer schemas, pass ``input_schema=`` explicitly to ``@tool``.
    """

    hints = get_type_hints(fn)
    sig = inspect.signature(fn)
    descriptions = _parse_docstring_args(inspect.getdoc(fn) or "")
    props: dict[str, Any] = {}
    required: list[str] = []
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        ann = hints.get(param_name, str)
        prop = _type_to_schema(ann)
        desc = descriptions.get(param_name)
        if desc:
            prop = {**prop, "description": desc}
        props[param_name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(param_name)
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _docstring_description(fn: ToolFn) -> str:
    """The tool description derived from ``fn``'s docstring: the prose, minus
    the parameter / return / raise sections.

    Those sections already reach the model through the schema — each Args
    entry becomes that property's ``description`` (:func:`_derive_schema`) —
    so leaving them in the description sent every parameter's docs twice.
    Falls back to the whole docstring if stripping would leave nothing.
    """

    doc = (inspect.getdoc(fn) or "").strip()
    return _strip_docstring_sections(doc) or doc


def _strip_docstring_sections(doc: str) -> str:
    """``doc`` without its Args/Parameters/Returns/Raises/Yields sections
    (Google ``Args:`` or numpy ``Parameters`` + underline style). Prose after a
    section — a dedent back to the heading's column (Google), or a blank line
    then a non-entry line at that column (numpy) — is kept."""

    lines = doc.splitlines()
    drop: set[int] = set()
    for name, _numpy, start, end in _doc_sections(lines):
        if name in _STRIP_SECTIONS:
            drop.update(range(start, end))
    kept = [line for n, line in enumerate(lines) if n not in drop]
    text = "\n".join(line.rstrip() for line in kept)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _parse_docstring_args(doc: str) -> dict[str, str]:
    """Parse a docstring's parameter section into ``{param: description}``.

    Google style: ``name: description`` (or ``name (type): description``)
    entries under ``Args:`` / ``Arguments:`` / ``Parameters:`` — indented
    under the heading, or flush with it. Numpy style: ``name : type`` entries
    under a ``Parameters`` heading underlined with dashes, description on the
    following, deeper-indented lines. Continuation lines (indented past the
    entry) fold into the preceding description. Section boundaries are
    :func:`_section_end`'s; headings only count at the docstring's base indent
    and never inside an Example(s) section or a ``::`` literal block.
    """

    lines = doc.splitlines()
    out: dict[str, str] = {}
    for name, numpy, start, end in _doc_sections(lines):
        if name not in _PARAM_SECTIONS:
            continue
        entry_re = _NUMPY_ARG_LINE if numpy else _ARG_LINE
        entry_indent: int | None = None
        current: str | None = None
        for raw in lines[start + (2 if numpy else 1):end]:
            stripped = raw.strip()
            if not stripped:
                continue
            indent = _indent(raw)
            # ``name: description`` at the entries' column starts a new entry;
            # anything more indented continues the current description.
            m = entry_re.match(stripped)
            if m and (entry_indent is None or indent <= entry_indent):
                entry_indent = indent if entry_indent is None else entry_indent
                current = m.group(1)
                out[current] = "" if numpy else (m.group(2) or "").strip()
            elif current is not None:
                out[current] = f"{out[current]} {stripped}".strip()
    return out


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _doc_sections(lines: list[str]) -> list[tuple[str, bool, int, int]]:
    """Top-level ``(section, is_numpy, start, end)`` spans of a docstring.

    A heading only counts at the docstring's base (minimum) indent, so an
    indented ``Returns:`` continuation line or an ``Args:`` inside an example
    is text, not a section. Example(s) sections are spanned as a whole (their
    contents are never scanned for headings) and ``::`` literal blocks are
    skipped the same way."""

    base = min((_indent(line) for line in lines if line.strip()), default=0)
    out: list[tuple[str, bool, int, int]] = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        head = (_section_heading(lines, i)
                if raw.strip() and _indent(raw) == base else None)
        if head is not None:
            end = _section_end(lines, i, head[1])
            out.append((head[0], head[1], i, end))
            i = end
        elif raw.rstrip().endswith("::"):
            i = _literal_block_end(lines, i)
        else:
            i += 1
    return out


def _literal_block_end(lines: list[str], i: int) -> int:
    """Index of the first line after the ``::`` literal block opened by
    ``lines[i]`` — the next non-blank line at or left of its indent."""

    indent = _indent(lines[i])
    j = i + 1
    while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > indent):
        j += 1
    return j


def _section_heading(lines: list[str], i: int) -> tuple[str, bool] | None:
    """``(section, is_numpy)`` if ``lines[i]`` opens a docstring section —
    ``Args:`` (Google) or ``Parameters`` over a ``----------`` rule (numpy)."""

    low = lines[i].strip().lower()
    if low.endswith(":") and low[:-1] in _KNOWN_SECTIONS:
        return low[:-1], False
    if (low in _KNOWN_SECTIONS and i + 1 < len(lines)
            and _UNDERLINE.match(lines[i + 1].strip())):
        return low, True
    return None


def _section_end(lines: list[str], i: int, numpy: bool) -> int:
    """Index just past the section whose heading is ``lines[i]``.

    Every section ends at the next heading at (or left of) its heading's
    column. Beyond that:

    - Google, indented entries: a dedent back to the heading's column.
    - Google, entries flush with the heading (``Args:\nx: the x``): a blank
      line, or a heading-column line that isn't a ``name: ...`` entry.
    - Numpy (entries sit at the heading's column): a blank line followed by a
      heading-column line that isn't entry-shaped — trailing prose.
    """

    heading_indent = _indent(lines[i])
    j = i + (2 if numpy else 1)
    flush = False
    if not numpy:
        k = j
        while k < len(lines) and not lines[k].strip():
            k += 1
        flush = (k < len(lines) and _indent(lines[k]) == heading_indent
                 and _section_heading(lines, k) is None
                 and _ARG_LINE.match(lines[k].strip()) is not None)
    after_blank = False
    while j < len(lines):
        raw = lines[j]
        stripped = raw.strip()
        if not stripped:
            after_blank = True
            j += 1
            continue
        at_heading_col = _indent(raw) <= heading_indent
        if at_heading_col:
            if _section_heading(lines, j) is not None:
                break
            if numpy:
                if after_blank and not _NUMPY_ENTRY.match(stripped):
                    break
            elif flush:
                if after_blank or not _ARG_LINE.match(stripped):
                    break
            else:
                break
        after_blank = False
        j += 1
    return j


_PARAM_SECTIONS = frozenset({
    "args", "arguments", "parameters", "params",
    "keyword args", "keyword arguments", "other parameters",
})
_STRIP_SECTIONS = _PARAM_SECTIONS | {"returns", "return", "raises", "yields", "yield"}
_KNOWN_SECTIONS = _STRIP_SECTIONS | {
    "examples", "example", "note", "notes", "attributes", "see also",
    "warning", "warnings", "references", "todo",
}
# ``name: rest`` or ``name (type): rest`` — the leading token of an Args entry.
_ARG_LINE = re.compile(r"^([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*:\s*(.*)$")
# Numpy ``name : type`` (the type is optional).
_NUMPY_ARG_LINE = re.compile(r"^([A-Za-z_]\w*)\s*(?::\s*(.*))?$")
# Anything a numpy section's entry line can look like: ``name : type``,
# ``x, y : float``, or a bare (dotted / generic) type under Returns.
_NUMPY_ENTRY = re.compile(
    r"^\*{0,2}[A-Za-z_][\w.]*(?:\[.*\])?(?:\s*,\s*\*{0,2}[A-Za-z_]\w*)*\s*(?::.*)?$"
)
_UNDERLINE = re.compile(r"^-{3,}$")


_SCALAR_MAP = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
}

# Python types of Literal members → JSON Schema type, for enum typing.
_LITERAL_TYPE = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _type_to_schema(t: Any) -> dict[str, Any]:
    if t in _SCALAR_MAP:
        return _SCALAR_MAP[t]
    origin = get_origin(t) or getattr(t, "__origin__", None)
    # Optional[X] / X | None / Union[...] — strip NoneType and recurse on the
    # remaining member(s). Union types otherwise fall through to a bare object,
    # mis-typing every optional parameter on the wire.
    if origin is Union or origin is getattr(types, "UnionType", None):
        members = [a for a in get_args(t) if a is not type(None)]
        if len(members) == 1:
            return _type_to_schema(members[0])
        if members:
            # Heterogeneous union — advertise the alternatives so the model sees
            # each valid shape rather than a generic object.
            return {"anyOf": [_type_to_schema(m) for m in members]}
        return {"type": "object"}
    # Literal[...] — a closed set of scalar values becomes an enum.
    if origin is Literal:
        values = list(get_args(t))
        schema: dict[str, Any] = {"enum": values}
        member_types = {_LITERAL_TYPE.get(type(v)) for v in values}
        member_types.discard(None)
        if len(member_types) == 1:
            schema["type"] = member_types.pop()
        return schema
    if origin is list:
        args = get_args(t) or getattr(t, "__args__", ())
        inner = _type_to_schema(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": inner}
    if origin is dict:
        return {"type": "object"}
    return {"type": "object"}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


# Claude-Code tool names (and common synonyms) → mantis tool names, keyed by the
# normalized (lowercase, alnum-only) form. Only used as a last resort in
# ``ToolRegistry.resolve`` when a tool-call name doesn't match a registered tool.
_TOOL_ALIASES: dict[str, str] = {
    "read": "read_file", "readfile": "read_file", "view": "read_file", "cat": "read_file",
    "write": "write_file", "writefile": "write_file", "createfile": "write_file",
    "edit": "edit_file", "strreplace": "edit_file", "strreplaceeditor": "edit_file",
    "multiedit": "multi_edit",
    "notebookedit": "notebook_edit",
    "shell": "bash", "run": "bash", "runcommand": "bash", "terminal": "bash",
    "execute": "bash", "exec": "bash", "runshell": "bash", "command": "bash",
    "findfiles": "glob", "find": "glob",
    "search": "grep", "ripgrep": "grep", "rg": "grep", "searchtext": "grep",
    "searchcode": "grep", "grepsearch": "grep",
    "list": "ls", "listdir": "ls", "listfiles": "ls", "listdirectory": "ls",
    "websearch": "web_search", "searchweb": "web_search", "googlesearch": "web_search",
    "webfetch": "web_fetch", "fetch": "web_fetch", "fetchurl": "web_fetch",
    "gethttp": "web_fetch", "browse": "web_fetch",
    "agent": "task", "dispatchagent": "task", "subagent": "task", "delegate": "task",
    "todowrite": "todo_write", "todo": "todo_write", "updatetodos": "todo_write",
    "remember": "remember", "savememory": "remember",
    "looklsp": "lsp", "lsplookup": "lsp",
}


@dataclass
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

    def resolve(self, name: str) -> Tool | None:
        """Find a tool by the model's (possibly drifted) name. Tries, in order:
        exact match, case/underscore-insensitive match over registered tools,
        then a Claude-Code-name alias (``Read`` → ``read_file``, ``Bash`` →
        ``bash``, …). Many OSS models learned Claude's capitalized tool names, so
        this keeps their calls working against mantis's lower_snake tools."""
        if not name:
            return None
        t = self._by_name.get(name)
        if t is not None:
            return t
        import re  # noqa: PLC0415

        norm = re.sub(r"[^a-z0-9]", "", name.lower())
        if not norm:
            return None
        for reg_name, tool in self._by_name.items():
            if re.sub(r"[^a-z0-9]", "", reg_name.lower()) == norm:
                return tool
        alias = _TOOL_ALIASES.get(norm)
        if alias:
            return self._by_name.get(alias)
        return None

    def to_wire(self, *, compact: bool = False) -> list[dict[str, Any]]:
        """The schemas that go on the wire this turn — deferred tools are
        excluded until :meth:`surface` loads them. ``compact=True`` sends each
        tool's ``description_short`` where it has one (see :meth:`Tool.to_wire`)."""
        # Surfaced tools go LAST, in the order they were loaded, so the list
        # only ever appends: slotting one back into its registration position
        # would shift every later schema and invalidate a local server's
        # prompt (KV prefix) cache. Sorted on the Tool, not the dict, because
        # an Agent may run from a copy of the registry ``tool_search`` holds.
        live = [t for t in self._by_name.values() if not t.deferred]
        live.sort(key=lambda t: t._surfaced_seq)
        return [t.to_wire(compact=compact) for t in live]

    # -- deferred tools -------------------------------------------------------
    #
    # The registry knows about every tool; the *request* only carries the ones
    # that are live. ``deferred_index()`` is the cheap catalogue (name + first
    # line of description) that tells the model what it could load, and
    # ``surface()`` promotes a tool to live for the rest of the session.

    def defer(self, *names: str) -> int:
        """Mark tools deferred by name. Returns how many changed."""
        n = 0
        for name in names:
            t = self._by_name.get(name)
            if t is not None and not t.deferred:
                t.deferred = True
                n += 1
        return n

    def surface(self, *names: str) -> list[Tool]:
        """Promote deferred tools to live. Returns the ones that changed."""
        out = []
        for name in names:
            t = self.resolve(name)
            if t is not None and t.deferred:
                t.deferred = False
                t._surfaced_seq = next(_SURFACE_SEQ)
                out.append(t)
        return out

    def deferred_tools(self) -> list[Tool]:
        return [t for t in self._by_name.values() if t.deferred]

    def deferred_index(self) -> list[tuple[str, str]]:
        """``(name, one-line summary)`` for every deferred tool — small enough
        to name them all in the system prompt."""
        out = []
        for t in self.deferred_tools():
            first = (t.description or "").strip().splitlines()
            out.append((t.name, first[0][:120] if first else ""))
        return out

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


def unknown_tool_message(name: str, registry: "ToolRegistry") -> str:
    """An unknown-tool error the MODEL can recover from. Small local models
    invent tool names constantly ('model', 'search', 'run') — a bare 'not
    found' makes them retry blindly; naming the close match (or telling them
    no tool is needed) lets them self-correct in one step."""
    import difflib  # noqa: PLC0415
    names = [t.name for t in registry]
    close = difflib.get_close_matches(name, names, n=1, cutoff=0.5)
    hint = f" Did you mean {close[0]!r}?" if close else ""
    return (
        f"tool {name!r} does not exist.{hint} Only these tools are available: "
        f"{', '.join(sorted(names))}. If none fits, answer directly in text — "
        "no tool call is needed to reply."
    )


async def dispatch_tool_calls(
    registry: ToolRegistry,
    calls: list[ToolUseBlock],
) -> list[ToolResultBlock]:
    """Run every tool call in ``calls`` and return result blocks in order.

    Parallel by default; non-parallel-safe tools are serialized by tool name.
    Tool names are resolved via ``registry.resolve`` so Claude-cased/aliased
    names (``Read`` → ``read_file``, ``Bash`` → ``bash``) work the same as in
    the agent loop.

    Order of returned results matches the order of ``calls`` — callers can
    pair them by ``tool_use_id``, but stable ordering keeps debugging sane.

    NOTE: this is a bare dispatch helper — it does **no** permission checking or
    ``PreToolUse`` hook gating. The production agent loop routes tool calls
    through ``StreamingToolExecutor`` + the permission system instead; SDK
    consumers calling this directly own approval/sandboxing themselves.
    """

    if not calls:
        return []

    results: list[ToolResultBlock | None] = [None] * len(calls)
    # Per-name locks for non-parallel-safe tools.
    locks: dict[str, anyio.Lock] = {}

    async def run_one(idx: int, call: ToolUseBlock) -> None:
        t = registry.resolve(call.name)
        if t is None:
            results[idx] = ToolResultBlock(
                tool_use_id=call.id,
                content=unknown_tool_message(call.name, registry),
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
