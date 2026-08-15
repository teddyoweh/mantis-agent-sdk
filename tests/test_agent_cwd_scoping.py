"""``cwd`` must scope the built-in tools, not just the text the model reads.

Found by dogfooding: a coding agent was given ``cwd=<temp dir>``, told the model
"Working directory: <temp dir>" in its env context block, and then wrote every
relative path into the *host process's* directory instead. The model emitted
sensible relative paths in good faith, its files landed outside the intended
tree, and its own follow-up ``ls`` disagreed with its own writes — which read as
"the small model is confused" when the SDK was the one lying.

The same prompt on the same 7B model went from 0 files created to a correct,
passing fizzbuzz + test once the tools resolved against ``cwd``.

Two rules pinned here:
  * a relative path resolves against the agent's ``cwd`` when set;
  * ``None`` (the default) keeps the host process cwd, so existing callers are
    bit-for-bit unaffected.
"""

from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest

from mantis_agent import Agent, MantisAgentOptions
from mantis_agent.builtin_tools import fs as fs_tools
from mantis_agent.compat_query import _build_agent as _build_compat_agent
from mantis_agent.query import _agent_from_options


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "seed.txt").write_text("seed\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_relative_path_resolves_against_agent_cwd(workdir: Path, monkeypatch):
    token = fs_tools.AGENT_CWD.set(str(workdir))
    try:
        assert fs_tools.resolve_path("out.py") == workdir / "out.py"
        assert fs_tools.resolve_path("sub/dir/out.py") == workdir / "sub/dir/out.py"
    finally:
        fs_tools.AGENT_CWD.reset(token)


def test_absolute_and_tilde_paths_are_untouched(workdir: Path):
    token = fs_tools.AGENT_CWD.set(str(workdir))
    try:
        assert fs_tools.resolve_path("/etc/hosts") == Path("/etc/hosts")
        assert fs_tools.resolve_path("~/x") == Path.home() / "x"
    finally:
        fs_tools.AGENT_CWD.reset(token)


def test_no_agent_cwd_keeps_process_cwd_behavior():
    """The regression guard for every existing caller: unset means unchanged."""

    assert fs_tools.agent_cwd() is None
    assert fs_tools.resolve_path("rel.txt") == Path("rel.txt")


# ---------------------------------------------------------------------------
# The tools themselves
# ---------------------------------------------------------------------------


def test_write_then_read_round_trips_inside_cwd(workdir: Path):
    """What the failing dogfood run needed: write_file and read_file agreeing
    on where a relative path points."""

    token = fs_tools.AGENT_CWD.set(str(workdir))
    try:
        async def main() -> str:
            await fs_tools.write_file.fn(path="hello.py", content="print('hi')\n")
            return await fs_tools.read_file.fn(path="hello.py")

        out = anyio.run(main)
    finally:
        fs_tools.AGENT_CWD.reset(token)

    assert (workdir / "hello.py").read_text() == "print('hi')\n"
    assert "print('hi')" in out
    # And nothing landed next to the test process.
    assert not (Path(os.getcwd()) / "hello.py").exists()


def test_bash_starts_in_the_agent_cwd(workdir: Path):
    """``ls`` has to see what ``write_file`` just wrote, or the model spends
    turns arguing with its own output."""

    token = fs_tools.AGENT_CWD.set(str(workdir))
    try:
        async def main() -> str:
            return await fs_tools.bash.fn(command="pwd && ls")

        out = anyio.run(main)
    finally:
        fs_tools.AGENT_CWD.reset(token)
        fs_tools._BASH_CWD_BY_SCOPE.pop("__global__", None)

    assert str(workdir) in out
    assert "seed.txt" in out


# ---------------------------------------------------------------------------
# Plumbing: the option has to reach the Agent from both paths
# ---------------------------------------------------------------------------


def test_agent_takes_cwd():
    assert Agent(model="mock", backend="mock", cwd="/tmp/x").cwd == "/tmp/x"


def test_cwd_reaches_the_agent_from_dict_options():
    agent = _agent_from_options({"model": "mock", "backend": "mock", "cwd": "/tmp/x"})
    assert agent.cwd == "/tmp/x"
    assert not (agent.extra or {}).get("cwd")


def test_cwd_reaches_the_agent_from_typed_options():
    agent = _build_compat_agent(
        MantisAgentOptions(model="mock", backend="mock", cwd="/tmp/x").to_query_options()
    )
    assert agent.cwd == "/tmp/x"


def test_cwd_defaults_to_none():
    """Unset must stay unset — this is what keeps the change additive."""

    assert Agent(model="mock", backend="mock").cwd is None


# ---------------------------------------------------------------------------
# SubAgentSpec needs a destination, not just a model name
# ---------------------------------------------------------------------------


def test_subagent_spec_carries_a_backend():
    """Dogfooded: a spec with a model and no backend minted a child that fell
    back to the openai_compat default (localhost:8000) and raised
    ``ProviderError: Not Found``. Called through a parent model, that surfaced
    as the child politely reporting it "couldn't find" the answer — a connection
    failure disguised as a result.
    """

    from mantis_agent import SubAgentSpec, as_subagent_tool

    spec = SubAgentSpec(
        name="child",
        system_prompt="p",
        model="mock",
        backend="mock",
        api_key="sk-child",
    )
    assert spec.backend == "mock"
    assert spec.api_key == "sk-child"
    # And the wrapper still builds a Tool from it.
    assert as_subagent_tool(spec).name == "child"


def test_subagent_spec_backend_is_optional():
    """Sharing the parent's provider stays the recommended path, so the new
    fields must not become required."""

    from mantis_agent import SubAgentSpec

    spec = SubAgentSpec(name="child", system_prompt="p", model="mock")
    assert spec.backend is None
    assert spec.api_key is None
