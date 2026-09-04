"""Baseten adapter: the prepare-upload → tarball → POST /models recipe (respx)."""

from __future__ import annotations

import io
import json
import tarfile

import anyio
import httpx
import pytest
import respx

from mantis_agent.deploy.base import DeployError, DeployOpts, Deployment, GpuSpec
from mantis_agent.deploy.providers.baseten import (
    API_BASE,
    BasetenProvider,
    build_archive,
    build_config,
)

H100 = GpuSpec(provider_id="H100", family="H100", vram_gb=80, price_per_hour=6.5)
UPLOAD = "https://s3.example.com/upload/abc?sig=1"
EP = "https://model-m1.api.baseten.co/environments/production/sync/v1"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "bt_test")
    monkeypatch.delenv("HF_TOKEN", raising=False)

    async def _fast(_s: float) -> None:
        return None

    monkeypatch.setattr("mantis_agent.deploy._http.sleep", _fast)


def test_build_config_is_the_documented_docker_server_recipe():
    cfg = build_config("Qwen/Qwen3-8B", GpuSpec(provider_id="H100", family="H100", vram_gb=80, count=2), "vllm",
                       DeployOpts(max_model_len=8192, served_model_name="qwen"), name="mantis-qwen", use_hf_secret=True)
    assert cfg["base_image"]["image"].startswith("vllm/vllm-openai:")
    assert cfg["weights"][0]["source"] == "hf://Qwen/Qwen3-8B@main"
    assert cfg["weights"][0]["auth_secret_name"] == "hf_access_token" and cfg["secrets"] == {"hf_access_token": None}
    assert cfg["resources"] == {"accelerator": "H100:2", "use_gpu": True}
    start = cfg["docker_server"]["start_command"]
    assert start.startswith("vllm serve /app/checkpoint") and "--served-model-name qwen" in start
    assert "--tensor-parallel-size 2" in start and "--max-model-len 8192" in start
    assert cfg["docker_server"]["server_port"] == 8000 and cfg["docker_server"]["predict_endpoint"] == "/v1/chat/completions"
    plain = build_config("a/b", H100, "vllm", DeployOpts(), name="x", use_hf_secret=False)
    assert "secrets" not in plain and "auth_secret_name" not in plain["weights"][0]


def test_archive_holds_a_config_yaml():
    data = build_archive(build_config("Qwen/Qwen3-8B", H100, "vllm", DeployOpts(), name="n", use_hf_secret=False))
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
        yaml_text = tar.extractfile("config.yaml").read().decode()
    assert "config.yaml" in names and "model" in names
    assert "hf://Qwen/Qwen3-8B@main" in yaml_text and "start_command:" in yaml_text
    assert "accelerator: \"H100\"" in yaml_text


def test_deploy_runs_the_four_step_flow(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    with respx.mock() as router:
        secret = router.post(f"{API_BASE}/secrets").mock(return_value=httpx.Response(200, json={"name": "hf_access_token"}))
        prep = router.post(f"{API_BASE}/prepare_model_upload").mock(
            return_value=httpx.Response(200, json={"upload_url": UPLOAD, "model_archive_id": "arch-9"}))
        put = router.put(UPLOAD).mock(return_value=httpx.Response(200))
        create = router.post(f"{API_BASE}/models").mock(
            return_value=httpx.Response(200, json={"model_id": "m1", "model_deployment_id": "d1"}))
        auto = router.patch(f"{API_BASE}/models/m1/deployments/d1/autoscaling_settings").mock(
            return_value=httpx.Response(200, json={}))
        dep = anyio.run(lambda: BasetenProvider().deploy(
            "Qwen/Qwen3-8B", H100, "vllm", DeployOpts(min_replicas=0, max_replicas=1, idle_timeout_s=600)))
    assert json.loads(secret.calls.last.request.read()) == {"name": "hf_access_token", "value": "hf_secret"}
    assert prep.called and put.called and len(put.calls.last.request.read()) > 50
    sent = json.loads(create.calls.last.request.read())
    assert sent["source"] == {"kind": "model_archive", "model_archive_id": "arch-9"}
    assert sent["raw_config"]["weights"][0]["source"] == "hf://Qwen/Qwen3-8B@main"
    assert json.loads(auto.calls.last.request.read()) == {"min_replica": 0, "max_replica": 1, "scale_down_delay": 600}
    assert dep.id == "m1" and dep.raw["model_id"] == "m1" and dep.raw["deployment_id"] == "d1"
    assert dep.endpoint_url == EP and dep.served_model_name == "Qwen/Qwen3-8B"
    assert dep.auth_env == "BASETEN_API_KEY" and dep.status == "building"
    assert "hf_secret" not in json.dumps(dep.raw)


def test_401_hint_and_upload_failure():
    with respx.mock() as router:
        router.post(f"{API_BASE}/prepare_model_upload").mock(return_value=httpx.Response(401, json={"error": "invalid api key"}))
        with pytest.raises(DeployError) as ei:
            anyio.run(lambda: BasetenProvider().deploy("a/b", H100, "vllm", DeployOpts()))
        assert "BASETEN_API_KEY" in ei.value.hint
        router.post(f"{API_BASE}/prepare_model_upload").mock(return_value=httpx.Response(200, json={"upload_url": UPLOAD}))
        router.put(UPLOAD).mock(return_value=httpx.Response(403, text="denied"))
        with pytest.raises(DeployError, match="upload rejected"):
            anyio.run(lambda: BasetenProvider().deploy("a/b", H100, "vllm", DeployOpts()))


def _dep() -> Deployment:
    return Deployment(id="m1", provider="baseten", model="Qwen/Qwen3-8B", engine="vllm", status="building",
                      gpu=H100, served_model_name="Qwen/Qwen3-8B", endpoint_url=EP, auth_env="BASETEN_API_KEY",
                      opts=DeployOpts(min_replicas=0, max_replicas=1), raw={"model_id": "m1", "deployment_id": "d1"})


def test_wait_ready_building_then_active_then_cold_503():
    with respx.mock() as router:
        router.get(f"{API_BASE}/models/m1/deployments/d1").mock(side_effect=[
            httpx.Response(200, json={"id": "d1", "status": "BUILDING"}),
            httpx.Response(200, json={"id": "d1", "status": "LOADING_MODEL"}),
            httpx.Response(200, json={"id": "d1", "status": "ACTIVE", "active_replica_count": 1}),
        ])
        models = router.get(f"{EP}/models").mock(side_effect=[
            httpx.Response(503, json={"error": "waking"}), httpx.Response(200, json={"data": []})])
        out = anyio.run(lambda: BasetenProvider().wait_ready(_dep(), timeout_s=120))
    assert out.status == "running" and models.call_count == 2
    assert models.calls.last.request.headers["authorization"] == "Bearer bt_test"
    with respx.mock() as router:
        router.get(f"{API_BASE}/models/m1/deployments/d1").mock(return_value=httpx.Response(200, json={"status": "BUILD_FAILED"}))
        with pytest.raises(DeployError, match="failed"):
            anyio.run(lambda: BasetenProvider().wait_ready(_dep(), timeout_s=10))


def test_status_logs_delete_list_gpus_cost():
    with respx.mock() as router:
        router.get(f"{API_BASE}/models/m1/deployments/d1").mock(
            return_value=httpx.Response(200, json={"status": "SCALED_TO_ZERO"}))
        assert anyio.run(lambda: BasetenProvider().status(_dep())).status == "scaled_to_zero"
        router.get(f"{API_BASE}/models/m1/deployments/d1/logs").mock(return_value=httpx.Response(200, json={"logs": [
            {"timestamp": "t1", "level": "INFO", "message": "starting"}, {"timestamp": "t2", "message": "ready"}]}))

        async def collect():
            return [line async for line in BasetenProvider().logs(_dep(), tail=5)]

        assert anyio.run(collect) == ["t1 INFO starting", "t2 ready"]
        deact = router.post(f"{API_BASE}/models/m1/deployments/d1/deactivate").mock(return_value=httpx.Response(200))
        delete = router.delete(f"{API_BASE}/models/m1").mock(return_value=httpx.Response(204))
        anyio.run(lambda: BasetenProvider().delete(_dep()))
        assert deact.called and delete.called
        router.get(f"{API_BASE}/instance_type_prices").mock(return_value=httpx.Response(200, json={"instance_type_prices": [
            {"name": "1x H100", "gpu_type": "H100", "gpu_count": 1, "gpu_memory_limit_mib": 81920, "price_per_minute": 0.10833},
            {"name": "1x A10G", "gpu_type": "A10G", "gpu_count": 1, "gpu_memory_limit_mib": 24576, "price_per_minute": 0.02012},
            {"name": "cpu", "gpu_type": None, "gpu_count": 0, "price_per_minute": 0.001},
        ]}))
        gpus = anyio.run(BasetenProvider().list_gpus)
        router.get(f"{API_BASE}/instance_type_prices").mock(return_value=httpx.Response(500))
        static = anyio.run(BasetenProvider().list_gpus)
    assert [g.provider_id for g in gpus] == ["A10G", "H100"]
    assert gpus[1].price_per_hour == pytest.approx(6.5, abs=0.01) and gpus[1].vram_gb == 80
    assert any(g.provider_id == "A100" and g.price_per_hour == 4.0 for g in static)
    cost = anyio.run(lambda: BasetenProvider().cost(_dep()))
    assert cost.per_hour_usd == 6.5 and cost.idle_per_hour_usd == 0.0


def test_list_deployments_reads_production_deployment():
    with respx.mock() as router:
        router.get(f"{API_BASE}/models").mock(return_value=httpx.Response(200, json={"models": [{
            "id": "m7", "name": "other", "production_deployment": {"id": "d7", "status": "ACTIVE", "instance_type_name": "A100"}}]}))
        listed = anyio.run(BasetenProvider().list_deployments)
    assert listed[0].id == "m7" and listed[0].status == "running" and listed[0].raw["deployment_id"] == "d7"
    assert listed[0].gpu.family == "A100-80"
    assert listed[0].endpoint_url == "https://model-m7.api.baseten.co/environments/production/sync/v1"
