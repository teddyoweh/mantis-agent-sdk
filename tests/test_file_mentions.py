"""@-file-mention completer — find_file_mentions(partial, root)."""

from __future__ import annotations

from pathlib import Path

from mantis_agent.tui_fullscreen import find_file_mentions


def _make_tree(root: Path) -> None:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / ".git").mkdir()
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "README.md").write_text("x")
    (root / "src" / "main.py").write_text("x")
    (root / "src" / "util.py").write_text("x")
    (root / "tests" / "test_main.py").write_text("x")
    (root / ".git" / "config").write_text("x")           # ignored dir
    (root / "node_modules" / "pkg" / "index.js").write_text("x")  # ignored dir
    (root / ".hidden").write_text("x")                   # dotfile skipped


def test_matches_by_substring(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    hits = find_file_mentions("main", str(tmp_path))
    assert "src/main.py" in hits
    assert "tests/test_main.py" in hits


def test_basename_prefix_ranked_first(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    hits = find_file_mentions("main", str(tmp_path))
    # main.py (basename starts with 'main') outranks test_main.py (contains).
    assert hits.index("src/main.py") < hits.index("tests/test_main.py")


def test_skips_vcs_and_build_dirs_and_dotfiles(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    allhits = find_file_mentions("", str(tmp_path))
    assert not any(".git" in h for h in allhits)
    assert not any("node_modules" in h for h in allhits)
    assert ".hidden" not in allhits
    assert "README.md" in allhits


def test_empty_partial_lists_files(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    hits = find_file_mentions("", str(tmp_path))
    assert "src/main.py" in hits and "README.md" in hits


def test_limit_respected(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    for i in range(20):
        (tmp_path / "d" / f"f{i}.txt").write_text("x")
    hits = find_file_mentions("f", str(tmp_path), limit=5)
    assert len(hits) == 5
