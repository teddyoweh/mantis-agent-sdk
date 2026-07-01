"""grep output-modes / context-lines / type-filter / head-limit (T1.4).

Tests the Python fallback directly (deterministic) and the grep tool end-to-end
(uses ripgrep when present, falls back otherwise)."""

from __future__ import annotations

from pathlib import Path

import anyio

import mantis_agent.builtin_tools.fs as fs
from mantis_agent.builtin_tools.fs import grep


def _tree(d: Path) -> None:
    (d / "a.py").write_text("import os\ndef foo():\n    return 1\n# foo again\n")
    (d / "b.txt").write_text("foo bar\nfoo baz\nqux\n")
    (d / "c.md").write_text("# foo heading\n")


def test_py_content_mode(tmp_path) -> None:
    _tree(tmp_path)
    out = fs._py_grep("foo", str(tmp_path), None, False, "content")
    assert "a.py:2:def foo():" in out
    assert "b.txt:1:foo bar" in out


def test_py_files_with_matches(tmp_path) -> None:
    _tree(tmp_path)
    out = fs._py_grep("foo", str(tmp_path), None, False, "files_with_matches")
    lines = out.splitlines()
    assert all(":" + "foo" not in ln for ln in lines)  # no line:text, just paths
    assert any(ln.endswith("a.py") for ln in lines)
    assert any(ln.endswith("b.txt") for ln in lines)


def test_py_count_mode(tmp_path) -> None:
    _tree(tmp_path)
    out = fs._py_grep("foo", str(tmp_path), None, False, "count")
    assert any(ln.endswith("b.txt:2") for ln in out.splitlines())  # 2 hits in b.txt


def test_py_type_filter(tmp_path) -> None:
    _tree(tmp_path)
    out = fs._py_grep("foo", str(tmp_path), None, False, "content", 0, "py")
    assert ".py:" in out
    assert ".txt:" not in out and ".md:" not in out


def test_py_context_lines(tmp_path) -> None:
    (tmp_path / "x.py").write_text("line1\nMATCH\nline3\n")
    out = fs._py_grep("MATCH", str(tmp_path), None, False, "content", 1)
    # context lines use '-' separator; match uses ':'
    assert "x.py:2:MATCH" in out
    assert "x.py:1-line1" in out
    assert "x.py:3-line3" in out


def test_py_head_limit(tmp_path) -> None:
    (tmp_path / "m.txt").write_text("\n".join(f"foo {i}" for i in range(50)))
    out = fs._py_grep("foo", str(tmp_path), None, False, "content", 0, None, 5)
    body = [ln for ln in out.splitlines() if "truncated" not in ln]
    assert len(body) <= 5
    assert "truncated" in out


def test_tool_end_to_end_output_modes(tmp_path) -> None:
    _tree(tmp_path)

    async def run(**kw):
        return await grep.fn(pattern="foo", path=str(tmp_path), **kw)

    content = anyio.run(lambda: run())
    assert "foo" in content and ":" in content
    files = anyio.run(lambda: run(output_mode="files_with_matches"))
    assert "a.py" in files
    count = anyio.run(lambda: run(output_mode="count"))
    assert any(c.endswith(("1", "2")) for c in count.splitlines())
