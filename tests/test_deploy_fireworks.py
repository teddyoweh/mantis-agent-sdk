"""Fireworks on-demand deployments: the adapter against a mocked control plane."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mantis_agent.deploy import _http
from mantis_agent.deploy.base import DeployError, DeployOpts, Deployment, GpuSpec, get_provider
from mantis_agent.deploy.providers import fireworks_dedicated as fw

pytestmark = pytest.mark.anyio

API = fw.API_BASE


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"

LIB = [
    {"name": "accounts/fireworks/models/glm-4p7", "huggingFaceUrl": "https://huggingface.co/zai-org/GLM-4.7",
     "contextLength": 202752},
    {"name": "accounts/fireworks/models/qwen3-8b", "huggingFaceUrl": "https://huggingface.co/Qwen/Qwen3-8B"},
    {"name": "accounts/fireworks/models/gpt-oss-120b", "huggingFaceUrl": ""},
]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw_test_key")
    monkeypatch.delenv("FIREWORKS_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(fw, "_library_cache", None)

    async def _nosleep(_s):
        return None

    monkeypatch.setattr(_http, "sleep", _nosleep)


def h100(n=1):
    return GpuSpec(provider_id="NVIDIA_H100_80GB", family="H100", vram_gb=80, count=n, price_per_hour=8.0 * n)


def test_registered_and_described():
    p = get_provider("fireworks-dedicated")
    assert p.display_name == "Fireworks" and p.scale_to_zero
    assert [c.env for c in p.credential_fields] == ["FIREWORKS_API_KEY", "FIREWORKS_ACCOUNT_ID"]
    assert p.credential_fields[1].required is False


@pytest.mark.parametrize("hf,want", [
    ("zai-org/GLM-4.7", "accounts/fireworks/models/glm-4p7"),          # exact huggingFaceUrl
    ("zai-org/GLM-4.7-FP8", "accounts/fireworks/models/glm-4p7"),      # precision tag dropped
    ("qwen/qwen3-8b", "accounts/fireworks/models/qwen3-8b"),           # case-insensitive
    ("openai/gpt-oss-120b", "accounts/fireworks/models/gpt-oss-120b"),  # by short name
    ("meta-llama/Nope-1B", None),
])
def test_library_match(hf, want):
    hit = fw.library_match(hf, LIB)
    assert (hit or {}).get("name") == want


async def test_list_gpus_prices_scale_with_count(env):
    gpus = await fw.FireworksDedicatedProvider().list_gpus()
    h = {(g.provider_id, g.count): g.price_per_hour for g in gpus}
    assert h[("NVIDIA_H100_80GB", 1)] == 8.0 and h[("NVIDIA_H100_80GB", 4)] == 32.0
    assert h[("NVIDIA_B200_180GB", 8)] == 104.0


async def test_deploy_resolves_the_library_model_and_sends_a_shape(env):
    prov = fw.FireworksDedicatedProvider()
    with respx.mock(assert_all_called=True) as r:
        r.get(API + "/v1/accounts").mock(return_value=httpx.Response(200, json={"accounts": [{"name": "accounts/teddy"}]}))
        r.get(API + "/v1/accounts/fireworks/models").mock(return_value=httpx.Response(200, json={"models": LIB}))
        create = r.post(API + "/v1/accounts/teddy/deployments").mock(return_value=httpx.Response(200, json={
            "name": "accounts/teddy/deployments/mantis-zai-org-glm-4-7-fp8", "state": "CREATING",
            "acceleratorType": "NVIDIA_H100_80GB", "acceleratorCount": 8}))
        dep = await prov.deploy("zai-org/GLM-4.7-FP8", h100(4), "vllm", DeployOpts())
    body = json.loads(create.calls[0].request.content)
    assert body["baseModel"] == "accounts/fireworks/models/glm-4p7"
    assert body["deploymentShape"] == "default"
    assert body["acceleratorType"] == "NVIDIA_H100_80GB" and body["acceleratorCount"] == 4
    assert body["minReplicaCount"] == 0 and body["maxReplicaCount"] == 1
    assert create.calls[0].request.headers["Authorization"] == "Bearer fw_test_key"
    # the model string is the deployment; the endpoint is the shared inference API
    assert dep.served_model_name == "accounts/teddy/deployments/mantis-zai-org-glm-4-7-fp8"
    assert dep.endpoint_url == "https://api.fireworks.ai/inference/v1" and dep.auth_env == "FIREWORKS_API_KEY"
    assert dep.status == "starting"
    # the shape chose 8 GPUs: the record and the price follow what is actually billed
    assert dep.gpu.count == 8 and dep.gpu.price_per_hour == 64.0


async def test_a_shape_conflict_retries_without_the_count(env, monkeypatch):
    monkeypatch.setenv("FIREWORKS_ACCOUNT_ID", "teddy")
    prov = fw.FireworksDedicatedProvider()
    with respx.mock() as r:
        r.get(API + "/v1/accounts/fireworks/models").mock(return_value=httpx.Response(200, json={"models": LIB}))
        create = r.post(API + "/v1/accounts/teddy/deployments").mock(side_effect=[
            httpx.Response(400, json={"error": "no validated deployment shape for acceleratorCount=1"}),
            httpx.Response(200, json={"name": "accounts/teddy/deployments/x", "state": "CREATING"}),
        ])
        await prov.deploy("Qwen/Qwen3-8B", h100(1), "vllm", DeployOpts())
    assert "acceleratorCount" in json.loads(create.calls[0].request.content)
    assert "acceleratorCount" not in json.loads(create.calls[1].request.content)


async def test_a_model_outside_the_library_says_how_to_get_it_in(env, monkeypatch):
    monkeypatch.setenv("FIREWORKS_ACCOUNT_ID", "teddy")
    with respx.mock() as r:
        r.get(API + "/v1/accounts/fireworks/models").mock(return_value=httpx.Response(200, json={"models": LIB}))
        with pytest.raises(DeployError) as e:
            await fw.FireworksDedicatedProvider().deploy("meta-llama/Nope-1B", h100(), "vllm", DeployOpts())
    assert "isn't in the Fireworks model library" in str(e.value) and "firectl create model" in e.value.hint


async def test_status_maps_states_and_scale_to_zero(env, monkeypatch):
    monkeypatch.setenv("FIREWORKS_ACCOUNT_ID", "teddy")
    prov = fw.FireworksDedicatedProvider()
    dep = Deployment(id="d1", provider="fireworks-dedicated", model="m", engine="vllm", status="starting",
                     gpu=h100(), served_model_name="accounts/teddy/deployments/d1", raw={"account": "teddy"})
    url = API + "/v1/accounts/teddy/deployments/d1"
    with respx.mock() as r:
        route = r.get(url)
        route.mock(return_value=httpx.Response(200, json={"state": "READY", "replicaCount": 1}))
        assert (await prov.status(dep)).status == "running"
        route.mock(return_value=httpx.Response(200, json={"state": "READY", "replicaCount": 0, "minReplicaCount": 0}))
        assert (await prov.status(dep)).status == "scaled_to_zero"
        route.mock(return_value=httpx.Response(200, json={"state": "FAILED", "status": {"message": "out of capacity"}}))
        d = await prov.status(dep)
        assert d.status == "failed" and d.message == "out of capacity"
        route.mock(return_value=httpx.Response(404))
        assert (await prov.status(dep)).status == "deleted"


async def test_several_accounts_need_an_explicit_id(env):
    with respx.mock() as r:
        r.get(API + "/v1/accounts").mock(return_value=httpx.Response(200, json={
            "accounts": [{"name": "accounts/a"}, {"name": "accounts/b"}]}))
        with pytest.raises(DeployError) as e:
            await fw.FireworksDedicatedProvider().validate_credentials()
    assert "FIREWORKS_ACCOUNT_ID" in e.value.hint
