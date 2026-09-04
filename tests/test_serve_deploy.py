"""Tests for the ``mantis serve`` Deploy page — bring-your-own GPU provider.

The dashboard is a thin, sync, JSON-shaped skin over the async
``mantis_agent.deploy.manager`` contract. Every endpoint is exercised here
against a *fake* manager (monkeypatched function by function) and a fake
provider registered through ``register_provider`` — so these tests pin the
dashboard's behaviour independently of the adapters: input validation,
credential redaction, the background-job lifecycle, the LAN token, and that
the served page carries the Deploy sections and the ``g d`` binding.
No network anywhere.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

SECRET_VALUE = "fk-live-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
HF_TOKEN = "hf_abcdefghijklmnopqrstuvwxyz012345"
HEADER_SECRET = "supersecretheadervalue9876"


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    """A fake GPU provider + a fake manager. ``calls`` records every manager
    call so tests can assert what the dashboard passed through."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(h))
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")
    monkeypatch.delenv("FAKEGPU_API_KEY", raising=False)

    from mantis_agent import serve
    from mantis_agent.deploy import base, manager
    from mantis_agent.deploy.base import (
        DEPLOY_PROVIDERS,
        Account,
        CostEstimate,
        CredentialField,
        DeployError,
        Deployment,
        DeployOpts,
        GpuSpec,
        ModelInfo,
        NotSupported,
        register_provider,
    )

    serve._inspect_cache.clear()
    serve._deploy_jobs.clear()
    serve._deploy_accounts.clear()
    serve._model_info_cache.clear()
    serve._enrich_pending.clear()
    serve._model_info_loaded = False

    calls: dict[str, list] = {}

    def rec(name, *a, **kw):
        calls.setdefault(name, []).append((a, kw))

    gpus = [
        GpuSpec("H100", "H100", 80, price_per_hour=3.5, available=True, label="H100 80GB"),
        GpuSpec("A10G", "A10G", 24, price_per_hour=0.75, available=True),
        GpuSpec("MYSTERY", "other", 48, price_per_hour=None),
        GpuSpec("L4x2", "L4", 24, count=2, price_per_hour=1.2, region="eu-west"),
    ]
    info = ModelInfo(id="org/model-8b", source="hf", architectures=("LlamaForCausalLM",), params_b=8.0,
                     dtype="BF16", gated=True, license="llama3", downloads=12345, context_len=8192,
                     vllm_ok=True, est_vram_gb=19.5)

    @register_provider
    class FakeGpu:
        id = "fakegpu"
        display_name = "Fake GPU Cloud"
        credential_fields = (CredentialField("FAKEGPU_API_KEY", "API key", help="console → keys"),
                             CredentialField("FAKEGPU_REGION", "Region", secret=False, required=False))
        engines = ("vllm", "sglang")
        console_url = "https://console.fakegpu.test"
        scale_to_zero = True
        public_by_default = True

        def configured(self):
            import os
            return bool(os.environ.get("FAKEGPU_API_KEY"))

        async def cost(self, dep):
            rec("cost", dep.id)
            return CostEstimate(per_hour_usd=0.75, idle_per_hour_usd=0.0, accrued_usd=1.25, basis="list price")

    def summary(pid, configured, **extra):
        d = manager.ProviderSummary({
            "id": pid, "display_name": extra.get("display_name", pid.title()), "configured": configured,
            "credential_fields": list(FakeGpu.credential_fields), "engines": list(FakeGpu.engines),
            "console_url": FakeGpu.console_url, "scale_to_zero": True, "public_by_default": True})
        return d

    state = {"configured": False, "validate_fails": False, "logs_unsupported": False}

    def make_dep(status="running"):
        return Deployment(
            id="dep-1", provider="fakegpu", model="org/model-8b", engine="vllm", status=status,
            gpu=gpus[1], served_model_name="org/model-8b",
            endpoint_url="https://dep-1.fakegpu.test/v1", name="model-8b",
            opts=DeployOpts(hf_token=HF_TOKEN, max_model_len=4096),
            auth_env="FAKEGPU_API_KEY",
            auth_headers={"Modal-Key": "${MODAL_TOKEN_ID}", "X-Real": HEADER_SECRET},
            message="ready · token=" + SECRET_VALUE, raw={"api_key": SECRET_VALUE})

    async def providers():
        rec("providers")
        return [summary("fakegpu", state["configured"], display_name="Fake GPU Cloud"),
                summary("runpod", False, display_name="RunPod Serverless")]

    async def save_credentials(pid, values):
        rec("save_credentials", pid, dict(values))
        state["configured"] = True
        return Account(ok=True, provider=pid, user="teddy", balance_usd=12.4, message="ok")

    async def validate(pid):
        rec("validate", pid)
        if state["validate_fails"]:
            raise DeployError("401 from the provider", hint="check the key", provider=pid)
        return Account(ok=True, provider=pid, credits_usd=3.0)

    async def gpus_fn(pid, *, min_vram_gb=None):
        rec("gpus", pid, min_vram_gb)
        return [g for g in gpus if min_vram_gb is None or g.total_vram_gb >= min_vram_gb]

    async def inspect_model(model, *, hf_token=None):
        rec("inspect_model", model)
        if model == "missing/model":
            raise DeployError("not found on the Hub", hint="check the id")
        return info

    async def search_models(query="", *, limit=25, sort="trending"):
        rec("search_models", query, limit, sort)
        return [info, ModelInfo(id="org/tiny", source="hf", params_b=0.5, vllm_ok=False, reason="MambaForCausalLM", est_vram_gb=2.0)]

    async def fit(i, candidates, *, context_len=None):
        rec("fit", i.id, [c.provider_id for c in candidates])
        out = []
        for g in candidates:
            if g.total_vram_gb >= 40:
                out.append((g, "fits"))
            elif g.total_vram_gb >= 20:
                out.append((g, "tight"))
            else:
                out.append((g, "no: needs 20 GB, has %d" % g.total_vram_gb))
        return out

    async def deploy(pid, model, *, gpu, engine="vllm", opts=None, wait=True, progress=None):
        rec("deploy", pid, model, gpu, engine, opts, wait)
        if progress:
            progress("pulling image")
            progress("api_key=" + SECRET_VALUE + " exported")
        if model == "fail/model":
            raise DeployError("quota exceeded", hint="add credits", provider=pid)
        time.sleep(0.05)
        return make_dep("running")

    async def status(dep_id, *, refresh=True):
        rec("status", dep_id, refresh)
        return make_dep("starting")

    async def list_deployments(*, refresh=False, provider_id=None):
        rec("list_deployments", refresh, provider_id)
        return [make_dep("deleted"), make_dep("running")]

    def logs(dep_id, *, tail=200):
        rec("logs", dep_id, tail)
        if state["logs_unsupported"]:
            raise NotSupported("no logs API", hint="use the console")

        async def gen():
            for line in ("boot", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123", "listening"):
                yield line
        return gen()

    async def connect(dep_id, *, set_current=True):
        rec("connect", dep_id, set_current)
        return {"model": "org/model-8b", "backend": "https://dep-1.fakegpu.test/v1",
                "api_key_env": "FAKEGPU_API_KEY", "headers": {"X-Real": HEADER_SECRET}}

    async def teardown(dep_id, *, progress=None):
        rec("teardown", dep_id)
        if progress:
            progress("deleting endpoint")

    for name, fn in [("providers", providers), ("save_credentials", save_credentials), ("validate", validate),
                     ("gpus", gpus_fn), ("inspect_model", inspect_model), ("search_models", search_models),
                     ("fit", fit), ("deploy", deploy), ("status", status),
                     ("list_deployments", list_deployments), ("logs", logs), ("connect", connect),
                     ("teardown", teardown)]:
        monkeypatch.setattr(manager, name, fn)
    # cost goes through the adapter — resolve it without importing the real providers package
    monkeypatch.setattr(base, "get_provider", lambda pid: FakeGpu())

    yield {"calls": calls, "state": state, "gpus": gpus, "info": info, "make_dep": make_dep, "serve": serve}
    DEPLOY_PROVIDERS.pop("fakegpu", None)


def _wait_job(serve, job_id, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        j = serve.deploy_job(job_id)
        if j["status"] != "running":
            return j
        time.sleep(0.02)
    raise AssertionError("job never finished")


# ---------------------------------------------------------------------------
# providers · credentials
# ---------------------------------------------------------------------------


def test_providers_carry_logo_fields_and_no_secrets(fake):
    serve = fake["serve"]
    r = serve.deploy_providers()
    assert r["ok"] and r["configured_count"] == 0
    ids = [p["id"] for p in r["providers"]]
    assert ids == ["fakegpu", "runpod"]
    fg, rp = r["providers"]
    assert fg["logo"] is None and rp["logo"] == "runpod"          # only ids the logo set knows
    assert fg["credential_fields"][0] == {"env": "FAKEGPU_API_KEY", "label": "API key", "secret": True,
                                          "required": True, "help": "console → keys"}
    assert fg["engines"] == ["vllm", "sglang"] and fg["scale_to_zero"] is True and fg["public_by_default"] is True
    assert fg["account"] is None


def test_providers_carry_a_key_guide_when_provider_guides_has_one(fake, monkeypatch):
    from mantis_agent import provider_guides, serve

    assert serve.deploy_providers()["providers"][0]["guide"] is None      # no entry yet → None, key present
    monkeypatch.setitem(provider_guides.GUIDES, "fakegpu", {
        "name": "Fake GPU", "env_var": "FAKEGPU_API_KEY", "keys_url": "https://console.fakegpu.test/keys",
        "intro": "Serverless GPUs billed by the second.", "steps": ["Sign in", "Settings → API keys", "Create key"],
        "key_shape": "fk-live-…", "free_note": "$10 free credit on signup.", "pricing_url": "https://fakegpu.test/pricing"})
    g = serve.deploy_providers()["providers"][0]["guide"]
    assert g["keys_url"] == "https://console.fakegpu.test/keys" and g["steps"] == ["Sign in", "Settings → API keys", "Create key"]
    assert g["key_hint"] == "fk-live-…" and g["free_note"].startswith("$10") and g["name"] == "Fake GPU"
    assert SECRET_VALUE not in json.dumps(g)


def test_deploy_logos_load_from_json_and_replace_placeholders(tmp_path, monkeypatch):
    from mantis_agent import serve_logos

    f = tmp_path / "deploy_logos.json"
    f.write_text(json.dumps({
        "runpod": {"viewBox": "0 0 24 24", "paths": ["M2 2h20v20H2z", {"d": "M6 6h12v12H6z", "fill": "#fff", "opacity": "0.5"}],
                   "tint": "#673ab7", "source": "runpod.io", "license": "trademark"},
        "bogus": {"paths": []},
        "vastai": "not a dict",
    }), encoding="utf-8")
    marks = serve_logos.load_deploy_logos(f)
    assert set(marks) == {"runpod"} and marks["runpod"]["tint"] == "#673ab7"
    assert '<path d="M2 2h20v20H2z"/>' in marks["runpod"]["svg"] and 'fill="#fff" opacity="0.5"' in marks["runpod"]["svg"]
    assert serve_logos.load_deploy_logos(tmp_path / "missing.json") == {}
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert serve_logos.load_deploy_logos(tmp_path / "bad.json") == {}
    # the served page inlines whatever PROVIDER_LOGOS holds at request time
    monkeypatch.setitem(serve_logos.PROVIDER_LOGOS, "runpod", marks["runpod"])
    httpd, base = _boot()
    try:
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            page = r.read().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert "M2 2h20v20H2z" in page and "bigMark" in page and "cs-guide" in page and "addkey" in page


def _wait(pred, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_models_enrich_progressively_and_cache_with_ttl(fake, monkeypatch):
    """Search returns bare ids at once (partial), background lookups fill
    them, the second paint is complete from the on-disk cache, and a stale
    cache entry is looked up again."""
    serve, calls = fake["serve"], fake["calls"]
    from mantis_agent.deploy import manager
    from mantis_agent.deploy.base import ModelInfo

    serve._model_info_cache.clear()
    serve._enrich_pending.clear()
    serve._model_info_loaded = False

    async def bare_search(query="", *, limit=25, sort="trending"):
        return [ModelInfo(id="org/model-8b", source="hf"), ModelInfo(id="org/tiny", source="hf", params_b=0.5, vllm_ok=False, est_vram_gb=2.0)]

    async def inspect_model(model, *, hf_token=None):
        calls.setdefault("inspect_model", []).append(((model,), {}))
        if model == "org/tiny":
            raise AssertionError("already complete — must not be looked up")
        return fake["info"]
    monkeypatch.setattr(manager, "search_models", bare_search)
    monkeypatch.setattr(manager, "inspect_model", inspect_model)

    r = serve.deploy_models("")
    assert r["ok"] and r["partial"] is True and r["pending"] == ["org/model-8b"]
    assert r["models"][0]["org"] == "org" and r["models"][0]["params_b"] is None
    assert _wait(lambda: not serve._enrich_pending)
    e = serve.deploy_models_enrich("org/model-8b")          # the page asks only for what was pending
    assert e["pending"] == []
    got = e["models"]["org/model-8b"]
    assert got["params_b"] == 8.0 and got["vllm_ok"] is True and got["est_vram_gb"] == 19.5 and "at" not in got
    assert HF_TOKEN not in json.dumps(e)
    # second paint: complete, from cache, no new lookup
    n = len(calls["inspect_model"])
    r2 = serve.deploy_models("")
    assert r2["partial"] is False and r2["models"][0]["params_b"] == 8.0 and r2["models"][0]["license"] == "llama3"
    assert len(calls["inspect_model"]) == n
    assert (serve._cache_dir() / "model-info.json").exists()
    # TTL: an entry older than a day is treated as missing and re-queued
    with serve._deploy_lock:
        serve._model_info_cache["org/model-8b"]["at"] = time.time() - serve.MODEL_INFO_TTL_S - 5
    assert serve._model_info_get("org/model-8b") is None
    r3 = serve.deploy_models("")
    assert r3["partial"] is True and _wait(lambda: not serve._enrich_pending)
    assert len(calls["inspect_model"]) == n + 1
    # a failed lookup is remembered (briefly) and reported, not retried every paint
    async def boom(model, *, hf_token=None):
        raise RuntimeError("hub down")
    monkeypatch.setattr(manager, "inspect_model", boom)
    with serve._deploy_lock:
        serve._model_info_cache.pop("org/model-8b", None)
    serve.deploy_models("")
    assert _wait(lambda: not serve._enrich_pending)
    assert serve.deploy_models_enrich("org/model-8b")["models"]["org/model-8b"]["error"] == "hub down"
    assert serve.deploy_models("")["partial"] is False


def test_org_avatar_proxy_caches_misses_and_rejects_bad_ids(fake, monkeypatch):
    serve = fake["serve"]
    fetched = []

    def fake_fetch(org):
        fetched.append(org)
        return (b"\x89PNG\r\n" + b"x" * 40, "image/png") if org.lower() == "qwen" else None
    monkeypatch.setattr(serve, "_fetch_org_avatar", fake_fetch)
    for bad in ("../etc", "a/b", "", ".hidden", "x" * 120, "org?x"):
        assert serve.org_avatar(bad)[0] == 400, bad
    code, body, ctype = serve.org_avatar("Qwen")
    assert code == 200 and ctype == "image/png" and body.startswith(b"\x89PNG")
    assert (serve._avatar_dir() / "qwen.png").exists() and (serve._avatar_dir() / "qwen.json").exists()
    assert serve.org_avatar("qwen")[0] == 200 and fetched == ["Qwen"]          # cache hit, no refetch
    assert serve.org_avatar("nobody")[0] == 204 and serve.org_avatar("nobody")[0] == 204
    assert fetched == ["Qwen", "nobody"]                                        # the miss is cached too
    # expire the hit → refetched
    meta = serve._avatar_dir() / "qwen.json"
    m = json.loads(meta.read_text())
    m["at"] -= serve.ORG_AVATAR_TTL_S + 1
    meta.write_text(json.dumps(m))
    assert serve.org_avatar("qwen")[0] == 200 and fetched[-1] == "qwen"
    # over HTTP: 200 with the image type, 204 for a miss, 400 for traversal
    httpd, base = _boot()
    try:
        with urllib.request.urlopen(base + "/api/deploy/org-avatar?org=qwen", timeout=5) as r:
            assert r.status == 200 and r.headers["Content-Type"] == "image/png"
        with urllib.request.urlopen(base + "/api/deploy/org-avatar?org=nobody", timeout=5) as r:
            assert r.status == 204
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(base + "/api/deploy/org-avatar?org=..%2F..%2Fetc", timeout=5)
        assert ei.value.code == 400
        with urllib.request.urlopen(base + "/api/deploy/models/enrich?ids=", timeout=5) as r:
            assert json.loads(r.read()) == {"ok": True, "models": {}, "pending": []}
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            page = r.read().decode()
        assert "ORG_MARKS" in page and "org-avatar" in page and 'loading = "lazy"' in page and "enrichLoop" in page
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_org_logos_json_expands_aliases(tmp_path):
    from mantis_agent import serve_logos

    f = tmp_path / "org_logos.json"
    f.write_text(json.dumps({"Qwen": {"paths": ["M0 0h24v24H0z"], "tint": "#615ced", "aliases": ["qwen-ai", "QwenLM"]},
                             "meta-llama": {"paths": ["M1 1h2v2H1z"]}}), encoding="utf-8")
    marks = serve_logos.load_org_logos(f)
    assert set(marks) == {"qwen", "qwen-ai", "qwenlm", "meta-llama"}
    assert marks["qwenlm"] is marks["qwen"] and marks["qwen"]["tint"] == "#615ced"
    assert serve_logos.load_org_logos(tmp_path / "nope.json") == {}


def test_save_creds_validates_and_never_echoes_the_value(fake):
    serve, calls = fake["serve"], fake["calls"]
    r = serve.deploy_save_creds("fakegpu", {"FAKEGPU_API_KEY": SECRET_VALUE, "FAKEGPU_REGION": " ", "junk": ""})
    assert r["ok"] and r["saved"] == ["FAKEGPU_API_KEY"]
    assert r["account"]["ok"] and r["account"]["balance_usd"] == 12.4
    assert SECRET_VALUE not in json.dumps(r) and SECRET_VALUE[:12] not in json.dumps(r)
    # the value DID reach the manager, whole
    assert calls["save_credentials"][0][0] == ("fakegpu", {"FAKEGPU_API_KEY": SECRET_VALUE})
    # …and the strip now shows the account without a second network call
    p = serve.deploy_providers()["providers"][0]
    assert p["configured"] is True and p["account"]["user"] == "teddy"
    assert serve.deploy_save_creds("", {"A": "b"})["ok"] is False
    assert serve.deploy_save_creds("fakegpu", {})["ok"] is False
    assert serve.deploy_save_creds("fakegpu", {"A": "   "})["ok"] is False


def test_validate_records_the_account_and_failures_are_answers(fake):
    serve, state = fake["serve"], fake["state"]
    r = serve.deploy_validate("fakegpu")
    assert r["ok"] and r["account"]["credits_usd"] == 3.0
    state["validate_fails"] = True
    r = serve.deploy_validate("fakegpu")
    assert r["ok"] is False and r["error"] == "401 from the provider" and r["hint"] == "check the key"
    assert r["kind"] == "DeployError" and r["provider"] == "fakegpu"
    p = serve.deploy_providers()["providers"][0]
    assert p["account"]["ok"] is False and "401" in p["account"]["message"]
    assert serve.deploy_validate(None)["ok"] is False


# ---------------------------------------------------------------------------
# gpus · models · inspect
# ---------------------------------------------------------------------------


def test_gpus_sorted_cheapest_first_with_unpriced_last(fake):
    serve, calls = fake["serve"], fake["calls"]
    r = serve.deploy_gpus("fakegpu", "20")
    assert r["ok"] and [g["provider_id"] for g in r["gpus"]] == ["A10G", "L4x2", "H100", "MYSTERY"]
    assert calls["gpus"][0][0] == ("fakegpu", 20)
    l4 = next(g for g in r["gpus"] if g["provider_id"] == "L4x2")
    assert l4["total_vram_gb"] == 48 and l4["display"] == "L4x2×2" and l4["region"] == "eu-west"
    assert serve.deploy_gpus("fakegpu", "notanumber")["ok"]
    assert serve.deploy_gpus("", None)["ok"] is False


def test_models_search_passes_sort_and_limit_and_flags_curated(fake):
    serve, calls = fake["serve"], fake["calls"]
    r = serve.deploy_models("", "bogus", "500")
    assert r["ok"] and r["curated"] is True and r["sort"] == "trending"
    assert calls["search_models"][-1][0] == ("", 100, "trending")
    r = serve.deploy_models("llama", "downloads", "abc")
    assert r["curated"] is False and calls["search_models"][-1][0] == ("llama", 25, "downloads")
    m0, m1 = r["models"]
    assert m0["id"] == "org/model-8b" and m0["gated"] is True and m0["vllm_ok"] is True
    assert m1["vllm_ok"] is False and m1["reason"] == "MambaForCausalLM"
    assert m0["architectures"] == ["LlamaForCausalLM"]


def test_provider_requirements_ride_the_providers_endpoint(fake, monkeypatch):
    """An adapter can be fully keyed and still unable to run — Modal deploys
    by driving its own SDK. The offline check rides the card endpoint."""
    from mantis_agent.deploy import manager

    async def providers():
        return [manager.ProviderSummary({
            "id": "modal", "display_name": "Modal", "configured": True, "credential_fields": [],
            "engines": ["vllm"], "console_url": "https://modal.com", "scale_to_zero": True,
            "public_by_default": False, "requirements_ok": False,
            "requirements_hint": "the `modal` package is not installed — pip install mantis-agent-sdk[modal]"}),
            manager.ProviderSummary({
            "id": "runpod", "display_name": "RunPod", "configured": True, "credential_fields": [],
            "engines": ["vllm"], "console_url": "https://runpod.io", "scale_to_zero": True,
            "public_by_default": False, "requirements_ok": True, "requirements_hint": ""})]
    monkeypatch.setattr(manager, "providers", providers)
    rows = {p["id"]: p for p in fake["serve"].deploy_providers()["providers"]}
    assert rows["modal"]["requirements_ok"] is False and "pip install" in rows["modal"]["requirements_hint"]
    assert rows["runpod"]["requirements_ok"] is True and rows["runpod"]["requirements_hint"] == ""


def test_hf_token_state_rides_the_card_endpoints(fake, monkeypatch):
    """A gated repo is only deployable with a Hugging Face token, so both
    endpoints that feed the model cards say whether one is configured."""
    from mantis_agent import serve

    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert serve.hf_token_set() is False
    assert serve.deploy_models("x")["hf_token_set"] is False
    assert serve.deploy_inspect("org/model-8b", ttl_s=0)["hf_token_set"] is False
    monkeypatch.setenv("HF_TOKEN", "hf_" + "a" * 30)
    assert serve.hf_token_set() is True
    assert serve.deploy_models("x")["hf_token_set"] is True
    assert serve.deploy_inspect("org/model-8b", ttl_s=0)["hf_token_set"] is True


def test_saving_hf_token_busts_the_gated_verdict_cache(fake, monkeypatch):
    """Saving HF_TOKEN changes what every gated model may do, so the cached
    fit/gating verdicts computed without it are dropped."""
    from mantis_agent import serve

    serve.deploy_inspect("org/model-8b")
    assert "org/model-8b" in serve._inspect_cache
    r = serve.deploy_save_creds("hf", {"HF_TOKEN": "hf_" + "b" * 30})
    assert r["ok"] and r["saved"] == ["HF_TOKEN"] and "hf_" + "b" * 30 not in json.dumps(r)
    assert serve._inspect_cache == {}
    # a non-token credential leaves the cache alone
    serve.deploy_inspect("org/model-8b")
    serve.deploy_save_creds("fakegpu", {"FAKEGPU_API_KEY": SECRET_VALUE})
    assert "org/model-8b" in serve._inspect_cache


def test_gated_kind_reaches_the_cards(fake, monkeypatch):
    from mantis_agent.deploy import manager
    from mantis_agent.deploy.base import ModelInfo

    async def search(query="", *, limit=25, sort="trending"):
        return [ModelInfo(id="org/auto", source="hf", gated=True, gated_kind="auto", params_b=8.0, vllm_ok=True, est_vram_gb=19.0),
                ModelInfo(id="org/manual", source="hf", gated=True, gated_kind="manual", params_b=8.0, vllm_ok=True, est_vram_gb=19.0)]
    monkeypatch.setattr(manager, "search_models", search)
    rows = {m["id"]: m for m in fake["serve"].deploy_models("x")["models"]}
    assert rows["org/auto"]["gated"] is True and rows["org/auto"]["gated_kind"] == "auto"
    assert rows["org/manual"]["gated_kind"] == "manual"


def test_verdict_parsing():
    from mantis_agent import serve

    assert serve._verdict_parts("fits") == ("fits", "")
    assert serve._verdict_parts("tight") == ("tight", "")
    assert serve._verdict_parts("no: needs 48 GB") == ("no", "needs 48 GB")
    assert serve._verdict_parts("no — needs 48 GB") == ("no", "needs 48 GB")
    assert serve._verdict_parts("weird") == ("no", "weird")


def test_inspect_fits_per_configured_provider_and_caches(fake):
    serve, calls, state = fake["serve"], fake["calls"], fake["state"]
    r = serve.deploy_inspect("org/model-8b")
    assert r["ok"] and r["model"]["params_b"] == 8.0 and r["fits"] == []     # nothing configured yet
    serve._inspect_cache.clear()
    state["configured"] = True
    r = serve.deploy_inspect("org/model-8b")
    assert [f["provider"] for f in r["fits"]] == ["fakegpu"]          # runpod isn't configured
    f = r["fits"][0]
    assert f["display_name"] == "Fake GPU Cloud" and f["engines"] == ["vllm", "sglang"]
    by = {g["provider_id"]: g for g in f["gpus"]}
    assert by["H100"]["verdict"] == "fits" and by["A10G"]["verdict"] == "tight"
    assert by["L4x2"]["verdict"] == "fits"
    assert by["MYSTERY"]["verdict"] == "fits" and by["MYSTERY"]["price_per_hour"] is None
    assert calls["fit"][-1][0] == ("org/model-8b", ["H100", "A10G", "MYSTERY", "L4x2"])
    n = len(calls["inspect_model"])
    r2 = serve.deploy_inspect("org/model-8b")
    assert r2 is r and len(calls["inspect_model"]) == n                   # cached
    assert serve.deploy_inspect("org/model-8b", ttl_s=0)["ok"] and len(calls["inspect_model"]) == n + 1
    bad = serve.deploy_inspect("missing/model")
    assert bad["ok"] is False and bad["hint"] == "check the id"
    assert serve.deploy_inspect("")["ok"] is False


def test_inspect_survives_one_broken_provider(fake, monkeypatch):
    serve, state = fake["serve"], fake["state"]
    from mantis_agent.deploy import manager
    from mantis_agent.deploy.base import DeployError

    state["configured"] = True

    async def boom(pid, *, min_vram_gb=None):
        raise DeployError("catalogue is down")
    monkeypatch.setattr(manager, "gpus", boom)
    r = serve.deploy_inspect("org/model-8b")
    assert r["ok"] and r["fits"][0]["gpus"] == [] and r["fits"][0]["error"] == "catalogue is down"


# ---------------------------------------------------------------------------
# jobs — deploy / teardown run in the background, the page polls
# ---------------------------------------------------------------------------


def test_deploy_job_streams_progress_then_the_redacted_deployment(fake):
    serve, calls = fake["serve"], fake["calls"]
    r = serve.deploy_up("fakegpu", "org/model-8b", {"provider_id": "A10G"}, "VLLM",
                        {"max_model_len": "4096", "hf_token": HF_TOKEN, "min_replicas": "0",
                         "trust_remote_code": True, "extra_engine_args": "--foo 1", "bogus": "x"})
    assert r["ok"] and r["job"] and r["gpu"] == "A10G" and r["engine"] == "vllm"
    j = _wait_job(serve, r["job"])
    assert j["ok"] and j["status"] == "done" and j["kind"] == "deploy" and j["target"] == "org/model-8b"
    assert j["lines"][0] == "pulling image"
    assert SECRET_VALUE not in "\n".join(j["lines"]) and "api_key=" in j["lines"][1]   # progress is redacted
    assert j["elapsed_s"] >= 0 and j["ended_at"] and j["error"] is None
    d = j["result"]
    assert d["id"] == "dep-1" and d["status"] == "running" and d["is_live"] is True
    assert d["gpu"]["provider_id"] == "A10G" and d["gpu"]["total_vram_gb"] == 24
    assert d["endpoint_url"] == "https://dep-1.fakegpu.test/v1" and d["auth_env"] == "FAKEGPU_API_KEY"
    assert "raw" not in d                                                # the provider object never ships
    assert d["opts"]["hf_token"] != HF_TOKEN and HF_TOKEN not in json.dumps(j)
    assert d["auth_headers"]["Modal-Key"] == "${MODAL_TOKEN_ID}"       # env refs stay readable
    assert HEADER_SECRET not in json.dumps(j)
    assert SECRET_VALUE not in d["message"] and d["message"].startswith("ready")
    # what reached the manager
    (pid, model, gpu, engine, opts, wait), _ = calls["deploy"][0]
    assert (pid, model, gpu, engine, wait) == ("fakegpu", "org/model-8b", "A10G", "vllm", True)
    assert opts.max_model_len == 4096 and opts.hf_token == HF_TOKEN and opts.min_replicas == 0
    assert opts.trust_remote_code is True and opts.extra_engine_args == ["--foo", "1"]
    assert not hasattr(opts, "bogus")


def test_deploy_job_error_path_and_input_validation(fake):
    serve = fake["serve"]
    r = serve.deploy_up("fakegpu", "fail/model", "A10G", None, None)
    j = _wait_job(serve, r["job"])
    assert j["status"] == "error" and j["error"] == "quota exceeded" and j["hint"] == "add credits"
    assert j["result"] is None and j["lines"] == ["pulling image", j["lines"][1]]
    assert serve.deploy_job("nope")["ok"] is False
    assert serve.deploy_up("fakegpu", "m", "", "vllm")["ok"] is False
    assert serve.deploy_up("", "m", "A10G", "vllm")["ok"] is False
    assert serve.deploy_up("fakegpu", "m", "A10G", "ollama")["ok"] is False
    assert not [j for j in serve._deploy_jobs.values() if j["status"] == "running"]


def test_teardown_job(fake):
    serve, calls = fake["serve"], fake["calls"]
    r = serve.deploy_down("dep-1")
    j = _wait_job(serve, r["job"])
    assert j["status"] == "done" and j["kind"] == "teardown" and j["lines"] == ["deleting endpoint"]
    assert j["result"] is None and calls["teardown"][0][0] == ("dep-1",)
    assert serve.deploy_down("")["ok"] is False


# ---------------------------------------------------------------------------
# list · status · logs · connect
# ---------------------------------------------------------------------------


def test_list_carries_cost_and_orders_live_first(fake):
    serve, calls = fake["serve"], fake["calls"]
    r = serve.deploy_list("1")
    assert r["ok"] and r["refreshed"] is True and r["live_count"] == 1
    assert calls["list_deployments"][0][0] == (True, None)
    live, gone = r["deployments"]
    assert live["status"] == "running" and gone["status"] == "deleted"
    assert live["cost"] == {"per_hour_usd": 0.75, "idle_per_hour_usd": 0.0, "accrued_usd": 1.25, "basis": "list price"}
    assert gone["cost"] is None and calls["cost"] == [(("dep-1",), {})]   # not asked for a deleted one
    assert SECRET_VALUE not in json.dumps(r) and HEADER_SECRET not in json.dumps(r)


def test_status_logs_and_unsupported_logs(fake):
    serve, calls, state = fake["serve"], fake["calls"], fake["state"]
    r = serve.deploy_status("dep-1")
    assert r["ok"] and r["deployment"]["status"] == "starting" and r["deployment"]["cost"]["accrued_usd"] == 1.25
    assert serve.deploy_status("")["ok"] is False
    r = serve.deploy_logs("dep-1", "5000")
    assert r["ok"] and r["supported"] and r["tail"] == 2000 and calls["logs"][-1][0] == ("dep-1", 2000)
    assert r["lines"][0] == "boot" and "abcdefghijklmnopqrstuvwxyz0123" not in "\n".join(r["lines"])
    state["logs_unsupported"] = True
    r = serve.deploy_logs("dep-1", "x")
    assert r["ok"] is False and r["supported"] is False and r["hint"] == "use the console"
    assert serve.deploy_logs(None)["ok"] is False


def test_connect_returns_paste_ready_lines_and_masks_headers(fake):
    serve, calls = fake["serve"], fake["calls"]
    r = serve.deploy_connect("dep-1")
    assert r["ok"] and calls["connect"][0][0] == ("dep-1", True)
    assert r["model"] == "org/model-8b" and r["backend"] == "https://dep-1.fakegpu.test/v1"
    assert r["api_key_env"] == "FAKEGPU_API_KEY"
    assert r["shell"] == "MANTIS_AGENT_MODEL=org/model-8b MANTIS_AGENT_BASE_URL=https://dep-1.fakegpu.test/v1 mantis"
    assert r["python"] == 'MantisAgentOptions(model="org/model-8b", backend="https://dep-1.fakegpu.test/v1")'
    assert HEADER_SECRET not in json.dumps(r) and r["headers"]["X-Real"]
    assert serve.deploy_connect("")["ok"] is False


def test_overview_counts_live_deployments(fake):
    serve = fake["serve"]
    assert serve.deployments_live_count() == 1
    assert serve.overview()["deployments_live"] == 1


def test_endpoints_degrade_when_the_deploy_core_is_absent(fake, monkeypatch):
    serve = fake["serve"]
    from mantis_agent.deploy import manager

    async def ni(*a, **kw):
        raise NotImplementedError
    for name in ("providers", "list_deployments", "search_models", "inspect_model", "validate"):
        monkeypatch.setattr(manager, name, ni)
    for r in (serve.deploy_providers(), serve.deploy_list(), serve.deploy_models("x"),
              serve.deploy_inspect("a/b", ttl_s=0), serve.deploy_validate("fakegpu")):
        assert r["ok"] is False and "isn't available" in r["error"] and r["hint"]
    assert serve.deployments_live_count() == 0 and serve.overview()["deployments_live"] == 0


def test_redaction_helper_keeps_env_names_readable():
    from mantis_agent import serve

    out = serve._deploy_redact({
        "auth_env": "RUNPOD_API_KEY", "api_key_env": "X_KEY", "hf_token": HF_TOKEN,
        "auth_headers": {"A": "${TOK}", "B": HEADER_SECRET}, "values": {"K": SECRET_VALUE},
        "message": "token=" + SECRET_VALUE, "credential_fields": [{"env": "K_KEY", "secret": True}],
        "lines": ["Bearer abcdefghijklmnopqrstuvwxyz0123", "fine"],
    })
    assert out["auth_env"] == "RUNPOD_API_KEY" and out["api_key_env"] == "X_KEY"
    assert out["hf_token"] != HF_TOKEN and HF_TOKEN not in json.dumps(out)
    assert out["auth_headers"] == {"A": "${TOK}", "B": serve._mask_key(HEADER_SECRET)}
    assert SECRET_VALUE not in json.dumps(out) and out["credential_fields"][0]["secret"] is True
    assert "abcdefghijklmnopqrstuvwxyz0123" not in out["lines"][0] and out["lines"][1] == "fine"


# ---------------------------------------------------------------------------
# over HTTP — routing, the LAN token, the page
# ---------------------------------------------------------------------------


def _boot(token=None, enforce_get=False):
    from mantis_agent import serve

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    httpd.token = token
    httpd.enforce_get = enforce_get
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


GETS = ("/api/deploy/providers", "/api/deploy/gpus?provider=fakegpu&min_vram=20",
        "/api/deploy/models?q=llama&sort=downloads&limit=5", "/api/deploy/inspect?model=org/model-8b",
        "/api/deploy/list?refresh=1", "/api/deploy/status?id=dep-1", "/api/deploy/logs?id=dep-1&tail=50",
        "/api/deploy/job?id=nope")
POSTS = (("/api/deploy/creds", {"provider": "fakegpu", "values": {"FAKEGPU_API_KEY": SECRET_VALUE}}),
         ("/api/deploy/validate", {"provider": "fakegpu"}),
         ("/api/deploy/up", {"provider": "fakegpu", "model": "org/model-8b", "gpu": "A10G", "engine": "vllm"}),
         ("/api/deploy/connect", {"id": "dep-1"}),
         ("/api/deploy/down", {"id": "dep-1"}))


def test_every_endpoint_routes_over_http(fake):
    httpd, base = _boot(token="tok123")
    try:
        def get(path):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, json.loads(r.read())

        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Mantis-Token": "tok123"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())

        for path in GETS:
            code, body = get(path)
            assert code == 200 and "ok" in body, path
        code, body = post(POSTS[0][0], POSTS[0][1])
        assert body["ok"] and SECRET_VALUE not in json.dumps(body)
        for path, payload in POSTS[1:]:
            code, body = post(path, payload)
            assert code == 200 and body["ok"] is True, path
            if "job" in body:
                assert _wait_job(fake["serve"], body["job"])["status"] == "done"
        code, body = get("/api/deploy/list")
        assert body["live_count"] == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_deploy_endpoints_require_the_token(fake):
    httpd, base = _boot(token="tok123", enforce_get=True)
    try:
        for path in GETS:
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(base + path, timeout=5)
            assert ei.value.code == 401, path
            req = urllib.request.Request(base + path, headers={"X-Mantis-Token": "tok123"})
            with urllib.request.urlopen(req, timeout=5) as r:
                assert r.status == 200, path
        for path, payload in POSTS:
            req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(req, timeout=5)
            assert ei.value.code == 401, path
        assert not fake["calls"].get("save_credentials") and not fake["calls"].get("deploy")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_page_carries_the_deploy_sections_and_key_binding(fake):
    httpd, base = _boot()
    try:
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            page = r.read().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()
    for marker in ('data-v="deploy">Deploy</button>', 'id="deploypad"', "loadDeploy",
                   "renderDpProviders", "openCredSheet", "renderDpPicker", "renderModelRows", "renderFit", "fitTable",
                   "confirmDeploy", "openJobSheet", "renderDeployDone", "useDeployment", "renderDeployments",
                   "openLogs", "confirmTeardown", 'd: "deploy"', '"12345678"', "dp-grid", "dp-fit", "dp-deps",
                   "deployments_live", "MANTIS_AGENT_BASE_URL", "MantisAgentOptions(", "/api/deploy/job",
                   "Add a GPU provider to deploy any model", "No deployments yet", "highlightDeployment", "while running",
                   "pairHeader", "spec-grid", "cost-big", '"Deploy to " +', "--dim", "backdrop-filter: blur(3px)", "deploys to",
                   "scale to zero", "public endpoint", "plain http", "vllm ✓", "<b>d</b> pages",
                   ".vd.tight", ".dp-drow", "refreshDeployments(false)"):
        assert marker in page, marker
    # the six new marks ship inline, no CDN
    for pid in ("runpod", "hf", "modal", "deepinfra", "baseten", "vastai"):
        assert f'"{pid}"' in page, pid
    assert "cdn." not in page and "googleapis" not in page
    # section order on the Deploy page: providers → deployments → pick a model → fit & deploy
    js = page.split("<script>")[1]
    order = [js.index('section(pad, "GPU providers · "'), js.index('section(pad, "Deployments"'),
             js.index('section(pad, "Pick a model"'), js.index('section(pad, "Fit & deploy")')]
    assert order == sorted(order), order
    # the provider toggle: pills with real marks, scoping, persistence
    for marker in ("providerToggle", "dp-ptoggle", "PROV_SHORT", "setDeployProvider", "initDeployProvider",
                   "DEPLOY_PROV_KEY", "openAddKey", 'f.provider === DEPLOY.provider', 'runpod: "RunPod"'):
        assert marker in js, marker
    # gated models are blocked before the confirm sheet, with the kind in the copy
    for marker in ("gatedBlocked", "gatedChip", "hfTokenForm", "refreshGating", "renderHfState", "hf-notice",
                   '"Needs HF token"', "huggingface.co/settings/tokens", "owner approves access by hand",
                   "click Agree", 'm.gated_kind === "manual"', "Hugging Face token: ", 'HF_TOKEN: i.value.trim()'):
        assert marker in js, marker
    assert ".hf-notice" in page and ".hf-form" in page
    # a provider whose package is missing: its own card state, an Install
    # action and a re-check — and it can never reach the spend path
    for marker in ("provReady", "reqShort", "reqCmd", "openInstallSheet", '"Needs the " + pkg + " package"',
                   '"Needs package"', "uv tool install --force", "Re-check",
                   "provReady(DEPLOY.providers.find", "openInstallSheet(p)"):
        assert marker in js, marker
    assert ".dpc.blocked" in page
    # the full hint appears once — on the card; the toggle and table stay terse
    fit = js[js.index("function fitTable("):js.index("function collectOpts(")]
    assert "requirements_hint" not in fit
    toggle = js[js.index("function providerToggle("):js.index("function openAddKey(")]
    assert "requirements_hint" not in toggle and "reqShort(p)" in toggle
    # the credential form + guide are a SHEET, never an in-card panel: a card
    # that grew to fit a guide stretched its whole grid row
    for gone in ("dp-form", "dp-guide"):
        assert gone not in page, gone
    # the card renders a link to the key page and nothing else from the guide
    card = js[js.index("function renderDpProviders("):js.index("// The credential sheet.")]
    for gone in ("credential_fields", "guide.steps", "guide.intro", "key_hint", "free_note", "input(", "dp-form"):
        assert gone not in card, "provider card still renders " + gone
    assert "trapFocus" in js and ".cs-foot" in page and ".cs-guide" in page
    assert 'grid-template-columns: repeat(auto-fill, minmax(300px, 1fr))' in page
    assert ".dp-ptoggle" in page and "flex-wrap: nowrap" in page


def test_deploy_marks_are_valid_svg_with_their_own_viewbox():
    """Each mark is self-contained and carries a tint. The viewBox is per
    entry — an official mark (Modal's) may be drawn on its own grid, and the
    page sizes marks in CSS, so nothing may hardcode a 24-unit box."""
    import re
    import xml.dom.minidom as md

    from mantis_agent.serve_logos import PROVIDER_LOGOS
    from mantis_agent.serve_ui import INDEX_HTML

    for pid in ("runpod", "hf", "modal", "deepinfra", "baseten", "vastai"):
        svg = PROVIDER_LOGOS[pid]["svg"]
        md.parseString(svg)
        assert re.search(r'viewBox="[-\d. ]+"', svg), pid
        assert "currentColor" in svg and PROVIDER_LOGOS[pid]["tint"], pid
        # the SVG namespace is the only URL allowed; nothing is fetched
        body = svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
        for bad in ("http://", "https://", "<image", "xlink", "url("):
            assert bad not in body, (pid, bad)
    assert 'viewBox="0 0 24 24"' not in INDEX_HTML.split("<script>")[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_inline_javascript_parses(tmp_path):
    from mantis_agent.serve_ui import INDEX_HTML

    m = re.search(r"<script>(.*)</script>", INDEX_HTML, re.S)
    assert m
    js = m.group(1).replace("__LOGOS__", "{}").replace("__TOKEN__", "")
    f = tmp_path / "page.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
