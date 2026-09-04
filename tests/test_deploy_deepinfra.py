"""DeepInfra custom-LLM adapter against a mocked API (respx)."""

from __future__ import annotations

import json

import anyio
import httpx
import pytest
import respx

from mantis_agent.deploy.base import DeployError, DeployOpts, Deployment, GpuSpec, NotSupported
from mantis_agent.deploy.providers.deepinfra import API_BASE, GPUS, OPENAI_BASE, DeepInfraProvider

H100 = GpuSpec(provider_id="H100-80GB", family="H100", vram_gb=80, price_per_hour=2.2)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DEEPINFRA_API_KEY", "di_test")
    monkeypatch.delenv("HF_TOKEN", raising=False)

    async def _fast(_s: float) -> None:
        return None

    monkeypatch.setattr("mantis_agent.deploy._http.sleep", _fast)


def test_static_gpu_table():
    gpus = anyio.run(DeepInfraProvider().list_gpus)
    singles = [g for g in gpus if g.count == 1]
    assert {g.provider_id for g in singles} == set(GPUS)
    assert min(g.price_per_hour for g in singles) == 0.89
    assert any(g.provider_id == "H100-80GB" and g.count == 4 and g.total_vram_gb == 320 for g in gpus)


def test_deploy_one_call_and_served_model_name(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    with respx.mock() as router:
        create = router.post(f"{API_BASE}/deploy/llm").mock(
            return_value=httpx.Response(200, json={"deploy_id": "dp_42", "status": "initializing"}))
        dep = anyio.run(lambda: DeepInfraProvider().deploy(
            "Qwen/Qwen3-8B", H100, "vllm", DeployOpts(min_replicas=0, max_replicas=2)))
    sent = json.loads(create.calls.last.request.read())
    assert sent["gpu"] == "H100-80GB" and sent["num_gpus"] == 1
    assert sent["hf"] == {"repo": "Qwen/Qwen3-8B", "token": "hf_x"}
    assert sent["settings"] == {"min_instances": 0, "max_instances": 2}
    assert create.calls.last.request.headers["authorization"] == "Bearer di_test"
    assert dep.id == "dp_42" and dep.served_model_name == "deploy_id:dp_42"
    assert dep.endpoint_url == OPENAI_BASE and dep.auth_env == "DEEPINFRA_API_KEY"
    assert dep.status == "starting" and "hf_x" not in json.dumps(dep.raw)


def test_deploy_rejects_quantization_and_unknown_gpu():
    with pytest.raises(DeployError, match="quantized"):
        anyio.run(lambda: DeepInfraProvider().deploy("a/b", H100, "vllm", DeployOpts(quantization="awq")))
    with pytest.raises(DeployError, match="unknown GPU"):
        anyio.run(lambda: DeepInfraProvider().deploy(
            "a/b", GpuSpec(provider_id="L4", family="L4", vram_gb=24), "vllm", DeployOpts()))


def test_401_and_402_hints():
    with respx.mock() as router:
        router.post(f"{API_BASE}/deploy/llm").mock(return_value=httpx.Response(401, json={"detail": "bad token"}))
        with pytest.raises(DeployError) as ei:
            anyio.run(lambda: DeepInfraProvider().deploy("a/b", H100, "vllm", DeployOpts()))
        assert "DEEPINFRA_API_KEY" in ei.value.hint and "bad token" in str(ei.value)
        router.post(f"{API_BASE}/deploy/llm").mock(return_value=httpx.Response(402, json={"detail": "insufficient balance"}))
        with pytest.raises(DeployError) as ei:
            anyio.run(lambda: DeepInfraProvider().deploy("a/b", H100, "vllm", DeployOpts()))
        assert "billing" in ei.value.hint or "quota" in ei.value.hint


def _dep() -> Deployment:
    return Deployment(id="dp_42", provider="deepinfra", model="Qwen/Qwen3-8B", engine="vllm", status="starting",
                      gpu=H100, served_model_name="deploy_id:dp_42", endpoint_url=OPENAI_BASE,
                      auth_env="DEEPINFRA_API_KEY", opts=DeployOpts(min_replicas=0, max_replicas=1))


def test_wait_ready_polls_status():
    with respx.mock() as router:
        st = router.get(f"{API_BASE}/deploy/dp_42").mock(side_effect=[
            httpx.Response(200, json={"deploy_id": "dp_42", "status": "initializing"}),
            httpx.Response(200, json={"deploy_id": "dp_42", "status": "running"}),
        ])
        out = anyio.run(lambda: DeepInfraProvider().wait_ready(_dep(), timeout_s=60))
    assert out.status == "running" and st.call_count == 2
    with respx.mock() as router:
        router.get(f"{API_BASE}/deploy/dp_42").mock(return_value=httpx.Response(200, json={"status": "failed", "status_message": "OOM"}))
        with pytest.raises(DeployError, match="OOM"):
            anyio.run(lambda: DeepInfraProvider().wait_ready(_dep(), timeout_s=10))


def test_logs_not_supported():
    with pytest.raises(NotSupported) as ei:
        DeepInfraProvider().logs(_dep())
    assert "deepinfra.com" in ei.value.hint


def test_delete_list_and_cost():
    with respx.mock() as router:
        d = router.delete(f"{API_BASE}/deploy/dp_42").mock(return_value=httpx.Response(200, json={"ok": True}))
        anyio.run(lambda: DeepInfraProvider().delete(_dep()))
        assert d.called
        router.get(f"{API_BASE}/deploy/list").mock(return_value=httpx.Response(200, json=[
            {"deploy_id": "dp_7", "model_name": "mine", "gpu": "A100-80GB", "num_gpus": 2,
             "hf": {"repo": "meta-llama/Llama-3.1-8B-Instruct"}, "status": "running",
             "settings": {"min_instances": 1, "max_instances": 1}}]))
        listed = anyio.run(DeepInfraProvider().list_deployments)
        acct = anyio.run(DeepInfraProvider().validate_credentials)
    assert acct.ok and "1 custom" in acct.message
    d0 = listed[0]
    assert d0.served_model_name == "deploy_id:dp_7" and d0.status == "running" and d0.gpu.count == 2
    cost = anyio.run(lambda: DeepInfraProvider().cost(d0))
    assert cost.per_hour_usd == pytest.approx(0.89 * 2) and cost.idle_per_hour_usd == pytest.approx(0.89 * 2)
    assert anyio.run(lambda: DeepInfraProvider().cost(_dep())).idle_per_hour_usd == 0.0
