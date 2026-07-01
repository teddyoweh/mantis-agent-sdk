"""Permission Ask prompt — file-editing tools show a readable change summary."""

from __future__ import annotations

from types import SimpleNamespace as T

from mantis_agent.permissions import _format_prompt, _snip


def test_edit_file_shows_change() -> None:
    p = _format_prompt(T(name="edit_file"),
                       {"path": "src/app.py", "old_string": "def old():", "new_string": "def new():"})
    assert p.startswith("edit src/app.py:")
    assert "def old():" in p and "def new():" in p and "→" in p


def test_edit_file_snips_long_strings() -> None:
    long = "x" * 200
    p = _format_prompt(T(name="edit_file"), {"path": "a.py", "old_string": long, "new_string": "y"})
    assert "…" in p and len(p) < 120                    # not a wall of text


def test_write_file_shows_line_count() -> None:
    p = _format_prompt(T(name="write_file"), {"path": "cfg.json", "content": "a\nb\nc"})
    assert p == "write cfg.json (3 lines)"


def test_multi_edit_shows_change_count() -> None:
    p = _format_prompt(T(name="multi_edit"), {"path": "m.py", "edits": [{}, {}]})
    assert p == "edit m.py (2 changes)"


def test_notebook_edit_shows_cell() -> None:
    p = _format_prompt(T(name="notebook_edit"), {"notebook_path": "n.ipynb", "cell_number": 4})
    assert p == "edit notebook n.ipynb (cell 4)"


def test_bash_unchanged() -> None:
    assert _format_prompt(T(name="bash"), {"command": "npm test"}) == "bash: npm test"
    danger = _format_prompt(T(name="bash"), {"command": "rm -rf /"})
    assert "bash: rm -rf /" in danger and "⚠" in danger


def test_generic_tool_fallback() -> None:
    p = _format_prompt(T(name="weird_tool"), {"a": 1, "b": 2})
    assert p.startswith("weird_tool(")


def test_snip_collapses_and_caps() -> None:
    assert _snip("  hello   world  ") == "hello world"
    assert _snip("x" * 100, 10).endswith("…") and len(_snip("x" * 100, 10)) == 10
