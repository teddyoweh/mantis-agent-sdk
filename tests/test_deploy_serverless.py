"""Per-token offers: Fireworks serverless and Modal Shared Endpoints, offered
before a GPU in the Deploy sheet. Mocked control planes, no network."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mantis_agent.deploy import serverless as sl
from mantis_agent.deploy.base import DeployError
from mantis_agent.deploy.providers import fireworks_dedicated as fw

pytestmark = pytest.mark.anyio

SERVERLESS = [
    {"name": "accounts/fireworks/models/glm-5p3-flash", "displayName": "GLM 5.3 Flash",
     "huggingFaceUrl": "https://huggingface.co/zai-org/GLM-5.3-Flash", "contextLength": 1048576, "supportsTools": True},
    # two slugs, one repo — only qwen3p8-max has a serverless price
    {"name": "accounts/fireworks/models/qwen3p8-2p4t-a95b", "displayName": "Qwen3.8-2.4T-A95B",
     "huggingFaceUrl": "https://huggingface.co/Qwen/Qwen3.8-2.4T-A95B"},
    {"name": "accounts/fireworks/models/qwen3p8-max", "displayName": "Qwen 3.8 Max",
     "huggingFaceUrl": "https://huggingface.co/Qwen/Qwen3.8-2.4T-A95B", "contextLength": 0},
]
SERVERLESS_URL = fw.API_BASE + "/v1/accounts/fireworks/models"
MODELS_URL = sl.MODAL_SHARED_BASE + "/models"
HOST = "glm-flash-x1.us-west.modal.direct"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    for k in ("FIREWORKS_API_KEY", "MODAL_PROXY_TOKEN_ID", "MODAL_PROXY_TOKEN_SECRET",
              "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "MANTIS_AGENT_API_KEY",
              "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY"):
        # setenv first so teardown restores the original state even when the
        # code under test writes os.environ itself (store.save_credentials does)
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k)
    monkeypatch.setattr(fw, "_serverless_cache", None)
    return monkeypatch


def _serverless(router):
    return router.get(SERVERLESS_URL).mock(return_value=httpx.Response(200, json={"models": SERVERLESS}))


# ---------------------------------------------------------------------------
# Fireworks
# ---------------------------------------------------------------------------


async def test_fireworks_offer_comes_from_the_live_serverless_list(env):
    env.setenv("FIREWORKS_API_KEY", "fw_test")
    with respx.mock(assert_all_called=True) as router:
        route = _serverless(router)
        o = await sl.fireworks_offer("zai-org/GLM-5.3-Flash")
        # cached: a second sheet doesn't ask again
        await sl.fireworks_offer("zai-org/GLM-5.3-Flash")
    assert route.call_count == 1
    q = route.calls[0].request.url.params
    assert q["filter"] == "supports_serverless=true"
    assert route.calls[0].request.headers["authorization"] == "Bearer fw_test"
    assert o is not None and o.ready and o.auth_ready and o.kind == "serverless"
    assert o.model == "accounts/fireworks/models/glm-5p3-flash"
    assert o.backend == "https://api.fireworks.ai/inference/v1"
    # the budget table's exact row, not glm-5p3's by prefix
    assert (o.price_in, o.price_out, o.price_cached) == (0.15, 0.50, 0.03)
    assert o.context == 1048576


async def test_two_slugs_on_one_repo_pick_the_priced_one(env):
    env.setenv("FIREWORKS_API_KEY", "fw_test")
    with respx.mock() as router:
        _serverless(router)
        o = await sl.fireworks_offer("Qwen/Qwen3.8-2.4T-A95B")
    assert o.model == "accounts/fireworks/models/qwen3p8-max" and o.price_out == 6.00
    assert o.context is None  # a 0 from the API is not a context length


async def test_fireworks_needs_a_key_and_a_serverless_model(env):
    with respx.mock(assert_all_called=False) as router:
        route = _serverless(router)
        assert await sl.fireworks_offer("zai-org/GLM-5.3-Flash") is None
        assert route.call_count == 0  # no key, no call
        env.setenv("FIREWORKS_API_KEY", "fw_test")
        assert await sl.fireworks_offer("meta-llama/Llama-3.1-8B-Instruct") is None


async def test_fireworks_key_saved_in_the_model_catalogue_counts(env):
    from mantis_agent import catalog

    catalog.set_key("fireworks", "fw_from_catalog")
    with respx.mock() as router:
        route = _serverless(router)
        o = await sl.fireworks_offer("zai-org/GLM-5.3-Flash")
    assert o is not None
    assert route.calls[0].request.headers["authorization"] == "Bearer fw_from_catalog"


CEREBRAS = {"data": [
    {"id": "gpt-oss-120b", "name": "OpenAI GPT OSS", "hugging_face_id": "openai/gpt-oss-120b",
     "pricing": {"prompt": "0.00000035", "completion": "0.00000075"},
     "limits": {"max_context_length": 131072}, "capabilities": {"tools": True}},
    {"id": "qwen-3.8-27b", "name": "Qwen 3.8 27B", "hugging_face_id": "Qwen/Qwen3.8-27B",
     "pricing": {"prompt": "0.00000099", "completion": "0.00000149"},
     "limits": {"max_context_length": 65536}, "capabilities": {"tools": True}},
]}
OPENROUTER = {"data": [
    {"id": "z-ai/glm-5.3-flash", "name": "Z.ai: GLM 5.3 Flash", "hugging_face_id": "zai-org/GLM-5.3-Flash",
     "pricing": {"prompt": "0.0000000052", "completion": "0.0000001985"}, "context_length": 1048576,
     "supported_parameters": ["tools", "temperature"]},
    {"id": "z-ai/glm-5.3-flash:free", "hugging_face_id": "zai-org/GLM-5.3-Flash",
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "openai/gpt-oss-120b", "name": "OpenAI: gpt-oss-120b", "hugging_face_id": "openai/gpt-oss-120b",
     "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}, "supported_parameters": ["tools"]},
    {"id": "acme/only-on-openrouter", "hugging_face_id": "acme/Only", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "openai/gpt-6-astra", "pricing": {"prompt": "0.00001", "completion": "0.00005"}},  # closed: no repo
]}


def _public(router, cerebras=CEREBRAS, openrouter=OPENROUTER):
    return (router.get(sl.CEREBRAS_PUBLIC_MODELS).mock(return_value=httpx.Response(200, json=cerebras)),
            router.get(sl.OPENROUTER_MODELS).mock(return_value=httpx.Response(200, json=openrouter)))


async def test_catalog_is_one_row_per_model_with_every_provider_that_serves_it(env):
    from mantis_agent import catalog

    env.setattr(sl, "_public_cache", {})
    env.setenv("FIREWORKS_API_KEY", "fw_test")
    catalog.set_key("cerebras", "csk-test")
    rows = SERVERLESS + [
        {"name": "accounts/fireworks/models/qwen3-embedding-8b", "displayName": "Qwen3 Embedding 8B",
         "huggingFaceUrl": "https://huggingface.co/Qwen/Qwen3-Embedding-8B", "supportsTools": False},
        {"name": "accounts/fireworks/models/gpt-oss-120b", "displayName": "OpenAI gpt-oss-120b", "supportsTools": True,
         "huggingFaceUrl": "https://huggingface.co/openai/gpt-oss-120b", "contextLength": 131072},
        {"name": "accounts/fireworks/models/ember-1", "displayName": "Ember-1", "supportsTools": True},
    ]
    with respx.mock(assert_all_called=False) as router:
        router.get(SERVERLESS_URL).mock(return_value=httpx.Response(200, json={"models": rows}))
        _cer, orouter = _public(router)
        out = await sl.catalog()
    by = {r.name: r for r in out}
    # rows: models a connected provider serves; embeddings aren't chat models
    assert set(by) == {"GLM 5.3 Flash", "OpenAI gpt-oss-120b", "Qwen 3.8 Max", "Qwen 3.8 27B", "Ember-1"}
    gpt = by["OpenAI gpt-oss-120b"]
    # only providers you have: no OpenRouter key, no OpenRouter route — and its
    # list was never fetched
    assert [r.provider for r in gpt.routes] == ["fireworks", "cerebras"]
    assert all(r.connected for r in gpt.routes)
    assert (gpt.routes[1].price_in, gpt.routes[1].price_out) == (0.35, 0.75)
    assert gpt.routes[1].backend == "https://api.cerebras.ai/v1" and gpt.routes[1].model == "gpt-oss-120b"
    assert orouter.call_count == 0
    # a Modal shared endpoint nobody has made yet is not a route either
    assert [r.provider for r in by["GLM 5.3 Flash"].routes] == ["fireworks"]
    # two Fireworks slugs on one repo: the priced one, and ITS name
    assert by["Qwen 3.8 Max"].routes[0].model == "accounts/fireworks/models/qwen3p8-max"
    assert by["Qwen 3.8 27B"].routes[0].provider == "cerebras"
    assert by["Ember-1"].hf_id == "" and by["Ember-1"].tools is True
    # sorted by the best route's blended price
    assert out[0].name == "GLM 5.3 Flash"


async def test_a_connected_openrouter_joins_the_rows_it_serves(env):
    from mantis_agent import catalog

    env.setattr(sl, "_public_cache", {})
    env.setenv("FIREWORKS_API_KEY", "fw_test")
    catalog.set_key("openrouter", "sk-or-test")
    with respx.mock(assert_all_called=False) as router:
        _serverless(router)
        cer, _or = _public(router)
        out = await sl.catalog()
    assert cer.call_count == 0  # no Cerebras key here
    glm = next(r for r in out if r.name == "GLM 5.3 Flash")
    # cheapest first; ":free" / ":batch" are not routes
    assert [r.provider for r in glm.routes] == ["openrouter", "fireworks"]
    assert glm.routes[0].model == "z-ai/glm-5.3-flash" and glm.routes[0].price_out == 0.1985
    # a model only OpenRouter serves is a row of its own now that you have it
    assert any(r.hf_id == "openai/gpt-oss-120b" for r in out)


async def test_catalog_is_empty_with_nothing_connected_and_survives_an_outage(env):
    env.setattr(sl, "_public_cache", {})
    with respx.mock(assert_all_called=False) as router:
        route = _serverless(router)
        _public(router)
        assert await sl.catalog() == []
        assert route.call_count == 0  # no Fireworks key, no Fireworks call
    env.setattr(sl, "_public_cache", {})
    env.setenv("FIREWORKS_API_KEY", "fw_test")
    env.setattr(fw, "_serverless_cache", None)
    from mantis_agent import catalog

    catalog.set_key("cerebras", "csk-test")
    catalog.set_key("openrouter", "sk-or-test")
    with respx.mock() as router:
        _serverless(router)
        router.get(sl.CEREBRAS_PUBLIC_MODELS).mock(side_effect=httpx.ConnectError("down"))
        router.get(sl.OPENROUTER_MODELS).mock(return_value=httpx.Response(503))
        out = await sl.catalog()
    assert {r.name for r in out} == {"GLM 5.3 Flash", "Qwen 3.8 Max"}


async def test_the_sheet_offers_every_provider_for_its_model(env):
    from mantis_agent import catalog

    env.setattr(sl, "_public_cache", {})
    catalog.set_key("cerebras", "csk-test")
    with respx.mock(assert_all_called=False) as router:
        _public(router)
        out = await sl.offers("openai/gpt-oss-120b")
    # the providers you have — OpenRouter isn't one
    assert [(o.provider, o.ready, o.kind) for o in out] == [("cerebras", True, "api")]


def test_use_a_connected_catalogue_provider(env):
    from mantis_agent import catalog

    with pytest.raises(DeployError, match="isn't connected"):
        sl.use_offer("openrouter", hf_model="zai-org/GLM-5.3-Flash", model="z-ai/glm-5.3-flash")
    catalog.set_key("cerebras", "csk-test")
    r = sl.use_offer("cerebras", hf_model="openai/gpt-oss-120b", model="gpt-oss-120b")
    assert r == {"model": "gpt-oss-120b", "backend": "https://api.cerebras.ai/v1", "provider": "cerebras"}
    assert catalog.get_last_model() == {"model": "gpt-oss-120b", "backend": "https://api.cerebras.ai/v1"}


def test_the_terminal_restores_the_provider_the_backend_names(env, monkeypatch):
    """``openai/gpt-oss-120b`` is in Groq's list AND OpenRouter's; the saved
    backend says which one was chosen, so it wins over the id heuristic."""
    from mantis_agent import catalog
    from mantis_agent.tui import MantisTUI

    catalog.set_key("openrouter", "sk-or-test")
    catalog.set_key("groq", "gsk_test")
    catalog.set_last_model("openai/gpt-oss-120b", "https://openrouter.ai/api/v1")
    env.delenv("MANTIS_AGENT_MODEL", raising=False)
    tui = MantisTUI(model="qwen2.5-7b-instruct", backend="http://localhost:11434/v1", api_key=None,  # the default
                    system=None, max_tokens=1024, temperature=None, max_turns=5)
    tui._restore_last_model()
    assert (tui.model, tui.backend, tui.api_key) == ("openai/gpt-oss-120b", "https://openrouter.ai/api/v1", "sk-or-test")


# ---------------------------------------------------------------------------
# Modal
# ---------------------------------------------------------------------------


def test_modal_offer_says_what_is_missing_until_it_is_ready(env):
    o = sl.modal_offer("zai-org/glm-5.3-flash")  # case doesn't matter
    assert o.kind == "shared" and not o.ready and not o.auth_ready and o.model == ""
    assert "Shared Endpoint" in o.needs
    assert o.setup_url == "https://modal.com/endpoints?model=zai-org%2FGLM-5.3-Flash&type=shared"
    assert (o.price_in, o.price_out, o.price_cached) == (0.45, 1.50, 0.09)
    assert sl.modal_offer("openai/gpt-oss-120b") is None  # dedicated-only on Modal
    env.setenv("MODAL_PROXY_TOKEN_ID", "wk-abc")
    env.setenv("MODAL_PROXY_TOKEN_SECRET", "ws-def")
    o = sl.modal_offer("zai-org/GLM-5.3-Flash")
    assert o.auth_ready and not o.ready


async def test_offers_puts_ready_first_and_one_outage_hides_nothing(env):
    env.setenv("FIREWORKS_API_KEY", "fw_test")
    with respx.mock() as router:
        _serverless(router)
        out = await sl.offers("zai-org/GLM-5.3-Flash")
    assert [o.provider for o in out] == ["fireworks", "modal"]
    env.setattr(fw, "_serverless_cache", None)
    with respx.mock() as router:
        router.get(SERVERLESS_URL).mock(return_value=httpx.Response(500, json={"error": "down"}))
        out = await sl.offers("zai-org/GLM-5.3-Flash")
    assert [o.provider for o in out] == ["modal"]


# ---------------------------------------------------------------------------
# use_offer — switching, never creating
# ---------------------------------------------------------------------------


def test_use_fireworks_switches_mantis_to_the_serverless_model(env):
    from mantis_agent import catalog

    with pytest.raises(DeployError):
        sl.use_offer("fireworks", hf_model="zai-org/GLM-5.3-Flash",
                     model="accounts/fireworks/models/glm-5p3-flash")  # no key
    env.setenv("FIREWORKS_API_KEY", "fw_test")
    with pytest.raises(DeployError):
        sl.use_offer("fireworks", hf_model="x/y", model="zai-org/GLM-5.3-Flash")
    r = sl.use_offer("fireworks", hf_model="zai-org/GLM-5.3-Flash", model="accounts/fireworks/models/glm-5p3-flash")
    assert r == {"model": "accounts/fireworks/models/glm-5p3-flash",
                 "backend": "https://api.fireworks.ai/inference/v1", "provider": "fireworks"}
    assert catalog.get_last_model()["backend"] == "https://api.fireworks.ai/inference/v1"
    assert catalog.get_recent_models()[0] == "accounts/fireworks/models/glm-5p3-flash"
    # the terminal resolves the model to Fireworks and its key
    assert catalog.provider_for_model(r["model"]).id == "fireworks"


@pytest.mark.parametrize("bad", ["not a host", "inference.us-west.modal.direct", "foo.modal.run", ""])
def test_use_modal_rejects_what_is_not_an_endpoint_hostname(env, bad):
    env.setenv("MODAL_PROXY_TOKEN_ID", "wk-abc")
    env.setenv("MODAL_PROXY_TOKEN_SECRET", "ws-def")
    with pytest.raises(DeployError):
        sl.use_offer("modal", hf_model="zai-org/GLM-5.3-Flash", model=bad)


def test_use_modal_saves_the_token_checks_the_gateway_and_remembers_the_host(env):
    from mantis_agent import catalog

    with pytest.raises(DeployError, match="proxy token"):
        sl.use_offer("modal", hf_model="zai-org/GLM-5.3-Flash", model=HOST)
    with pytest.raises(DeployError, match="both halves"):
        sl.use_offer("modal", hf_model="zai-org/GLM-5.3-Flash", model=HOST, token_id="wk-abc")
    with respx.mock() as router:
        route = router.get(MODELS_URL).mock(return_value=httpx.Response(
            200, json={"object": "list", "data": [{"id": HOST}, {"id": "other.us-west.modal.direct"}]}))
        r = sl.use_offer("modal", hf_model="zai-org/GLM-5.3-Flash", model="https://" + HOST.upper() + "/",
                         token_id="wk-abc", token_secret="ws-def")
    assert route.calls[0].request.headers["authorization"] == "Bearer wk-abc.ws-def"
    assert r["model"] == HOST and r["backend"] == sl.MODAL_SHARED_BASE and "warning" not in r
    assert catalog.get_last_model() == {"model": HOST, "backend": sl.MODAL_SHARED_BASE}
    saved = json.loads((catalog._store_path().parent / "settings.json").read_text())["env"]
    assert saved["MODAL_PROXY_TOKEN_ID"] == "wk-abc" and saved["MODAL_PROXY_TOKEN_SECRET"] == "ws-def"
    # next time the sheet opens, the Modal row is one click
    o = sl.modal_offer("zai-org/GLM-5.3-Flash")
    assert o.ready and o.model == HOST and o.needs == ""


def test_use_modal_answers_what_the_gateway_says(env):
    env.setenv("MODAL_PROXY_TOKEN_ID", "wk-abc")
    env.setenv("MODAL_PROXY_TOKEN_SECRET", "ws-def")
    with respx.mock() as router:
        router.get(MODELS_URL).mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))
        with pytest.raises(DeployError, match="turned the proxy token away"):
            sl.use_offer("modal", hf_model="zai-org/GLM-5.3-Flash", model=HOST)
    with respx.mock() as router:
        router.get(MODELS_URL).mock(return_value=httpx.Response(200, json={"data": [{"id": "else.us-west.modal.direct"}]}))
        with pytest.raises(DeployError) as ei:
            sl.use_offer("modal", hf_model="zai-org/GLM-5.3-Flash", model=HOST)
        assert "else.us-west.modal.direct" in (ei.value.hint or "")
    # a network that resets *.modal.direct can't check — it switches and says so
    with respx.mock() as router:
        router.get(MODELS_URL).mock(side_effect=httpx.ConnectError("reset"))
        r = sl.use_offer("modal", hf_model="zai-org/GLM-5.3-Flash", model=HOST)
    assert r["model"] == HOST and "modal.direct" in r["warning"]


def test_unknown_provider_is_refused(env):
    with pytest.raises(DeployError):
        sl.use_offer("runpod", hf_model="a/b", model="x")


# ---------------------------------------------------------------------------
# what the rest of mantis does with it
# ---------------------------------------------------------------------------


def test_the_cost_readout_bills_fireworks_serverless_at_its_rate():
    from mantis_agent.budget import lookup_pricing

    pr = lookup_pricing("accounts/fireworks/models/glm-5p3-flash", "fireworks")
    assert (pr.prompt_per_million, pr.completion_per_million, pr.cache_read_per_million) == (0.15, 0.50, 0.03)
    assert lookup_pricing("accounts/fireworks/models/glm-5p3", "fireworks").completion_per_million == 4.40


def test_the_shared_gateway_routes_to_the_modal_adapter_and_flash_urls_do_not():
    from mantis_agent.providers.base import detect_provider

    assert detect_provider("https://inference.us-west.modal.direct/v1") == "modal"
    assert detect_provider("https://inference.eu-west.modal.direct") == "modal"
    # a dedicated Flash endpoint keeps the auth its deployment record carries
    assert detect_provider("https://ws--mantis-glm.us-east.modal.direct/v1") == "openai_compat"
    assert detect_provider("https://ws--app.modal.run/v1") == "modal"


def test_the_shared_gateway_sends_the_proxy_token_over_a_stale_key(monkeypatch):
    from mantis_agent.providers.modal_provider import ModalProvider

    monkeypatch.setenv("MODAL_PROXY_TOKEN_ID", "wk-abc")
    monkeypatch.setenv("MODAL_PROXY_TOKEN_SECRET", "ws-def")
    # the terminal hands non-catalogue URLs MANTIS_AGENT_API_KEY — here an old
    # self-hosted endpoint's key, which the gateway would reject
    p = ModalProvider(base_url=sl.MODAL_SHARED_BASE, api_key="sk-old-selfhost-key")
    assert p._inner.client.headers["authorization"] == "Bearer wk-abc.ws-def"
    # an explicit joined token is kept as given
    p = ModalProvider(base_url=sl.MODAL_SHARED_BASE, api_key="wk-zzz.ws-yyy")
    assert p._inner.client.headers["authorization"] == "Bearer wk-zzz.ws-yyy"
    # a *.modal.run web endpoint is untouched: its bearer is the vLLM key
    p = ModalProvider(base_url="https://ws--app.modal.run/v1", api_key="vllm-key")
    assert p._inner.client.headers["authorization"] == "Bearer vllm-key"
