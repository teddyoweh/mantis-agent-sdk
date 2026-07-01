"""Read-before-write guard: write_file won't blind-clobber an unseen/stale file."""

from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest

import mantis_agent.builtin_tools.fs as fs
from mantis_agent.builtin_tools.fs import edit_file, read_file, write_file


@pytest.fixture(autouse=True)
def _clear_tracker():
    fs._FILE_READS.clear()
    yield
    fs._FILE_READS.clear()


def test_new_file_writes_freely(tmp_path: Path) -> None:
    p = tmp_path / "new.txt"
    out = anyio.run(lambda: write_file.fn(path=str(p), content="hello"))
    assert p.read_text() == "hello"
    assert "Wrote" in out


def test_existing_unread_file_is_guarded(tmp_path: Path) -> None:
    p = tmp_path / "exists.txt"
    p.write_text("precious unseen content")
    with pytest.raises(ValueError, match="hasn't been read"):
        anyio.run(lambda: write_file.fn(path=str(p), content="clobbered"))
    assert p.read_text() == "precious unseen content"   # untouched


def test_read_then_write_allowed(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    p.write_text("original")
    anyio.run(lambda: read_file.fn(path=str(p)))          # now seen
    anyio.run(lambda: write_file.fn(path=str(p), content="new"))
    assert p.read_text() == "new"


def test_write_then_overwrite_allowed(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    anyio.run(lambda: write_file.fn(path=str(p), content="v1"))   # creates + records
    anyio.run(lambda: write_file.fn(path=str(p), content="v2"))   # allowed
    assert p.read_text() == "v2"


def test_modified_since_read_is_guarded(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    p.write_text("v1")
    anyio.run(lambda: read_file.fn(path=str(p)))
    # simulate an external edit AFTER our read (bump mtime into the future)
    future = os.stat(p).st_mtime + 100
    os.utime(p, (future, future))
    with pytest.raises(ValueError, match="modified on disk"):
        anyio.run(lambda: write_file.fn(path=str(p), content="v2"))


def test_edit_records_so_write_is_allowed(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    p.write_text("alpha beta")
    anyio.run(lambda: read_file.fn(path=str(p)))
    anyio.run(lambda: edit_file.fn(path=str(p), old_string="alpha", new_string="ALPHA"))
    # edit updated the tracker's mtime → a following write_file doesn't false-trip
    anyio.run(lambda: write_file.fn(path=str(p), content="rewritten"))
    assert p.read_text() == "rewritten"
