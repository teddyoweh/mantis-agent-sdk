"""lsp symbols operation — file/project outline."""

from __future__ import annotations

from pathlib import Path

import anyio

from mantis_agent.builtin_tools.codenav import find_symbols, lsp

_SRC = '''\
CONST = 1


def top_level():
    def nested():   # nested funcs are not surfaced as top-level symbols
        pass
    return 1


class Alpha:
    def method_a(self):
        pass

    async def method_b(self):
        pass


class Beta:
    pass
'''


def _proj(tmp_path: Path) -> Path:
    (tmp_path / "m.py").write_text(_SRC)
    return tmp_path


def test_find_symbols_classes_methods_functions(tmp_path: Path) -> None:
    _proj(tmp_path)
    syms = find_symbols(str(tmp_path))
    got = {(kind, name, cls) for _r, _ln, kind, name, cls in syms}
    assert ("def", "top_level", None) in got
    assert ("class", "Alpha", None) in got
    assert ("method", "method_a", "Alpha") in got     # method scoped to class
    assert ("method", "method_b", "Alpha") in got     # async method too
    assert ("class", "Beta", None) in got


def test_name_filter(tmp_path: Path) -> None:
    _proj(tmp_path)
    syms = find_symbols(str(tmp_path), name_filter="method")
    names = {name for _r, _ln, _k, name, _c in syms}
    assert names == {"method_a", "method_b"}


def test_lsp_symbols_outline(tmp_path: Path) -> None:
    _proj(tmp_path)
    out = anyio.run(lambda: lsp.fn(operation="symbols", path=str(tmp_path)))
    assert "class Alpha" in out
    assert "method_a()" in out and "top_level()" in out
    # methods are indented deeper than classes
    for line in out.splitlines():
        if "method_a()" in line:
            assert line.startswith("    ")   # method indent (4 spaces)


def test_lsp_symbols_empty(tmp_path: Path) -> None:
    out = anyio.run(lambda: lsp.fn(operation="symbols", path=str(tmp_path)))
    assert "no symbols" in out
