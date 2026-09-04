"""Hugging Face Inference Endpoints adapter against a mocked API (respx)."""

from __future__ import annotations

import json

import anyio
import httpx
import pytest
import respx

from mantis_agent.deploy.base import DeployError, DeployOpts, Deployment, GpuSpec
from mantis_agent.deploy.providers.hf_endpoints import API_BASE, HFEndpointsProvider

WHOAMI = "https://huggingface.co/api/whoami-v2"
A100 = GpuSpec(provider_id="aws/us-east-1/nvidia-a100/x1", family="A100-80", vram_gb=80, price_per_hour=2.5,
               region="us-east-1")
EP_URL = "https://abc123.us-east-1.aws.endpoints.huggingface.cloud"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    monkeypatch.delenv("HF_ENDPOINTS_NAMESPACE", raising=False)

    async def _fast(_s: float) -> None:
        return None

    monkeypatch.setattr("mantis_agent.deploy._http.sleep", _fast)


def _whoami(router):
    return router.get(WHOAMI).mock(return_value=httpx.Response(200, json={"name": "teddy", "orgs": [{"name": "acme"}]}))


def test_validate_and_namespace():
    with respx.mock() as router:
        _whoami(router)
        acct = anyio.run(HFEndpointsProvider().validate_credentials)
    assert acct.ok and acct.user == "teddy" and "acme" in acct.message


def test_bad_token_hint(monkeypatch):
    with respx.mock() as router:
        router.get(WHOAMI).mock(return_value=httpx.Response(401, json={"error": "Invalid credentials"}))
        with pytest.raises(DeployError) as ei:
            anyio.run(HFEndpointsProvider().validate_credentials)
    assert "HF_TOKEN" in ei.value.hint
    monkeypatch.delenv("HF_TOKEN")
    assert HFEndpointsProvider().configured() is False


def test_list_gpus_parses_provider_catalogue_and_falls_back():
    with respx.mock() as router:
        _whoami(router)
        router.get(f"{API_BASE}/v2/provider/teddy").mock(return_value=httpx.Response(200, json={"vendors": [
            {"name": "aws", "regions": [{"name": "us-east-1", "computes": [
                {"accelerator": "gpu", "instanceType": "nvidia-a100", "instanceSize": "x1",
                 "numberOfAccelerators": 1, "pricePerHour": 2.5, "status": "available"},
                {"accelerator": "gpu", "instanceType": "nvidia-l4", "instanceSize": "x1",
                 "numberOfAccelerators": 1, "pricePerHour": 0.8, "status": "not_available"},
                {"accelerator": "cpu", "instanceType": "intel-icl", "instanceSize": "x1", "pricePerHour": 0.03},
            ]}]},
        ]}))
        gpus = anyio.run(HFEndpointsProvider().list_gpus)
    assert [g.provider_id for g in gpus] == ["aws/us-east-1/nvidia-l4/x1", "aws/us-east-1/nvidia-a100/x1"]
    assert gpus[0].available is False and gpus[1].vram_gb == 80 and gpus[1].family == "A100-80"
    with respx.mock() as router:
        _whoami(router)
        router.get(f"{API_BASE}/v2/provider/teddy").mock(return_value=httpx.Response(500))
        static = anyio.run(HFEndpointsProvider().list_gpus)
    assert any(g.provider_id == "aws/us-east-1/nvidia-a100/x1" and g.price_per_hour == 2.5 for g in static)


def test_deploy_sends_custom_image_and_scale_to_zero():
    with respx.mock() as router:
        _whoami(router)
        create = router.post(f"{API_BASE}/v2/endpoint/teddy").mock(return_value=httpx.Response(200, json={
            "name": "mantis-qwen-qwen3-8b", "status": {"state": "pending", "url": None}}))
        dep = anyio.run(lambda: HFEndpointsProvider().deploy(
            "Qwen/Qwen3-8B", A100, "vllm", DeployOpts(min_replicas=0, max_replicas=1, idle_timeout_s=900, max_model_len=16384)))
    sent = json.loads(create.calls.last.request.read())
    assert sent["type"] == "protected" and sent["provider"] == {"vendor": "aws", "region": "us-east-1"}
    assert sent["compute"]["instanceType"] == "nvidia-a100" and sent["compute"]["instanceSize"] == "x1"
    assert sent["compute"]["scaling"] == {"minReplica": 0, "maxReplica": 1, "scaleToZeroTimeout": 15}
    assert sent["model"]["repository"] == "Qwen/Qwen3-8B" and sent["model"]["task"] == "text-generation"
    vllm = sent["model"]["image"]["vLLM"]
    assert vllm["url"].startswith("vllm/vllm-openai:") and vllm["port"] == 8000 and vllm["tensorParallelSize"] == 1
    assert vllm["maxModelLen"] == 16384
    assert sent["model"]["secrets"] == {"HF_TOKEN": "hf_test"}
    assert dep.id == "mantis-qwen-qwen3-8b" and dep.status == "pending" and dep.endpoint_url is None
    assert dep.auth_env == "HF_TOKEN" and dep.raw["namespace"] == "teddy"
    assert "hf_test" not in json.dumps(dep.raw)


def test_engine_keys_and_bad_gpu_id():
    with respx.mock() as router:
        _whoami(router)
        create = router.post(f"{API_BASE}/v2/endpoint/teddy").mock(return_value=httpx.Response(200, json={"name": "x", "status": {"state": "pending"}}))
        anyio.run(lambda: HFEndpointsProvider().deploy("org/m", A100, "sglang", DeployOpts(name="x")))
        assert "sGLang" in json.loads(create.calls.last.request.read())["model"]["image"]
        anyio.run(lambda: HFEndpointsProvider().deploy("org/m", A100, "llamacpp", DeployOpts(name="x")))
        assert "llamacpp" in json.loads(create.calls.last.request.read())["model"]["image"]
        with pytest.raises(DeployError, match="vendor/region"):
            anyio.run(lambda: HFEndpointsProvider().deploy(
                "org/m", GpuSpec(provider_id="nvidia-a100", family="A100-80", vram_gb=80), "vllm", DeployOpts()))


def _dep(status="pending", url=None) -> Deployment:
    return Deployment(id="mantis-qwen-qwen3-8b", provider="hf", model="Qwen/Qwen3-8B", engine="vllm", status=status,
                      gpu=A100, served_model_name="Qwen/Qwen3-8B", endpoint_url=url, auth_env="HF_TOKEN",
                      opts=DeployOpts(min_replicas=0, max_replicas=1), raw={"namespace": "teddy"})


def test_wait_ready_polls_status_then_models_with_scale_up_header():
    dep = _dep()
    with respx.mock() as router:
        router.get(f"{API_BASE}/v2/endpoint/teddy/mantis-qwen-qwen3-8b").mock(side_effect=[
            httpx.Response(200, json={"status": {"state": "initializing"}}),
            httpx.Response(200, json={"status": {"state": "running", "url": EP_URL}}),
        ])
        models = router.get(f"{EP_URL}/v1/models").mock(side_effect=[
            httpx.Response(503, json={"error": "scaling up"}),
            httpx.Response(200, json={"data": []}),
        ])
        out = anyio.run(lambda: HFEndpointsProvider().wait_ready(dep, timeout_s=120))
    assert out.status == "running" and out.endpoint_url == f"{EP_URL}/v1"
    assert models.calls.last.request.headers["x-scale-up-timeout"] == "600"
    assert models.calls.last.request.headers["authorization"] == "Bearer hf_test"


def test_wait_ready_fails_fast_on_failed_state():
    with respx.mock() as router:
        router.get(f"{API_BASE}/v2/endpoint/teddy/mantis-qwen-qwen3-8b").mock(
            return_value=httpx.Response(200, json={"status": {"state": "failed", "message": "quota exceeded"}}))
        with pytest.raises(DeployError, match="quota exceeded"):
            anyio.run(lambda: HFEndpointsProvider().wait_ready(_dep(), timeout_s=30))


def test_status_map_logs_delete_list_cost():
    dep = _dep("running", f"{EP_URL}/v1")
    with respx.mock() as router:
        router.get(f"{API_BASE}/v2/endpoint/teddy/mantis-qwen-qwen3-8b").mock(
            return_value=httpx.Response(200, json={"status": {"state": "scaledToZero", "url": EP_URL}}))
        assert anyio.run(lambda: HFEndpointsProvider().status(dep)).status == "scaled_to_zero"
        router.get(f"{API_BASE}/v3/endpoint/teddy/mantis-qwen-qwen3-8b/logs").mock(
            return_value=httpx.Response(200, json={"logs": [{"timestamp": "t1", "message": "boot"}, {"timestamp": "t2", "message": "ready"}]}))

        async def collect():
            return [line async for line in HFEndpointsProvider().logs(dep, tail=10)]

        assert anyio.run(collect) == ["t1 boot", "t2 ready"]
        d = router.delete(f"{API_BASE}/v2/endpoint/teddy/mantis-qwen-qwen3-8b").mock(return_value=httpx.Response(202))
        anyio.run(lambda: HFEndpointsProvider().delete(dep))
        assert d.called
        _whoami(router)
        router.get(f"{API_BASE}/v2/endpoint/teddy").mock(return_value=httpx.Response(200, json={"items": [{
            "name": "other-ep", "status": {"state": "running", "url": "https://x.endpoints.huggingface.cloud"},
            "model": {"repository": "meta-llama/Llama-3.1-8B-Instruct", "image": {"tgi": {}}},
            "compute": {"instanceType": "nvidia-l4", "instanceSize": "x1", "scaling": {"minReplica": 1, "maxReplica": 2}},
            "provider": {"vendor": "aws", "region": "us-east-1"}}]}))
        listed = anyio.run(HFEndpointsProvider().list_deployments)
    assert listed[0].id == "other-ep" and listed[0].engine == "tgi" and listed[0].status == "running"
    assert listed[0].endpoint_url == "https://x.endpoints.huggingface.cloud/v1" and listed[0].gpu.family == "L4"
    cost = anyio.run(lambda: HFEndpointsProvider().cost(listed[0]))
    assert cost.per_hour_usd == pytest.approx(0.8 * 2) and cost.idle_per_hour_usd == pytest.approx(0.8)
