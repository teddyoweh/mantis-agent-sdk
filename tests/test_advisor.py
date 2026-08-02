"""The advisor — a stronger model the agent escalates decisions to.

The claim worth testing is the cross-provider one: the advisor resolves its own
provider, base URL and key independently of whatever the session is running, so
a local model can consult a hosted one. Everything else is plumbing around it.
"""

from __future__ import annotations

import json

import pytest

from mantis_agent.advisor import (
    AdvisorConfig,
    _render_transcript,
    advisor_prompt_section,
    advisor_status,
    make_advisor_tool,
    resolve_advisor,
)
from mantis_agent.types import AssistantMessage, TextBlock, ToolUseBlock, UserMessage

pytestmark = pytest.mark.anyio


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """No advisor unless a test asks for one — otherwise these read whatever
    the developer running them happens to have configured."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))
    monkeypatch.delenv("MANTIS_ADVISOR", raising=False)
    return home


# -- resolution --------------------------------------------------------------


def test_off_unless_configured() -> None:
    assert resolve_advisor() is None


def test_explicit_model_beats_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("MANTIS_ADVISOR", "gpt-5.4")
    assert resolve_advisor().model == "gpt-5.4"
    assert resolve_advisor().source == "env"
    flag = resolve_advisor("claude-opus-5")
    assert flag.model == "claude-opus-5" and flag.source == "flag"


def test_environment_beats_settings(monkeypatch, clean_env) -> None:
    (clean_env / "settings.json").write_text(
        json.dumps({"advisorModel": "deepseek-chat"}), encoding="utf-8")
    assert resolve_advisor().model == "deepseek-chat"
    assert resolve_advisor().source == "settings"
    monkeypatch.setenv("MANTIS_ADVISOR", "gpt-5.4")
    assert resolve_advisor().model == "gpt-5.4"


def test_off_is_a_real_value_everywhere(monkeypatch) -> None:
    """Turning it off has to work from the flag AND the environment, or a
    CI runner inherits an advisor it can't shake off."""
    monkeypatch.setenv("MANTIS_ADVISOR", "off")
    assert resolve_advisor() is None
    monkeypatch.setenv("MANTIS_ADVISOR", "gpt-5.4")
    assert resolve_advisor("off") is None
    assert resolve_advisor("none") is None


@pytest.mark.parametrize(
    ("query", "want_label"),
    [("claude-opus-5", "Claude (Anthropic)"), ("gpt-5.4", "OpenAI")],
)
def test_a_model_id_carries_its_provider(query: str, want_label: str) -> None:
    """The point of the feature: the advisor knows where it lives, so the main
    model's backend is irrelevant to reaching it."""
    cfg = resolve_advisor(query)
    assert cfg.label == want_label
    assert cfg.backend and cfg.backend.startswith("http")
    assert cfg.cross_provider is True


def test_aliases_resolve_the_way_slash_model_does() -> None:
    """`/advisor opus` has to work — nobody types full model ids."""
    cfg = resolve_advisor("opus")
    assert cfg.model.startswith("claude-opus")
    assert cfg.label == "Claude (Anthropic)"


def test_an_unknown_model_falls_back_to_the_session_backend() -> None:
    """A local Ollama tag isn't in the catalog. That must degrade to 'run it on
    my own backend', not crash the session at launch."""
    cfg = resolve_advisor("my-finetune:latest")
    assert cfg.model == "my-finetune:latest"
    assert cfg.backend is None and cfg.label is None
    assert cfg.cross_provider is False


def test_a_broken_settings_file_does_not_block_launch(clean_env) -> None:
    (clean_env / "settings.json").write_text("{ not json", encoding="utf-8")
    assert resolve_advisor() is None            # no advisor, no exception


# -- status ------------------------------------------------------------------


def test_status_off() -> None:
    assert advisor_status(None) == {"on": False}


def test_status_flags_a_missing_key_and_a_pointless_pairing() -> None:
    no_key = advisor_status(
        AdvisorConfig(model="claude-opus-5", backend="https://api.anthropic.com/v1",
                      label="Claude (Anthropic)"))
    assert no_key["on"] and no_key["reachable"] is False
    assert no_key["cross_provider"] is True

    same = advisor_status(AdvisorConfig(model="gpt-5.4"), main_model="gpt-5.4")
    assert same["same_as_main"] is True
    assert same["reachable"] is True            # no backend → the session's own


# -- transcript rendering ----------------------------------------------------


def test_transcript_renders_roles_tool_calls_and_results() -> None:
    text = _render_transcript([
        UserMessage(content="fix the login bug"),
        AssistantMessage(content=[
            TextBlock(text="looking at auth.py"),
            ToolUseBlock(id="1", name="read_file", input={"path": "auth.py"}),
        ]),
        UserMessage(content=[{"type": "tool_result", "content": "def login(): ..."}]),
    ])
    assert "### user" in text and "### assistant" in text
    assert "fix the login bug" in text
    assert "[called read_file]" in text
    assert "def login()" in text


def test_transcript_trims_from_the_front() -> None:
    """The advisor is asked about what's happening NOW, so the recent turns are
    the ones that must survive the cap."""
    msgs = [UserMessage(content=f"turn {i} " + "x" * 500) for i in range(50)]
    out = _render_transcript(msgs, limit=2000)
    assert len(out) < 2400
    assert "turn 49" in out and "turn 0 " not in out
    assert "trimmed" in out


def test_an_empty_conversation_says_so() -> None:
    assert "empty" in _render_transcript([])


# -- the tool itself ---------------------------------------------------------


class _FakeAgent:
    """Stands in for the advisor's Agent — records what it was constructed
    with so the test can assert the cross-provider wiring."""

    seen: dict = {}

    def __init__(self, **kw):
        _FakeAgent.seen = kw

    async def run(self, messages):
        _FakeAgent.seen["prompt"] = messages[0].content
        return [AssistantMessage(content=[TextBlock(text="Ship it. The plan is sound.")])]


async def test_consulting_reaches_the_advisors_own_backend(monkeypatch) -> None:
    """A local main model escalating to a hosted advisor: the Agent must be
    built with the ADVISOR's model and base URL, not the session's."""
    import mantis_agent.agent as agent_mod

    monkeypatch.setattr(agent_mod, "Agent", _FakeAgent)
    made: dict = {}

    def fake_factory(**kw):
        made["kw"] = kw
        return "PROVIDER"

    monkeypatch.setattr("mantis_agent.providers.base.resolve", lambda name: fake_factory)
    monkeypatch.setattr("mantis_agent.providers.base.detect_provider", lambda x: "anthropic")

    cfg = AdvisorConfig(model="claude-opus-5", backend="https://api.anthropic.com/v1",
                        api_key="sk-adv", label="Claude (Anthropic)")
    convo = [UserMessage(content="refactor auth to middleware")]
    tool = make_advisor_tool(cfg, messages=convo, provider="SESSION_PROVIDER")

    out = await tool.fn(question="Sound approach, or is there a simpler route?")

    assert made["kw"] == {"base_url": "https://api.anthropic.com/v1", "api_key": "sk-adv"}
    assert _FakeAgent.seen["model"] == "claude-opus-5"
    assert _FakeAgent.seen["provider"] == "PROVIDER"     # NOT the session's
    assert _FakeAgent.seen["max_steps"] == 1             # judgement, not a second loop
    assert "refactor auth to middleware" in _FakeAgent.seen["prompt"]
    assert "simpler route" in _FakeAgent.seen["prompt"]
    assert out == "[advisor · claude-opus-5]\nShip it. The plan is sound."


async def test_the_tool_reads_the_live_conversation(monkeypatch) -> None:
    """It holds the caller's list, not a copy — so turns that happen after the
    agent was built are still visible when the model finally escalates."""
    import mantis_agent.agent as agent_mod

    monkeypatch.setattr(agent_mod, "Agent", _FakeAgent)
    convo: list = []
    tool = make_advisor_tool(AdvisorConfig(model="local-model"), messages=convo)

    convo.append(UserMessage(content="a thing that happened later"))
    await tool.fn(question="what now?")
    assert "a thing that happened later" in _FakeAgent.seen["prompt"]


async def test_no_tools_for_the_advisor(monkeypatch) -> None:
    """It gives judgement; a tool-using advisor would just be a second agent
    racing the first one over the same files."""
    import mantis_agent.agent as agent_mod

    monkeypatch.setattr(agent_mod, "Agent", _FakeAgent)
    tool = make_advisor_tool(AdvisorConfig(model="m"), messages=[])
    await tool.fn(question="q")
    assert not _FakeAgent.seen.get("tools")


async def test_a_failed_consult_is_advice_not_a_crash(monkeypatch) -> None:
    """The advisor being unreachable must not take the session down with it —
    the agent gets told to carry on."""
    import mantis_agent.agent as agent_mod

    class _Boom:
        def __init__(self, **kw):
            pass

        async def run(self, messages):
            raise RuntimeError("402 payment required")

    monkeypatch.setattr(agent_mod, "Agent", _Boom)
    tool = make_advisor_tool(AdvisorConfig(model="claude-opus-5"), messages=[])
    out = await tool.fn(question="is this right?")
    assert "402 payment required" in out
    assert "own judgement" in out


async def test_an_empty_question_is_refused() -> None:
    tool = make_advisor_tool(AdvisorConfig(model="m"), messages=[])
    assert "actual question" in await tool.fn(question="   ")


async def test_the_ui_is_told_a_consult_is_happening(monkeypatch) -> None:
    """It spends real tokens on a second model — that can't be invisible."""
    import mantis_agent.agent as agent_mod

    monkeypatch.setattr(agent_mod, "Agent", _FakeAgent)
    seen: list = []
    tool = make_advisor_tool(AdvisorConfig(model="claude-opus-5"), messages=[],
                             on_consult=lambda m, q: seen.append((m, q)))
    await tool.fn(question="check this")
    assert seen == [("claude-opus-5", "check this")]


async def test_a_broken_ui_callback_does_not_break_the_consult(monkeypatch) -> None:
    import mantis_agent.agent as agent_mod

    monkeypatch.setattr(agent_mod, "Agent", _FakeAgent)

    def boom(m, q):
        raise RuntimeError("console gone")

    tool = make_advisor_tool(AdvisorConfig(model="m"), messages=[], on_consult=boom)
    assert "Ship it" in await tool.fn(question="q")


def test_the_tool_is_read_only() -> None:
    """It must be safe to call under any permission mode — it edits nothing."""
    tool = make_advisor_tool(AdvisorConfig(model="m"), messages=[])
    assert tool.name == "consult_advisor"
    assert tool.is_read_only is True


# -- the prompt section ------------------------------------------------------


def test_prompt_section_is_empty_when_unpaired() -> None:
    assert advisor_prompt_section(None) == ""


def test_prompt_section_names_the_model_and_when_to_use_it() -> None:
    text = advisor_prompt_section(AdvisorConfig(model="claude-opus-5"))
    assert "claude-opus-5" in text and "consult_advisor" in text
    # the part that stops it being called on every trivial turn
    assert "not routinely" in text
    assert "read the code" in text
