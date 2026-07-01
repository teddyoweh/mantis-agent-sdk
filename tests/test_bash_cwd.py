"""Persistent working directory across bash calls (shared-shell cwd)."""

from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest

import mantis_agent.builtin_tools.fs as fs
from mantis_agent.builtin_tools.fs import _extract_cwd_marker, bash


@pytest.fixture(autouse=True)
def _reset_cwd():
    fs._BASH_CWD["cwd"] = None
    yield
    fs._BASH_CWD["cwd"] = None


def _run(cmd: str) -> str:
    return anyio.run(lambda: bash.fn(command=cmd))


def test_cd_persists_across_calls(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    _run(f"cd {sub}")
    out = _run("pwd")
    assert out.strip().endswith("/sub")
    assert os.path.realpath(out.strip()) == os.path.realpath(str(sub))


def test_relative_cd_resolves_against_tracked(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    _run(f"cd {tmp_path}")
    _run("cd a")
    _run("cd b")
    assert _run("pwd").strip().endswith("/a/b")


def test_marker_not_leaked() -> None:
    out = _run("echo hello")
    assert "hello" in out
    assert "__MANTIS_CWD" not in out


def test_exit_code_preserved() -> None:
    assert "[exit code: 1]" in _run("false")


def test_vanished_dir_falls_back(tmp_path: Path) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    _run(f"cd {gone}")
    gone.rmdir()                       # the tracked dir disappears
    out = _run("pwd")                  # must not crash; falls back to a real cwd
    assert "no such" not in out.lower()
    assert fs._BASH_CWD["cwd"] is None or os.path.isdir(fs._BASH_CWD["cwd"])


def test_extract_marker_helper() -> None:
    clean, cwd = _extract_cwd_marker("line one\nline two\n__MANTIS_CWD_9f3a__:/tmp/x\n")
    assert clean.strip() == "line one\nline two"
    assert cwd == "/tmp/x"
    clean2, cwd2 = _extract_cwd_marker("just output\n")
    assert cwd2 is None and clean2 == "just output\n"
