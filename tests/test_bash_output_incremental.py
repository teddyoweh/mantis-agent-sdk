"""bash_output returns only NEW output since the last read (not the whole log)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import anyio
import pytest

import mantis_agent.builtin_tools.fs as fs
from mantis_agent.builtin_tools.fs import bash_output


@pytest.fixture()
def _bg(tmp_path):
    """A fake background entry with a controllable log file + a real process."""
    log = tmp_path / "bg.log"
    log.write_text("")
    proc = subprocess.Popen(["sleep", "30"])
    fs._BG_SHELLS["bg_t"] = {"proc": proc, "log": str(log), "cmd": "demo"}
    try:
        yield log
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        fs._BG_SHELLS.pop("bg_t", None)


def _read() -> str:
    return anyio.run(lambda: bash_output.fn(bash_id="bg_t"))


def test_first_read_gets_all(_bg: Path) -> None:
    _bg.write_text("line one\nline two\n")
    out = _read()
    assert "line one" in out and "line two" in out
    assert "running" in out


def test_second_read_only_new(_bg: Path) -> None:
    _bg.write_text("first\n")
    assert "first" in _read()
    with _bg.open("a") as fh:
        fh.write("second\n")
    out = _read()
    assert "second" in out
    assert "first" not in out                 # not re-dumped


def test_no_new_output_note(_bg: Path) -> None:
    _bg.write_text("only\n")
    _read()                                    # consumes it
    assert "no new output" in _read()          # nothing since


def test_empty_before_any_output(_bg: Path) -> None:
    assert "no output yet" in _read()


def test_unknown_id() -> None:
    out = anyio.run(lambda: bash_output.fn(bash_id="does_not_exist"))
    assert "no background shell" in out
