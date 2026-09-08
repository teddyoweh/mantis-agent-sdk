"""Natural-language model search: query parsing, grouping, both tiers.

Everything here is deterministic — the Hub is respx'd, the agent tier runs on
the mock provider, and no test touches a live model. The rule tier is the one
that has to be good enough to ship on its own, so it carries most of the
pinning; the agent tier is pinned on the two things that actually matter — it
may only group ids the Hub returned, and any failure degrades to rules instead
of raising.
"""

from __future__ import annotations

import json

import anyio
import httpx
import pytest
import respx

from mantis_agent.deploy import smart_search as ss
from mantis_agent.deploy import store as _store
from mantis_agent.deploy.base import GpuSpec, register_provider

# Register the built-in adapters at IMPORT time. conftest snapshots
# DEPLOY_PROVIDERS before each test and restores it after, so whichever test is
# the first to trigger the lazy provider import would otherwise have its
# registrations rolled back — and the modules, already in sys.modules, never
# register again.
_store._import_builtin_providers()

HUB = "https://huggingface.co/api/models"


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    """No home, no configured model, no retry sleeps."""

    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    for var in ("MANTIS_AGENT_MODEL", "MANTIS_AGENT_BASE_URL", "HF_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    async def _fast(_s: float) -> None:
        return None

    monkeypatch.setattr("mantis_agent.deploy._http.sleep", _fast)
    yield


# ---------------------------------------------------------------------------
# a small, fixed Hub
# ---------------------------------------------------------------------------


def _row(mid, params_b, *, arch="Qwen3ForCausalLM", dtype="BF16", license="apache-2.0",
         downloads=1000, likes=10, updated="2025-06-01T00:00:00.000Z", gated=False,
         tags=(), library=None):
    return {
        "id": mid,
        "gated": gated,
        "downloads": downloads,
        "likes": likes,
        "lastModified": updated,
        "tags": list(tags) + ([f"license:{license}"] if license else []),
        "cardData": {"license": license} if license else {},
        "config": {"architectures": [arch]},
        "safetensors": {"parameters": {dtype: int(params_b * 1e9)}, "total": int(params_b * 1e9)},
        **({"library_name": library} if library else {}),
    }


ROWS = [
    _row("Qwen/Qwen3-Coder-30B-A3B-Instruct", 30.5, downloads=900_000, likes=900,
         updated="2025-08-01T00:00:00.000Z", tags=["code"]),
    _row("bigcode/starcoder2-15b", 15.0, arch="Starcoder2ForCausalLM", downloads=500_000,
         likes=400, updated="2024-02-01T00:00:00.000Z"),
    _row("Qwen/Qwen3-8B", 8.2, downloads=2_000_000, likes=2500, updated="2025-05-01T00:00:00.000Z"),
    _row("Qwen/Qwen3-4B", 4.0, downloads=700_000, likes=600, updated="2025-05-01T00:00:00.000Z"),
    _row("meta-llama/Llama-3.3-70B-Instruct", 70.6, arch="LlamaForCausalLM", license="llama3.3",
         downloads=1_500_000, likes=1800, updated="2025-01-01T00:00:00.000Z", gated="auto"),
    _row("deepseek-ai/DeepSeek-R1-Distill-Qwen-32B", 32.8, arch="Qwen2ForCausalLM", license="mit",
         downloads=800_000, likes=1200, updated="2025-03-01T00:00:00.000Z"),
    _row("some-org/legacy-classifier", 1.0, arch="BertForSequenceClassification",
         downloads=10, likes=1, updated="2022-01-01T00:00:00.000Z"),
    _row("some-org/tiny-gguf", 7.0, downloads=50, likes=2, library="gguf",
         updated="2025-07-01T00:00:00.000Z", tags=["gguf"]),
]


def _hub(rows=None):
    """A respx router with the Hub search mocked to ``rows``."""

    router = respx.mock(assert_all_called=False)
    router.get(HUB).mock(return_value=httpx.Response(200, json=list(ROWS if rows is None else rows)))
    return router


def _find(query, **kw):
    with _hub(kw.pop("rows", None)):
        return anyio.run(lambda: ss.find_models(query, **kw))


# ---------------------------------------------------------------------------
# query parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query,expect", [
    ("best coding model under 40B", {"max_params_b": 40.0}),
    ("model below 13 billion params", {"max_params_b": 13.0}),
    ("something over 30B", {"min_params_b": 30.0}),
    ("70B+ chat model", {"min_params_b": 70.0}),
    ("at least 7B", {"min_params_b": 7.0}),
    ("between 7B and 30B", {"min_params_b": 7.0, "max_params_b": 30.0}),
    ("qwen 8b", {"about_params_b": 8.0}),
])
def test_size_limits(query, expect):
    got = ss.parse_query(query).as_dict()
    for k, v in expect.items():
        assert got[k] == v
    assert "max_vram_gb" not in got  # "40B" is never read as 40 GB


@pytest.mark.parametrize("query,vram,gpu", [
    ("cheapest model that fits 24GB", 24.0, None),
    ("what runs in 48 gb of vram", 48.0, None),
    ("strongest reasoning model I can run on an A100", 80.0, "A100"),
    ("something for my 4090", 24.0, "4090"),
    ("a model for an H200", 141.0, "H200"),
])
def test_vram_and_gpu_names(query, vram, gpu):
    f = ss.parse_query(query)
    assert f.max_vram_gb == vram and f.gpu == gpu


@pytest.mark.parametrize("query,task", [
    ("best model for coding", "coding"),
    ("strongest reasoning model", "reasoning"),
    ("a vision model", "vision"),
    ("multilingual translation model", "multilingual"),
    ("an embedding model", "embedding"),
    ("good agentic tool-use model", "agentic"),
    ("just a fast model", None),
])
def test_task_words(query, task):
    assert ss.parse_query(query).task == task


def test_recency_licence_gating_and_objective():
    f = ss.parse_query("latest apache licensed ungated model")
    assert f.recency is True and f.sort == "updated" and f.license == "apache-2.0"
    assert f.exclude_gated is True and f.since is None
    assert ss.parse_query("best open coding model under 40B, 2025").since == 2025
    assert ss.parse_query("mit licensed model").license == "mit"
    assert ss.parse_query("permissive licence for commercial use").license == "permissive"
    assert ss.parse_query("cheapest model that fits 24GB").objective == "cheapest"
    assert ss.parse_query("smallest usable model").objective == "smallest"
    assert ss.parse_query("most downloaded model").objective == "popular"
    assert ss.parse_query("most downloaded model").sort == "downloads"


def test_search_text_prefers_brands_then_the_task_term():
    assert ss.parse_query("qwen coding model").text == "qwen"
    assert ss.parse_query("best coding model").text == "coder"
    assert ss.parse_query("under 40B").text == ""


def test_interpretation_reads_as_one_english_line():
    line = ss.describe(ss.parse_query("best open coding model under 40B, 2025"))
    assert line == "coding models, under 40B parameters, updated in 2025 or later"


# ---------------------------------------------------------------------------
# columns
# ---------------------------------------------------------------------------


def test_columns_are_chosen_for_the_query_shape():
    vocab = set(ss.COLUMNS)
    plain = ss.choose_columns(ss.parse_query("best coding model"))
    assert set(plain) <= vocab and "downloads" in plain and "fit" not in plain
    sized = ss.choose_columns(ss.parse_query("cheapest model that fits 24GB"))
    assert "fit" in sized and "dtype" in sized and "price" not in sized
    with_provider = ss.choose_columns(ss.parse_query("cheapest model that fits 24GB"), has_provider=True)
    assert "price" in with_provider and "fit" in with_provider
    licensed = ss.choose_columns(ss.parse_query("apache licensed model"))
    assert "license" in licensed
    recent = ss.choose_columns(ss.parse_query("latest models"))
    assert "updated" in recent
    # Stable order, always a subset of the vocabulary, never duplicated.
    for cols in (plain, sized, with_provider, licensed, recent):
        assert cols == [c for c in ss.COLUMNS if c in set(cols)]


def test_column_value_renders_every_column():
    res = _find("best coding model under 40B", use_agent=False)
    info = res.groups[0].models[0]
    extra = res.extras.get(info.id)
    for col in ss.COLUMNS:
        assert isinstance(ss.column_value(col, info, extra), str)
    assert ss.column_value("params", info).endswith("B")


# ---------------------------------------------------------------------------
# the rule tier, end to end
# ---------------------------------------------------------------------------


def test_rules_tier_groups_by_task_signal_and_says_why():
    res = _find("best open coding model under 40B, 2025", use_agent=False)
    assert res.source == "rules"
    assert res.interpretation == "coding models, under 40B parameters, updated in 2025 or later"
    assert res.filters == {"max_params_b": 40.0, "task": "coding", "since": 2025, "text": "coder"}
    titles = [g.title for g in res.groups]
    assert titles[0] == "Repos that say they are coding models"
    assert "not a benchmark" in res.groups[0].reason
    ids = [m.id for m in res.models]
    assert "Qwen/Qwen3-Coder-30B-A3B-Instruct" in ids
    assert res.groups[0].best == "Qwen/Qwen3-Coder-30B-A3B-Instruct"
    # 70B is over the cap, the classifier is not servable, 2024 is before 2025.
    assert "meta-llama/Llama-3.3-70B-Instruct" not in ids
    assert "some-org/legacy-classifier" not in ids
    assert "bigcode/starcoder2-15b" not in ids
    assert any("size range" in n for n in res.notes)
    assert any("vLLM cannot serve" in n for n in res.notes)


def test_grouping_is_stable_across_runs():
    a = _find("best coding model", use_agent=False)
    b = _find("best coding model", use_agent=False)
    assert [(g.title, [m.id for m in g.models], g.best) for g in a.groups] == \
           [(g.title, [m.id for m in g.models], g.best) for g in b.groups]


def test_no_task_falls_back_to_honest_size_bands():
    res = _find("a good open model", use_agent=False)
    titles = [g.title for g in res.groups]
    assert titles[0].startswith("Small") and any(t.startswith("Mid-size") for t in titles)
    assert all(g.reason for g in res.groups)


def test_gated_models_are_flagged_in_their_own_group_not_hidden():
    res = _find("best chat model", use_agent=False)
    gated = [g for g in res.groups if g.title.startswith("Gated")]
    assert gated and [m.id for m in gated[0].models] == ["meta-llama/Llama-3.3-70B-Instruct"]
    assert "HF_TOKEN" in gated[0].reason


def test_ungated_query_drops_them_and_says_how_many():
    res = _find("ungated chat model", use_agent=False)
    assert "meta-llama/Llama-3.3-70B-Instruct" not in [m.id for m in res.models]
    assert any("gated repo(s) were dropped" in n for n in res.notes)


def test_unservable_repos_never_reach_a_group():
    res = _find("a good open model", use_agent=False)
    ids = [m.id for m in res.models]
    assert "some-org/legacy-classifier" not in ids  # sequence-classification head
    assert "some-org/tiny-gguf" not in ids          # GGUF, vLLM cannot load it


def test_licence_filter_keeps_only_what_it_can_verify():
    res = _find("mit licensed model", use_agent=False)
    assert [m.id for m in res.models] == ["deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"]
    assert any("licence is not mit" in n for n in res.notes)


def test_vram_constraint_groups_by_fit_without_any_provider():
    res = _find("cheapest model that fits 24GB", use_agent=False)
    assert "fit" in res.columns and "price" not in res.columns  # no provider = no price
    by_title = {g.title: [m.id for m in g.models] for g in res.groups}
    assert by_title["Fits 24 GB comfortably"] == ["Qwen/Qwen3-4B"]   # ~9.6 GB estimate
    assert by_title["Tight on 24 GB"] == ["Qwen/Qwen3-8B"]           # ~19.7 GB of 21.6 usable
    assert "Qwen/Qwen3-Coder-30B-A3B-Instruct" in by_title["Too big for that card"]
    for m in res.models:
        assert res.extras[m.id]["fit"]


def test_empty_result_set_says_so_rather_than_pretending():
    res = _find("model under 1B", use_agent=False, rows=[])
    assert res.groups == []
    assert any("nothing on the Hub matched" in n for n in res.notes)


def test_result_is_json_serialisable_with_the_documented_shape():
    res = _find("best coding model under 40B", use_agent=False)
    obj = json.loads(json.dumps(res.to_dict()))
    assert set(obj) == {"query", "interpretation", "groups", "columns", "filters",
                        "source", "notes", "extras"}
    g = obj["groups"][0]
    assert set(g) == {"title", "reason", "models", "best"}
    assert {"id", "params_b", "est_vram_gb", "gated", "license"} <= set(g["models"][0])
    assert set(obj["columns"]) <= set(ss.COLUMNS)
    assert all(ss.COLUMN_SOURCES[c].split(".")[0] in ("info", "extra") for c in obj["columns"])


# ---------------------------------------------------------------------------
# provider-aware fit
# ---------------------------------------------------------------------------


@register_provider
class _FitFake:
    id = "fitfake"
    display_name = "Fit Fake"
    credential_fields = ()
    engines = ("vllm",)
    console_url = "https://x"
    scale_to_zero = True
    public_by_default = False

    def configured(self) -> bool:
        return True

    async def list_gpus(self):
        return [GpuSpec(provider_id="L4", family="L4", vram_gb=24, price_per_hour=0.80),
                GpuSpec(provider_id="A100", family="A100-80", vram_gb=80, price_per_hour=2.50)]


def test_provider_adds_fit_and_price_and_orders_by_what_fits():
    res = _find("best open model", use_agent=False, provider_id="fitfake")
    assert "fit" in res.columns and "price" in res.columns
    assert res.groups[0].title.startswith("Fits")
    # The cheapest card it fits *comfortably* — a tight L4 is not sold as a fit.
    assert res.extras["Qwen/Qwen3-4B"] == {"updated": "2025-05-01T00:00:00.000Z", "trending": None,
                                           "fit": "fits", "price_per_hour": 0.80, "gpu": "L4"}
    small = res.extras["Qwen/Qwen3-8B"]
    assert small["gpu"] == "A100" and small["price_per_hour"] == 2.50 and small["fit"] == "fits"
    big = res.extras["meta-llama/Llama-3.3-70B-Instruct"]
    assert big["gpu"] is None and big["fit"].startswith("no:")


# ---------------------------------------------------------------------------
# the agent tier
# ---------------------------------------------------------------------------


PLAN = {
    "interpretation": "coding models small enough for one 80 GB card",
    "columns": ["params", "downloads", "made-up-column"],
    "groups": [
        {"title": "Purpose-built coding repos", "reason": "the id says coder; no benchmark implied",
         "model_ids": ["Qwen/Qwen3-Coder-30B-A3B-Instruct", "not/a-real-model"],
         "best": "Qwen/Qwen3-Coder-30B-A3B-Instruct"},
        {"title": "Strong generalists", "reason": "top downloads on the Hub",
         "model_ids": ["Qwen/Qwen3-8B"], "best": ""},
    ],
    "max_params_b": 40.0,
    "min_params_b": 0.0,
    "max_vram_gb": 80.0,
    "notes": ["mantis runs no evals, so this ranks adoption and recency"],
}


def _mock_agent(text: str):
    """Swap ``_make_agent`` for one wired to a MockProvider replaying ``text``."""

    from mantis_agent import Agent
    from mantis_agent.events import (
        ContentBlockDelta, ContentBlockStart, ContentBlockStop, MessageDelta,
        MessageStart, MessageStop, TextDelta,
    )
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.types import TextBlock, Usage

    events = [
        MessageStart(message_id="m1", model="mock"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text=text)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1)),
        MessageStop(),
    ]
    provider = MockProvider(scripted_events=events)

    def factory(model, backend, tools, response_format):
        return Agent(model="mock", provider=provider, tools=tools, response_format=response_format,
                     system="x", max_steps=2, auto_compact=False, include_memory=False,
                     include_env=False, include_recall=False, persist=False)

    return provider, factory


def _agent_find(monkeypatch, text: str, query="best coding model under 40B", **kw):
    monkeypatch.setenv("MANTIS_AGENT_MODEL", "qwen3-coder")
    provider, factory = _mock_agent(text)
    monkeypatch.setattr(ss, "_make_agent", factory)
    return provider, _find(query, use_agent=True, **kw)


def test_agent_tier_shapes_the_answer_and_is_held_to_verifiable_ids(monkeypatch):
    provider, res = _agent_find(monkeypatch, json.dumps(PLAN))
    assert res.source == "agent"
    assert res.interpretation == "coding models small enough for one 80 GB card"
    assert [g.title for g in res.groups] == ["Purpose-built coding repos", "Strong generalists"]
    # The hallucinated id is dropped, and the drop is disclosed.
    assert "not/a-real-model" not in [m.id for m in res.models]
    assert any("are not in the Hub results" in n for n in res.notes)
    # Columns are intersected with the fixed vocabulary and re-ordered.
    assert res.columns == ["params", "downloads"]
    # A group that named no best gets the top-ranked one it actually has.
    assert res.groups[1].best == "Qwen/Qwen3-8B"
    # Numeric filters the agent inferred are merged over the parsed ones.
    assert res.filters["max_params_b"] == 40.0 and res.filters["max_vram_gb"] == 80.0
    assert any("no evals" in n for n in res.notes)
    # The candidate list is pre-fetched and passed in — the agent does not crawl —
    # and the Hub tool is offered for the gap, under a hard call budget.
    prompt = str(provider.last_call_kwargs["messages"][-1].content)
    assert "Qwen/Qwen3-Coder-30B-A3B-Instruct" in prompt and "dl=900000" in prompt
    assert "search_hub" in {t["name"] for t in (provider.last_call_kwargs["tools"] or [])}
    assert provider.last_call_kwargs["extra"]["response_format"]["type"] == "json_schema"


def test_the_agent_is_told_not_to_invent_anything():
    system = ss._AGENT_SYSTEM.lower()
    assert "never invent an id" in system
    assert "runs no evals" in system
    assert "group only ids from the candidate list" in system


def test_malformed_agent_output_falls_back_to_rules_instead_of_raising(monkeypatch):
    _provider, res = _agent_find(monkeypatch, "Sure! Here are some great models :)")
    assert res.source == "rules"
    assert res.groups and any("rule parser" in n for n in res.notes)


def test_agent_that_groups_only_unknown_ids_falls_back(monkeypatch):
    plan = {**PLAN, "groups": [{"title": "x", "reason": "y", "model_ids": ["ghost/model"], "best": ""}]}
    _provider, res = _agent_find(monkeypatch, json.dumps(plan))
    assert res.source == "rules"


def test_no_configured_model_skips_the_agent_and_says_so():
    res = _find("best coding model", use_agent=True)
    assert res.source == "rules"
    assert any("rule parser" in n for n in res.notes)


def test_use_agent_false_never_looks_for_a_model(monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_MODEL", "qwen3-coder")

    def _boom(*a, **k):
        raise AssertionError("the agent tier must not run when use_agent=False")

    monkeypatch.setattr(ss, "_make_agent", _boom)
    res = _find("best coding model", use_agent=False)
    assert res.source == "rules"
    assert any("disabled by the caller" in n for n in res.notes)


def test_configured_model_reads_the_env_then_the_catalog(monkeypatch):
    assert ss.configured_model() is None
    monkeypatch.setenv("MANTIS_AGENT_MODEL", "m1")
    monkeypatch.setenv("MANTIS_AGENT_BASE_URL", "http://localhost:11434")
    assert ss.configured_model() == ("m1", "http://localhost:11434")
    monkeypatch.delenv("MANTIS_AGENT_MODEL")
    monkeypatch.delenv("MANTIS_AGENT_BASE_URL")
    from mantis_agent import catalog

    catalog.set_last_model("qwen3:8b", "http://localhost:11434")
    assert ss.configured_model() == ("qwen3:8b", "http://localhost:11434")


# ---------------------------------------------------------------------------
# the manager wrapper
# ---------------------------------------------------------------------------


def test_manager_find_models_is_the_public_door():
    from mantis_agent.deploy import manager

    assert "find_models" in manager.__all__
    with _hub():
        res = anyio.run(lambda: manager.find_models("best coding model under 40B", use_agent=False))
    assert isinstance(res, ss.SmartSearchResult) and res.source == "rules" and res.groups
