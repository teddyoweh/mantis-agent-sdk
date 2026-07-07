"""Wave 4: swarm engine, crash recovery, vision guard, subagent live progress."""

from __future__ import annotations

import subprocess

import anyio
import pytest

from mantis_agent.tui import MantisTUI, model_supports_vision


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    yield


def _tui() -> MantisTUI:
    return MantisTUI(model="gpt-5.4", backend="https://api.openai.com/v1", api_key="k",
                     system=None, max_tokens=1, temperature=None, max_turns=1)


# -- vision guard -------------------------------------------------------------------


def test_vision_classification() -> None:
    assert model_supports_vision("gpt-5.4")
    assert model_supports_vision("gpt-4o-mini")
    assert model_supports_vision("claude-opus-4-8")
    assert model_supports_vision("gemini-2.5-pro")
    assert model_supports_vision("llava:13b")
    assert model_supports_vision("qwen2-vl:7b")
    assert not model_supports_vision("qwen2.5-coder:7b")
    assert not model_supports_vision("deepseek-chat")
    assert not model_supports_vision("mistral:7b")


# -- subagent live progress -----------------------------------------------------------


def test_task_tool_reports_progress() -> None:
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.subagent import make_task_tool
    from mantis_agent.builtin_tools.fs import sleep as sleep_tool

    events: list[dict] = []
    t = make_task_tool(model="mock", provider=MockProvider(default_text="done"),
                       tools=[sleep_tool], on_progress=events.append)
    out = anyio.run(lambda: t.fn(prompt="investigate", description="find stuff"))
    assert out == "done"
    phases = [e["phase"] for e in events]
    assert phases[0] == "start" and phases[-1] == "end"
    assert events[0]["type"] == "explore" and events[0]["desc"] == "find stuff"


def test_progress_sink_tracks_lifecycle() -> None:
    t = _tui()
    t._subagent_progress({"id": 1, "phase": "start", "type": "explore", "desc": "x"})
    t._subagent_progress({"id": 1, "phase": "tool", "tool": "grep"})
    t._subagent_progress({"id": 1, "phase": "tool", "tool": "read_file"})
    assert t._live_subagents[1]["tools"] == 2
    t._subagent_progress({"id": 1, "phase": "end"})
    assert 1 not in t._live_subagents


# -- crash recovery --------------------------------------------------------------------


def test_unclean_exit_offers_resume(tmp_path, monkeypatch) -> None:
    from mantis_agent.session_tree import SessionTranscript, new_session_id
    monkeypatch.setenv("MANTIS_AGENT_PROJECT_ROOT", str(tmp_path))
    # session 1 "crashes": marker written with clean=False, has a message
    t1 = _tui()
    sid = new_session_id()
    t1.transcript = SessionTranscript(sid)
    t1.transcript.append_message("user", "hello before crash")
    t1._mark_session_state(clean=False)
    # next launch (a NEW session) sees the hint
    t2 = _tui()
    t2.transcript = SessionTranscript(new_session_id())
    hint = t2._check_unclean_exit()
    assert hint and sid[:8] in hint and "unexpectedly" in hint
    # clean exit clears it
    t1._mark_session_state(clean=True)
    assert t2._check_unclean_exit() is None


def test_same_session_resume_no_hint(tmp_path, monkeypatch) -> None:
    from mantis_agent.session_tree import SessionTranscript, new_session_id
    monkeypatch.setenv("MANTIS_AGENT_PROJECT_ROOT", str(tmp_path))
    t = _tui()
    sid = new_session_id()
    t.transcript = SessionTranscript(sid)
    t.transcript.append_message("user", "hi")
    t._mark_session_state(clean=False)
    # resuming that same session → no self-referential hint
    assert t._check_unclean_exit() is None


# -- swarm engine ------------------------------------------------------------------------


def _make_repo(path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "app.py").write_text("print('v1')\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c",
                    "user.name=t", "commit", "-qm", "init"], check=True)


def test_swarm_runs_parallel_and_applies_winner(tmp_path) -> None:
    from mantis_agent.swarm import run_swarm
    from pathlib import Path
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)

    async def runner(worktree: str, task: str, index: int) -> str:
        # each attempt writes a DIFFERENT change in its own worktree
        Path(worktree, "app.py").write_text(f"print('attempt {index}')\n")
        return f"attempt {index} done"

    async def judge(viable):
        return viable[-1].index, "last attempt is cleanest"

    async def go():
        return await run_swarm("improve app.py", 3, repo,
                               agent_runner=runner, judge=judge)
    res = anyio.run(go)
    assert res.winner == 2 and res.applied
    assert (repo / "app.py").read_text() == "print('attempt 2')\n"   # winner landed
    assert len(res.candidates) == 3
    # worktrees cleaned up
    out = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                         capture_output=True, text=True).stdout
    assert "attempt-" not in out


def test_swarm_survives_a_crashed_attempt(tmp_path) -> None:
    from mantis_agent.swarm import run_swarm
    from pathlib import Path
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)

    async def runner(worktree: str, task: str, index: int) -> str:
        if index == 0:
            raise RuntimeError("attempt 0 exploded")
        Path(worktree, "app.py").write_text(f"print('a{index}')\n")
        return "ok"

    async def judge(viable):
        assert all(c.index != 0 for c in viable)      # crashed one excluded
        return viable[0].index, "first viable"

    res = anyio.run(lambda: run_swarm("x", 3, repo, agent_runner=runner, judge=judge))
    assert res.winner in (1, 2) and res.applied
    assert res.candidates[0].error and "exploded" in res.candidates[0].error


def test_swarm_no_viable_attempts(tmp_path) -> None:
    from mantis_agent.swarm import run_swarm
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)

    async def runner(worktree: str, task: str, index: int) -> str:
        return "did nothing"                           # no diff produced

    async def judge(viable):  # pragma: no cover — never called
        raise AssertionError
    res = anyio.run(lambda: run_swarm("x", 2, repo, agent_runner=runner, judge=judge))
    assert res.winner is None and not res.applied
    assert "no attempt produced a usable diff" in res.reason


def test_swarm_requires_git_repo(tmp_path) -> None:
    from mantis_agent.swarm import run_swarm

    async def runner(w, t, i): return ""
    async def judge(v): return 0, ""
    with pytest.raises(RuntimeError, match="not a git repository"):
        anyio.run(lambda: run_swarm("x", 2, tmp_path / "notrepo",
                                    agent_runner=runner, judge=judge))
