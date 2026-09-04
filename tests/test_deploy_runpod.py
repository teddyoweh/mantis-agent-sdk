"""RunPod Serverless adapter against a mocked REST v2 (respx)."""

from __future__ import annotations

import anyio
import httpx
import pytest
import respx

from mantis_agent.deploy.base import DeployError, DeployOpts, GpuSpec
from mantis_agent.deploy.providers.runpod import CONTROL_BASE, INFERENCE_BASE, RunPodProvider

CTRL = CONTROL_BASE
INF = INFERENCE_BASE
A100 = GpuSpec(provider_id="AMPERE_80", family="A100-80", vram_gb=80, price_per_hour=2.72)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "rp_test")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("MANTIS_RUNPOD_VLLM_IMAGE", raising=False)

    async def _fast(_s: float) -> None:
        return None

    monkeypatch.setattr("mantis_agent.deploy._http.sleep", _fast)


def test_configured_and_unconfigured(monkeypatch):
    assert RunPodProvider().configured() is True
    monkeypatch.delenv("RUNPOD_API_KEY")
    assert RunPodProvider().configured() is False
    with pytest.raises(DeployError) as ei:
        anyio.run(RunPodProvider().list_gpus)
    assert "deploy creds runpod" in ei.value.hint


def test_list_gpus_overlays_live_prices_on_pools():
    with respx.mock() as router:
        router.get(f"{CTRL}/catalog/gpus").mock(return_value=httpx.Response(200, json={"gpus": [
            {"id": "NVIDIA A100 80GB PCIe", "name": "NVIDIA A100 80GB PCIe", "memory": 80,
             "price": {"serverless": 2.50, "secure": 1.39}, "availability": "HIGH"},
            {"id": "NVIDIA H100 80GB HBM3", "name": "NVIDIA H100 80GB HBM3", "memory": 80,
             "price": {"serverless": 0.00116}, "availability": "LOW"},
        ]}))
        gpus = anyio.run(RunPodProvider().list_gpus)
    by_id = {g.provider_id: g for g in gpus}
    assert by_id["AMPERE_80"].price_per_hour == 2.5 and by_id["AMPERE_80"].available is True
    assert by_id["ADA_80_PRO"].price_per_hour == pytest.approx(0.00116 * 3600, abs=0.01)
    assert by_id["HOPPER_141"].price_per_hour == 5.58  # static fallback for pools not in the reply
    assert gpus[0].price_per_hour <= gpus[-1].price_per_hour


def test_list_gpus_static_when_catalogue_fails():
    with respx.mock() as router:
        router.get(f"{CTRL}/catalog/gpus").mock(return_value=httpx.Response(500, json={"error": "boom"}))
        gpus = anyio.run(RunPodProvider().list_gpus)
    assert {g.provider_id for g in gpus} >= {"AMPERE_80", "ADA_80_PRO", "ADA_24"}


def test_deploy_builds_the_documented_body(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    with respx.mock() as router:
        create = router.post(f"{CTRL}/serverless").mock(
            return_value=httpx.Response(200, json={"id": "ep123", "name": "mantis-qwen-qwen3-8b"}))
        dep = anyio.run(lambda: RunPodProvider().deploy(
            "Qwen/Qwen3-8B", A100, "vllm",
            DeployOpts(max_model_len=8192, min_replicas=0, max_replicas=2, idle_timeout_s=60, trust_remote_code=True)))
    body = create.calls.last.request.read()
    import json

    sent = json.loads(body)
    assert sent["image"].startswith("runpod/worker-v1-vllm:")
    assert sent["gpu"] == {"pools": ["AMPERE_80"], "count": 1}
    assert sent["workers"] == {"min": 0, "max": 2, "idleTimeout": 60}
    assert sent["flashboot"] == "FLASHBOOT" and sent["type"] == "QUEUE"
    assert sent["env"]["MODEL_NAME"] == "Qwen/Qwen3-8B" and sent["env"]["MAX_MODEL_LEN"] == "8192"
    assert sent["env"]["HF_TOKEN"] == "hf_secret" and sent["env"]["TENSOR_PARALLEL_SIZE"] == "1"
    assert sent["env"]["TRUST_REMOTE_CODE"] == "1"
    assert create.calls.last.request.headers["authorization"] == "Bearer rp_test"
    assert dep.id == "ep123" and dep.provider == "runpod"
    assert dep.endpoint_url == f"{INF}/ep123/openai/v1"
    assert dep.served_model_name == "Qwen/Qwen3-8B" and dep.auth_env == "RUNPOD_API_KEY"
    assert dep.status == "scaled_to_zero"
    assert "hf_secret" not in json.dumps(dep.raw)  # secrets never in raw


def test_deploy_image_override(monkeypatch):
    monkeypatch.setenv("MANTIS_RUNPOD_VLLM_IMAGE", "runpod/worker-v1-vllm:v2.7.0stable-cuda12.1.0")
    with respx.mock() as router:
        create = router.post(f"{CTRL}/serverless").mock(return_value=httpx.Response(200, json={"id": "e"}))
        anyio.run(lambda: RunPodProvider().deploy("Qwen/Qwen3-8B", A100, "vllm", DeployOpts()))
    import json

    assert json.loads(create.calls.last.request.read())["image"].endswith("v2.7.0stable-cuda12.1.0")


def test_401_has_a_credential_hint():
    with respx.mock() as router:
        router.post(f"{CTRL}/serverless").mock(return_value=httpx.Response(401, json={"error": "Unauthorized"}))
        with pytest.raises(DeployError) as ei:
            anyio.run(lambda: RunPodProvider().deploy("Qwen/Qwen3-8B", A100, "vllm", DeployOpts()))
    assert "RUNPOD_API_KEY" in ei.value.hint and "HTTP 401" in str(ei.value)
    assert ei.value.provider == "runpod"


def test_wait_ready_tolerates_cold_start_503():
    dep = _dep()
    with respx.mock() as router:
        models = router.get(f"{INF}/ep123/openai/v1/models").mock(side_effect=[
            httpx.Response(503, text="no workers"),
            httpx.Response(502, text="gateway"),
            httpx.Response(200, json={"data": [{"id": "Qwen/Qwen3-8B"}]}),
        ])
        out = anyio.run(lambda: RunPodProvider().wait_ready(dep, timeout_s=60))
    assert out.status == "running" and models.call_count == 3


def test_status_reads_endpoint_and_health():
    dep = _dep()
    with respx.mock() as router:
        router.get(f"{CTRL}/serverless/ep123").mock(return_value=httpx.Response(200, json={
            "id": "ep123", "workers": {"min": 0, "max": 2}, "env": {"HF_TOKEN": "hf_secret"}}))
        router.get(f"{INF}/ep123/health").mock(return_value=httpx.Response(200, json={
            "jobs": {"inQueue": 0}, "workers": {"idle": 1, "running": 0}}))
        out = anyio.run(lambda: RunPodProvider().status(dep))
    assert out.status == "running" and "idle=1" in out.message
    assert out.raw["endpoint"]["env"]["HF_TOKEN"] != "hf_secret"
    with respx.mock() as router:
        router.get(f"{CTRL}/serverless/ep123").mock(return_value=httpx.Response(200, json={"id": "ep123", "workers": {"min": 0}}))
        router.get(f"{INF}/ep123/health").mock(return_value=httpx.Response(200, json={"workers": {"idle": 0, "running": 0}}))
        assert anyio.run(lambda: RunPodProvider().status(dep)).status == "scaled_to_zero"
    with respx.mock() as router:
        router.get(f"{CTRL}/serverless/ep123").mock(return_value=httpx.Response(404))
        assert anyio.run(lambda: RunPodProvider().status(dep)).status == "deleted"


def test_logs_stream_sse_from_each_worker():
    dep = _dep()
    with respx.mock() as router:
        router.get(f"{CTRL}/serverless/ep123/workers").mock(return_value=httpx.Response(200, json={"workers": [{"id": "w1", "status": "IDLE"}]}))
        router.get(f"{CTRL}/serverless/ep123/workers/w1/logs").mock(return_value=httpx.Response(
            200, text="event: log\ndata: INFO vllm started\n\ndata: INFO model loaded\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"}))

        async def collect():
            return [line async for line in RunPodProvider().logs(dep, tail=50)]

        lines = anyio.run(collect)
    assert lines[0].startswith("--- worker w1")
    assert lines[1:] == ["INFO vllm started", "INFO model loaded"]


def test_delete_and_list_and_cost():
    dep = _dep()
    with respx.mock() as router:
        d = router.delete(f"{CTRL}/serverless/ep123").mock(return_value=httpx.Response(204))
        anyio.run(lambda: RunPodProvider().delete(dep))
        assert d.called
        router.get(f"{CTRL}/serverless").mock(return_value=httpx.Response(200, json=[{
            "id": "ep9", "name": "other", "image": "runpod/worker-v1-vllm:stable-cuda12.1.0",
            "env": {"MODEL_NAME": "meta-llama/Llama-3.1-8B-Instruct"}, "gpu": {"pools": ["ADA_80_PRO"], "count": 1},
            "workers": {"min": 0, "max": 1, "idleTimeout": 5}}]))
        listed = anyio.run(RunPodProvider().list_deployments)
    assert listed[0].id == "ep9" and listed[0].model == "meta-llama/Llama-3.1-8B-Instruct"
    assert listed[0].gpu.family == "H100" and listed[0].endpoint_url == f"{INF}/ep9/openai/v1"
    cost = anyio.run(lambda: RunPodProvider().cost(dep))
    assert cost.per_hour_usd == pytest.approx(2.72 * 2) and cost.idle_per_hour_usd == 0.0
    dep.opts.min_replicas = 1
    assert anyio.run(lambda: RunPodProvider().cost(dep)).idle_per_hour_usd == pytest.approx(2.72)


def _dep():
    from mantis_agent.deploy.base import Deployment

    return Deployment(id="ep123", provider="runpod", model="Qwen/Qwen3-8B", engine="vllm", status="scaled_to_zero",
                      gpu=A100, served_model_name="Qwen/Qwen3-8B", endpoint_url=f"{INF}/ep123/openai/v1",
                      opts=DeployOpts(min_replicas=0, max_replicas=2), auth_env="RUNPOD_API_KEY")
