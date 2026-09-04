"""Five-family model UX: the listing groups by family with auth state and the
exact ``/enable`` line; ``/model <id>`` routes each family natively and names
where it went; a locked provider prints the enable command, not a 401 later.

Everything is coded against the catalog generically — ``provider_rows`` walks
``catalog.CATALOG`` — so a provider added there (xAI/Grok) appears here without
a code change, and a family whose provider has not landed still has a header.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from rich.console import Console

from mantis_agent import catalog
from mantis_agent.tui import (
    FAMILIES,
    FAMILY_ORDER,
    MantisTUI,
    enable_hint,
    family_of_provider,
    family_tag,
    missing_key_markup,
    model_family,
    provider_auth,
    provider_rows,
    switch_note,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _tui(model: str = "gpt-5.6", backend: str = "https://api.openai.com/v1",
         api_key: str | None = "sk-x", width: int = 80) -> MantisTUI:
    t = MantisTUI(model=model, backend=backend, api_key=api_key, system=None,
                  max_tokens=1, temperature=None, max_turns=1)
    t.console = Console(width=width, record=True, force_terminal=False, color_system=None)
    return t


@pytest.fixture(autouse=True)
def _clean_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "saved_key", lambda pid: None)
    for p in catalog.CATALOG:
        for var in (p.api_key_env, *p.key_env_aliases):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


# -- families ---------------------------------------------------------------------


def test_the_five_families_plus_local_and_selfhost_are_named() -> None:
    assert [FAMILIES[f][1] for f in FAMILY_ORDER] == [
        "Local", "OpenAI", "Claude", "Gemini", "Grok", "Hosted OSS", "Self-host"]
    glyphs = [FAMILIES[f][0] for f in FAMILY_ORDER]
    assert len(set(glyphs)) == len(glyphs)   # each family has its own glyph


@pytest.mark.parametrize(("pid", "fam"), [
    ("openai", "openai"), ("anthropic", "anthropic"), ("gemini", "gemini"),
    ("xai", "xai"), ("groq", "oss"), ("deepseek", "oss"), ("together", "oss"),
    ("nope", "oss"),
])
def test_family_of_provider(pid: str, fam: str) -> None:
    assert family_of_provider(pid) == fam


def test_model_family_follows_the_backend_url() -> None:
    assert model_family("claude-opus-5", "https://api.anthropic.com/v1")["name"] == "Claude"
    assert model_family("gpt-5", "https://api.openai.com/v1")["name"] == "OpenAI"
    assert model_family("gemini-2.5-pro", "https://generativelanguage.googleapis.com/v1beta/openai")["name"] == "Gemini"
    assert model_family("openai/gpt-oss-120b", "https://api.groq.com/openai/v1")["name"] == "Hosted OSS"
    assert model_family("qwen3:8b", "http://localhost:11434/v1")["id"] == "local"
    # A Claude id through your own proxy is self-hosted for every purpose the
    # footer cares about — auth, cost, who to blame for a 401.
    sh = model_family("claude-opus-5", "http://gpu-box:8000/v1")
    assert sh["id"] == "selfhost" and sh["provider_label"] == "gpu-box:8000/v1"


def test_model_family_falls_back_to_the_name_prefix() -> None:
    assert model_family("gemini-2.5-pro", "")["name"] == "Gemini"
    assert model_family("claude-sonnet-5", None)["name"] == "Claude"
    assert model_family("gpt-5.6", None)["name"] == "OpenAI"


def test_family_tag_is_glyph_and_name() -> None:
    assert family_tag("claude-sonnet-5", "https://api.anthropic.com/v1") == "✦ Claude"
    assert family_tag("qwen3:8b", "http://127.0.0.1:11434/v1") == "⌂ Local"


# -- provider rows (the generic /enable · /disable · /models data) ------------------


def test_provider_rows_cover_every_catalog_provider_in_family_order() -> None:
    rows = provider_rows()
    assert {r["id"] for r in rows} == {p.id for p in catalog.CATALOG}
    order = [FAMILY_ORDER.index(r["family"]) for r in rows]
    assert order == sorted(order)
    for r in rows:
        assert r["hint"].startswith(f"/enable {r['id']}")
        assert r["enabled"] is False and r["auth"] == ""


def test_provider_rows_report_auth_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat-1")
    monkeypatch.setattr(catalog, "saved_key", lambda pid: "gsk_1" if pid == "groq" else None)
    by = {r["id"]: r for r in provider_rows()}
    assert by["openai"]["enabled"] and by["openai"]["auth"] == "$OPENAI_API_KEY (env)"
    assert by["anthropic"]["enabled"] and by["anthropic"]["auth"] == "OAuth token"
    assert by["groq"]["enabled"] and by["groq"]["auth"] == "saved key"
    assert not by["gemini"]["enabled"]
    only = provider_rows(only_enabled=True)
    assert {r["id"] for r in only} == {"openai", "anthropic", "groq"}


def test_provider_auth_recognises_an_oauth_token_in_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-oat01-abc")
    assert provider_auth(catalog.BY_ID["anthropic"]) == "OAuth token"


def test_enable_hint_names_the_command_and_the_console() -> None:
    hint = enable_hint(catalog.BY_ID["anthropic"])
    assert hint.startswith("/enable anthropic")
    assert "console.anthropic.com" in hint


def test_missing_key_line_prints_the_exact_enable_command() -> None:
    c = Console(width=100, record=True, force_terminal=False, color_system=None)
    c.print(missing_key_markup("gemini-2.5-pro", catalog.BY_ID["gemini"]))
    out = c.export_text()
    assert "gemini-2.5-pro needs Gemini" in out
    assert "/enable gemini" in out
    assert "aistudio.google.com" in out


# -- the switch confirmation ----------------------------------------------------------


def test_switch_note_says_where_the_model_routes() -> None:
    assert switch_note("gpt-5.6", "https://api.openai.com/v1", "$OPENAI_API_KEY (env)") == \
        "model → gpt-5.6 · ◯ OpenAI · via api.openai.com · $OPENAI_API_KEY (env)"
    assert switch_note("claude-opus-5", "https://api.anthropic.com/v1", "OAuth token") == \
        "model → claude-opus-5 · ✦ Claude (Claude (Anthropic)) · via api.anthropic.com · OAuth token"
    note = switch_note("gemini-2.5-pro", "https://generativelanguage.googleapis.com/v1beta/openai", "saved key")
    assert note.startswith("model → gemini-2.5-pro · ◆ Gemini")
    assert switch_note("qwen3:8b", "http://localhost:11434/v1", "local") == \
        "model → qwen3:8b · ⌂ Local (Ollama) · via localhost:11434 · local"


# -- /models list: family-grouped listing --------------------------------------------


def _listing(monkeypatch: pytest.MonkeyPatch, width: int = 80, **env: str) -> str:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    t = _tui(width=width)
    monkeypatch.setattr(t, "_available_models", lambda: (["qwen3:8b"], True))
    t._show_models()
    return t.console.export_text()


def test_models_listing_groups_by_family_with_auth_and_enable_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _listing(monkeypatch, OPENAI_API_KEY="sk-live")
    for head in ("⌂ Local", "◯ OpenAI", "✦ Claude", "◆ Gemini", "✕ Grok", "◈ Hosted OSS", "⚙ Self-host"):
        assert head in out, head
    pos = [out.index(h) for h in ("⌂ Local", "◯ OpenAI", "✦ Claude", "◆ Gemini", "✕ Grok",
                                   "◈ Hosted OSS", "⚙ Self-host")]
    assert pos == sorted(pos)                         # family order is fixed
    assert "$OPENAI_API_KEY (env)" in out             # auth state per group
    assert "/enable anthropic" in out and "/enable gemini" in out
    assert "qwen3:8b (installed)" in out
    assert "/model <id>" in out


def test_models_listing_fits_80_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _listing(monkeypatch, 80)
    assert max(len(ln) for ln in out.splitlines()) <= 80


def test_models_listing_marks_the_current_model(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _listing(monkeypatch, OPENAI_API_KEY="sk-live")
    assert re.search(r"›\s+gpt-5\.6\b", out)
    assert out.splitlines()[1].startswith("Models  ◯ gpt-5.6")


def test_models_listing_names_a_family_whose_provider_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "CATALOG", tuple(p for p in catalog.CATALOG if p.id != "xai"))
    out = _listing(monkeypatch)
    assert "✕ Grok" in out and "no provider in this catalog yet" in out


# -- /enable and /disable list every provider generically ----------------------------


def test_bare_enable_lists_every_provider_with_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-x")
    t = _tui()
    asyncio.run(t._handle_slash("/enable"))
    out = t.console.export_text()
    assert "usage: /enable <provider>" in out
    for p in catalog.CATALOG:
        assert re.search(rf"^\s+{re.escape(p.id)}\b", out, re.M), p.id
    assert "● $GEMINI_API_KEY (env)" in out
    assert "/enable anthropic" in out
    assert max(len(ln) for ln in out.splitlines()) <= 80      # cut, never wrapped


def test_bare_disable_lists_only_enabled_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-x")
    t = _tui()
    asyncio.run(t._handle_slash("/disable"))
    out = t.console.export_text()
    assert "gemini" in out and "anthropic" not in out


# -- /model <id> switches each family natively ---------------------------------------


@pytest.mark.parametrize(("query", "provider_id"), [
    ("gpt-5", "openai"),
    ("claude-opus-5", "anthropic"),
    ("gemini-2.5-pro", "gemini"),
    ("grok-4", "xai"),
])
def test_model_command_routes_each_family(monkeypatch: pytest.MonkeyPatch, query: str,
                                          provider_id: str) -> None:
    if provider_id not in catalog.BY_ID:
        pytest.skip(f"{provider_id} is not in this catalog yet")
    monkeypatch.setenv(catalog.BY_ID[provider_id].api_key_env, "k-" + provider_id)
    t = _tui(model="qwen3:8b", backend="http://localhost:11434/v1", api_key=None)
    monkeypatch.setattr(t, "_available_models", lambda: (["qwen3:8b"], True))
    got: list[Any] = []

    async def _activate(model: str, prov: Any) -> None:
        got.append((model, prov.id if prov else None))
    monkeypatch.setattr(t, "_activate", _activate)

    async def _no_picker() -> None:
        raise AssertionError("the picker must not open for a family prefix")
    monkeypatch.setattr(t, "_select_model", _no_picker)
    asyncio.run(t._handle_slash(f"/model {query}"))
    assert len(got) == 1
    model, pid = got[0]
    assert pid == provider_id
    assert model.startswith(query)               # gpt-5 → the OpenAI flagship gpt-5.x


def test_prefix_flagship_only_when_every_candidate_extends_the_query() -> None:
    from mantis_agent.tui import prefix_flagship

    assert prefix_flagship("gpt-5", ("gpt-5.6-sol", "gpt-5.6", "gpt-5.4")) == "gpt-5.6-sol"
    assert prefix_flagship("oss", ("openai/gpt-oss-120b", "gpt-oss-120b")) is None
    assert prefix_flagship("", ("a",)) is None


def test_model_command_on_a_locked_provider_prints_the_enable_command(monkeypatch: pytest.MonkeyPatch) -> None:
    t = _tui(model="qwen3:8b", backend="http://localhost:11434/v1", api_key=None)
    monkeypatch.setattr(t, "_available_models", lambda: (["qwen3:8b"], True))

    async def _cancel(*a: Any, **k: Any) -> None:
        return None
    monkeypatch.setattr(t, "_pick", _cancel)     # the how-to-run picker: cancelled
    asyncio.run(t._handle_slash("/model gemini-2.5-pro"))
    out = t.console.export_text()
    assert "needs Gemini" in out
    assert "/enable gemini" in out
    assert "aistudio.google.com" in out
    assert t.model == "qwen3:8b"                  # nothing switched


def test_apply_prints_the_routing_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
    t = _tui(model="qwen3:8b", backend="http://localhost:11434/v1", api_key=None)
    monkeypatch.setattr(catalog, "set_last_model", lambda *a, **k: None)
    monkeypatch.setattr(catalog, "push_recent_model", lambda *a, **k: None)
    monkeypatch.setattr(t, "_build_agent", lambda: None)
    asyncio.run(t._apply("gpt-5.6", "https://api.openai.com/v1", "sk-live", ""))
    out = t.console.export_text()
    assert "model → gpt-5.6 · ◯ OpenAI · via api.openai.com · $OPENAI_API_KEY (env)" in out
