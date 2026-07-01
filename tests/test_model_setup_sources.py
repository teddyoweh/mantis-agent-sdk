"""`mantis setup` must cover every model source — local, hosted API, free
hosted, self-host, and Anthropic/Claude — and route each correctly. These lock
in the wiring the /loop built so a regression can't silently drop a source.
"""

from __future__ import annotations

import mantis_agent.catalog as catalog
from mantis_agent.setup_wizard import (
    FREE_PROVIDER_IDS,
    _arrow_select,
    _ping_chat_model,
    _probe_openai_models,
)


# -- Anthropic / Claude ------------------------------------------------------


def test_anthropic_is_a_catalog_provider() -> None:
    prov = catalog.BY_ID.get("anthropic")
    assert prov is not None
    assert prov.base_url == "https://api.anthropic.com/v1"
    assert prov.api_key_env == "ANTHROPIC_API_KEY"


def test_claude_models_route_to_anthropic() -> None:
    for m in ("claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"):
        prov = catalog.provider_for_model(m)
        assert prov is not None and prov.id == "anthropic", m


def test_anthropic_uses_x_api_key_not_bearer() -> None:
    anth = catalog.BY_ID["anthropic"]
    h = catalog._models_headers(anth, "sk-secret")
    assert h["x-api-key"] == "sk-secret"
    assert "anthropic-version" in h
    assert "Authorization" not in h


def test_openai_compat_providers_use_bearer() -> None:
    h = catalog._models_headers(catalog.BY_ID["openai"], "sk-secret")
    assert h == {"Authorization": "Bearer sk-secret"}


def test_no_key_means_no_auth_headers() -> None:
    assert catalog._models_headers(catalog.BY_ID["anthropic"], None) == {}


# -- Free hosted filter ------------------------------------------------------


def test_free_provider_ids_are_all_real_catalog_ids() -> None:
    for pid in FREE_PROVIDER_IDS:
        assert pid in catalog.BY_ID, pid


# -- Self-host probe ---------------------------------------------------------


def test_selfhost_probe_unreachable_returns_none() -> None:
    # A closed port must degrade to None (→ manual model entry), never raise.
    assert _probe_openai_models("http://127.0.0.1:59999/v1", "") is None


# -- Model ping (validate-before-save) ---------------------------------------


def test_grouped_provider_models_enabled_before_disabled() -> None:
    # The unified /models picker relies on this ordering: every enabled provider
    # group must come before every disabled one (active/usable models on top).
    groups = catalog.grouped_provider_models()
    seen_disabled = False
    for g in groups:
        if not g["enabled"]:
            seen_disabled = True
        else:
            assert not seen_disabled, "an enabled group appeared after a disabled one"


def test_grouped_provider_models_are_real_providers_with_lists() -> None:
    for g in catalog.grouped_provider_models():
        assert g["provider_id"] in catalog.BY_ID
        assert isinstance(g["models"], list)
        assert "enabled" in g


def test_model_ping_unreachable_does_not_block_save() -> None:
    # A network flake must NOT block a save — returns (True, "") so setup is
    # usable offline / behind a proxy. (A real 4xx from the model is separate.)
    ok, _detail = _ping_chat_model("http://127.0.0.1:59999/v1", "some-model", "")
    assert ok is True


# -- Arrow selector ----------------------------------------------------------


def test_arrow_select_non_tty_returns_sentinel() -> None:
    # Under pytest stdin/stdout aren't TTYs — the selector must return -1 so
    # callers fall back to numeric input() instead of hanging on app.run().
    assert _arrow_select("pick", [("a", "x"), ("b", "y")]) == -1


class _NullConsole:
    def print(self, *a: object, **k: object) -> None:  # noqa: D102
        pass


def test_pick_model_id_numeric_fallback(monkeypatch) -> None:
    from mantis_agent import setup_wizard as sw
    monkeypatch.setattr("builtins.input", lambda *a: "2")
    assert sw._pick_model_id(_NullConsole(), ["m-a", "m-b", "m-c"]) == "m-b"


def test_pick_model_id_enter_takes_first(monkeypatch) -> None:
    from mantis_agent import setup_wizard as sw
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert sw._pick_model_id(_NullConsole(), ["first", "second"]) == "first"


def test_pick_model_id_accepts_typed_exact_id(monkeypatch) -> None:
    from mantis_agent import setup_wizard as sw
    monkeypatch.setattr("builtins.input", lambda *a: "org/my-custom-model")
    assert sw._pick_model_id(_NullConsole(), ["m-a"]) == "org/my-custom-model"
