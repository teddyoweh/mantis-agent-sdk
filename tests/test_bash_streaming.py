"""Foreground ``bash`` live output — opt-in via ``set_bash_output_sink``.

Contract:
* No sink installed → the buffered ``communicate()`` path, unchanged.
* Sink installed → chunks arrive WHILE the command runs; the returned result is
  the same text the buffered path produces; the internal ``$PWD`` marker never
  reaches the sink; cwd persistence, stdin, stderr, timeout/kill and truncation
  all behave as before; a raising sink is ignored.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

import anyio
import pytest

import mantis_agent.builtin_tools.fs as fs
from mantis_agent.builtin_tools import (
    bash,
    bash_output_sink,
    reset_bash_output_sink,
    set_bash_output_sink,
)


@pytest.fixture(autouse=True)
def _fresh_cwd():
    fs._BASH_CWD_BY_SCOPE["__global__"] = {"cwd": None}
    yield
    fs._BASH_CWD_BY_SCOPE["__global__"] = {"cwd": None}


def _run(command: str, timeout: int = 10, stdin: str = "", sink=None) -> str:
    async def go() -> str:
        token = set_bash_output_sink(sink)
        try:
            return await bash.fn(command, timeout, stdin)
        finally:
            reset_bash_output_sink(token)

    return anyio.run(go)


def test_sink_api_set_and_reset() -> None:
    assert bash_output_sink() is None
    cb = lambda s: None  # noqa: E731
    token = set_bash_output_sink(cb)
    assert bash_output_sink() is cb
    reset_bash_output_sink(token)
    assert bash_output_sink() is None


def test_default_path_is_unchanged_without_a_sink(monkeypatch) -> None:
    called = {"pump": 0}
    real = fs._pump_streams

    async def spy(*a, **k):
        called["pump"] += 1
        return await real(*a, **k)

    monkeypatch.setattr(fs, "_pump_streams", spy)
    out = anyio.run(bash.fn, "echo hi; echo err >&2; exit 3", 10)
    assert called["pump"] == 0
    assert out == "hi\n\n[stderr]\nerr\n[exit code: 3]"


def test_sink_receives_output_while_the_command_runs() -> None:
    seen: list[tuple[float, str]] = []
    start = time.monotonic()
    out = _run("echo first; sleep 0.6; echo second", sink=lambda s: seen.append((time.monotonic() - start, s)))
    end = time.monotonic() - start
    text = "".join(s for _, s in seen)
    assert "first" in text and "second" in text
    first_at = next(t for t, s in seen if "first" in s)
    assert first_at < end - 0.4, "first line only arrived after the command finished"
    assert out == "first\nsecond"


def test_streamed_result_matches_buffered_result() -> None:
    cmd = "printf 'a\\nb\\n'; echo warn >&2; exit 2"
    buffered = _run(cmd)
    streamed = _run(cmd, sink=lambda s: None)
    assert streamed == buffered == "a\nb\n\n[stderr]\nwarn\n[exit code: 2]"


def test_cwd_marker_is_withheld_from_sink_and_cwd_still_persists(tmp_path) -> None:
    chunks: list[str] = []
    _run(f"cd {tmp_path} && echo moved", sink=chunks.append)
    assert not any(fs._CWD_MARKER in c for c in chunks)
    assert "".join(chunks).strip() == "moved"
    assert _run("pwd", sink=chunks.append).strip() == str(tmp_path.resolve()) or _run("pwd").strip().endswith(tmp_path.name)


def test_stderr_and_stdin_flow_in_streaming_mode() -> None:
    chunks: list[str] = []
    out = _run('read -r n; echo "hi $n"; echo oops >&2', stdin="Teddy\n", sink=chunks.append)
    assert "hi Teddy" in out and "[stderr]\noops" in out
    joined = "".join(chunks)
    assert "hi Teddy" in joined and "oops" in joined


def test_raising_sink_does_not_break_the_command() -> None:
    def boom(_s: str) -> None:
        raise RuntimeError("ui exploded")

    assert _run("echo ok", sink=boom) == "ok"


def test_async_sink_is_awaited() -> None:
    got: list[str] = []

    async def sink(s: str) -> None:
        await anyio.sleep(0)
        got.append(s)

    assert _run("echo async", sink=sink) == "async"
    assert "async" in "".join(got)


def test_streaming_timeout_kills_child_process_group() -> None:
    with pytest.raises(TimeoutError, match="timed out"):
        _run("bash -c 'exec -a mantis-stream-timeout-child sleep 30'", timeout=1, sink=lambda s: None)
    time.sleep(0.3)
    ps = subprocess.run(["ps", "-axo", "pid,command"], capture_output=True, text=True).stdout
    strays = [ln for ln in ps.splitlines() if "mantis-stream-timeout-child" in ln and "ps -axo" not in ln]
    for ln in strays:  # never leave a stray behind even if the assertion fails
        try:
            os.kill(int(ln.split()[0]), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass
    assert not strays, f"child survived the timeout: {strays}"


def test_huge_output_is_bounded_and_truncated_in_streaming_mode() -> None:
    n_chunks = 0

    def sink(_s: str) -> None:
        nonlocal n_chunks
        n_chunks += 1

    out = _run("yes abcdefghij | head -c 600000", sink=sink)
    assert n_chunks > 1
    assert "truncated" in out
    assert len(out) <= fs._MAX_OUTPUT + 200
