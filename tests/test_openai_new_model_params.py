"""OpenAI's GPT-5 / o-series reject the legacy ``max_tokens`` (they require
``max_completion_tokens``) and only accept the default temperature. mantis's
openai_compat provider handles this in ``_build_payload`` — these lock that in so
a user picking gpt-5.x / o1 / o3 doesn't hit a 400 on the first turn.
"""

from __future__ import annotations

from mantis_agent.providers.openai_compat import OpenAICompatProvider
from mantis_agent.types import UserMessage


def _payload(model: str) -> dict:
    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    return p._build_payload(
        model=model,
        messages=[UserMessage(content="hi")],
        system=None,
        tools=None,
        max_tokens=100,
        temperature=0.7,
        extra=None,
        path="A",
    )


def test_gpt5_and_o_series_use_max_completion_tokens_no_temperature() -> None:
    for model in ("gpt-5.4", "gpt-5.5", "o1", "o1-mini", "o3", "o4-mini"):
        pl = _payload(model)
        assert pl.get("max_completion_tokens") == 100, model
        assert "max_tokens" not in pl, model
        assert "temperature" not in pl, model  # these models reject non-default temp


def test_legacy_openai_models_keep_max_tokens_and_temperature() -> None:
    pl = _payload("gpt-4o")
    assert pl.get("max_tokens") == 100
    assert "max_completion_tokens" not in pl
    assert pl.get("temperature") == 0.7
