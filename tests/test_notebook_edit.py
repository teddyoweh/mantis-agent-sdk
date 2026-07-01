"""notebook_edit (T2): replace / insert / delete cells in a .ipynb."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from mantis_agent.builtin_tools.fs import CODING_TOOLS, notebook_edit, read_file


def _nb(tmp_path: Path) -> Path:
    p = tmp_path / "n.ipynb"
    p.write_text(json.dumps({"cells": [
        {"cell_type": "code", "source": ["x = 1"], "outputs": [{"output_type": "stream", "text": ["1"]}],
         "execution_count": 3},
        {"cell_type": "markdown", "source": ["# Title"]},
    ], "nbformat": 4, "nbformat_minor": 5}))
    return p


def _load(p: Path) -> dict:
    return json.loads(p.read_text())


def test_replace_clears_outputs(tmp_path: Path) -> None:
    p = _nb(tmp_path)
    out = anyio.run(lambda: notebook_edit.fn(path=str(p), new_source="x = 2", cell_number=0))
    assert "replaced cell 0" in out
    nb = _load(p)
    assert "".join(nb["cells"][0]["source"]) == "x = 2"
    assert nb["cells"][0]["outputs"] == []          # stale outputs cleared
    assert nb["cells"][0]["execution_count"] is None


def test_insert(tmp_path: Path) -> None:
    p = _nb(tmp_path)
    anyio.run(lambda: notebook_edit.fn(path=str(p), new_source="import os",
                                       cell_number=0, edit_mode="insert", cell_type="code"))
    nb = _load(p)
    assert len(nb["cells"]) == 3
    assert "".join(nb["cells"][0]["source"]) == "import os"
    assert nb["cells"][0]["cell_type"] == "code"
    assert nb["cells"][1]["cell_type"] == "code"    # old first cell pushed down


def test_insert_markdown(tmp_path: Path) -> None:
    p = _nb(tmp_path)
    anyio.run(lambda: notebook_edit.fn(path=str(p), new_source="## Notes",
                                       cell_number=99, edit_mode="insert", cell_type="markdown"))
    nb = _load(p)
    assert nb["cells"][-1]["cell_type"] == "markdown"   # clamped to the end
    assert "outputs" not in nb["cells"][-1]


def test_delete(tmp_path: Path) -> None:
    p = _nb(tmp_path)
    out = anyio.run(lambda: notebook_edit.fn(path=str(p), cell_number=1, edit_mode="delete"))
    assert "deleted cell 1" in out
    assert len(_load(p)["cells"]) == 1


def test_out_of_range(tmp_path: Path) -> None:
    p = _nb(tmp_path)
    with pytest.raises(ValueError, match="out of range"):
        anyio.run(lambda: notebook_edit.fn(path=str(p), cell_number=9, edit_mode="delete"))


def test_roundtrip_read_after_edit(tmp_path: Path) -> None:
    p = _nb(tmp_path)
    anyio.run(lambda: notebook_edit.fn(path=str(p), new_source="y = 42", cell_number=0))
    rendered = anyio.run(lambda: read_file.fn(path=str(p)))
    assert "y = 42" in rendered


def test_registered() -> None:
    assert any(t.name == "notebook_edit" for t in CODING_TOOLS)
