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
from pathlib import Path

from ..tools import tool

_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", ".tox",
    ".mypy_cache", ".pytest_cache", "build", ".ruff_cache",
}


def _py_files(root: str, cap: int = 3000) -> list[str]:
    p = Path(root).expanduser()
    if p.is_file():
        return [str(p)] if p.suffix == ".py" else []
    out: list[str] = []
    for dp, dns, fns in os.walk(p):
        dns[:] = [d for d in dns if d not in _IGNORE_DIRS and not d.startswith(".")]
        for f in fns:
            if f.endswith(".py"):
                out.append(os.path.join(dp, f))
                if len(out) >= cap:
                    return out
    return out


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


@tool(name="lsp", is_read_only=True)
async def lsp(operation: str, symbol: str, path: str = ".") -> str:
    """Semantic code navigation for Python (via the stdlib ``ast`` module) —
    more precise than grep because it distinguishes definitions from mentions.

    Args:
        operation: ``definition`` (where the symbol is defined) or
            ``references`` (everywhere it's used).
        symbol: The exact name to look up (function, class, method, variable).
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
    return "operation must be 'definition' or 'references'"
