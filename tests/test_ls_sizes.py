"""ls shows human-readable file sizes, a dir/file count header, dirs first."""

from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest

from mantis_agent.builtin_tools.fs import _human_size, ls


@pytest.mark.parametrize("n,expected", [
    (0, "0 B"), (512, "512 B"), (1023, "1023 B"),
    (1024, "1.0 KB"), (1536, "1.5 KB"),
    (5 * 1024 * 1024, "5.0 MB"), (3 * 1024 ** 3, "3.0 GB"),
])
def test_human_size(n: int, expected: str) -> None:
    assert _human_size(n) == expected


def test_ls_shows_sizes_and_header(tmp_path: Path) -> None:
    (tmp_path / "small.txt").write_text("hi")
    (tmp_path / "big.csv").write_bytes(b"x" * 4200)
    os.mkdir(tmp_path / "src")
    out = anyio.run(lambda: ls.fn(path=str(tmp_path)))
    assert "(1 dir, 2 files)" in out
    assert "src/" in out                          # dir marked + first
    assert "small.txt (2 B)" in out
    assert "big.csv (4.1 KB)" in out
    # dir precedes files
    assert out.index("src/") < out.index("small.txt")


def test_ls_singular_plural(tmp_path: Path) -> None:
    (tmp_path / "only.txt").write_text("x")
    out = anyio.run(lambda: ls.fn(path=str(tmp_path)))
    assert "(0 dirs, 1 file)" in out              # singular 'file', plural 'dirs'


def test_ls_empty(tmp_path: Path) -> None:
    out = anyio.run(lambda: ls.fn(path=str(tmp_path)))
    assert "is empty" in out


def test_ls_on_file(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hello")
    out = anyio.run(lambda: ls.fn(path=str(f)))
    assert "file" in out and "bytes" in out
