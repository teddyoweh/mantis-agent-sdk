"""The `monitor` tool — wait for a background shell's output/exit, a file, or
a port in ONE tool call, instead of the model looping sleep + bash_output."""

from __future__ import annotations

import anyio

from mantis_agent.builtin_tools.fs import _BG_SHELLS, bash, bash_output, monitor


def _run(coro_fn, *a, **k):
    return anyio.run(lambda: coro_fn(*a, **k))


def test_monitor_requires_something_to_watch() -> None:
    out = _run(monitor.fn)
    assert "nothing to watch" in out


def test_monitor_pattern_needs_bash_id() -> None:
    out = _run(monitor.fn, until_pattern="ready")
    assert "needs a bash_id" in out


def test_monitor_unknown_shell() -> None:
    out = _run(monitor.fn, bash_id="bg_nope")
    assert "no background shell" in out


def test_monitor_matches_pattern_in_background_output() -> None:
    async def go():
        start = await bash.fn("echo booting; sleep 0.4; echo SERVER READY on 8080; sleep 5",
                              run_in_background=True)
        bid = start.split(" as ")[1].split(" ")[0]
        out = await monitor.fn(bash_id=bid, until_pattern=r"SERVER READY", timeout_s=10, poll_s=0.1)
        assert "matched" in out and "SERVER READY on 8080" in out
        # bash_output still sees the full output — monitor didn't consume it
        full = await bash_output.fn(bid)
        assert "booting" in full and "SERVER READY" in full
        _BG_SHELLS.get(bid, {}).get("proc").kill()
    anyio.run(go)


def test_monitor_reports_exit_when_pattern_never_matches() -> None:
    async def go():
        start = await bash.fn("echo nope; exit 3", run_in_background=True)
        bid = start.split(" as ")[1].split(" ")[0]
        out = await monitor.fn(bash_id=bid, until_pattern="never-appears", timeout_s=10, poll_s=0.1)
        assert "exited with code 3" in out and "pattern never matched" in out
    anyio.run(go)


def test_monitor_waits_for_exit_without_pattern() -> None:
    async def go():
        start = await bash.fn("sleep 0.3; exit 0", run_in_background=True)
        bid = start.split(" as ")[1].split(" ")[0]
        out = await monitor.fn(bash_id=bid, timeout_s=10, poll_s=0.1)
        assert "exited with code 0" in out
    anyio.run(go)


def test_monitor_file_appears(tmp_path) -> None:
    async def go():
        target = tmp_path / "artifact.txt"

        async def writer():
            await anyio.sleep(0.3)
            target.write_text("done")

        async with anyio.create_task_group() as tg:
            tg.start_soon(writer)
            out = await monitor.fn(path=str(target), timeout_s=10, poll_s=0.1)
            assert "appeared" in out
    anyio.run(go)


def test_monitor_file_change(tmp_path) -> None:
    async def go():
        target = tmp_path / "log.txt"
        target.write_text("v1")

        async def writer():
            await anyio.sleep(0.3)
            target.write_text("v2 changed")

        async with anyio.create_task_group() as tg:
            tg.start_soon(writer)
            out = await monitor.fn(path=str(target), timeout_s=10, poll_s=0.1)
            assert "changed" in out
    anyio.run(go)


def test_monitor_port_opens() -> None:
    async def go():
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(1)
        try:
            out = await monitor.fn(port=port, timeout_s=10, poll_s=0.1)
            assert f"port {port} is accepting" in out
        finally:
            srv.close()
    anyio.run(go)


def test_monitor_timeout_is_clear() -> None:
    out = _run(monitor.fn, path="/definitely/not/a/real/path/xyz", timeout_s=1, poll_s=0.2)
    assert "timeout" in out


def test_monitor_bad_regex_falls_back_to_literal() -> None:
    async def go():
        start = await bash.fn("echo 'weird [pattern' ; sleep 3", run_in_background=True)
        bid = start.split(" as ")[1].split(" ")[0]
        out = await monitor.fn(bash_id=bid, until_pattern="weird [pattern", timeout_s=10, poll_s=0.1)
        assert "matched" in out
        _BG_SHELLS.get(bid, {}).get("proc").kill()
    anyio.run(go)


def test_monitor_is_read_only_and_registered() -> None:
    from mantis_agent.builtin_tools import CODING_TOOLS
    assert monitor.is_read_only is True   # auto-allowed; explore subagents get it
    assert any(t.name == "monitor" for t in CODING_TOOLS)
