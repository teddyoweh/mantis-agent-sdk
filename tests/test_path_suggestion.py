"""Missing-file errors suggest a close-name file so a path typo self-corrects."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from mantis_agent.builtin_tools.fs import _path_suggestion, edit_file, read_file


def test_read_typo_suggests(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{}")
    with pytest.raises(FileNotFoundError) as ei:
        anyio.run(lambda: read_file.fn(path=str(tmp_path / "config.jsonn")))
    assert "Did you mean" in str(ei.value) and "config.json" in str(ei.value)


def test_edit_typo_suggests(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1")
    with pytest.raises(FileNotFoundError) as ei:
        anyio.run(lambda: edit_file.fn(path=str(tmp_path / "mian.py"), old_string="x", new_string="y"))
    assert "main.py" in str(ei.value)


def test_no_close_match_plain_error(tmp_path: Path) -> None:
    (tmp_path / "alpha.py").write_text("x")
    with pytest.raises(FileNotFoundError) as ei:
        anyio.run(lambda: read_file.fn(path=str(tmp_path / "zzzzzzz.txt")))
    assert "Did you mean" not in str(ei.value)


def test_bad_directory_no_crash() -> None:
    with pytest.raises(FileNotFoundError) as ei:
        anyio.run(lambda: read_file.fn(path="/no_such_dir_xyz_123/file.txt"))
    assert "no such file" in str(ei.value)


def test_suggestion_helper(tmp_path: Path) -> None:
    (tmp_path / "server.py").write_text("x")
    assert "server.py" in _path_suggestion(tmp_path / "servr.py")
    assert _path_suggestion(tmp_path / "wildlydifferent.md") == ""
    # exact existing name yields nothing (not a miss)
    assert _path_suggestion(tmp_path / "server.py") == ""
