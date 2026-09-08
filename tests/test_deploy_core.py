"""manager.py end-to-end against a fake provider registered via register_provider."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import anyio
import httpx
import pytest
import respx

from mantis_agent.deploy import manager
from mantis_agent.deploy.base import (
    Account,
    CostEstimate,
    CredentialField,
    DeployError,
    DeployOpts,
    Deployment,
    GpuSpec,
    ModelInfo,
    NotSupported,
    register_provider,
)

FAKE_EP = "https://fake.example/v1"
GPU_S = GpuSpec(provider_id="small", family="L4", vram_gb=24, price_per_hour=0.7)
GPU_L = GpuSpec(provider_id="big", family="A100-80", vram_gb=80, price_per_hour=2.5)


@register_provider
class FakeProvider:
    id = "fake"
    display_name = "Fake Cloud"
    credential_fields = (CredentialField("FAKE_API_KEY", "Fake key", help="nowhere"),)
    engines = ("vllm", "sglang")
    console_url = "https://fake.example/console"
    scale_to_zero = True
    public_by_default = False

    calls: list[str] = []

    def configured(self) -> bool:
        return bool(os.environ.get("FAKE_API_KEY"))

    async def validate_credentials(self) -> Account:
        return Account(ok=True, provider="fake", user="tester", balance_usd=12.5)

    async def list_gpus(self) -> list[GpuSpec]:
        return [GPU_L, GPU_S]

    async def deploy(self, model, gpu, engine, opts) -> Deployment:
        self.calls.append("deploy")
        return Deployment(id="fk-1", provider="fake", model=model, engine=engine, status="starting", gpu=gpu,
                          served_model_name=opts.served_model_name or model, endpoint_url=FAKE_EP,
                          name=opts.name or "fk", opts=opts, auth_env="FAKE_API_KEY",
                          auth_headers={"X-Extra": "${FAKE_EXTRA}"}, raw={"token": "s3cret", "ok": 1})

    async def status(self, dep) -> Deployment:
        self.calls.append("status")
        dep.status = "running" if dep.status != "deleted" else "deleted"
        dep.message = "refreshed"
        return dep

    async def wait_ready(self, dep, timeout_s=1200) -> Deployment:
        self.calls.append("wait")
        dep.status = "running"
        return dep

    async def logs(self, dep, tail=200) -> AsyncIterator[str]:
        for i in range(min(tail, 3)):
            yield f"line {i}"

    async def delete(self, dep) -> None:
        self.calls.append("delete")

    async def list_deployments(self) -> list[Deployment]:
        return [Deployment(id="fk-remote", provider="fake", model="org/remote", engine="vllm", status="running",
                           gpu=GPU_S, served_model_name="org/remote", endpoint_url=FAKE_EP, auth_env="FAKE_API_KEY")]

    async def cost(self, dep) -> CostEstimate:
        return CostEstimate(per_hour_usd=2.5, idle_per_hour_usd=0.0, basis="fake")


INFO_8B = ModelInfo(id="Qwen/Qwen3-8B", source="hf", architectures=("Qwen3ForCausalLM",), params_b=8.19,
                    dtype="BF16", vllm_ok=True, est_vram_gb=22.7, context_len=40960)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_API_KEY", "fk_key")
    monkeypatch.setenv("FAKE_EXTRA", "extra-val")
    for v in ("MANTIS_AGENT_API_KEY", "MANTIS_AGENT_BASE_URL", "MANTIS_AGENT_MODEL", "MANTIS_AGENT_EXTRA_HEADERS", "HF_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    FakeProvider.calls.clear()

    async def _fast(_s: float) -> None:
        return None

    monkeypatch.setattr("mantis_agent.deploy._http.sleep", _fast)

    async def _inspect(model, *, hf_token=None):
        return INFO_8B

    monkeypatch.setattr(manager, "inspect_model", _inspect)
    yield tmp_path


def test_providers_summary_shape():
    provs = anyio.run(manager.providers)
    fake = next(p for p in provs if p["id"] == "fake")
    assert isinstance(fake, dict) and fake["configured"] is True
    assert isinstance(fake["credential_fields"][0], CredentialField)
    assert fake["engines"] == ["vllm", "sglang"] and fake["scale_to_zero"] is True
    assert {p["id"] for p in provs} >= {"runpod", "hf", "deepinfra", "baseten", "fake"}


def test_validate_and_save_credentials(_env, monkeypatch):
    monkeypatch.delenv("FAKE_API_KEY")
    acct = anyio.run(lambda: manager.validate("fake"))
    assert acct.ok is False and "FAKE_API_KEY" in acct.message
    acct = anyio.run(lambda: manager.save_credentials("fake", {"FAKE_API_KEY": "new"}))
    assert acct.ok and acct.user == "tester" and os.environ["FAKE_API_KEY"] == "new"
    assert json.loads((_env / "settings.json").read_text())["env"]["FAKE_API_KEY"] == "new"
    with pytest.raises(DeployError, match="unknown deploy provider"):
        anyio.run(lambda: manager.save_credentials("nope", {"X": "y"}))


def test_gpus_sorted_and_filtered():
    assert [g.provider_id for g in anyio.run(lambda: manager.gpus("fake"))] == ["small", "big"]
    assert [g.provider_id for g in anyio.run(lambda: manager.gpus("fake", min_vram_gb=40))] == ["big"]


def test_fit_verdict_strings():
    verdicts = anyio.run(lambda: manager.fit(INFO_8B, [GPU_S, GPU_L]))
    assert verdicts[0][0].provider_id == "big" and verdicts[0][1] == "fits"
    kind, _, reason = verdicts[1][1].partition(":")
    assert kind == "no" and reason.strip()


def test_deploy_end_to_end_then_connect_then_teardown(_env):
    lines: list[str] = []
    dep = anyio.run(lambda: manager.deploy("fake", "Qwen/Qwen3-8B", gpu="big", engine="vllm",
                                           opts=DeployOpts(hf_token="hf_x"), progress=lines.append))
    assert FakeProvider.calls == ["deploy", "wait"]
    assert dep.status == "running" and dep.id == "fk-1"
    assert any("Fit: ok" in ln for ln in lines) and any("Ready:" in ln for ln in lines)
    on_disk = json.loads((_env / "deployments.json").read_text())["deployments"][0]
    assert on_disk["id"] == "fk-1" and on_disk["opts"]["hf_token"] is None
    assert on_disk["raw"]["token"] != "s3cret" and on_disk["raw"]["ok"] == 1

    with respx.mock() as router:
        models = router.get(f"{FAKE_EP}/models").mock(side_effect=[
            httpx.Response(503, text="cold"), httpx.Response(200, json={"data": [{"id": "Qwen/Qwen3-8B"}]})])
        info = anyio.run(lambda: manager.connect("fk-1"))
    assert models.call_count == 2
    assert models.calls.last.request.headers["authorization"] == "Bearer fk_key"
    assert models.calls.last.request.headers["x-extra"] == "extra-val"
    assert info == {"model": "Qwen/Qwen3-8B", "backend": FAKE_EP, "api_key_env": "FAKE_API_KEY",
                    "headers": {"X-Extra": "extra-val"}}
    from mantis_agent import catalog

    assert catalog.get_last_model() == {"model": "Qwen/Qwen3-8B", "backend": FAKE_EP}
    assert catalog.get_recent_models()[0] == "Qwen/Qwen3-8B"
    assert os.environ["MANTIS_AGENT_API_KEY"] == "fk_key" and os.environ["MANTIS_AGENT_BASE_URL"] == FAKE_EP
    assert json.loads(os.environ["MANTIS_AGENT_EXTRA_HEADERS"]) == {"X-Extra": "extra-val"}

    assert anyio.run(lambda: manager.cost("fk-1")).per_hour_usd == 2.5
    assert anyio.run(lambda: manager.status("fk-1")).message == "refreshed"
    assert anyio.run(lambda: manager.status("fk-1", refresh=False)).message == "refreshed"

    anyio.run(lambda: manager.teardown("fk-1"))
    assert "delete" in FakeProvider.calls
    assert anyio.run(lambda: manager.list_deployments()) == []
    with pytest.raises(DeployError, match="was deleted"):
        anyio.run(lambda: manager.connect("fk-1"))


def test_deploy_refuses_a_gpu_that_does_not_fit_unless_forced():
    with pytest.raises(DeployError, match="does not fit") as ei:
        anyio.run(lambda: manager.deploy("fake", "Qwen/Qwen3-8B", gpu="small", wait=False))
    assert "--min-vram" in ei.value.hint and FakeProvider.calls == []
    dep = anyio.run(lambda: manager.deploy("fake", "Qwen/Qwen3-8B", gpu="small", wait=False,
                                           opts=DeployOpts(extra={"force": True})))
    assert dep.status == "starting" and FakeProvider.calls == ["deploy"]


def test_deploy_preflight_errors():
    with pytest.raises(DeployError, match="has no GPU"):
        anyio.run(lambda: manager.deploy("fake", "Qwen/Qwen3-8B", gpu="nope", wait=False))
    with pytest.raises(DeployError, match="does not serve engine"):
        anyio.run(lambda: manager.deploy("fake", "Qwen/Qwen3-8B", gpu="big", engine="tgi", wait=False))
    with pytest.raises(DeployError, match="not configured"):
        os.environ.pop("FAKE_API_KEY")
        anyio.run(lambda: manager.deploy("fake", "Qwen/Qwen3-8B", gpu="big", wait=False))


def test_deploy_gated_without_token_blocks(monkeypatch):
    gated = ModelInfo(id="meta-llama/Llama-3.1-8B-Instruct", source="hf", gated=True, vllm_ok=True, est_vram_gb=20)

    async def _inspect(model, *, hf_token=None):
        return gated

    monkeypatch.setattr(manager, "inspect_model", _inspect)
    with pytest.raises(DeployError, match="gated") as ei:
        anyio.run(lambda: manager.deploy("fake", gated.id, gpu="big", wait=False))
    assert "HF_TOKEN" in ei.value.hint


def test_multi_gpu_shorthand_and_progress_sink_errors_are_swallowed():
    def bad_progress(_line: str) -> None:
        raise RuntimeError("sink broke")

    dep = anyio.run(lambda: manager.deploy("fake", "Qwen/Qwen3-8B", gpu="big:2", wait=False, progress=bad_progress))
    assert dep.gpu.count == 2 and dep.gpu.price_per_hour == 5.0


def test_list_refresh_adopts_remote_and_drops_vanished(_env):
    anyio.run(lambda: manager.deploy("fake", "Qwen/Qwen3-8B", gpu="big", wait=False))
    deps = anyio.run(lambda: manager.list_deployments(refresh=True, provider_id="fake"))
    ids = {d.id for d in deps}
    assert ids == {"fk-remote"}  # fk-1 is not in the provider's list → marked deleted; remote adopted
    assert anyio.run(lambda: manager.status("fk-1", refresh=False)).status == "deleted"


def test_logs_iterator_and_not_supported(monkeypatch):
    anyio.run(lambda: manager.deploy("fake", "Qwen/Qwen3-8B", gpu="big", wait=False))

    async def collect():
        return [ln async for ln in manager.logs("fk-1", tail=2)]

    assert anyio.run(collect) == ["line 0", "line 1"]

    def no_logs(self, dep, tail=200):
        raise NotSupported("nope", hint="console")

    monkeypatch.setattr(FakeProvider, "logs", no_logs)
    with pytest.raises(NotSupported):
        anyio.run(collect)


def test_connect_errors(monkeypatch):
    anyio.run(lambda: manager.deploy("fake", "Qwen/Qwen3-8B", gpu="big", wait=False))
    monkeypatch.setattr(manager, "CONNECT_TIMEOUT_S", 10.0)
    with respx.mock() as router:
        router.get(f"{FAKE_EP}/models").mock(return_value=httpx.Response(401))
        with pytest.raises(DeployError, match="refused our credentials"):
            anyio.run(lambda: manager.connect("fk-1"))
        router.get(f"{FAKE_EP}/models").mock(return_value=httpx.Response(503))
        with pytest.raises(DeployError, match="not ready after"):
            anyio.run(lambda: manager.connect("fk-1"))
    monkeypatch.delenv("FAKE_API_KEY")
    with pytest.raises(DeployError, match="FAKE_API_KEY is not set"):
        anyio.run(lambda: manager.connect("fk-1"))


def test_search_models_curated_when_empty(monkeypatch):
    async def fake_search(query="", *, limit=25, sort="trending", token=None, gated=None):
        return [{"id": "org/hit", "safetensors": {"parameters": {"BF16": 1e9}}, "config": {"architectures": ["LlamaForCausalLM"]}}]

    monkeypatch.setattr("mantis_agent.deploy.hf_hub.search", fake_search)
    out = anyio.run(lambda: manager.search_models("", limit=5))
    assert len(out) == 5 and out[0].id == "Qwen/Qwen3-8B" and "curated" in out[0].tags
    out = anyio.run(lambda: manager.search_models("hit", limit=5))
    assert [m.id for m in out] == ["org/hit"] and out[0].vllm_ok is True


# ---------------------------------------------------------------------------
# The package holds a `providers` SUBPACKAGE (the adapters). Re-exporting a
# `providers` FUNCTION beside it meant the first adapter import rebound the
# name to the module, so the function stopped being callable partway through a
# process. The list is reached through `manager.providers` instead.
# ---------------------------------------------------------------------------


def test_the_package_does_not_shadow_its_adapters_subpackage() -> None:
    import importlib

    import anyio

    import mantis_agent.deploy as deploy_pkg
    from mantis_agent.deploy import manager

    assert "providers" not in deploy_pkg.__all__

    # Importing the adapters is what used to do the shadowing; the manager's
    # entry point must survive it, twice.
    importlib.import_module("mantis_agent.deploy.providers")
    first = anyio.run(manager.providers)
    importlib.import_module("mantis_agent.deploy.providers")
    second = anyio.run(manager.providers)

    assert first and len(first) == len(second)
    assert {p["id"] for p in first} == {p["id"] for p in second}
