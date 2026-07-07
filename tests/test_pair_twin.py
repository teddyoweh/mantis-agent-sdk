"""`pair` — persistent same-model twins the agent CONVERSES with. The defining
property vs `task`: each named peer keeps its own running dialogue across
calls (propose → pushback → revise), and peers are independent."""

from __future__ import annotations

import anyio

from mantis_agent.providers.mock import MockProvider
from mantis_agent.subagent import make_pair_tool


def _pair(default_text: str = "pushback", **kw):
    prov = MockProvider(default_text=default_text)
    return make_pair_tool(model="mock", provider=prov, tools=[], **kw), prov


def test_pair_replies_with_peer_tag() -> None:
    t, _ = _pair("I disagree — check gate.py:88 first.")
    out = anyio.run(lambda: t.fn(message="plan: rewrite the auth gate"))
    assert out == "[twin] I disagree — check gate.py:88 first."


def test_pair_requires_message() -> None:
    t, _ = _pair()
    assert "required" in anyio.run(lambda: t.fn(message="   "))


def test_pair_conversation_continuity() -> None:
    # THE core property: the twin's second exchange must carry the first one —
    # the provider sees the prior user message AND its own prior reply.
    t, prov = _pair("noted")

    async def go():
        await t.fn(message="first: my plan is X")
        await t.fn(message="second: I changed it to Y")
    anyio.run(go)
    last_call_msgs = prov.calls[-1]["messages"]
    texts = []
    for m in last_call_msgs:
        c = getattr(m, "content", "")
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, list):
            texts.extend(getattr(b, "text", "") for b in c)
    joined = " ".join(texts)
    assert "first: my plan is X" in joined      # earlier user turn remembered
    assert "noted" in joined                     # twin's own earlier reply
    assert "second: I changed it to Y" in joined


def test_pair_peers_are_independent() -> None:
    t, prov = _pair("ok")

    async def go():
        await t.fn(message="only for skeptic", peer="skeptic")
        await t.fn(message="only for perf", peer="perf")
    anyio.run(go)
    # perf's history must NOT contain skeptic's exchange
    last = prov.calls[-1]["messages"]
    joined = " ".join(str(getattr(m, "content", "")) for m in last)
    assert "only for perf" in joined and "only for skeptic" not in joined


def test_pair_reset_forgets() -> None:
    t, prov = _pair("ok")

    async def go():
        await t.fn(message="remember me")
        await t.fn(message="fresh start", reset=True)
    anyio.run(go)
    joined = " ".join(str(getattr(m, "content", "")) for m in prov.calls[-1]["messages"])
    assert "remember me" not in joined and "fresh start" in joined


def test_pair_persona_lands_in_system_prompt() -> None:
    t, prov = _pair("ok")
    anyio.run(lambda: t.fn(message="hi", peer="skeptic",
                           persona="argue against every design choice"))
    sys = prov.calls[-1].get("system") or ""
    assert "skeptic" in sys and "argue against every design choice" in sys
    assert "twin of the main coding agent" in sys


def test_pair_history_trims_from_front() -> None:
    t, prov = _pair("r", max_history=6)

    async def go():
        for i in range(8):
            await t.fn(message=f"msg-{i}")
    anyio.run(go)
    joined = " ".join(str(getattr(m, "content", "")) for m in prov.calls[-1]["messages"])
    assert "msg-0" not in joined         # oldest dropped
    assert "msg-7" in joined             # newest kept


def test_pair_registered_in_tui_agent() -> None:
    from mantis_agent.tui import MantisTUI
    t = MantisTUI(model="gpt-5.4", backend="https://api.openai.com/v1", api_key="k",
                  system=None, max_tokens=1, temperature=None, max_turns=1)
    agent = t._build_agent()
    p = agent.tools.get("pair")
    assert p is not None and "TWIN" in p.description
    assert p.input_schema["required"] == ["message"]


# -- /twin — the USER talks to the same twins ------------------------------------


class _Rec:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.width = 80
    def print(self, *a, **k) -> None:
        # unwrap rich renderables (Markdown keeps its source in .markup)
        self.lines.append(" ".join(str(getattr(x, "markup", x)) for x in a))
    def text(self) -> str:
        return "\n".join(self.lines)


def test_parse_twin_arg() -> None:
    from mantis_agent.tui import MantisTUI
    assert MantisTUI.parse_twin_arg("skeptic: check this") == ("skeptic", "check this")
    assert MantisTUI.parse_twin_arg("just a message") == ("twin", "just a message")
    # a colon mid-sentence must not be misparsed as a peer name
    assert MantisTUI.parse_twin_arg("my plan: do X then Y") == ("twin", "my plan: do X then Y")


def test_slash_twin_shares_state_with_pair_tool(monkeypatch) -> None:
    # THE point of /twin: the user's exchange and the model's pair calls hit
    # the SAME conversation. Agent talks first via pair; /twin then sees it.
    from mantis_agent.tui import MantisTUI
    t = MantisTUI(model="mock", backend="http://localhost:11434", api_key=None,
                  system=None, max_tokens=1, temperature=None, max_turns=1)
    prov = MockProvider(default_text="twin says hi")
    monkeypatch.setattr(t, "_build_agent", t._build_agent)  # no-op; build normally
    t.agent = t._build_agent()
    # swap the pair tool for one on the same state but a mock provider
    t._pair_tool = make_pair_tool(model="mock", provider=prov, tools=[],
                                  conversations=t._twin_conversations,
                                  personas=t._twin_personas)

    async def go():
        await t._pair_tool.fn(message="agent-side exchange", peer="skeptic")  # the model
        t.console = _Rec()
        await t._cmd_twin("list")
        assert "skeptic" in t.console.text()
        t.console = _Rec()
        await t._cmd_twin("skeptic: user-side follow-up")           # the user
        assert "twin says hi" in t.console.text()
    anyio.run(go)
    # both exchanges live in ONE history
    joined = " ".join(str(getattr(m, "content", "")) for m in t._twin_conversations["skeptic"])
    assert "agent-side exchange" in joined and "user-side follow-up" in joined


def test_slash_twin_reset_and_empty_list() -> None:
    from mantis_agent.tui import MantisTUI
    t = MantisTUI(model="mock", backend="http://localhost:11434", api_key=None,
                  system=None, max_tokens=1, temperature=None, max_turns=1)
    t._pair_tool = make_pair_tool(model="mock", provider=MockProvider(default_text="x"),
                                  tools=[], conversations=t._twin_conversations,
                                  personas=t._twin_personas)

    async def go():
        t.console = _Rec()
        await t._cmd_twin("")                       # empty list
        assert "no twins yet" in t.console.text()
        await t._cmd_twin("hello there")            # default twin exchange
        assert "twin" in t._twin_conversations
        t.console = _Rec()
        await t._cmd_twin("reset all")
        assert "forgot 1 twin" in t.console.text()
        assert not t._twin_conversations
    anyio.run(go)


def test_twin_state_survives_agent_rebuild() -> None:
    from mantis_agent.tui import MantisTUI
    t = MantisTUI(model="gpt-5.4", backend="https://api.openai.com/v1", api_key="k",
                  system=None, max_tokens=1, temperature=None, max_turns=1)
    t.agent = t._build_agent()
    t._twin_conversations["skeptic"] = [1, 2, 3]    # simulate an ongoing dialogue
    t.agent = t._build_agent()                       # model switch / rebuild
    assert t._twin_conversations["skeptic"] == [1, 2, 3]  # memory intact
