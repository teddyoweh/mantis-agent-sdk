"""``lsp`` — semantic code navigation for Python, the mantis way.

Claude Code's LSP tool shells out to a language server (pyright, rust-analyzer,
…). That's heavy and needs the server installed. Since Python is mantis's home
turf, this tool gets the 80% of that value — goto-definition and find-references
— from the stdlib ``ast`` module alone: zero dependencies, works out of the box.
It resolves defs (functions, classes, methods, module-level assignments) and
usages (names + attribute accesses) across the project by AST, which grep can't
do (grep can't tell a definition from a mention, or skip a comment).
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from ..tools import tool

_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", ".tox",
    ".mypy_cache", ".pytest_cache", "build", ".ruff_cache",
}


def _rx(p: str) -> "re.Pattern[str]":
    return re.compile(p)


# Definition patterns for non-Python languages (Python uses ast). Each is
# ``(regex_capturing_the_name, kind)`` matched per line. Kept precise — only
# declaration syntax, so a call or a control-flow brace isn't mistaken for a def.
_JS_DEFS = [
    (_rx(r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)"), "function"),
    (_rx(r"(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)"), "class"),
    (_rx(r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*="), "const"),
]
_TS_EXTRA = [
    (_rx(r"(?:export\s+)?interface\s+(\w+)"), "interface"),
    (_rx(r"(?:export\s+)?type\s+(\w+)\s*="), "type"),
    (_rx(r"(?:export\s+)?enum\s+(\w+)"), "enum"),
]
_LANG_DEFS: dict[str, list] = {
    "js": _JS_DEFS,
    "ts": _JS_DEFS + _TS_EXTRA,
    "go": [(_rx(r"func\s+(?:\([^)]*\)\s*)?(\w+)"), "func"), (_rx(r"type\s+(\w+)\s"), "type")],
    "rs": [(_rx(r"(?:pub\s+)?(?:async\s+)?fn\s+(\w+)"), "fn"),
           (_rx(r"(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)"), "type")],
    "java": [(_rx(r"(?:public|private|protected|static|final|abstract|\s)*(?:class|interface|enum)\s+(\w+)"), "class")],
    "rb": [(_rx(r"def\s+(\w+)"), "def"), (_rx(r"class\s+(\w+)"), "class"),
           (_rx(r"module\s+(\w+)"), "module")],
    "c": [(_rx(r"^[\w\*\s]+?\s+\**(\w+)\s*\([^;{]*\)\s*\{?\s*$"), "func")],
}
_EXT_LANG = {
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js",
    ".ts": "ts", ".tsx": "ts",
    ".go": "go", ".rs": "rs", ".java": "java", ".rb": "rb",
    ".c": "c", ".h": "c", ".cpp": "c", ".cc": "c", ".hpp": "c",
}
_ALL_EXTS = {".py", *_EXT_LANG}


def _source_files(root: str, exts: set[str], cap: int = 3000) -> list[str]:
    p = Path(root).expanduser()
    if p.is_file():
        return [str(p)] if p.suffix in exts else []
    out: list[str] = []
    for dp, dns, fns in os.walk(p):
        dns[:] = [d for d in dns if d not in _IGNORE_DIRS and not d.startswith(".")]
        for f in fns:
            if os.path.splitext(f)[1] in exts:
                out.append(os.path.join(dp, f))
                if len(out) >= cap:
                    return out
    return out


def _py_files(root: str, cap: int = 3000) -> list[str]:
    return _source_files(root, {".py"}, cap)


def _regex_defs(root: str, symbol: str | None) -> list[tuple[str, int, str, str]]:
    """``(relpath, line, kind, name)`` for non-Python definitions. ``symbol``
    None → every definition (outline); else only exact-name matches."""
    hits: list[tuple[str, int, str, str]] = []
    base = Path(root).expanduser()
    base_dir = base if base.is_dir() else base.parent
    for f in _source_files(root, set(_EXT_LANG)):
        lang = _EXT_LANG.get(Path(f).suffix)
        if lang is None:
            continue
        try:
            text = Path(f).read_text("utf-8", "replace")
        except OSError:
            continue
        rel = os.path.relpath(f, base_dir)
        for i, line in enumerate(text.splitlines(), 1):
            for rx, kind in _LANG_DEFS[lang]:
                m = rx.search(line)
                if m and (symbol is None or m.group(1) == symbol):
                    hits.append((rel, i, kind, m.group(1)))
                    break
    return hits


def find_definitions(symbol: str, root: str) -> list[tuple[str, int, str]]:
    """Locations ``(relpath, line, kind)`` where ``symbol`` is defined —
    function/method (``def``), class, or a module/class-level assignment."""
    hits: list[tuple[str, int, str]] = []
    base = Path(root).expanduser()
    base_dir = base if base.is_dir() else base.parent
    for f in _py_files(root):
        try:
            tree = ast.parse(Path(f).read_text("utf-8", "replace"))
        except (SyntaxError, OSError, ValueError):
            continue
        rel = os.path.relpath(f, base_dir)
        for node in ast.walk(tree):
            kind = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
                kind = "def"
            elif isinstance(node, ast.ClassDef) and node.name == symbol:
                kind = "class"
            elif isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == symbol for t in node.targets):
                    kind = "assign"
            if kind:
                hits.append((rel, node.lineno, kind))
    # Non-Python files (JS/TS/Go/Rust/Java/Ruby/C) via regex.
    hits.extend((rel, ln, kind) for rel, ln, kind, _name in _regex_defs(root, symbol))
    hits.sort()
    return hits


def find_references(symbol: str, root: str, *, limit: int = 100) -> list[tuple[str, int, str]]:
    """Locations ``(relpath, line, source_line)`` where ``symbol`` is used —
    a bare name or an attribute access (``x.symbol``). Deduped per line."""
    hits: list[tuple[str, int, str]] = []
    base = Path(root).expanduser()
    base_dir = base if base.is_dir() else base.parent
    for f in _py_files(root):
        try:
            src = Path(f).read_text("utf-8", "replace")
            tree = ast.parse(src)
        except (SyntaxError, OSError, ValueError):
            continue
        rel = os.path.relpath(f, base_dir)
        lines = src.splitlines()
        seen_lines: set[int] = set()
        for node in ast.walk(tree):
            hit = (
                (isinstance(node, ast.Name) and node.id == symbol)
                or (isinstance(node, ast.Attribute) and node.attr == symbol)
            )
            ln = getattr(node, "lineno", None)
            if hit and ln is not None and ln not in seen_lines:
                seen_lines.add(ln)
                text = lines[ln - 1].strip() if ln - 1 < len(lines) else ""
                hits.append((rel, ln, text[:200]))
                if len(hits) >= limit:
                    return sorted(hits)
    return sorted(hits)


def find_symbols(root: str, *, name_filter: str = "", limit: int = 500) -> list[tuple[str, int, str, str, str | None]]:
    """Outline of a file / project: ``(relpath, line, kind, name, enclosing_class)``
    for every class, method, and top-level function. ``name_filter`` (substring,
    case-insensitive) narrows a big project to the symbols you care about."""
    out: list[tuple[str, int, str, str, str | None]] = []
    base = Path(root).expanduser()
    base_dir = base if base.is_dir() else base.parent
    nf = name_filter.lower()
    for f in _py_files(root):
        try:
            tree = ast.parse(Path(f).read_text("utf-8", "replace"))
        except (SyntaxError, OSError, ValueError):
            continue
        rel = os.path.relpath(f, base_dir)

        def _visit(node: ast.AST, cls: str | None, rel: str = rel) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    if not nf or nf in child.name.lower():
                        out.append((rel, child.lineno, "class", child.name, cls))
                    _visit(child, child.name)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = "method" if cls else "def"
                    if not nf or nf in child.name.lower():
                        out.append((rel, child.lineno, kind, child.name, cls))

        _visit(tree, None)
        if len(out) >= limit:
            break
    # Non-Python files (JS/TS/Go/Rust/Java/Ruby/C) via regex — no class nesting.
    for rel, ln, kind, name in _regex_defs(root, None):
        if not nf or nf in name.lower():
            out.append((rel, ln, kind, name, None))
    out.sort(key=lambda s: (s[0], s[1]))
    return out[:limit]


def _render_symbols(syms: list[tuple[str, int, str, str, str | None]]) -> str:
    lines: list[str] = []
    cur_file = None
    for rel, ln, kind, name, cls in syms:
        if rel != cur_file:
            lines.append(f"\n{rel}:")
            cur_file = rel
        indent = "    " if kind == "method" else "  "
        if kind == "class":
            label = f"class {name}"
        elif kind in ("def", "method", "function", "func", "fn"):
            label = f"{name}()"
        else:  # interface, type, enum, const, module, struct — not callable
            label = f"{name} [{kind}]"
        lines.append(f"{indent}{label}  (line {ln})")
    return "\n".join(lines).strip()


@tool(name="lsp", is_read_only=True)
async def lsp(operation: str, symbol: str = "", path: str = ".") -> str:
    """Semantic code navigation — Python via the stdlib ``ast`` module (precise),
    plus JS/TS/Go/Rust/Java/Ruby/C via targeted regex. More precise than grep:
    it matches declaration syntax, not every mention.

    Args:
        operation: ``definition`` (where a symbol is defined), ``references``
            (everywhere it's used), or ``symbols`` (an outline of the file/dir:
            classes with their methods + top-level functions).
        symbol: The name to look up (required for definition/references; an
            optional substring filter for symbols).
        path: File or directory to search (default: the working directory).
    """
    op = (operation or "").lower()
    if op in ("definition", "definitions", "def", "goto", "goto_definition"):
        defs = find_definitions(symbol, path)
        if not defs:
            return f"no definition of {symbol!r} found under {path}"
        return "\n".join(f"{r}:{ln}: [{k}] {symbol}" for r, ln, k in defs)
    if op in ("references", "reference", "refs", "usages", "uses"):
        refs = find_references(symbol, path)
        if not refs:
            return f"no references to {symbol!r} found under {path}"
        head = f"{len(refs)} reference(s) to {symbol!r}:"
        return head + "\n" + "\n".join(f"{r}:{ln}: {t}" for r, ln, t in refs)
    if op in ("symbols", "symbol", "outline", "document_symbol", "workspace_symbol"):
        syms = find_symbols(path, name_filter=symbol or "")
        if not syms:
            return f"no symbols found under {path}"
        return _render_symbols(syms)
    return "operation must be 'definition', 'references', or 'symbols'"
