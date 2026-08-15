#!/usr/bin/env python3
"""Doc-vs-code drift checker — does every snippet in the docs actually work?

Motivation
----------

Both doc trees shipped a "Forcing a backend" example that passed ``base_url``
and ``api_key`` as options. Neither was read by any code path: unknown option
keys flow silently into ``Agent.extra``, so the snippet ran, made a request to
the wrong URL with no auth, and told the reader nothing. A wrong example is
worse than a missing one, and nothing in CI could tell the difference.

So this checker doesn't compare docs against a hand-maintained list of "valid
options" — that list would rot the same way the prose did. It asks the *code*:

    put this key in an options dict, build the agent, and see whether the key
    ends up doing something or lands in ``extra`` (i.e. is silently ignored).

Same idea for the rest of the rules: every fact is probed against the live
package, so the day the code changes, the check changes with it.

Rules
-----

``import``
    Every name imported from ``mantis_agent`` in a snippet must exist.

``kwarg``
    Every keyword argument to a ``mantis_agent`` class/function must be
    accepted by its real signature.

``option``
    Every key in an options dict / ``MantisAgentOptions(...)`` must be honored
    by the code path that snippet uses — see ``_wire_key_is_honored``.

``shape``
    ``query()`` has two option shapes with two different message shapes. A
    snippet must not mix them (dict options → ``msg.message.content``;
    ``MantisAgentOptions`` → ``msg.content``).

``env``
    Every ``MANTIS_*`` environment variable named in a snippet must appear in
    the source. Catches typos and renamed vars.

``syntax``
    Every python block must parse.

Usage
-----

    python scripts/check_doc_snippets.py              # both doc trees
    python scripts/check_doc_snippets.py docs         # just one
    python scripts/check_doc_snippets.py --json       # machine-readable

Exit code is 1 when any finding survives, so it works as a CI gate. The
pytest wrapper lives in ``tests/test_docs_snippets.py``.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import importlib
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, NamedTuple

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: The two doc trees. They have drifted apart historically (mkdocs ships with
#: the repo, web/ is what mantisagent.cc serves), so both get checked.
DOC_ROOTS = ("docs", "web/content/docs")

#: A snippet can opt out with this marker on the fence line or the line above,
#: for genuinely illustrative pseudo-code. Used sparingly — an opt-out is a
#: promise that the block isn't meant to be copy-pasted.
SKIP_MARKER = "docs-check: skip"

PY_LANGS = {"python", "py", "python3"}
SH_LANGS = {"bash", "sh", "shell", "console", "shell-session", "zsh"}


class Finding(NamedTuple):
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.path}:{self.line} [{self.rule}] {self.message}"


class Block(NamedTuple):
    lang: str
    code: str
    #: 1-based line in the markdown file where the code (not the fence) starts.
    line: int


# ---------------------------------------------------------------------------
# Markdown extraction
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^(?P<indent>\s*)(?P<ticks>`{3,}|~{3,})(?P<info>[^\n]*)$")


def iter_blocks(text: str) -> Iterator[Block]:
    """Yield fenced code blocks. Handles ``` and ~~~, nested indentation, and
    the ``python title="x"`` / ``{.python}`` info strings both doc tools use."""

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        ticks, indent = m.group("ticks"), m.group("indent")
        info = m.group("info").strip()
        skip = SKIP_MARKER in info or (i > 0 and SKIP_MARKER in lines[i - 1])
        # First token of the info string is the language; strip attribute
        # syntax like {.python .copy} and hl_lines="…".
        lang = re.split(r"[\s{}.,;:]+", info.lstrip("{."), maxsplit=1)[0].lower()
        body: list[str] = []
        start = i + 2  # 1-based line of the first code line
        i += 1
        while i < len(lines):
            close = _FENCE.match(lines[i])
            if close and close.group("ticks")[0] == ticks[0] and len(
                close.group("ticks")
            ) >= len(ticks) and not close.group("info").strip():
                break
            body.append(lines[i][len(indent):] if lines[i].startswith(indent) else lines[i])
            i += 1
        i += 1
        if not skip:
            yield Block(lang, "\n".join(body), start)


# ---------------------------------------------------------------------------
# Probes — every question is asked of the live package, never of a list
# ---------------------------------------------------------------------------

_SENTINEL = "__docs_check_sentinel__"


def _resolve(dotted: str) -> Any:
    """``"mantis_agent.query"`` / ``"mantis_agent.Agent"`` → the object, or None."""

    try:
        return importlib.import_module(dotted)
    except ImportError:
        pass
    mod, _, attr = dotted.rpartition(".")
    if not mod:
        return None
    try:
        parent = importlib.import_module(mod)
    except ImportError:
        return None
    return getattr(parent, attr, None)


def _accepts_kwargs(obj: Any) -> bool:
    """True when a **kwargs catch-all makes kwarg checking meaningless."""

    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


def _max_positional(obj: Any) -> int | None:
    """How many positional arguments ``obj`` accepts, or None if unbounded."""

    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return None
    count = 0
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_POSITIONAL:
            return None  # *args — anything goes
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                      inspect.Parameter.POSITIONAL_OR_KEYWORD):
            count += 1
    return count


def _valid_kwargs(obj: Any) -> set[str] | None:
    """Accepted keyword names, or None when we can't tell (so we stay quiet)."""

    if _accepts_kwargs(obj):
        return None
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return None
    return {
        p.name
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }


#: ``Agent.extra`` is a deliberate escape hatch: several features read their
#: config back out of it by name, so landing in ``extra`` is not by itself
#: proof that a key is dead. This spots a genuine read — ``extra.get("k")``,
#: ``extra["k"]``, ``extra_opt.get("k")`` — anywhere in the package.
def _extra_key_is_read(key: str) -> bool:
    pat = re.compile(
        r"extra\w*\s*(?:\.get\(\s*|\[\s*)['\"]" + re.escape(key) + r"['\"]"
    )
    return bool(pat.search(_source_blob()))


def _wire_key_is_honored(key: str) -> bool:
    """Does ``query(options={key: ...})`` (dict / wire shape) do anything?

    The dict path builds an ``Agent`` via ``query._agent_from_options``, and
    every key it doesn't recognize is swept into ``Agent.extra``. So: build an
    agent with the key set to a sentinel and look for the sentinel in
    ``extra``. If it's sitting there and nothing in the package ever reads
    that name back out, the key is a no-op and any doc showing it is lying to
    the reader.

    A key that raises on a sentinel value is *recognized* — it got as far as
    type-validating our nonsense — so that counts as honored.
    """

    from mantis_agent.query import _agent_from_options

    opts = {"model": "mock", "backend": "mock", key: _SENTINEL}
    try:
        agent = _agent_from_options(opts)
    except (TypeError, ValueError):
        return True
    extra = getattr(agent, "extra", None) or {}
    if extra.get(key) != _SENTINEL:
        return True  # consumed by a real constructor param
    return _extra_key_is_read(key)


def _hook_event_is_honored(event: str) -> bool:
    """Does ``hooks={event: [...]}`` register anything?

    The dict form maps event names onto the internal ``Hooks`` dataclass and
    **silently skips** names it doesn't know, so a plausible invention like
    ``"PreModelCall"`` registers nothing and reports no error. Probe it: feed
    one event and see whether any slot on the result is populated.
    """

    from mantis_agent import HookMatcher
    from mantis_agent.claude_compat import _convert_hooks_dict

    async def _noop(_ctx: Any) -> None:
        return None

    try:
        hooks = _convert_hooks_dict({event: [HookMatcher(hooks=[_noop])]})
    except Exception:  # noqa: BLE001
        return True
    return any(
        getattr(hooks, f.name, None) is not None
        for f in dataclasses.fields(hooks)
    )


def _typed_option_fields() -> set[str]:
    """Field names ``MantisAgentOptions`` actually accepts."""

    from mantis_agent import MantisAgentOptions

    return {f.name for f in dataclasses.fields(MantisAgentOptions)}


#: Type annotations a plain string sentinel is a *legitimate* value for. The
#: "accepted but silently dropped" probe below only runs on these: feeding a
#: string to a ``list[...]`` or ``Callable`` field can no-op for reasons that
#: have nothing to do with drift (a str is iterable, so a list field quietly
#: accepts and discards it), which would report a false alarm.
_SCALAR_ANNOTATION = re.compile(
    r"^(?:str|int|float|bool|None|Literal\[[^\]]*\]|\s|\|)+$"
)


def _is_scalar_field(name: str) -> bool:
    from mantis_agent import MantisAgentOptions

    for f in dataclasses.fields(MantisAgentOptions):
        if f.name == name:
            return bool(_SCALAR_ANNOTATION.match(str(f.type)))
    return False


def _typed_key_is_honored(key: str) -> bool:
    """Does ``MantisAgentOptions(key=...)`` survive the trip to the agent?

    Two ways to fail: the dataclass rejects the kwarg outright (a hard
    TypeError the reader would at least see), or it accepts the field and
    ``to_query_options()`` drops it on the floor (silent, which is worse).
    ``extra`` is the documented escape hatch, so it is honored by definition.
    """

    from mantis_agent import MantisAgentOptions

    if key not in _typed_option_fields():
        return False
    if key in {"extra"} or not _is_scalar_field(key):
        return True
    try:
        baseline = MantisAgentOptions().to_query_options()
        wire = MantisAgentOptions(**{key: _SENTINEL}).to_query_options()
    except Exception:  # noqa: BLE001 — validated our nonsense, therefore seen
        return True
    # Deliberately compares whole outputs rather than looking for the key by
    # name: fields get renamed (system_prompt → system), nested (allowed_tools
    # → extra), or split into several keys on the way through. Any difference
    # at all means the field reached the wire; no difference means it was
    # accepted by the dataclass and then quietly discarded.
    return wire != baseline


_SOURCE_BLOB: str | None = None


def _source_blob() -> str:
    """All package source concatenated, for "is this name real" questions."""

    global _SOURCE_BLOB
    if _SOURCE_BLOB is None:
        parts: list[str] = []
        for py in sorted((REPO / "mantis_agent").rglob("*.py")):
            try:
                parts.append(py.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        _SOURCE_BLOB = "\n".join(parts)
    return _SOURCE_BLOB


# ---------------------------------------------------------------------------
# Python block checks
# ---------------------------------------------------------------------------

_ENV_RE = re.compile(r"\bMANTIS_[A-Z0-9_]+\b")


class _PyVisitor(ast.NodeVisitor):
    """Collects the facts we can check from one snippet's AST."""

    def __init__(self) -> None:
        #: local name → dotted mantis path, for `from mantis_agent import Agent`
        self.aliases: dict[str, str] = {}
        self.import_nodes: list[tuple[ast.AST, str, str]] = []  # node, module, name
        self.calls: list[ast.Call] = []

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        mod = node.module or ""
        if mod == "mantis_agent" or mod.startswith("mantis_agent."):
            for a in node.names:
                if a.name == "*":
                    continue
                self.import_nodes.append((node, mod, a.name))
                self.aliases[a.asname or a.name] = f"{mod}.{a.name}"
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for a in node.names:
            if a.name == "mantis_agent" or a.name.startswith("mantis_agent."):
                self.aliases[a.asname or a.name.split(".")[0]] = a.name
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        self.calls.append(node)
        self.generic_visit(node)


_PUBLIC_NAMES: set[str] | None = None


def _public_api_names() -> set[str]:
    """Everything ``mantis_agent.__all__`` exports."""

    global _PUBLIC_NAMES
    if _PUBLIC_NAMES is None:
        import mantis_agent

        _PUBLIC_NAMES = set(getattr(mantis_agent, "__all__", ()) or ())
    return _PUBLIC_NAMES


def _resolve_local(name: str, aliases: dict[str, str]) -> str | None:
    """Dotted mantis path for a name used in a snippet.

    Prefers an explicit import in the same block, then falls back to the public
    API — continuation snippets routinely omit the imports shown a few lines
    earlier, and skipping those blocks left whole pages unchecked.
    """

    head = name.split(".")[0]
    if head in aliases:
        return aliases[head]
    if head in _public_api_names():
        return f"mantis_agent.{head}"
    return None


def _dotted_name(node: ast.AST) -> str | None:
    """``a.b.c`` → "a.b.c" for Name/Attribute chains."""

    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return ".".join(reversed(parts))


def _dict_keys(node: ast.AST) -> list[tuple[str, int]] | None:
    """String keys of a dict literal, with line numbers. None if not a literal."""

    if not isinstance(node, ast.Dict):
        return None
    out: list[tuple[str, int]] = []
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            out.append((k.value, getattr(k, "lineno", 0)))
    return out


def _dead_option_message(key: str) -> str:
    """Distinguish "this key was never a thing" from "this key stopped being
    wired up" — the reader needs different advice in each case."""

    # Looks for the name in any form — quoted key, identifier, attribute — so
    # "appears nowhere" is a claim worth making. A name that exists somewhere
    # but isn't wired to options gets the softer, accurate message instead.
    if re.search(r"\b" + re.escape(key) + r"\b", _source_blob()) is None:
        return (f"options[{key!r}] appears nowhere in mantis_agent/ — this option "
                f"does not exist and never did")
    return (f"options[{key!r}] is silently ignored on the dict (wire-shape) path — "
            f"it lands in Agent.extra and nothing reads it back out")


def _known_fields(cls: Any) -> set[str] | None:
    """Attribute names an instance of ``cls`` has, or None if we can't tell.

    Covers both shapes the package uses: dataclasses (options, specs) and
    ``msgspec.Struct`` (messages, stream events). Struct fields don't show up
    in ``dir()`` on the class, so they need ``__struct_fields__``.
    """

    if dataclasses.is_dataclass(cls):
        return {f.name for f in dataclasses.fields(cls)} | set(dir(cls))
    struct_fields = getattr(cls, "__struct_fields__", None)
    if struct_fields is not None:
        return set(struct_fields) | set(dir(cls))
    return None


def _is_checkable_type(cls: Any) -> bool:
    return inspect.isclass(cls) and _known_fields(cls) is not None


def _returned_dataclass(fn: Any) -> Any:
    """The dataclass a mantis function returns, if its annotation names one.

    Used to check attribute access on results — ``lookup_model(...)`` returns a
    ``ModelCapability``, so ``cap.tool_use_path`` is checkable and, as it
    happens, wrong: that field does not exist and both doc trees printed it.
    """

    try:
        ret = inspect.signature(fn).return_annotation
    except (TypeError, ValueError):
        return None
    if ret is inspect.Signature.empty:
        return None
    if isinstance(ret, str):
        # Postponed annotation — resolve against the defining module, and give
        # up on anything that isn't a plain name (unions, generics, quotes).
        name = ret.strip().strip("'\"")
        if not name.isidentifier():
            return None
        mod = inspect.getmodule(fn)
        ret = getattr(mod, name, None) if mod else None
    return ret if dataclasses.is_dataclass(ret) else None


def check_python(block: Block, path: str) -> list[Finding]:
    out: list[Finding] = []

    def at(node: ast.AST | int) -> int:
        lineno = node if isinstance(node, int) else getattr(node, "lineno", 1)
        return block.line + max(0, lineno - 1)

    try:
        tree = ast.parse(block.code)
    except SyntaxError as e:
        # `Plugin(name="x", ...)` is the standard elision idiom, not a bug —
        # retry with trailing `...` arguments stripped and stay quiet if that
        # was the only thing wrong. Anything else is a real break.
        elided = re.sub(r",\s*\.\.\.\s*(?=[)\]}])", "", block.code)
        try:
            tree = ast.parse(elided)
        except SyntaxError:
            out.append(
                Finding(path, block.line + max(0, (e.lineno or 1) - 1), "syntax",
                        f"snippet does not parse: {e.msg}")
            )
            return out

    v = _PyVisitor()
    v.visit(tree)

    # --- rule: import ----------------------------------------------------
    for node, mod, name in v.import_nodes:
        if _resolve(f"{mod}.{name}") is None:
            out.append(Finding(
                path, at(node), "import",
                f"`from {mod} import {name}` — {name} does not exist in {mod}",
            ))

    # --- rules: kwarg / option / shape -----------------------------------
    uses_dict_options = False
    uses_typed_options = False

    for call in v.calls:
        fname = _dotted_name(call.func)
        if not fname:
            continue
        dotted = _resolve_local(fname, v.aliases)
        target: Any = None
        if dotted:
            tail = fname.split(".")[1:]
            target = _resolve(dotted)
            for part in tail:
                target = getattr(target, part, None) if target is not None else None

        is_query = bool(dotted) and dotted.endswith(".query") or fname.endswith("query")
        is_typed_opts = bool(dotted) and dotted.endswith("MantisAgentOptions")

        # options= on a query() call, or a bare `options = {...}` assignment
        # picked up below.
        if is_query:
            for kw in call.keywords:
                if kw.arg != "options":
                    continue
                keys = _dict_keys(kw.value)
                if keys is not None:
                    uses_dict_options = True
                    for key, ln in keys:
                        if not _wire_key_is_honored(key):
                            out.append(Finding(
                                path, at(ln), "option", _dead_option_message(key)))
                elif isinstance(kw.value, ast.Call) and _dotted_name(kw.value.func) and \
                        "MantisAgentOptions" in (_dotted_name(kw.value.func) or ""):
                    uses_typed_options = True

        if is_typed_opts:
            uses_typed_options = True
            fields = _typed_option_fields()
            for kw in call.keywords:
                if kw.arg is None:
                    continue
                if kw.arg not in fields:
                    out.append(Finding(
                        path, at(kw.value), "kwarg",
                        f"MantisAgentOptions(...) has no field {kw.arg!r} — "
                        f"this raises TypeError",
                    ))
                elif not _typed_key_is_honored(kw.arg):
                    out.append(Finding(
                        path, at(kw.value), "option",
                        f"MantisAgentOptions.{kw.arg} is accepted but dropped before "
                        f"the agent sees it",
                    ))
            continue

        if target is None or not (inspect.isclass(target) or inspect.isroutine(target)):
            continue

        # --- rule: arity ---------------------------------------------------
        # Passing an argument to a function that takes none is invisible to the
        # kwarg rule and fails only at runtime. ``iter_transcripts("~/…")``
        # shipped in the sessions guide against a zero-argument function.
        n_positional = len([a for a in call.args if not isinstance(a, ast.Starred)])
        if n_positional and not any(isinstance(a, ast.Starred) for a in call.args):
            limit = _max_positional(target)
            if limit is not None and n_positional > limit:
                out.append(Finding(
                    path, at(call), "arity",
                    f"{fname}() takes {limit} positional argument(s), "
                    f"{n_positional} given",
                ))

        valid = _valid_kwargs(target)
        if valid is None:
            continue
        for kw in call.keywords:
            if kw.arg is None:
                continue
            if kw.arg not in valid:
                out.append(Finding(
                    path, at(kw.value), "kwarg",
                    f"{fname}(...) does not accept {kw.arg!r} "
                    f"(accepts: {', '.join(sorted(valid)[:8])}…)",
                ))

    # --- rule: hook_event -------------------------------------------------
    # Any `hooks={...}` dict, wherever it appears. Unknown event names are
    # dropped without a word, so the snippet runs and the hook never fires.
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "hooks":
            keys = _dict_keys(node.value)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == "hooks":
            keys = _dict_keys(node.value)
        else:
            continue
        for event, ln in keys or []:
            if not _hook_event_is_honored(event):
                out.append(Finding(
                    path, at(ln), "hook_event",
                    f"hook event {event!r} is not one the dict form recognizes — "
                    f"it is silently skipped and the hook never runs",
                ))

    # A bare `options = {...}` / `options: dict = {...}` module-level dict is
    # the other shape docs use; check its keys the same way.
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        if not names or not any("option" in n.lower() for n in names):
            continue
        keys = _dict_keys(node.value) if node.value is not None else None
        if keys is None:
            continue
        uses_dict_options = True
        for key, ln in keys:
            if not _wire_key_is_honored(key):
                out.append(Finding(path, at(ln), "option", _dead_option_message(key)))

    # --- rule: shape -----------------------------------------------------
    # Two option shapes, two message shapes. Mixing them is the single most
    # confusing failure in this SDK, and it fails at *runtime* with an
    # AttributeError far from the cause.
    nested = re.search(r"\bmsg\.message\b", block.code)
    if uses_typed_options and not uses_dict_options and nested:
        out.append(Finding(
            path, at(block.code[:nested.start()].count("\n") + 1), "shape",
            "MantisAgentOptions yields the flat Claude-SDK shape — use "
            "`msg.content`, not `msg.message.content` (nested is the dict path)",
        ))
    flat = re.search(r"for block in msg\.content\b", block.code)
    if uses_dict_options and not uses_typed_options and flat:
        out.append(Finding(
            path, at(block.code[:flat.start()].count("\n") + 1), "shape",
            "a dict `options` yields the nested wire shape — use "
            "`msg.message.content`, not `msg.content`",
        ))

    # --- rule: attr -------------------------------------------------------
    # `cap = lookup_model(...)` then `cap.<field>` — checkable, and exactly how
    # a plausible-looking field name that never existed gets printed in a doc.
    returns: dict[str, Any] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        fname = _dotted_name(node.value.func)
        dotted = _resolve_local(fname, v.aliases) if fname else None
        if not dotted:
            continue
        target = _resolve(dotted)
        for part in (fname or "").split(".")[1:]:
            target = getattr(target, part, None) if target is not None else None
        cls = _returned_dataclass(target) if target is not None else None
        if cls is None:
            continue
        for t in node.targets:
            if isinstance(t, ast.Name):
                returns[t.id] = cls

    def _check_attrs(scope: ast.AST, bindings: dict[str, Any], why: str) -> None:
        for node in ast.walk(scope):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                continue
            cls = bindings.get(node.value.id)
            if cls is None or node.attr.startswith("_"):
                continue
            known = _known_fields(cls)
            if known is not None and node.attr not in known:
                out.append(Finding(
                    path, at(node), "attr",
                    f"{cls.__name__} has no attribute {node.attr!r} "
                    f"({why} `{node.value.id}`)",
                ))

    _check_attrs(tree, returns, "returned by the call assigned to")

    # Narrowing form: `if isinstance(ev.delta, ThinkingDelta): ev.delta.text` —
    # how a doc comes to print a field of the wrong sibling type. The expression
    # being narrowed is often an attribute chain, so bind on its source text.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.While)):
            continue
        for test in ast.walk(node.test):
            if not (isinstance(test, ast.Call)
                    and getattr(test.func, "id", None) == "isinstance"
                    and len(test.args) == 2):
                continue
            subject, type_node = test.args
            type_name = _dotted_name(type_node)
            dotted = _resolve_local(type_name, v.aliases) if type_name else None
            cls = _resolve(dotted) if dotted else None
            if cls is None or not _is_checkable_type(cls):
                continue
            subject_src = _dotted_name(subject)
            if not subject_src:
                continue
            known = _known_fields(cls) or set()
            # Only the branch the test guards — an `elif` narrows the same
            # subject to a *different* type, and its own ast.If node covers it.
            for stmt in node.body:
                for body_node in ast.walk(stmt):
                    if not isinstance(body_node, ast.Attribute):
                        continue
                    if _dotted_name(body_node.value) != subject_src:
                        continue
                    if body_node.attr.startswith("_") or body_node.attr in known:
                        continue
                    out.append(Finding(
                        path, at(body_node), "attr",
                        f"{cls.__name__} has no attribute {body_node.attr!r} "
                        f"(`{subject_src}` was narrowed to it by isinstance)",
                    ))

    out.extend(_check_env(block, path))
    return out


# ---------------------------------------------------------------------------
# Shell block checks
# ---------------------------------------------------------------------------


def _check_env(block: Block, path: str) -> list[Finding]:
    """Every ``MANTIS_*`` var named in the docs must exist in the source."""

    out: list[Finding] = []
    blob = _source_blob()
    seen: set[str] = set()
    for i, line in enumerate(block.code.splitlines(), start=1):
        for name in _ENV_RE.findall(line):
            if name in seen:
                continue
            seen.add(name)
            if name not in blob:
                out.append(Finding(
                    path, block.line + i - 1, "env",
                    f"${name} is documented but appears nowhere in mantis_agent/ "
                    f"— renamed or a typo",
                ))
    return out


def check_shell(block: Block, path: str) -> list[Finding]:
    return _check_env(block, path)


# ---------------------------------------------------------------------------
# Prose and JSON checks — the drift that isn't in a python block
# ---------------------------------------------------------------------------


#: Naming a variable in order to say it does *not* exist is legitimate — a
#: migration note is exactly where a reader looks after copying a dead knob
#: from an older page. Declare it once per file:
#:     <!-- docs-check: skip-env MANTIS_AGENT_BACKEND -->
_SKIP_ENV_RE = re.compile(r"docs-check:\s*skip-env\s+([A-Z0-9_,\s]+)")


def check_prose_env(text: str, path: str) -> list[Finding]:
    """``MANTIS_*`` vars named in prose or a reference table must be real.

    The env-var table is exactly where a reader goes to find the knob they
    need, and a row for a variable nothing reads is a dead end that looks
    authoritative. Checked over the whole file, since these live in tables and
    inline code spans rather than fenced blocks.
    """

    out: list[Finding] = []
    blob = _source_blob()
    exempt = {
        n.strip()
        for m in _SKIP_ENV_RE.findall(text)
        for n in m.replace(",", " ").split()
    }
    seen: set[str] = set()
    for i, line in enumerate(text.splitlines(), start=1):
        for name in _ENV_RE.findall(line):
            if name in seen or name in exempt:
                continue
            seen.add(name)
            if name not in blob:
                out.append(Finding(
                    path, i, "env",
                    f"${name} is documented but appears nowhere in mantis_agent/ "
                    f"— renamed or never existed",
                ))
    return out


_DIRECT_SETTINGS_KEYS: set[str] | None = None


def _direct_settings_keys() -> set[str]:
    """Settings keys read straight off a ``load_settings()`` result.

    Most keys reach the agent through ``apply_settings_to_options``, but some
    features read their own key directly — ``advisor.py`` does
    ``(load_settings(SETTING_SOURCES) or {}).get(_SETTING)``, where ``_SETTING``
    is the module constant ``"advisorModel"``. Those keys are just as real, so
    find them by AST rather than reporting a live feature as dead: locate
    ``.get(...)`` calls whose receiver involves ``load_settings`` (directly or
    through the variable it was assigned to), then resolve constant arguments
    to their literal value.
    """

    global _DIRECT_SETTINGS_KEYS
    if _DIRECT_SETTINGS_KEYS is not None:
        return _DIRECT_SETTINGS_KEYS

    found: set[str] = set()
    for py in sorted((REPO / "mantis_agent").rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue

        # Module-level `NAME = "literal"` constants, for `.get(NAME)`.
        consts: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        consts[t.id] = node.value.value

        def _mentions_load_settings(node: ast.AST) -> bool:
            return any(
                isinstance(n, ast.Call) and (
                    getattr(n.func, "id", None) == "load_settings"
                    or getattr(n.func, "attr", None) == "load_settings"
                )
                for n in ast.walk(node)
            )

        # Variables holding a load_settings() result.
        holders: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and node.value is not None \
                    and _mentions_load_settings(node.value):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        holders.add(t.id)

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and node.args):
                continue
            recv = node.func.value
            direct = _mentions_load_settings(recv)
            via_var = isinstance(recv, ast.Name) and recv.id in holders
            if not (direct or via_var):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in consts:
                found.add(consts[arg.id])

    _DIRECT_SETTINGS_KEYS = found
    return found


def _settings_key_is_honored(key: str) -> bool:
    """Does a ``settings.json`` key change the options the agent is built from?

    Same probe as the option rules: hand ``apply_settings_to_options`` a single
    key and see whether the merged result differs from the empty case. A key
    that changes nothing — and that no feature reads directly — is a key the
    reader can set all day with no effect.
    """

    from mantis_agent.settings import apply_settings_to_options

    if key in _direct_settings_keys():
        return True
    try:
        baseline = apply_settings_to_options({}, {})
        merged = apply_settings_to_options({}, {key: _SENTINEL})
    except Exception:  # noqa: BLE001 — validated our nonsense, therefore seen
        return True
    return merged != baseline


def _nested_settings_key_is_honored(top: str, sub: str) -> bool:
    """Same probe, one level in: does ``{top: {sub: …}}`` change anything?

    Blocks like ``sandbox`` are consumed by their own module rather than by the
    merge, so a sub-key that the merge ignores may still be read — accept a
    plain ``.get("sub")`` / ``["sub"]`` read anywhere in the package as proof.
    """

    from mantis_agent.settings import apply_settings_to_options

    try:
        baseline = apply_settings_to_options({}, {top: {}})
        merged = apply_settings_to_options({}, {top: {sub: _SENTINEL}})
        if merged != baseline:
            return True
    except Exception:  # noqa: BLE001 — validated our nonsense, therefore seen
        return True
    read = re.compile(r"(?:\.get\(\s*|\[\s*)['\"]" + re.escape(sub) + r"['\"]")
    return bool(read.search(_source_blob()))


def check_settings_json(block: Block, path: str) -> list[Finding]:
    """Check keys in a ``settings.json`` example.

    Only blocks that look like settings files are checked: at least one
    top-level key has to be a real settings key, which keeps MCP configs and
    other unrelated JSON out of scope.
    """

    try:
        data = json.loads(block.code)
    except ValueError:
        return []
    if not isinstance(data, dict) or not data:
        return []
    honored = {k: _settings_key_is_honored(k) for k in data if isinstance(k, str)}
    if not any(honored.values()):
        return []  # not a settings file

    # Nested keys under a honored dict-valued key get the same treatment —
    # ``permissions`` is real but only ``allow``/``deny`` inside it are, and a
    # plausible-looking invention like ``permissions.default_mode`` is exactly
    # what a reader would copy and never notice doing nothing.
    for top, value in data.items():
        if not (honored.get(top) and isinstance(value, dict)):
            continue
        for sub in value:
            if isinstance(sub, str):
                honored[f"{top}.{sub}"] = _nested_settings_key_is_honored(top, sub)

    out: list[Finding] = []
    for i, line in enumerate(block.code.splitlines(), start=1):
        for key, ok in honored.items():
            leaf = key.rsplit(".", 1)[-1]
            if ok or f'"{leaf}"' not in line:
                continue
            out.append(Finding(
                path, block.line + i - 1, "settings",
                f"settings.json key {key!r} is read by nothing — setting it has "
                f"no effect on the agent",
            ))
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def check_file(md: Path, rel: str) -> list[Finding]:
    text = md.read_text(encoding="utf-8", errors="replace")
    out: list[Finding] = []
    for block in iter_blocks(text):
        if block.lang in PY_LANGS:
            out.extend(check_python(block, rel))
        elif block.lang in SH_LANGS:
            out.extend(check_shell(block, rel))
        elif block.lang == "json":
            out.extend(check_settings_json(block, rel))
    out.extend(check_prose_env(text, rel))
    return out


def iter_doc_files(roots: Iterable[str]) -> Iterator[Path]:
    """Accepts directories *and* single files — checking one page while editing
    it is the common case, and silently checking nothing (the old behavior for
    a file argument) is worse than an error."""

    for root in roots:
        base = (REPO / root) if not Path(root).is_absolute() else Path(root)
        if base.is_file():
            yield base
            continue
        if not base.exists():
            raise SystemExit(f"no such doc root: {root}")
        for pattern in ("*.md", "*.mdx"):
            yield from sorted(base.rglob(pattern))


def run(roots: Iterable[str] = DOC_ROOTS) -> list[Finding]:
    findings: list[Finding] = []
    for md in iter_doc_files(roots):
        rel = str(md.relative_to(REPO))
        findings.extend(check_file(md, rel))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("roots", nargs="*", default=None,
                    help=f"doc roots to check (default: {' '.join(DOC_ROOTS)})")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    findings = run(args.roots or DOC_ROOTS)
    if args.json:
        print(json.dumps([f._asdict() for f in findings], indent=2))
    else:
        by_rule: dict[str, int] = {}
        for f in findings:
            by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
            print(f)
        total = len(findings)
        print()
        if total:
            summary = ", ".join(f"{k}={v}" for k, v in sorted(by_rule.items()))
            print(f"{total} finding(s) — {summary}")
        else:
            print("docs match the code.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
