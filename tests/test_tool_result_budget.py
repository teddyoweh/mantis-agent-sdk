"""Tool-result caps scale with the context window; shell output keeps its tail.

The executor's static caps (60k read_file, 40k bash) are sized for big models;
with a known message budget each result is held to a slice of it. Self-limiting
tools (read_file, bash) see that cap and cut cleanly with a "how to get more"
notice. Plus: rg honours slash globs relative to the agent cwd.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import anyio
import pytest

import mantis_agent.builtin_tools.fs as fs
from mantis_agent.builtin_tools.fs import AGENT_CWD, bash, bash_output, grep, read_file
from mantis_agent.streaming.executor import (
    _RESULT_CHAR_CAP,
    StreamingToolExecutor,
    _truncate_block_content,
    _truncate_tool_result,
    result_char_budget_for,
)
from mantis_agent.tools import Tool, ToolRegistry, tool
from mantis_agent.types import ImageBlock, TextBlock, ToolUseBlock


def _reg(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.add(t)
    return reg


async def _dispatch(reg: ToolRegistry, name: str, budget: int | None, inp: dict | None = None):
    async with StreamingToolExecutor(reg, result_char_budget=budget) as ex:
        ex.add_tool_call(ToolUseBlock(id="c1", name=name, input=inp or {}))
        (res,) = await ex.wait_all()
    return res


# -- budget math -----------------------------------------------------------


def test_budget_is_a_slice_of_the_message_budget_with_a_floor() -> None:
    assert result_char_budget_for(None) is None
    assert result_char_budget_for(0) is None
    assert result_char_budget_for(1000) == 4000          # floor
    assert result_char_budget_for(20_000) == 12_000      # 15% × 4 chars/token
    # A big window is bounded by the static caps, not the budget.
    assert len(_truncate_tool_result("A" * 100_000, "read_file",
                                     result_char_budget_for(1_000_000))) <= 60_000


def test_unknown_window_keeps_the_static_caps() -> None:
    big = "A" * 100_000
    assert 59_000 < len(_truncate_tool_result(big, "read_file")) <= 60_000
    assert 39_000 < len(_truncate_tool_result(big, "bash")) <= 40_000
    assert len(_truncate_tool_result("B" * 35_000, "read_file")) == 35_000


# -- executor end to end -----------------------------------------------------


def test_small_window_caps_a_60k_read_on_a_line_boundary(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    f.write_text("".join(f"line {i:05d} " + "x" * 50 + "\n" for i in range(1000)))  # ~61k
    budget = result_char_budget_for(8000)  # an 8k-class model
    res = anyio.run(_dispatch, _reg(read_file), "read_file", budget, {"path": str(f)})
    assert not res.is_error
    assert len(res.content) <= budget
    assert "elided" not in res.content                    # cut by read_file, not the backstop
    assert "Continue with read_file(path, offset=" in res.content
    last = [ln for ln in res.content.splitlines() if "\t" in ln][-1]
    shown = int(last.split("\t")[0])
    assert f"offset={shown + 1}" in res.content


def test_small_window_caps_a_custom_tool_with_a_notice() -> None:
    @tool(name="dump")
    async def dump(n: int) -> str:
        """Big blob."""
        return "S" + "X" * n + "E"

    res = anyio.run(_dispatch, _reg(dump), "dump", 5000, {"n": 60_000})
    assert len(res.content) <= 5000
    assert "elided" in res.content and res.content.startswith("S") and res.content.endswith("E")

    # No budget: the old 30k default.
    res = anyio.run(_dispatch, _reg(dump), "dump", None, {"n": 60_000})
    assert 29_000 < len(res.content) <= 30_000


def test_mcp_list_of_text_is_capped_and_images_survive() -> None:
    img = ImageBlock(source={"type": "base64", "media_type": "image/png", "data": "Zm9v" * 50_000})

    @tool(name="mcp__srv__dump")
    async def dump() -> list:
        """Rich result."""
        return [TextBlock(text="H" + "a" * 40_000 + "T"), img, TextBlock(text="b" * 20_000)]

    res = anyio.run(_dispatch, _reg(dump), "mcp__srv__dump", 8000)
    texts = [b for b in res.content if isinstance(b, TextBlock)]
    assert sum(len(b.text) for b in texts) <= 8000
    assert texts[0].text.startswith("H") and texts[0].text.endswith("T")
    assert res.content[1] is img                          # untouched
    # Under the cap: returned as-is.
    small = [TextBlock(text="ok"), img]
    assert _truncate_block_content(small, "x", 8000) is small


# -- bash / bash_output -----------------------------------------------------


def _bash(cmd: str, cap: int | None = None) -> str:
    async def go():
        token = _RESULT_CHAR_CAP.set(cap)
        try:
            return await bash.fn(command=cmd)
        finally:
            _RESULT_CHAR_CAP.reset(token)
    return anyio.run(go)


def test_bash_long_output_keeps_the_tail_and_exit_code() -> None:
    cmd = (
        "for i in $(seq 1 20000); do echo \"noise line $i\"; done; "
        "echo 'FAILED tests/test_x.py::test_y - AssertionError'; exit 3"
    )
    out = _bash(cmd, cap=6000)
    assert len(out) <= 6000
    assert out.startswith("noise line 1\n")
    assert out.endswith("[exit code: 3]")
    assert "FAILED tests/test_x.py::test_y" in out
    assert "truncated" in out
    saved = out.split("Full output saved to ", 1)[1].split(" — ", 1)[0]
    try:
        full = Path(saved).read_text()
        assert "noise line 10000" in full and "FAILED" in full
    finally:
        os.unlink(saved)
        fs._SAVED_OUTPUTS.remove(saved)


def test_bash_short_output_unchanged() -> None:
    assert _bash("echo hi; exit 2", cap=6000) == "hi\n[exit code: 2]"
    assert _bash("exit 4") == "[exit code: 4]"
    assert _bash("true") == "(no output, exit code 0)"


def test_bash_output_points_at_the_background_log() -> None:
    async def go():
        started = await bash.fn(command="seq 1 20000; echo LAST", run_in_background=True)
        bid = started.split(" as ", 1)[1].split(" ", 1)[0]
        for _ in range(100):
            if fs._bg_shells()[bid]["proc"].poll() is not None:
                break
            await anyio.sleep(0.05)
        token = _RESULT_CHAR_CAP.set(5000)
        try:
            out = await bash_output.fn(bash_id=bid)
        finally:
            _RESULT_CHAR_CAP.reset(token)
        log = fs._bg_shells()[bid]["log"]
        await fs.bash_kill.fn(bash_id=bid)
        return out, log

    out, log = anyio.run(go)
    assert len(out) <= 5000
    assert out.startswith("[bg_")                          # header kept outside the body
    assert out.rstrip().endswith("LAST")
    assert f"Full output saved to {log}" in out


# -- read_file direct call (no executor) ------------------------------------


def test_read_file_without_executor_keeps_the_200k_cap(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("".join(f"{'y' * 40}\n" for _ in range(3000)))
    out = anyio.run(lambda: read_file.fn(path=str(f)))
    assert "output capped" not in out                      # 2000 × ~45 < 200k
    assert out.endswith("[1000 more lines — continue with offset=2001]")


# -- grep: rg globs are relative to the agent cwd ---------------------------


def test_rg_slash_glob_resolves_against_agent_cwd(tmp_path: Path, monkeypatch) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")
    agent = tmp_path / "agent"
    (agent / "pkg").mkdir(parents=True)
    (agent / "tests").mkdir()
    (agent / "pkg" / "a.py").write_text("needle_zq\n")
    (agent / "tests" / "b.py").write_text("needle_zq\n")
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.chdir(host)

    async def _have():
        return True

    monkeypatch.setattr(fs, "_have_rg", _have)
    token = AGENT_CWD.set(str(agent))
    try:
        inc = anyio.run(lambda: grep.fn(pattern="needle_zq", glob="pkg/*.py"))
        exc = anyio.run(lambda: grep.fn(pattern="needle_zq", glob="!tests/**"))
    finally:
        AGENT_CWD.reset(token)
    assert str(agent / "pkg" / "a.py") in inc and "b.py" not in inc
    assert str(agent / "pkg" / "a.py") in exc and "b.py" not in exc


def test_rg_vanishing_falls_back_to_python(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.txt").write_text("needle_zq\n")

    async def _have():
        return True

    async def _gone(*a, **k):
        raise FileNotFoundError("rg")

    monkeypatch.setattr(fs, "_have_rg", _have)
    monkeypatch.setattr(fs.anyio, "run_process", _gone)
    out = anyio.run(lambda: grep.fn(pattern="needle_zq", path=str(tmp_path)))
    assert "a.txt" in out


def test_exhausted_known_window_gets_the_floor_cap_not_static_caps():
    """Overhead filling a KNOWN window must not read as 'unknown' (static,
    largest caps) — it gets the floor cap."""
    from mantis_agent.streaming.executor import _MIN_RESULT_CAP, result_char_budget_for

    assert result_char_budget_for(max(1, 0)) == _MIN_RESULT_CAP
    assert result_char_budget_for(None) is None


# -- review regressions -------------------------------------------------------


def test_truncate_middle_never_exceeds_the_cap() -> None:
    from mantis_agent.streaming.executor import truncate_middle

    text = "z" * 50_000
    for cap in (0, 1, 50, 150, 199, 250, 400, 1000, 4000):
        assert len(truncate_middle(text, cap)) <= cap


def test_many_text_blocks_stay_within_the_cap() -> None:
    img = ImageBlock(source={"type": "base64", "media_type": "image/png", "data": "AAAA"})
    blocks = [TextBlock(text=f"block {i} " + "t" * 3000) for i in range(60)] + [img]
    out = _truncate_block_content(blocks, "some_mcp_tool", 4000)
    assert sum(len(b.text) for b in out if isinstance(b, TextBlock)) <= 4000
    assert out.count(img) == 1                              # images untouched
    assert "block 0 " in out[0].text                        # in order
    assert "more text block(s)" in out[-1].text             # the rest collapsed


def test_bash_output_with_a_huge_command_stays_in_cap_and_keeps_log_path() -> None:
    async def go():
        cmd = "seq 1 20000; echo LAST; : " + "x" * 20_000
        started = await bash.fn(command=cmd, run_in_background=True)
        bid = started.split(" as ", 1)[1].split(" ", 1)[0]
        for _ in range(100):
            if fs._bg_shells()[bid]["proc"].poll() is not None:
                break
            await anyio.sleep(0.05)
        token = _RESULT_CHAR_CAP.set(5000)
        try:
            out = await bash_output.fn(bash_id=bid)
        finally:
            _RESULT_CHAR_CAP.reset(token)
        log = fs._bg_shells()[bid]["log"]
        await fs.bash_kill.fn(bash_id=bid)
        return out, log

    out, log = anyio.run(go)
    assert len(out) <= 5000
    assert f"Full output saved to {log}" in out
    assert out.rstrip().endswith("LAST")


def test_streaming_bash_saves_the_full_output_not_the_bounded_buffer() -> None:
    token = fs.set_bash_output_sink(lambda s: None)
    try:
        out = _bash("seq 1 100000; echo DONE", cap=6000)
    finally:
        fs.reset_bash_output_sink(token)
    assert len(out) <= 6000 and out.rstrip().endswith("DONE")
    saved = out.split("Full output saved to ", 1)[1].split(" — ", 1)[0]
    full = Path(saved).read_text()
    assert "elided while streaming" not in full
    assert full.splitlines() == [str(i) for i in range(1, 100001)] + ["DONE"]
    # The raw per-stream tee files are gone; only the registered copy remains.
    assert not [p for p in os.listdir(os.path.dirname(saved)) if p.startswith("mantis-bash-stream-")]


def test_spill_files_are_capped_private_and_scoped(monkeypatch) -> None:
    import stat
    import tempfile

    monkeypatch.setattr(fs, "_SPILL_MAX_BYTES", 1000)
    path = fs._save_full_output("q" * 5000)
    assert path is not None
    data = Path(path).read_bytes()
    assert data.startswith(b"q" * 1000) and b"4,000 more bytes not saved" in data
    assert len(data) < 1200
    d = os.path.dirname(path)
    assert os.path.realpath(d) != os.path.realpath(tempfile.gettempdir())
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700

    # A subagent scope churning through saves can't prune the parent's file.
    tok = fs.TOOL_SCOPE.set("sub-agent-x")
    try:
        for _ in range(fs._MAX_SAVED_OUTPUTS + 5):
            fs._save_full_output("s" * 10)
    finally:
        fs.TOOL_SCOPE.reset(tok)
    assert os.path.exists(path)

    # atexit cleanup removes every saved file (and the dir; it is re-created on demand).
    sub = list(fs._SAVED_OUTPUTS_BY_SCOPE["sub-agent-x"])
    fs._cleanup_spills()
    assert not os.path.exists(path) and not any(os.path.exists(p) for p in sub)
    again = fs._save_full_output("r")
    assert again and os.path.exists(again)


def test_shell_truncation_fits_even_when_the_prefix_leaves_little_room() -> None:
    token = _RESULT_CHAR_CAP.set(4000)
    try:
        out = fs._truncate_shell("b" * 50_000, prefix="h" * 3500 + "\n",
                                 full_output_path="/some/log")
    finally:
        _RESULT_CHAR_CAP.reset(token)
    assert len(out) <= 4000
    assert "Full output saved to /some/log" in out
