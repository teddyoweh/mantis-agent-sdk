"""Notebook (.ipynb) read (T2): read_file renders cells instead of raw JSON."""

from __future__ import annotations

import json
from pathlib import Path

import anyio

from mantis_agent.builtin_tools.fs import _render_notebook, read_file

_NB = {
    "cells": [
        {"cell_type": "markdown", "source": ["# Title\n", "prose"]},
        {"cell_type": "code", "source": ["import os\n", "os.getcwd()"], "outputs": [
            {"output_type": "stream", "text": ["hello\n"]},
            {"output_type": "execute_result", "data": {"text/plain": ["'/tmp'"]}},
        ]},
        {"cell_type": "code", "source": ["1/0"], "outputs": [
            {"output_type": "error", "ename": "ZeroDivisionError", "evalue": "division by zero"},
        ]},
    ]
}


def test_read_file_renders_notebook(tmp_path: Path) -> None:
    p = tmp_path / "nb.ipynb"
    p.write_text(json.dumps(_NB))
    out = anyio.run(lambda: read_file.fn(path=str(p)))
    assert "Cell 1 · markdown" in out and "# Title" in out
    assert "Cell 2 · code" in out and "import os" in out
    assert "hello" in out and "'/tmp'" in out          # stream + result outputs
    assert "ZeroDivisionError: division by zero" in out  # error output
    assert '"cell_type"' not in out                     # NOT raw JSON


def test_source_as_string() -> None:
    nb = {"cells": [{"cell_type": "code", "source": "x = 1"}]}
    out = _render_notebook(json.dumps(nb))
    assert "x = 1" in out


def test_non_json_falls_back_to_none() -> None:
    assert _render_notebook("not json {") is None


def test_non_notebook_json_falls_back() -> None:
    assert _render_notebook(json.dumps({"foo": "bar"})) is None   # no 'cells'


def test_empty_notebook() -> None:
    assert _render_notebook(json.dumps({"cells": []})) == "(empty notebook)"


def test_image_output_noted() -> None:
    nb = {"cells": [{"cell_type": "code", "source": ["plot()"],
                     "outputs": [{"output_type": "display_data",
                                  "data": {"image/png": "base64..."}}]}]}
    out = _render_notebook(json.dumps(nb))
    assert "[image output]" in out
