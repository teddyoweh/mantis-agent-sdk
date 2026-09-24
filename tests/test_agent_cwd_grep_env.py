"""grep and the <env>/git context block honor the agent's working directory
(``AGENT_CWD``), not the host process's cwd."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import anyio
import pytest

import mantis_agent.builtin_tools.fs as fs
import mantis_agent.system_reminder as sr
from mantis_agent.builtin_tools.fs import AGENT_CWD, grep

MARKER = "zqx_agent_cwd_marker_91"


@pytest.fixture()
def dirs(tmp_path: Path, monkeypatch):
    agent_dir = tmp_path / "agent"
    host_dir = tmp_path / "host"
    agent_dir.mkdir()
    host_dir.mkdir()
    (agent_dir / "inside.py").write_text(f"x = '{MARKER}'\n")
    (host_dir / "decoy.py").write_text(f"y = '{MARKER}'\n")
    monkeypatch.chdir(host_dir)
    token = AGENT_CWD.set(str(agent_dir))
    yield agent_dir, host_dir
    AGENT_CWD.reset(token)


def _run_grep(**kw) -> str:
    return anyio.run(lambda: grep.fn(pattern=MARKER, **kw))


@pytest.fixture(params=["rg", "python"])
def backend(request, monkeypatch):
    if request.param == "rg":
        if shutil.which("rg") is None:
            pytest.skip("ripgrep not installed")
        want = True
    else:
        want = False

    async def _have():
        return want

    monkeypatch.setattr(fs, "_have_rg", _have)
    return request.param


def test_grep_dot_searches_agent_cwd(dirs, backend) -> None:
    agent_dir, _ = dirs
    out = _run_grep(path=".")
    assert "inside.py" in out
    assert "decoy.py" not in out
    assert str(agent_dir / "inside.py") in out


def test_grep_default_path_searches_agent_cwd(dirs, backend) -> None:
    out = _run_grep(output_mode="files_with_matches")
    assert "inside.py" in out and "decoy.py" not in out


def test_grep_relative_subpath_resolves_under_agent_cwd(dirs, backend) -> None:
    agent_dir, _ = dirs
    (agent_dir / "sub").mkdir()
    (agent_dir / "sub" / "deep.py").write_text(f"{MARKER}\n")
    out = _run_grep(path="sub", output_mode="files_with_matches")
    assert out.strip() == str(agent_dir / "sub" / "deep.py")


def test_both_backends_print_the_same_paths(dirs, monkeypatch) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")
    results = {}
    for want in (True, False):
        async def _have(w=want):
            return w
        monkeypatch.setattr(fs, "_have_rg", _have)
        results[want] = _run_grep(path=".", output_mode="files_with_matches").strip()
    assert results[True] == results[False]


def test_rg_caps_long_lines(tmp_path: Path, monkeypatch) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")

    async def _have():
        return True

    monkeypatch.setattr(fs, "_have_rg", _have)
    (tmp_path / "min.js").write_text(MARKER + "a" * 50_000 + "\n")
    out = anyio.run(lambda: grep.fn(pattern=MARKER, path=str(tmp_path)))
    assert MARKER in out
    assert len(out) < 2_000


def test_have_rg_is_cached_and_does_not_spawn(monkeypatch) -> None:
    fs._rg_on_path.cache_clear()
    calls = []
    monkeypatch.setattr(fs.shutil, "which", lambda name: calls.append(name) or "/x/rg")

    async def _boom(*a, **k):
        raise AssertionError("_have_rg must not spawn a process")

    monkeypatch.setattr(fs.anyio, "run_process", _boom)
    try:
        assert anyio.run(fs._have_rg) is True
        assert anyio.run(fs._have_rg) is True
        assert calls == ["rg"]
    finally:
        fs._rg_on_path.cache_clear()


# --- <env> / git context -------------------------------------------------

def _git_repo(path: Path, branch: str) -> None:
    def g(*args):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    g("init", "-q", "-b", branch)
    g("config", "user.name", "Env Tester")
    g("config", "user.email", "env@test")
    (path / "f.txt").write_text("hi\n")
    g("add", ".")
    g("commit", "-q", "-m", "first commit in agent repo")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_env_explicit_cwd_shows_that_dir_and_git(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo, "agent-branch")
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.chdir(host)
    env = sr.render_environment_context(cwd=str(repo))
    assert f"Working directory: {repo}" in env
    assert "Is directory a git repo: Yes" in env
    assert "Current branch: agent-branch" in env
    assert "first commit in agent repo" in env
    assert "f.txt" in env


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_env_defaults_to_agent_cwd_contextvar(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo, "ctxvar-branch")
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.chdir(host)
    token = AGENT_CWD.set(str(repo))
    try:
        env = sr.render_environment_context()
    finally:
        AGENT_CWD.reset(token)
    assert f"Working directory: {repo}" in env
    assert "Current branch: ctxvar-branch" in env


def test_env_without_any_cwd_uses_process_cwd(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sr, "_run_git", lambda args, cwd: None)
    env = sr.render_environment_context()
    assert f"Working directory: {Path.cwd()}" in env
    assert "Is directory a git repo: No" in env



def test_agent_without_cwd_inherits_the_enclosing_agent_cwd(tmp_path):
    """A subagent (no cwd of its own) runs inside the parent's AGENT_CWD —
    its tools must resolve there, matching the <env> block it rendered."""
    import anyio

    from mantis_agent import Agent
    from mantis_agent.builtin_tools.fs import AGENT_CWD
    from mantis_agent.events import (
        ContentBlockDelta,
        ContentBlockStart,
        ContentBlockStop,
        MessageDelta,
        MessageStart,
        MessageStop,
        TextDelta,
    )
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.types import TextBlock, Usage

    seen: list = []

    class _Probe(MockProvider):
        name = "mock"

        async def stream(self, **kw):
            seen.append(AGENT_CWD.get())
            yield MessageStart(message_id="m", model="mock")
            yield ContentBlockStart(index=0, block=TextBlock(text=""))
            yield ContentBlockDelta(index=0, delta=TextDelta(text="ok"))
            yield ContentBlockStop(index=0)
            yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
            yield MessageStop()

    async def go(agent):
        from mantis_agent.types import UserMessage

        async for _ in agent.run_iter([UserMessage(content="hi")]):
            pass

    parent_dir = str(tmp_path)
    token = AGENT_CWD.set(parent_dir)
    try:
        anyio.run(go, Agent(model="mock", provider=_Probe()))
        anyio.run(go, Agent(model="mock", provider=_Probe(), cwd=str(tmp_path / "own")))
    finally:
        AGENT_CWD.reset(token)
    assert seen == [parent_dir, str(tmp_path / "own")]
