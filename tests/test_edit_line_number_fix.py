"""edit_file/multi_edit auto-strip read_file's line-number prefixes from old_string
(the common 'copied the numbered output' mistake)."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from mantis_agent.builtin_tools.fs import (
    _reconcile_old_string,
    _strip_line_numbers,
    edit_file,
    multi_edit,
)

_SRC = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"


def test_strip_line_numbers() -> None:
    assert _strip_line_numbers("  1\tdef x():\n 12\t    pass") == "def x():\n    pass"
    assert _strip_line_numbers("no numbers here") == "no numbers here"


def test_reconcile() -> None:
    text = "def foo():\n    return 1"
    assert _reconcile_old_string("  5\tdef foo():", text) == "def foo():"   # stripped matches
    assert _reconcile_old_string("def foo():", text) == "def foo():"        # already matches
    assert _reconcile_old_string("nope", text) == "nope"                    # not found → unchanged


def test_edit_with_copied_line_numbers(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text(_SRC)
    old = "  1\tdef foo():\n  2\t    return 1"
    out = anyio.run(lambda: edit_file.fn(path=str(f), old_string=old, new_string="def foo():\n    return 42"))
    assert "Updated" in out
    assert "return 42" in f.read_text()


def test_multi_edit_with_copied_line_numbers(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text(_SRC)
    anyio.run(lambda: multi_edit.fn(path=str(f), edits=[
        {"old_string": "  2\t    return 1", "new_string": "    return 100"},
        {"old_string": "  5\t    return 2", "new_string": "    return 200"},
    ]))
    txt = f.read_text()
    assert "return 100" in txt and "return 200" in txt


def test_normal_edit_unaffected(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text(_SRC)
    anyio.run(lambda: edit_file.fn(path=str(f), old_string="return 1", new_string="return 9"))
    assert "return 9" in f.read_text()


def test_genuinely_missing_still_errors(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text(_SRC)
    with pytest.raises(ValueError, match="not found"):
        anyio.run(lambda: edit_file.fn(path=str(f), old_string="totally absent", new_string="x"))
