"""``api_key`` and ``base_url`` must actually reach the provider.

Both doc trees documented these as options for a long time while no code path
read either one. The failure was silent in the worst way: unknown option keys
flow into ``Agent.extra``, so a reader who copied the documented snippet got an
agent that pointed at the default URL with no auth and no error — a 401 or a
connection refused, far from the cause.

These tests pin the wiring end to end (option → Agent → provider → outgoing
HTTP headers), plus the precedence rules, so the documented form can't quietly
stop working again.
"""

from __future__ import annotations

import pytest

from mantis_agent import Agent, MantisAgentOptions
from mantis_agent.compat_query import _build_agent as _build_compat_agent
from mantis_agent.query import _agent_from_options

TOGETHER = "https://api.together.xyz/v1"
MODEL = "Qwen/Qwen2.5-72B-Instruct"


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch):
    """Strip every key the provider chain can discover, so a passing test is
    proof the *option* carried the credential rather than the environment."""

    for var in (
        "MANTIS_AGENT_API_KEY", "MANTIS_AGENT_BASE_URL", "OPENAI_API_KEY",
        "TOGETHER_API_KEY", "FIREWORKS_API_KEY", "GROQ_API_KEY",
        "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "DEEPINFRA_API_KEY",
        "CEREBRAS_API_KEY", "ANYSCALE_API_KEY", "MOONSHOT_API_KEY",
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)


def _wire(agent: Agent) -> tuple[str, str | None]:
    """The two things that decide whether a request lands: URL and auth."""

    client = agent.provider.client
    headers = dict(getattr(client, "headers", {}) or {})
    return str(client.base_url), headers.get("authorization")


# ---------------------------------------------------------------------------
# Agent — the direct form
# ---------------------------------------------------------------------------


def test_agent_base_url_and_api_key_reach_the_provider():
    url, auth = _wire(Agent(model=MODEL, base_url=TOGETHER, api_key="sk-test"))
    assert url.rstrip("/") == TOGETHER
    assert auth == "Bearer sk-test"


def test_backend_is_the_same_field_as_base_url():
    """The alias must be exactly an alias — not a second, subtly different
    code path — and must round-trip both ways."""

    by_backend = Agent(model=MODEL, backend=TOGETHER, api_key="sk-test")
    by_base_url = Agent(model=MODEL, base_url=TOGETHER, api_key="sk-test")
    assert _wire(by_backend) == _wire(by_base_url)
    assert by_backend.base_url == by_base_url.backend == TOGETHER


def test_conflicting_backend_and_base_url_raises():
    """Silently preferring one would send requests to a URL the caller can see
    they didn't ask for — the exact class of bug this alias exists to end."""

    with pytest.raises(ValueError, match="only one"):
        Agent(model=MODEL, backend=TOGETHER, base_url="https://elsewhere/v1")


def test_matching_backend_and_base_url_is_fine():
    agent = Agent(model=MODEL, backend=TOGETHER, base_url=TOGETHER)
    assert agent.backend == TOGETHER


def test_explicit_api_key_beats_the_environment(monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_API_KEY", "env-key")
    _, auth = _wire(Agent(model=MODEL, base_url=TOGETHER, api_key="explicit-key"))
    assert auth == "Bearer explicit-key"


def test_no_api_key_still_falls_back_to_the_environment(monkeypatch):
    """The option is additive: leaving it None must not break the env chain
    that every existing caller relies on."""

    monkeypatch.setenv("MANTIS_AGENT_API_KEY", "env-key")
    _, auth = _wire(Agent(model=MODEL, base_url=TOGETHER))
    assert auth == "Bearer env-key"


def test_empty_api_key_means_send_no_auth(monkeypatch):
    """``""`` is meaningful, distinct from None: some backends authenticate
    with their own headers and a stray Bearer breaks them."""

    monkeypatch.setenv("MANTIS_AGENT_API_KEY", "env-key")
    _, auth = _wire(Agent(model=MODEL, base_url=TOGETHER, api_key=""))
    assert auth is None


def test_api_key_is_not_in_repr():
    """A logged agent must not leak the credential."""

    assert "sk-secret" not in repr(Agent(model=MODEL, base_url=TOGETHER, api_key="sk-secret"))


def test_api_key_reaches_the_anthropic_passthrough():
    """Anthropic authenticates with x-api-key, not Bearer — an explicit key
    has to land in the right header."""

    agent = Agent(model="claude-opus-5", backend="anthropic", api_key="sk-ant-explicit")
    headers = dict(agent.provider.client.headers)
    assert headers.get("x-api-key") == "sk-ant-explicit"


# ---------------------------------------------------------------------------
# query() — both option shapes
# ---------------------------------------------------------------------------


def test_dict_options_wire_shape():
    agent = _agent_from_options(
        {"model": MODEL, "base_url": TOGETHER, "api_key": "sk-dict"}
    )
    assert _wire(agent) == (TOGETHER + "/", "Bearer sk-dict")


def test_dict_options_do_not_leak_into_extra():
    """The regression itself: these keys used to be swept into ``extra``,
    where nothing read them."""

    agent = _agent_from_options(
        {"model": MODEL, "base_url": TOGETHER, "api_key": "sk-dict"}
    )
    assert not (agent.extra or {}).get("api_key")
    assert not (agent.extra or {}).get("base_url")


def test_typed_options_carry_both_fields():
    opts = MantisAgentOptions(model=MODEL, base_url=TOGETHER, api_key="sk-typed")
    wire = opts.to_query_options()
    assert wire["base_url"] == TOGETHER
    assert wire["api_key"] == "sk-typed"
    assert "sk-typed" not in repr(opts)


def test_typed_options_reach_the_provider():
    agent = _build_compat_agent(
        MantisAgentOptions(model=MODEL, base_url=TOGETHER, api_key="sk-typed")
        .to_query_options()
    )
    assert _wire(agent) == (TOGETHER + "/", "Bearer sk-typed")


def test_typed_options_conflicting_urls_raise():
    opts = MantisAgentOptions(
        model=MODEL, backend=TOGETHER, base_url="https://elsewhere/v1"
    ).to_query_options()
    with pytest.raises(ValueError, match="only one"):
        _build_compat_agent(opts)


# ---------------------------------------------------------------------------
# fallback_model — implemented on Agent, but neither options path forwarded it
# ---------------------------------------------------------------------------


def test_fallback_model_reaches_the_agent_from_dict_options():
    """``Agent`` implements the retry-on-a-second-model path; the option that
    configures it used to land in ``Agent.extra``, where nothing read it."""

    agent = _agent_from_options(
        {"model": "mock", "backend": "mock", "fallback_model": "backup-model"}
    )
    assert agent.fallback_model == "backup-model"


def test_fallback_model_reaches_the_agent_from_typed_options():
    agent = _build_compat_agent(
        MantisAgentOptions(
            model="mock", backend="mock", fallback_model="backup-model"
        ).to_query_options()
    )
    assert agent.fallback_model == "backup-model"


def test_fallback_model_unset_stays_none():
    agent = _build_compat_agent(
        MantisAgentOptions(model="mock", backend="mock").to_query_options()
    )
    assert agent.fallback_model is None
