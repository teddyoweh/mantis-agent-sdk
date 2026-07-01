"""bash run_in_background + bash_output (T1.4): long-running commands run
detached and their output is polled later."""

from __future__ import annotations

import anyio

from mantis_agent.builtin_tools.fs import bash, bash_output


def _bid(start_msg: str) -> str:
    return start_msg.split("as ")[1].split(" ")[0]


def test_background_start_returns_id() -> None:
    async def main():
        msg = await bash.fn(command="echo hi", run_in_background=True)
        assert msg.startswith("Started in background as bg_")
        assert "bash_output" in msg
        return _bid(msg)

    bid = anyio.run(main)
    assert bid.startswith("bg_")


def test_background_output_collected_and_status() -> None:
    async def main():
        msg = await bash.fn(
            command="for i in 1 2 3; do echo line$i; done", run_in_background=True
        )
        bid = _bid(msg)
        await anyio.sleep(0.4)
        out = await bash_output.fn(bash_id=bid)
        return out

    out = anyio.run(main)
    assert "line1" in out and "line3" in out
    assert "exited with code 0" in out


def test_background_running_status() -> None:
    async def main():
        msg = await bash.fn(command="sleep 2", run_in_background=True)
        bid = _bid(msg)
        out = await bash_output.fn(bash_id=bid)  # poll immediately — still running
        return out

    out = anyio.run(main)
    assert "running" in out


def test_bash_output_unknown_id() -> None:
    out = anyio.run(lambda: bash_output.fn(bash_id="bg_does_not_exist"))
    assert "no background shell" in out


def test_foreground_still_works() -> None:
    out = anyio.run(lambda: bash.fn(command="echo foreground"))
    assert "foreground" in out


def test_bash_output_registered() -> None:
    from mantis_agent.builtin_tools.fs import CODING_TOOLS
    assert any(t.name == "bash_output" for t in CODING_TOOLS)
