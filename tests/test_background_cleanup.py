"""Background shells are terminated on session end (no orphaned dev servers)."""

from __future__ import annotations

import time

import anyio
import pytest

import mantis_agent.builtin_tools.fs as fs
from mantis_agent.builtin_tools.fs import bash, terminate_background_shells
from mantis_agent.builtin_tools.web import aclose_builtin_clients


@pytest.fixture(autouse=True)
def _clean_bg():
    terminate_background_shells()
    fs._BG_SHELLS.clear()
    yield
    terminate_background_shells()
    fs._BG_SHELLS.clear()


def _start(cmd: str):
    anyio.run(lambda: bash.fn(command=cmd, run_in_background=True))
    bid = list(fs._BG_SHELLS)[-1]
    return fs._BG_SHELLS[bid]["proc"]


def test_terminates_running_process() -> None:
    proc = _start("sleep 300")
    assert proc.poll() is None                     # running
    assert terminate_background_shells() == 1
    time.sleep(0.3)
    assert proc.poll() is not None                 # killed
    assert len(fs._BG_SHELLS) == 0                  # registry cleared


def test_idempotent_when_empty() -> None:
    assert terminate_background_shells() == 0


def test_already_exited_not_counted() -> None:
    proc = _start("true")                           # exits immediately
    time.sleep(0.2)
    assert proc.poll() is not None
    assert terminate_background_shells() == 0        # nothing to kill
    assert len(fs._BG_SHELLS) == 0                   # still cleared


def test_aclose_terminates_background() -> None:
    proc = _start("sleep 300")
    anyio.run(aclose_builtin_clients)               # the agent.aclose() path
    time.sleep(0.3)
    assert proc.poll() is not None                  # killed via aclose
    assert len(fs._BG_SHELLS) == 0
