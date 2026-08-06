"""Model-authored workflow scripts.

The :class:`~mantis_agent.workflow.Workflow` engine can express far more than
the ``coordinate`` tool's fixed fan-out → synthesize → verify shape: phases,
pipelines with no barrier between stages, loops that run until a search goes
dry, per-agent model and persona choices. None of that was reachable by the
model — it could only trigger the one hardcoded shape, or run a workflow a
human had written down as a ``.md`` definition.

This module lets the model write the orchestration itself, as a small Python
script, and hands it exactly the engine's primitives::

    meta = {"name": "review-diff", "description": "Review a diff, verify each finding"}

    phase("Review")
    reviews = await parallel([
        (lambda d=d: agent(f"Review the diff for {d} issues", label=d))
        for d in ("correctness", "security", "performance")
    ])

    phase("Verify")
    checked = await pipeline(
        reviews,
        lambda r: agent(f"Try to REFUTE these findings:\\n{r}", agent_type="verify"),
    )
    return {"reviews": reviews, "verdicts": checked}

Why a restricted subset, not ``exec``
------------------------------------
The script is written by the session's own model, which in most configurations
can already run shell commands — so this is **not** a security boundary and is
not presented as one. What it *is* is a guardrail against a confused or
injected script doing something outside its job: the validator rejects imports,
private/dunder attribute access, and the usual escape hatches (``eval``,
``open``, ``__import__``, ``globals``), so a workflow script can orchestrate
agents and compute over their results, and nothing else. A script that wants to
touch the filesystem should ask an *agent* to do it, where the permission
system applies.

Line numbers survive: the script's own AST is wrapped in an async function
node rather than string-concatenated, so a traceback points at the line the
model actually wrote.
"""

from __future__ import annotations

import ast
import json
from typing import Any, Awaitable, Callable

__all__ = [
    "ScriptError",
    "extract_meta",
    "run_workflow_script",
    "validate_script",
]


class ScriptError(Exception):
    """A script that could not be validated, compiled, or run.

    ``line`` is the 1-based line in the model's own source when known, so the
    error we hand back names the line it wrote.
    """

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message if line is None else f"line {line}: {message}")
        self.line = line


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

# Statement/expression nodes a workflow script may use. This is an allowlist:
# anything not named here is refused, so a new Python syntax feature can't
# quietly widen what a script can do.
_ALLOWED_NODES: frozenset[str] = frozenset({
    # module + statements
    "Module", "Expr", "Assign", "AnnAssign", "AugAssign", "Return", "Pass",
    "If", "For", "AsyncFor", "While", "Break", "Continue", "Try", "TryStar",
    "ExceptHandler", "Raise", "Assert", "With", "AsyncWith", "withitem",
    "FunctionDef", "AsyncFunctionDef", "Lambda", "arguments", "arg", "Delete",
    # expressions
    "BoolOp", "NamedExpr", "BinOp", "UnaryOp", "IfExp", "Dict", "Set",
    "ListComp", "SetComp", "DictComp", "GeneratorExp", "Await", "Compare",
    "Call", "FormattedValue", "JoinedStr", "Constant", "Attribute",
    "Subscript", "Starred", "Name", "List", "Tuple", "Slice", "comprehension",
    "keyword", "alias",
    # contexts + operators
    "Load", "Store", "Del", "And", "Or", "Add", "Sub", "Mult", "MatMult",
    "Div", "Mod", "Pow", "LShift", "RShift", "BitOr", "BitXor", "BitAnd",
    "FloorDiv", "Invert", "Not", "UAdd", "USub", "Eq", "NotEq", "Lt", "LtE",
    "Gt", "GtE", "Is", "IsNot", "In", "NotIn",
})

# Names a script may never reference. Most are unreachable anyway (they aren't
# in the namespace), but refusing them at validation time turns a confusing
# NameError at agent-spawn time into a clear message before anything runs.
_BANNED_NAMES: frozenset[str] = frozenset({
    "eval", "exec", "compile", "open", "__import__", "globals", "locals",
    "vars", "getattr", "setattr", "delattr", "hasattr", "input", "breakpoint",
    "exit", "quit", "help", "memoryview", "object", "super", "type",
})


def validate_script(source: str) -> ast.Module:
    """Parse ``source`` and refuse anything outside the workflow subset.

    Returns the parsed module on success; raises :class:`ScriptError` naming
    the offending line otherwise.
    """
    try:
        tree = ast.parse(source, filename="<workflow>", mode="exec")
    except SyntaxError as e:
        raise ScriptError(f"syntax error: {e.msg}", e.lineno) from None

    for node in ast.walk(tree):
        kind = type(node).__name__
        line = getattr(node, "lineno", None)

        if kind in ("Import", "ImportFrom"):
            raise ScriptError(
                "imports are not available in a workflow script — it orchestrates "
                "agents and computes over their results; anything else is an "
                "agent's job, where permissions apply",
                line,
            )
        if kind in ("Yield", "YieldFrom"):
            raise ScriptError("a workflow script cannot be a generator", line)
        if kind == "ClassDef":
            raise ScriptError("class definitions are not available", line)
        if kind == "Global" or kind == "Nonlocal":
            raise ScriptError(f"'{kind.lower()}' is not available", line)
        if kind not in _ALLOWED_NODES:
            raise ScriptError(f"unsupported syntax: {kind}", line)

        # Private/dunder attributes are the standard route out of a restricted
        # namespace (``x.__class__.__mro__[1].__subclasses__()``).
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ScriptError(
                f"attribute '{node.attr}' is not accessible (names starting with "
                "'_' are blocked)",
                line,
            )
        if isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            raise ScriptError(f"'{node.id}' is not available", line)
        # Same for a string used as an attribute name via subscript on a dunder.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("__") and node.value.endswith("__"):
                raise ScriptError(
                    f"dunder string {node.value!r} is not allowed", line)

    return tree


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


def extract_meta(tree: ast.Module) -> tuple[dict[str, Any], list[ast.stmt]]:
    """Pull a leading ``meta = {...}`` literal out of the module body.

    Returns ``(meta, remaining_body)``. ``meta`` must be a pure literal — it is
    read with ``literal_eval`` BEFORE the script runs, so the run can be named
    and its phases drawn in the viewer from the moment it starts, even if the
    body later fails.
    """
    body = list(tree.body)
    meta: dict[str, Any] = {}
    for i, stmt in enumerate(body):
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or target.id != "meta":
            continue
        try:
            value = ast.literal_eval(stmt.value)
        except (ValueError, SyntaxError):
            raise ScriptError(
                "meta must be a literal dict — no variables, calls or f-strings, "
                "so the run can be named before the script executes",
                getattr(stmt, "lineno", None),
            ) from None
        if not isinstance(value, dict):
            raise ScriptError("meta must be a dict", getattr(stmt, "lineno", None))
        meta = value
        del body[i]
        break
    return meta, body


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

_WRAPPER = "__mantis_workflow_main__"

# A deliberately small builtin set: enough to compute over agent results
# (counting, sorting, filtering, formatting) and nothing that reaches outside.
_SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "divmod": divmod, "enumerate": enumerate, "filter": filter, "float": float,
    "format": format, "frozenset": frozenset, "int": int, "isinstance": isinstance,
    "len": len, "list": list, "map": map, "max": max, "min": min, "range": range,
    "repr": repr, "reversed": reversed, "round": round, "set": set,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "True": True, "False": False, "None": None,
    # Exceptions a script may reasonably catch around an agent call.
    "Exception": Exception, "ValueError": ValueError, "KeyError": KeyError,
    "TypeError": TypeError, "IndexError": IndexError,
}


class _Json:
    """``json.dumps`` / ``json.loads`` without exposing the module object."""

    @staticmethod
    def dumps(obj: Any, indent: int | None = None) -> str:
        return json.dumps(obj, indent=indent, default=str)

    @staticmethod
    def loads(text: str) -> Any:
        return json.loads(text)


def _compile(tree: ast.Module, body: list[ast.stmt]) -> Any:
    """Wrap ``body`` in ``async def`` at the AST level and compile it.

    Wrapping the NODES (rather than re-indenting the source into a string)
    keeps every line number pointing at what the model wrote, so an error in a
    script reads as "line 12" and means line 12.
    """
    func = ast.AsyncFunctionDef(
        name=_WRAPPER,
        args=ast.arguments(
            posonlyargs=[], args=[], vararg=None, kwonlyargs=[],
            kw_defaults=[], kwarg=None, defaults=[],
        ),
        body=body or [ast.Pass()],
        decorator_list=[],
        returns=None,
        type_comment=None,
        type_params=[],
    )
    module = ast.Module(body=[func], type_ignores=[])
    ast.copy_location(func, tree.body[0] if tree.body else ast.Module())
    ast.fix_missing_locations(module)
    return compile(module, filename="<workflow>", mode="exec")


async def run_workflow_script(
    source: str,
    wf: Any,
    *,
    args: Any = None,
    log: Callable[[str], None] | None = None,
    sub_workflow: Callable[..., Awaitable[Any]] | None = None,
) -> Any:
    """Validate, compile and run ``source`` against a live ``Workflow``.

    The script's return value is the workflow's result. Every hook maps
    straight onto the engine, so what the model writes is what the viewer
    shows and what the concurrency cap governs.
    """
    tree = validate_script(source)
    meta, body = extract_meta(tree)

    if meta.get("name"):
        wf.name = str(meta["name"])
        wf.run.name = str(meta["name"])
    # Declare the phases up front so the rail renders in order from the start
    # rather than materializing as each phase happens to be reached.
    for entry in meta.get("phases") or []:
        if isinstance(entry, dict) and entry.get("title"):
            wf._get_phase(str(entry["title"]), str(entry.get("detail") or ""))

    code = _compile(tree, body)

    def _log(msg: str) -> None:
        wf.log(str(msg))
        if log is not None:
            log(str(msg))

    namespace: dict[str, Any] = {
        "__builtins__": dict(_SAFE_BUILTINS),
        # The engine, verbatim — same semantics as the Python API.
        "agent": wf.agent,
        "parallel": wf.parallel,
        "pipeline": wf.pipeline,
        "phase": _make_phase(wf),
        "log": _log,
        "print": _log,          # a script that prints means to say something
        "args": args,
        "budget": _BudgetView(wf),
        "json": _Json,
    }
    if sub_workflow is not None:
        namespace["workflow"] = sub_workflow

    try:
        exec(code, namespace)              # noqa: S102 — validated subset above
        return await namespace[_WRAPPER]()
    except ScriptError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface as a script error
        line = _script_line(exc)
        raise ScriptError(f"{type(exc).__name__}: {exc}", line) from exc


def _make_phase(wf: Any) -> Callable[..., None]:
    """``phase("X")`` creates the phase AND makes it current.

    Both halves matter: creating it puts the title in the rail in script order,
    and setting ``_current_phase`` is what makes subsequent ``agent()`` calls
    land under it instead of the default "main".
    """

    def phase(title: str, detail: str = "") -> None:
        wf._get_phase(str(title), str(detail))
        wf._current_phase = str(title)

    return phase


def _script_line(exc: BaseException) -> int | None:
    """The line in the model's script that raised, if the traceback has one."""
    tb = exc.__traceback__
    line = None
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == "<workflow>":
            line = tb.tb_lineno
        tb = tb.tb_next
    return line


class _BudgetView:
    """``budget.total`` / ``budget.spent()`` / ``budget.remaining()``.

    Lets a script scale its own depth to what it has been given — the
    loop-until-budget shape — without reaching into the tracker.
    """

    def __init__(self, wf: Any) -> None:
        self._wf = wf

    @property
    def total(self) -> float | None:
        b = getattr(self._wf, "budget", None)
        return getattr(b, "max_usd", None) if b is not None else None

    def spent(self) -> float:
        tracker = getattr(self._wf, "budget_tracker", None)
        return float(getattr(tracker, "total_usd", 0.0) or 0.0)

    def remaining(self) -> float:
        total = self.total
        if total is None:
            return float("inf")
        return max(0.0, float(total) - self.spent())
