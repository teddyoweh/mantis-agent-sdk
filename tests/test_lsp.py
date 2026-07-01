"""lsp — ast-based Python code navigation (T1.8)."""

from __future__ import annotations

from pathlib import Path

import anyio

from mantis_agent.builtin_tools.codenav import (
    find_definitions,
    find_references,
    lsp,
)

_SRC = '''\
import os

TIMEOUT = 30


class Widget:
    def render(self):
        return TIMEOUT


def render(x):
    """Not a comment mention: render used below."""
    w = Widget()
    return w.render() + x


# render appears in this comment but must NOT be a reference
'''


def _proj(tmp_path: Path) -> Path:
    (tmp_path / "mod.py").write_text(_SRC)
    return tmp_path


def test_find_definition_function_class_assign(tmp_path: Path) -> None:
    _proj(tmp_path)
    defs = {(k, ln) for _r, ln, k in find_definitions("render", str(tmp_path))}
    # both the method and the module-level function are defs
    assert ("def", 7) in defs      # Widget.render
    assert ("def", 11) in defs     # module render()

    assign = find_definitions("TIMEOUT", str(tmp_path))
    assert assign and assign[0][2] == "assign"

    cls = find_definitions("Widget", str(tmp_path))
    assert cls and cls[0][2] == "class"


def test_find_references_skips_comments(tmp_path: Path) -> None:
    _proj(tmp_path)
    refs = find_references("render", str(tmp_path))
    ref_lines = {ln for _r, ln, _t in refs}
    # the attribute call w.render() is a reference...
    assert any("w.render()" in t for _r, _ln, t in refs)
    # ...but the trailing comment mention (last line) is NOT
    assert max(ref_lines) < _SRC.count("\n")   # no ref on the comment line


def test_lsp_tool_definition(tmp_path: Path) -> None:
    _proj(tmp_path)
    out = anyio.run(lambda: lsp.fn(operation="definition", symbol="Widget", path=str(tmp_path)))
    assert "mod.py:6: [class] Widget" in out


def test_lsp_tool_references(tmp_path: Path) -> None:
    _proj(tmp_path)
    out = anyio.run(lambda: lsp.fn(operation="references", symbol="Widget", path=str(tmp_path)))
    assert "reference(s)" in out
    assert "w = Widget()" in out


def test_lsp_not_found(tmp_path: Path) -> None:
    _proj(tmp_path)
    out = anyio.run(lambda: lsp.fn(operation="definition", symbol="nope", path=str(tmp_path)))
    assert "no definition" in out


def test_lsp_invalid_operation(tmp_path: Path) -> None:
    out = anyio.run(lambda: lsp.fn(operation="hover", symbol="x", path=str(tmp_path)))
    assert "must be" in out


def test_syntax_error_file_skipped(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def (((")
    (tmp_path / "ok.py").write_text("def foo():\n    pass\n")
    out = anyio.run(lambda: lsp.fn(operation="definition", symbol="foo", path=str(tmp_path)))
    assert "ok.py" in out            # bad file skipped, good one found
