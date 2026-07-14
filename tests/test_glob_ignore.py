"""glob skips dependency/VCS/build junk by default, but honors explicit targeting."""

from __future__ import annotations

from pathlib import Path

import anyio

from mantis_agent.builtin_tools.fs import glob


def _tree(tmp: Path) -> None:
    (tmp / "app.py").write_text("x")
    (tmp / "lib.py").write_text("y")
    (tmp / ".venv" / "site-packages").mkdir(parents=True)
    (tmp / ".venv" / "site-packages" / "junk.py").write_text("z")
    (tmp / "node_modules" / "pkg").mkdir(parents=True)
    (tmp / "node_modules" / "pkg" / "dep.py").write_text("w")
    (tmp / "__pycache__").mkdir()
    (tmp / "__pycache__" / "cached.py").write_text("c")


def _glob(pattern: str, path) -> str:
    return anyio.run(lambda: glob.fn(pattern=pattern, path=str(path)))


def test_junk_excluded_by_default(tmp_path: Path) -> None:
    _tree(tmp_path)
    out = _glob("**/*.py", tmp_path)
    assert "app.py" in out and "lib.py" in out
    assert "junk.py" not in out          # .venv
    assert "dep.py" not in out           # node_modules
    assert "cached.py" not in out        # __pycache__


def test_explicit_pattern_into_junk_is_honored(tmp_path: Path) -> None:
    _tree(tmp_path)
    out = _glob("node_modules/**/*.py", tmp_path)
    assert "dep.py" in out               # user asked for it


def test_path_inside_junk_is_honored(tmp_path: Path) -> None:
    _tree(tmp_path)
    out = _glob("*.py", tmp_path / ".venv" / "site-packages")
    assert "junk.py" in out              # base is inside .venv → don't filter


def test_no_junk_dirs_unaffected(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("2")
    out = _glob("**/*.py", tmp_path)
    assert "a.py" in out and "b.py" in out
