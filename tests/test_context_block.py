"""The <env> + git + directory context block and its single-head injection.

Everything here is OFFLINE: the git block is exercised against a throwaway tmp
repo (skipped if `git` is unavailable) and via a monkeypatched `_run_git` seam,
so no test depends on the ambient repo or the network.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone

import anyio
import pytest

from mantis_agent import UserMessage
from mantis_agent import system_reminder as sr
from mantis_agent.agent import Agent
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

FIXED_NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def _text_turn(text: str) -> list:
    return [
        MessageStart(message_id="mock-1", model="mock-7b"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text=text)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=5, output_tokens=5)),
        MessageStop(),
    ]


# 1. <env> block
def test_env_block_contains_cwd_platform_and_date(tmp_path) -> None:
    block = sr.build_env_context_block(
        cwd=str(tmp_path),
        now=FIXED_NOW,
        platform_name="darwin",
        os_version="Darwin 25.2.0",
        dir_entries=["README.md", "src/", "tests/"],
    )
    assert "<env>" in block and "</env>" in block
    assert str(tmp_path) in block
    assert "darwin" in block
    assert "Darwin 25.2.0" in block
    assert "2026-06-30" in block
    assert "README.md" in block


def test_env_block_degrades_without_dir(tmp_path) -> None:
    block = sr.build_env_context_block(
        cwd=str(tmp_path), now=FIXED_NOW, platform_name="linux", os_version="x"
    )
    assert "<env>" in block
    assert "2026-06-30" in block


# 2. git block
def _init_git_repo(path) -> None:
    def g(*args):
        subprocess.run(["git", *args], cwd=path, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    g("init", "-b", "main")
    g("config", "user.name", "Test User")
    g("config", "user.email", "test@example.com")
    (path / "a.txt").write_text("hello")
    g("add", "a.txt")
    g("commit", "-m", "first commit")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_context_in_real_repo(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    block = sr.build_git_context(cwd=str(repo))
    assert "main" in block
    assert "Recent commits" in block
    assert "first commit" in block
    assert "Status" in block
    assert "Test User" in block


def test_git_context_monkeypatched_seam(monkeypatch, tmp_path) -> None:
    def fake_run(args, cwd):
        joined = " ".join(args)
        if "rev-parse" in joined and "--abbrev-ref" in joined and "origin" not in joined:
            return "feature/x"
        if joined.startswith("status"):
            return " M edited.py"
        if joined.startswith("log"):
            return "abc123 add feature\ndef456 fix bug"
        if "user.name" in joined:
            return "Jane Dev"
        if "user.email" in joined:
            return "jane@example.com"
        return None

    monkeypatch.setattr(sr, "_run_git", fake_run)
    block = sr.build_git_context(cwd=str(tmp_path))
    assert "feature/x" in block
    assert "edited.py" in block
    assert "add feature" in block
    assert "Jane Dev" in block


def test_git_context_outside_repo_degrades(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sr, "_run_git", lambda args, cwd: None)
    assert sr.build_git_context(cwd=str(tmp_path)) == ""
    env = sr.render_environment_context(cwd=str(tmp_path), now=FIXED_NOW)
    assert "<env>" in env
    assert "2026-06-30" in env
    assert "Recent commits" not in env


# 3. project memory: MANTIS.md AND AGENTS.md
def test_project_memory_discovers_mantis_and_agents(tmp_path, monkeypatch) -> None:
    from mantis_agent.project_memory import render_memory_prompt

    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "MANTIS.md").write_text("Mantis project rules here.")
    (proj / "AGENTS.md").write_text("Agents cross-tool rules here.")

    out = render_memory_prompt(proj)
    assert "Mantis project rules here." in out
    assert "Agents cross-tool rules here." in out


# 4. injected exactly once across two run_iter calls
def test_env_context_injected_exactly_once(monkeypatch) -> None:
    monkeypatch.delenv("MANTIS_AGENT_NO_CONTEXT", raising=False)  # enable injection
    monkeypatch.setattr(
        sr, "render_environment_context",
        lambda cwd=None, now=None: "<env>\nWorking directory: /x\n</env>",
    )

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=MockProvider(scripted_events=_text_turn("hi")),
            include_env=True,
            include_memory=False,
        )
        messages = [UserMessage(content="hello")]
        try:
            async for _ in agent.run_iter(messages):
                pass
            async for _ in agent.run_iter(messages):
                pass
        finally:
            await agent.aclose()

        meta = [m for m in messages
                if isinstance(m, UserMessage) and getattr(m, "isMeta", False)]
        assert len(meta) == 1
        assert messages[0] is meta[0]
        assert "<env>" in meta[0].content
        assert "Working directory: /x" in meta[0].content

    anyio.run(main)


def test_env_context_memoized_on_agent(monkeypatch) -> None:
    calls = {"n": 0}

    def counting(cwd=None, now=None):
        calls["n"] += 1
        return "<env>\nWorking directory: /x\n</env>"

    monkeypatch.delenv("MANTIS_AGENT_NO_CONTEXT", raising=False)  # enable injection
    monkeypatch.setattr(sr, "render_environment_context", counting)
    agent = Agent(model="mock-7b", provider=MockProvider(),
                  include_env=True, include_memory=False)
    agent._build_user_context()
    agent._build_user_context()
    assert calls["n"] == 1


# 5. compaction preserves the context head
def test_compaction_preserves_context_head() -> None:
    from mantis_agent.compact import SimpleCompactor
    from mantis_agent.system_reminder import wrap_system_reminder
    from mantis_agent.types import AssistantMessage

    head = UserMessage(
        content=wrap_system_reminder("<env>\nWorking directory: /x\n</env>"),
        isMeta=True,
    )
    body: list = []
    for i in range(6):
        body.append(UserMessage(content=f"user turn {i}"))
        body.append(AssistantMessage(content=[TextBlock(text=f"reply {i}")]))
    messages = [head, *body]

    async def summarize(_prompt: str) -> str:
        return "SUMMARY OF EARLIER TURNS"

    async def main():
        comp = SimpleCompactor(summarize, keep_recent_turns=2)
        out = await comp.compact(list(messages))
        assert out[0] is head
        assert getattr(out[0], "isMeta", False) is True
        assert "<env>" in out[0].content
        assert len(out) < len(messages)
        assert any(
            isinstance(m, UserMessage) and "SUMMARY OF EARLIER TURNS" in (m.content or "")
            for m in out
        )

    anyio.run(main)
