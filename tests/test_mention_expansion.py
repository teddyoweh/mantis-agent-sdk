"""@-file-mention expansion — referenced files' contents injected inline."""

from __future__ import annotations

from pathlib import Path

from mantis_agent.tui import render_mention_block, resolve_file_mentions


def test_resolves_existing_file(tmp_path: Path) -> None:
    (tmp_path / "foo.py").write_text("def hi():\n    return 1\n")
    res = resolve_file_mentions("explain @foo.py please", tmp_path)
    assert len(res) == 1
    assert res[0][0] == "foo.py"
    assert "def hi()" in res[0][1]


def test_ignores_nonexistent_and_nonfile(tmp_path: Path) -> None:
    assert resolve_file_mentions("see @nope.py", tmp_path) == []
    assert resolve_file_mentions("cc @teammate about it", tmp_path) == []   # no extension → not matched
    (tmp_path / "d.py").write_text("x")
    # a bare @word that isn't a real file is skipped even if extension-like
    assert resolve_file_mentions("email a@b.com", tmp_path) == []


def test_dedupes_repeat_mentions(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("A")
    res = resolve_file_mentions("compare @a.py with @a.py", tmp_path)
    assert len(res) == 1


def test_large_file_gets_note(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("x" * 60_000)
    res = resolve_file_mentions("see @big.txt", tmp_path, max_bytes=50_000)
    assert "too large" in res[0][1] and "read_file" in res[0][1]


def test_subdir_mention(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')")
    res = resolve_file_mentions("look at @src/app.py", tmp_path)
    assert res and "print('hi')" in res[0][1]


def test_render_block(tmp_path: Path) -> None:
    (tmp_path / "foo.py").write_text("CODE")
    block = render_mention_block(resolve_file_mentions("@foo.py", tmp_path))
    assert "Contents of @foo.py" in block and "CODE" in block
    assert "system-reminder" in block.lower()


def test_directory_mention_lists_contents(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x")
    (tmp_path / "src" / "sub").mkdir()
    r = resolve_file_mentions("look at @src/ please", tmp_path)
    assert len(r) == 1
    body = r[0][1]
    assert "directory listing" in body
    assert "app.py" in body and "sub/" in body      # dirs marked with /


def test_directory_mention_without_slash(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("hi")
    r = resolve_file_mentions("see @docs", tmp_path)
    assert r and "readme.md" in r[0][1]


def test_empty_directory_mention(tmp_path):
    (tmp_path / "empty").mkdir()
    r = resolve_file_mentions("@empty", tmp_path)
    assert r and "(empty directory)" in r[0][1]
