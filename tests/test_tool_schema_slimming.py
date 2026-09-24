"""Tool schemas: no duplicated param docs, minified path-B schemas, and compact
(short) descriptions for small-context / prompt-engineered models."""

from __future__ import annotations

import json

from mantis_agent.builtin_tools import CODING_TOOLS, web_fetch, web_search
from mantis_agent.builtin_tools.short_descriptions import SHORT_DESCRIPTIONS
from mantis_agent.capabilities import ModelCapability
from mantis_agent.providers.openai_compat import _render_prompt_engineered_tools
from mantis_agent.tools import ToolRegistry, _parse_docstring_args, tool


@tool
async def google_style(path: str, limit: int = 10) -> str:
    """Read part of a file.

    Args:
        path: File to read.
        limit: Max lines, capped
            at 2000.

    Returns:
        The file text.

    Raises:
        OSError: When unreadable.
    """
    return ""


@tool
async def numpy_style(city: str, units: str = "c") -> str:
    """Get the weather for a city.

    Parameters
    ----------
    city : str
        The city name.
    units : str
        ``c`` or ``f``.

    Returns
    -------
    str
        A forecast.
    """
    return ""


def test_docstring_sections_are_stripped_but_params_keep_their_docs():
    assert google_style.description == "Read part of a file."
    props = google_style.input_schema["properties"]
    assert props["path"]["description"] == "File to read."
    assert props["limit"]["description"] == "Max lines, capped at 2000."


def test_numpy_style_sections_are_stripped_and_parsed():
    assert numpy_style.description == "Get the weather for a city."
    props = numpy_style.input_schema["properties"]
    assert props["city"]["description"] == "The city name."
    assert props["units"]["description"] == "``c`` or ``f``."


def test_prose_after_a_google_section_is_kept_and_not_swallowed():
    # web_search's docstring has prose after its Args block (dedented).
    assert "Uses Exa" in web_search.description
    assert "Args:" not in web_search.description
    blocked = web_search.input_schema["properties"]["blocked_domains"]["description"]
    assert "Exa" not in blocked
    doc = "Do it.\n\nArgs:\n    x: The x.\n\nMore prose.\n"
    assert _parse_docstring_args(doc) == {"x": "The x."}


def test_explicit_description_is_used_verbatim():
    @tool(description="Custom.\n\nArgs:\n    q: keep me")
    async def f(q: str) -> str:
        """Doc.

        Args:
            q: The query.
        """
        return q

    assert f.description == "Custom.\n\nArgs:\n    q: keep me"

    @tool("g", "Claude form.\n\nArgs:\n    a: kept", {"a": float})
    async def g(args):
        return {"content": []}

    assert g.description == "Claude form.\n\nArgs:\n    a: kept"


def test_builtin_descriptions_carry_no_param_sections_and_no_param_lost():
    for t in (*CODING_TOOLS, web_fetch, web_search):
        assert "Args:" not in t.description, t.name
        for pname, prop in t.input_schema.get("properties", {}).items():
            if (t.name, pname) == ("bash", "run_in_background"):
                continue  # documented in the description prose, not an Args entry
            assert prop.get("description"), (t.name, pname)


def test_compact_wire_uses_short_descriptions():
    reg = ToolRegistry()
    reg.add(*CODING_TOOLS)
    full = {t["name"]: t for t in reg.to_wire()}
    compact = {t["name"]: t for t in reg.to_wire(compact=True)}
    assert compact["bash"]["description"] == SHORT_DESCRIPTIONS["bash"]
    assert full["bash"]["description"] == next(
        t for t in CODING_TOOLS if t.name == "bash").description
    # Schemas are identical; only descriptions shrink.
    assert compact["grep"]["input_schema"] == full["grep"]["input_schema"]
    assert len(json.dumps(list(compact.values()))) < len(json.dumps(list(full.values()))) * 0.8
    # A tool without a short description sends its full one either way.
    assert compact["notebook_edit"]["description"] == full["notebook_edit"]["description"]


def test_tool_decorator_accepts_description_short():
    @tool(description_short="Short.")
    async def h(x: str) -> str:
        """A long description."""
        return x

    assert h.to_wire()["description"] == "A long description."
    assert h.to_wire(compact=True)["description"] == "Short."


def test_path_b_prompt_schema_is_minified():
    reg = ToolRegistry()
    reg.add(google_style)
    block = _render_prompt_engineered_tools(reg.to_wire())
    assert json.dumps(google_style.input_schema, separators=(",", ":")) in block
    assert '\n  "' not in block  # no indent=2 pretty-printing
    assert "## google_style" in block


def _agent(model_cap: ModelCapability):
    from mantis_agent.agent import Agent
    from mantis_agent.providers.mock import MockProvider

    reg = ToolRegistry()
    reg.add(*CODING_TOOLS)
    return Agent(model="mock-model", provider=MockProvider(), tools=reg,
                 model_capability=model_cap)


def test_agent_compacts_for_small_window_or_prompt_engineered_path():
    big_native = ModelCapability(name="x", family="x", supports_native_tools=True,
                                 context_window=200_000)
    small_native = ModelCapability(name="x", family="x", supports_native_tools=True,
                                   context_window=16_384)
    big_prompted = ModelCapability(name="x", family="x", supports_native_tools=False,
                                   context_window=200_000)
    assert _agent(small_native)._compact_tool_wire() is True
    assert _agent(big_prompted)._compact_tool_wire() is True
    # Mock backend is native-tools capable; a big native window stays full.
    assert _agent(big_native)._compact_tool_wire() is False


# ---------------------------------------------------------------------------
# Review fixes: section boundaries, headings in examples, flush Google entries,
# a stable compact decision, compat namespacing, short-text staleness.
# ---------------------------------------------------------------------------

from mantis_agent.tools import _strip_docstring_sections  # noqa: E402


def test_numpy_trailing_prose_is_kept_and_not_folded_into_last_param():
    doc = (
        "Get the weather.\n\n"
        "Parameters\n----------\n"
        "city : str\n    The city name.\n"
        "units : str\n    c or f.\n\n"
        "Results are cached for ten minutes.\n"
    )
    assert _parse_docstring_args(doc) == {"city": "The city name.", "units": "c or f."}
    stripped = _strip_docstring_sections(doc)
    assert "Results are cached for ten minutes." in stripped
    assert "Parameters" not in stripped and "city" not in stripped
    # Returns with a bare type entry after a blank line still belongs to Returns.
    doc2 = "Do it.\n\nReturns\n-------\nstr\n    A value.\n\ndict[str, int]\n    Other.\n"
    assert _strip_docstring_sections(doc2) == "Do it."


def test_args_inside_examples_or_literal_blocks_is_not_a_section():
    doc = (
        "Run a job.\n\n"
        "Example:\n"
        "    Args:\n"
        "        nope: not a param\n\n"
        "Usage::\n\n"
        "    Args:\n"
        "        also_nope: literal\n\n"
        "Args:\n"
        "    job: The job id.\n"
    )
    assert _parse_docstring_args(doc) == {"job": "The job id."}
    stripped = _strip_docstring_sections(doc)
    assert "nope: not a param" in stripped and "also_nope: literal" in stripped
    assert "The job id." not in stripped


def test_indented_continuation_line_that_reads_like_a_heading_is_text():
    doc = (
        "Fetch a thing.\n\n"
        "Args:\n"
        "    mode: How the call behaves.\n"
        "        Returns:\n"
        "        the cached copy when set.\n"
        "    url: Where.\n"
    )
    parsed = _parse_docstring_args(doc)
    assert parsed["url"] == "Where."
    assert "the cached copy when set." in parsed["mode"]
    assert _strip_docstring_sections(doc) == "Fetch a thing."


def test_flush_google_entries_parse_again():
    doc = "Do it.\n\nArgs:\nx: the x\ny: the y\n    wrapped.\n\nTrailing prose.\n"
    assert _parse_docstring_args(doc) == {"x": "the x", "y": "the y wrapped."}
    assert _strip_docstring_sections(doc) == "Do it.\n\nTrailing prose."
    assert _parse_docstring_args("Args:\nx: the x\ny: the y") == {"x": "the x", "y": "the y"}


def test_compact_decision_is_stable_across_a_window_refinement(monkeypatch):
    small = ModelCapability(name="x", family="x", supports_native_tools=True,
                            context_window=8192)
    agent = _agent(small)
    assert agent._compact_tool_wire() is True
    # The probe learns the real (large) window after request 1 — no flip.
    monkeypatch.setattr(type(agent), "_effective_context_window", lambda self: 262_144)
    assert agent._compact_tool_wire() is True
    # A model switch re-decides; switching back restores the original decision.
    primary = agent.model
    agent.model = "other-model"
    assert agent._compact_tool_wire() is False
    agent.model = primary
    assert agent._compact_tool_wire() is True
    # A learned (overflow) limit is the one event that re-decides in place.
    agent._compact_wire_memo = None
    assert agent._compact_tool_wire() is False


def test_learned_limit_clears_the_compact_memo(monkeypatch):
    import mantis_agent.context_limits as cl

    agent = _agent(ModelCapability(name="x", family="x", supports_native_tools=True,
                                   context_window=200_000))
    assert agent._compact_tool_wire() is False
    monkeypatch.setattr(cl, "parse_limit", lambda err: 8192)
    monkeypatch.setattr(cl, "learned_limit", lambda *a, **k: None)
    monkeypatch.setattr(cl, "record_limit", lambda *a, **k: True)
    assert agent._learn_context_limit(RuntimeError("limit is 8192")) is True
    assert agent._compact_wire_memo is None


def test_compact_uses_the_agents_backend_capability_and_astra_override():
    from dataclasses import replace

    from mantis_agent.agent import Agent
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.providers.openai_compat import _GENERIC_VLLM_PROFILE

    big_native = ModelCapability(name="x", family="x", supports_native_tools=True,
                                 context_window=200_000)
    reg = ToolRegistry()
    reg.add(*CODING_TOOLS)
    prompted_backend = replace(_GENERIC_VLLM_PROFILE, supports_native_tools=False)
    # A custom provider without ``backend_capability``: the agent's own wins.
    provider = MockProvider()
    agent = Agent(model="mock-model", provider=provider, tools=reg,
                  model_capability=big_native, backend_capability=prompted_backend)
    assert agent._compact_tool_wire() is True

    agent = Agent(model="gpt-6-astra", provider=MockProvider(), tools=reg,
                  model_capability=big_native)
    agent.provider.name = "openai_compat"
    assert agent._compact_tool_wire() is True


def test_compat_mcp_namespacing_keeps_description_short():
    from mantis_agent.compat_query import _build_agent

    @tool(description_short="Short add.")
    async def add(a: int) -> str:
        """Add a long way round."""
        return str(a)

    inner = ToolRegistry()
    inner.add(add)

    class _Server:
        name = "calc"
        registry = inner

    class _Cfg:
        server = _Server()

    agent = _build_agent({"model": "mock-model", "backend": "mock",
                          "mcp_servers": {"calc": _Cfg()}})
    wire = {t["name"]: t for t in agent.tools.to_wire(compact=True)}
    assert wire["mcp__calc__add"]["description"] == "Short add."


def test_short_description_gaps_are_filled():
    assert "all-or-nothing" in SHORT_DESCRIPTIONS["multi_edit"]
    assert "Read the file first" in SHORT_DESCRIPTIONS["multi_edit"]
    assert "No interactive prompts" in SHORT_DESCRIPTIONS["bash"]
    assert "monitor" in SHORT_DESCRIPTIONS["sleep"]


def test_replaced_description_does_not_ship_a_stale_short():
    from dataclasses import replace

    bash = next(t for t in CODING_TOOLS if t.name == "bash")
    assert bash.to_wire(compact=True)["description"] == SHORT_DESCRIPTIONS["bash"]
    custom = replace(bash, description="Run commands in the sandbox only.")
    assert custom.to_wire(compact=True)["description"] == "Run commands in the sandbox only."
    # Replacing both keeps the user's new short.
    both = replace(bash, description="Custom.", description_short="Tiny.")
    assert both.to_wire(compact=True)["description"] == "Tiny."
    # A plain copy still uses the stock short.
    assert replace(bash).to_wire(compact=True)["description"] == SHORT_DESCRIPTIONS["bash"]
    # Attribute assignment of a new description also counts as stale.
    fresh = replace(bash)
    fresh.description = "Changed in place."
    assert fresh.to_wire(compact=True)["description"] == "Changed in place."
