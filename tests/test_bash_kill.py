"""bash_kill terminates a background shell the agent started."""

from __future__ import annotations

import time

import anyio
import pytest

import mantis_agent.builtin_tools.fs as fs
from mantis_agent.builtin_tools.fs import CODING_TOOLS, bash, bash_kill


@pytest.fixture(autouse=True)
def _clean():
    fs._BG_SHELLS.clear()
    yield
    from mantis_agent.builtin_tools.fs import terminate_background_shells
    terminate_background_shells()
    fs._BG_SHELLS.clear()


def _start(cmd: str) -> str:
    anyio.run(lambda: bash.fn(command=cmd, run_in_background=True))
    return list(fs._BG_SHELLS)[-1]


def test_registered() -> None:
    assert any(t.name == "bash_kill" for t in CODING_TOOLS)


def test_kills_running_process() -> None:
    bid = _start("sleep 60")
    proc = fs._BG_SHELLS[bid]["proc"]
    assert proc.poll() is None
    out = anyio.run(lambda: bash_kill.fn(bash_id=bid))
    assert "terminated" in out
    time.sleep(0.3)
    assert proc.poll() is not None                # dead
    assert bid not in fs._BG_SHELLS               # deregistered


def test_kill_unknown_id() -> None:
    out = anyio.run(lambda: bash_kill.fn(bash_id="bg_nope"))
    assert "no background shell" in out


def test_kill_already_exited() -> None:
    bid = _start("true")                          # exits immediately
    time.sleep(0.2)
    out = anyio.run(lambda: bash_kill.fn(bash_id=bid))
    assert "already exited" in out
    assert bid not in fs._BG_SHELLS


def test_kills_process_group() -> None:
    # a shell that forks a child; killpg should reap the child too
    bid = _start("sleep 60 & sleep 60")
    proc = fs._BG_SHELLS[bid]["proc"]
    anyio.run(lambda: bash_kill.fn(bash_id=bid))
    time.sleep(0.3)
    assert proc.poll() is not None
