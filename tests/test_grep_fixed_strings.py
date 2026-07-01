"""grep fixed_strings — literal search for code with regex metacharacters."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

import mantis_agent.builtin_tools.fs as fs
from mantis_agent.builtin_tools.fs import grep


@pytest.fixture()
def _py_only(monkeypatch):
    # exercise the Python fallback deterministically (no dependence on rg presence)
    async def _no_rg():
        return False
    monkeypatch.setattr(fs, "_have_rg", _no_rg)


def _write(tmp: Path) -> None:
    (tmp / "code.py").write_text(
        'a = config.get("key")\n'
        'b = configXget_notit\n'
        'c = arr[0]\n'
    )


def test_regex_dot_is_wildcard(tmp_path: Path, _py_only) -> None:
    _write(tmp_path)
    out = anyio.run(lambda: grep.fn(pattern="config.get", path=str(tmp_path), fixed_strings=False))
    assert "configXget" in out                     # '.' matched the 'X'


def test_fixed_matches_literally(tmp_path: Path, _py_only) -> None:
    _write(tmp_path)
    out = anyio.run(lambda: grep.fn(pattern="config.get", path=str(tmp_path), fixed_strings=True))
    assert 'config.get("key")' in out
    assert "configXget" not in out                 # literal '.' didn't match 'X'


def test_fixed_handles_regex_special_chars(tmp_path: Path, _py_only) -> None:
    _write(tmp_path)
    # 'arr[0]' as regex is a char class; as fixed it's literal
    out = anyio.run(lambda: grep.fn(pattern="arr[0]", path=str(tmp_path), fixed_strings=True))
    assert "arr[0]" in out


def test_fixed_survives_unbalanced_parens(tmp_path: Path, _py_only) -> None:
    _write(tmp_path)
    # 'get("key")' — balanced here, but a lone 'get(' as regex would error; fixed is safe
    out = anyio.run(lambda: grep.fn(pattern='get("key")', path=str(tmp_path), fixed_strings=True))
    assert 'get("key")' in out


def test_default_is_regex(tmp_path: Path, _py_only) -> None:
    _write(tmp_path)
    out = anyio.run(lambda: grep.fn(pattern=r"config\.get", path=str(tmp_path)))
    assert 'config.get("key")' in out and "configXget" not in out   # escaped dot
