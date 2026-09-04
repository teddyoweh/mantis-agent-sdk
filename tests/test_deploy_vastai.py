"""Vast.ai deploy adapter + the shared raw-VM bootstrap helpers.

All control-plane HTTP (``console.vast.ai/api/v0``) and the endpoint's
``/health`` / ``/v1/models`` are mocked with respx; sleeps are no-ops.
"""

from __future__ import annotations

import json as _json
import os

import httpx
import pytest
import respx

from mantis_agent.deploy import _http
from mantis_agent.deploy import _vm_bootstrap as boot
from mantis_agent.deploy.base import DeployError, DeployOpts, Deployment, GpuSpec, NotSupported
from mantis_agent.deploy.providers.vastai import (
    BASE_URL,
    VastAIProvider,
    gpu_family_from_name,
    parse_host_port,
)

MODEL = "Qwen/Qwen3-8B"
KEY_ENV = "MANTIS_DEPLOY_QWEN_QWEN3_8B_KEY"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"



@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("VAST_API_KEY", "vast-key-1")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv(KEY_ENV, raising=False)

    async def _nosleep(_s):
        return None

    monkeypatch.setattr(_http, "sleep", _nosleep)


def offer(**kw) -> GpuSpec:
    base = dict(provider_id="123", family="H100", vram_gb=80, count=1, price_per_hour=1.5, label="H100 SXM (80 GB)")
    base.update(kw)
    return GpuSpec(**base)


def _dep(**kw) -> Deployment:
    base = dict(
        id="555", provider="vastai", model=MODEL, engine="vllm", status="starting", gpu=offer(),
        served_model_name=MODEL, endpoint_url="http://1.2.3.4:41234/v1", auth_env=KEY_ENV, name="mantis-qwen-qwen3-8b",
    )
    base.update(kw)
    return Deployment(**base)


def running_instance(**kw) -> dict:
    inst = {
        "id": 555, "actual_status": "running", "intended_status": "running", "public_ipaddr": "1.2.3.4",
        "ports": {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41234"}], "22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41235"}]},
        "dph_total": 1.512, "gpu_name": "H100_SXM", "num_gpus": 1, "gpu_ram": 81920, "label": "mantis-qwen-qwen3-8b",
        "geolocation": "Norway, NO", "extra_env": [["HF_TOKEN", "hf_leak"]],
    }
    inst.update(kw)
    return inst


# ---------------------------------------------------------------------------
# _vm_bootstrap
# ---------------------------------------------------------------------------


def test_build_vllm_command_flags():
    opts = DeployOpts(tensor_parallel=2, max_model_len=16384, quantization="awq", trust_remote_code=True,
                      extra_engine_args=["--enforce-eager"], served_model_name="qwen")
    cmd = boot.build_vllm_command(MODEL, "vllm", opts, api_key="sk-x", port=8000)
    assert cmd[:3] == ["vllm", "serve", MODEL]
    assert cmd[3:] == [
        "--served-model-name", "qwen", "--host", "0.0.0.0", "--port", "8000", "--tensor-parallel-size", "2",
        "--max-model-len", "16384", "--quantization", "awq", "--trust-remote-code", "--api-key", "sk-x",
        "--enforce-eager",
    ]
    bare = boot.build_vllm_command(MODEL, "vllm", DeployOpts())
    assert "--api-key" not in bare and bare[-2:] == ["--tensor-parallel-size", "1"]


def test_build_sglang_command_and_unsupported_engines():
    cmd = boot.build_vllm_command(MODEL, "sglang", DeployOpts(max_model_len=4096), api_key="k")
    assert cmd[:5] == ["python", "-m", "sglang.launch_server", "--model-path", MODEL]
    assert "--tp" in cmd and "--context-length" in cmd and cmd[-2:] == ["--api-key", "k"]
    for engine in ("tgi", "llamacpp"):
        with pytest.raises(NotSupported):
            boot.build_vllm_command(MODEL, engine, DeployOpts())


def test_onstart_script_backgrounds_engine_and_holds_no_token():
    script = boot.build_onstart_script(MODEL, "vllm", DeployOpts(), api_key="sk-mantis-zzz")
    assert script.startswith("#!/bin/bash")
    assert 'export HF_TOKEN="${HF_TOKEN:-}"' in script   # read from env, never inlined
    assert "nohup vllm serve Qwen/Qwen3-8B --served-model-name Qwen/Qwen3-8B --host 0.0.0.0 --port 8000" in script
    assert "--api-key sk-mantis-zzz >> /var/log/mantis-serve.log 2>&1 &" in script


def test_names_keys_and_env_refs(monkeypatch):
    assert boot.slugify("Qwen/Qwen3-8B") == "qwen-qwen3-8b"
    assert boot.auth_env_name("qwen-qwen3-8b") == KEY_ENV
    k1, k2 = boot.generate_api_key(), boot.generate_api_key()
    assert k1 != k2 and k1.startswith("sk-mantis-")
    monkeypatch.setenv("A_ID", "wk")
    monkeypatch.delenv("B_ID", raising=False)
    assert boot.resolve_env_refs({"X": "${A_ID}", "Y": "${B_ID}", "Z": "literal"}) == {"X": "wk", "Z": "literal"}
    assert boot.vllm_image(DeployOpts()) == "vllm/vllm-openai:v0.21.0"
    assert boot.vllm_image(DeployOpts(engine_version="v0.22.0")) == "vllm/vllm-openai:v0.22.0"
    assert boot.vllm_image(DeployOpts(engine_version="latest")) == "vllm/vllm-openai:latest"
    assert boot.sglang_image(DeployOpts(engine_version="v0.5.1")) == "lmsysorg/sglang:v0.5.1"


async def test_wait_for_openai_retries_then_returns(env):
    with respx.mock() as router:
        route = router.get("http://1.2.3.4:41234/v1/models").mock(side_effect=[
            httpx.ConnectError("refused"), httpx.Response(503), httpx.Response(200, json={"data": [{"id": MODEL}]}),
        ])
        data = await boot.wait_for_openai("http://1.2.3.4:41234/v1/", {"Authorization": "Bearer k"}, 60)
    assert data["data"][0]["id"] == MODEL and route.call_count == 3
    assert route.calls[0].request.headers["Authorization"] == "Bearer k"


async def test_wait_for_openai_401_fails_fast_and_timeout_hint(env, monkeypatch):
    with respx.mock() as router:
        router.get("http://h:1/v1/models").mock(return_value=httpx.Response(401))
        with pytest.raises(DeployError) as ei:
            await boot.wait_for_openai("http://h:1/v1", {}, 60)
        assert "401" in str(ei.value) and "key" in (ei.value.hint or "")
    t = [0.0]

    def clock():
        t[0] += 40.0
        return t[0]

    monkeypatch.setattr(boot, "_clock", clock)
    with respx.mock() as router:
        router.get("http://h:1/v1/models").mock(return_value=httpx.Response(503))
        with pytest.raises(DeployError) as ei:
            await boot.wait_for_openai("http://h:1/v1", {}, 100)
    assert "not ready after 100s" in str(ei.value) and "logs" in (ei.value.hint or "")


def test_parse_host_port_variants():
    assert parse_host_port(running_instance()) == 41234
    assert parse_host_port({"ports": {"8000": "5000"}}) == 5000
    assert parse_host_port({"ports": [{"container_port": 8000, "host_port": 6000}]}) == 6000
    assert parse_host_port({"ports": {}}) is None
    assert parse_host_port({}) is None


def test_gpu_family_mapping():
    assert gpu_family_from_name("H100_SXM") == "H100"
    assert gpu_family_from_name("A100_SXM4", 81920) == "A100-80"
    assert gpu_family_from_name("A100_PCIE", 40960) == "A100-40"
    assert gpu_family_from_name("RTX_4090") == "other"
    assert gpu_family_from_name("Tesla T4") == "T4"
    assert gpu_family_from_name("L40S") == "L40S"


# ---------------------------------------------------------------------------
# validate_credentials()
# ---------------------------------------------------------------------------


async def test_validate_credentials_balance(env):
    with respx.mock() as router:
        route = router.get(f"{BASE_URL}/users/current/").mock(
            return_value=httpx.Response(200, json={"username": "teddy", "balance": 12.5, "api_key": "x"}),
        )
        acct = await VastAIProvider().validate_credentials()
    assert acct.ok and acct.user == "teddy" and acct.balance_usd == 12.5
    assert route.calls[0].request.headers["Authorization"] == "Bearer vast-key-1"


async def test_validate_credentials_401_hint(env):
    with respx.mock() as router:
        router.get(f"{BASE_URL}/users/current/").mock(return_value=httpx.Response(401, json={"error": "invalid api key"}))
        with pytest.raises(DeployError) as ei:
            await VastAIProvider().validate_credentials()
    assert "VAST_API_KEY" in (ei.value.hint or "") and ei.value.provider == "vastai"


async def test_not_configured(env, monkeypatch):
    monkeypatch.delenv("VAST_API_KEY")
    prov = VastAIProvider()
    assert prov.configured() is False
    acct = await prov.validate_credentials()
    assert acct.ok is False and "VAST_API_KEY" in acct.message


# ---------------------------------------------------------------------------
# list_gpus()
# ---------------------------------------------------------------------------


async def test_list_gpus_offer_table(env):
    offers = [
        {"id": 11, "gpu_name": "RTX_4090", "num_gpus": 2, "gpu_ram": 24564, "dph_total": 0.61, "geolocation": "Sweden, SE"},
        {"id": 12, "gpu_name": "RTX_4090", "num_gpus": 2, "gpu_ram": 24564, "dph_total": 0.75, "geolocation": "US"},  # dup
        {"id": 13, "gpu_name": "H100_SXM", "num_gpus": 1, "gpu_ram": 81559, "dph_total": 1.512, "geolocation": "Norway, NO"},
        {"id": 14, "gpu_name": "A100_SXM4", "num_gpus": 1, "gpu_ram": 81920, "dph_total": 0.9, "geolocation": None},
        {"gpu_name": "no-id", "dph_total": 0.1},
    ] + [
        {"id": 100 + i, "gpu_name": f"GPU_{i}", "num_gpus": 1, "gpu_ram": 8192, "dph_total": 5 + i} for i in range(50)
    ]
    with respx.mock() as router:
        route = router.post(f"{BASE_URL}/bundles/").mock(return_value=httpx.Response(200, json={"offers": offers}))
        rows = await VastAIProvider().list_gpus()
    body = _json.loads(route.calls[0].request.content)
    assert body["verified"] == {"eq": True} and body["rentable"] == {"eq": True}
    assert body["direct_port_count"] == {"gte": 1} and body["order"] == [["dph_total", "asc"]]

    assert len(rows) == 40
    assert rows[0].provider_id == "11" and rows[0].label == "RTX 4090 ×2 (24 GB)"
    assert rows[0].family == "other" and rows[0].count == 2 and rows[0].region == "Sweden, SE"
    assert rows[0].available is True
    assert "12" not in {r.provider_id for r in rows}  # deduped by gpu_name + num_gpus, cheapest kept
    h100 = next(r for r in rows if r.provider_id == "13")
    assert h100.family == "H100" and h100.vram_gb == 80 and h100.price_per_hour == 1.512
    assert h100.label == "H100 SXM (80 GB)"
    prices = [r.price_per_hour for r in rows]
    assert prices == sorted(prices)


# ---------------------------------------------------------------------------
# deploy() → poll → endpoint URL
# ---------------------------------------------------------------------------


async def test_deploy_creates_polls_and_derives_url(env, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    with respx.mock() as router:
        create = router.put(f"{BASE_URL}/asks/123/").mock(return_value=httpx.Response(200, json={"success": True, "new_contract": 555}))
        inst = router.get(f"{BASE_URL}/instances/555/").mock(side_effect=[
            httpx.Response(200, json={"instances": {"id": 555, "actual_status": "loading", "intended_status": "running"}}),
            httpx.Response(200, json={"instances": running_instance()}),
        ])
        opts = DeployOpts(max_model_len=8192)
        dep = await VastAIProvider().deploy(MODEL, offer(count=1), "vllm", opts)

    body = _json.loads(create.calls[0].request.content)
    assert body["image"] == "vllm/vllm-openai:v0.21.0"
    assert body["label"] == "mantis-qwen-qwen3-8b" and body["runtype"] == "args"
    assert body["disk"] == 80
    key = os.environ[KEY_ENV]
    assert key.startswith("sk-mantis-")
    assert f"-p 8000:8000 -e VLLM_API_KEY={key} -e HF_TOKEN=hf_secret" in body["env"]
    assert body["args_str"].startswith("--model Qwen/Qwen3-8B --served-model-name Qwen/Qwen3-8B --host 0.0.0.0 --port 8000 --tensor-parallel-size 1 --max-model-len 8192 --api-key ")
    assert key in body["args_str"]
    assert "onstart" not in body

    assert inst.call_count == 2
    assert dep.id == "555" and dep.provider == "vastai"
    assert dep.endpoint_url == "http://1.2.3.4:41234/v1"
    assert dep.served_model_name == MODEL and dep.auth_env == KEY_ENV and dep.auth_headers == {}
    assert dep.status == "running"  # instance running; health is checked by status()/wait_ready()
    assert "no scale-to-zero" in dep.message and "idle_timeout_s=300" in dep.message
    assert dep.raw["dph_total"] == 1.512
    raw = _json.dumps(dep.raw)
    assert "hf_leak" not in raw and key not in raw and "hf_secret" not in raw


async def test_deploy_disk_from_estimate_and_sglang_image(env):
    with respx.mock() as router:
        create = router.put(f"{BASE_URL}/asks/123/").mock(return_value=httpx.Response(200, json={"success": True, "new_contract": 556}))
        router.get(f"{BASE_URL}/instances/556/").mock(return_value=httpx.Response(200, json={"instances": running_instance(id=556)}))
        dep = await VastAIProvider().deploy(MODEL, offer(count=2), "sglang", DeployOpts(extra={"est_weights_gb": 64}))
    body = _json.loads(create.calls[0].request.content)
    assert body["image"] == "lmsysorg/sglang:latest" and body["disk"] == 116
    assert "sglang.launch_server --model-path Qwen/Qwen3-8B" in body["args_str"] and "--tp 2" in body["args_str"]
    assert dep.opts.tensor_parallel == 2


async def test_deploy_offer_gone_410_hint(env):
    with respx.mock() as router:
        router.put(f"{BASE_URL}/asks/123/").mock(return_value=httpx.Response(410))
        with pytest.raises(DeployError) as ei:
            await VastAIProvider().deploy(MODEL, offer(), "vllm", DeployOpts())
    assert "gone" in str(ei.value) and "pick another offer" in (ei.value.hint or "")


async def test_deploy_unsuccessful_body_and_bad_gpu_id(env):
    with respx.mock() as router:
        router.put(f"{BASE_URL}/asks/123/").mock(return_value=httpx.Response(200, json={"success": False, "msg": "insufficient credit"}))
        with pytest.raises(DeployError) as ei:
            await VastAIProvider().deploy(MODEL, offer(), "vllm", DeployOpts())
    assert "insufficient credit" in str(ei.value)
    with pytest.raises(DeployError):
        await VastAIProvider().deploy(MODEL, offer(provider_id="H100"), "vllm", DeployOpts())
    with pytest.raises(NotSupported):
        await VastAIProvider().deploy(MODEL, offer(), "tgi", DeployOpts())


async def test_deploy_failed_instance(env):
    with respx.mock() as router:
        router.put(f"{BASE_URL}/asks/123/").mock(return_value=httpx.Response(200, json={"success": True, "new_contract": 557}))
        router.get(f"{BASE_URL}/instances/557/").mock(return_value=httpx.Response(200, json={"instances": {"id": 557, "actual_status": "exited", "status_msg": "docker pull failed"}}))
        with pytest.raises(DeployError) as ei:
            await VastAIProvider().deploy(MODEL, offer(), "vllm", DeployOpts())
    assert "docker pull failed" in str(ei.value) and "destroy" in (ei.value.hint or "")


# ---------------------------------------------------------------------------
# status() / wait_ready()
# ---------------------------------------------------------------------------


async def test_status_combines_instance_and_health(env, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-mantis-k")
    prov = VastAIProvider()
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE_URL}/instances/555/").mock(return_value=httpx.Response(200, json={"instances": running_instance()}))
        health = router.get("http://1.2.3.4:41234/health").mock(return_value=httpx.Response(200))
        dep = await prov.status(_dep(endpoint_url=None))
        assert dep.status == "running" and dep.endpoint_url == "http://1.2.3.4:41234/v1"
        assert health.calls[0].request.headers["Authorization"] == "Bearer sk-mantis-k"

        health.mock(return_value=httpx.Response(503))
        assert (await prov.status(_dep())).status == "starting"
        health.mock(side_effect=httpx.ConnectError("refused"))
        assert (await prov.status(_dep())).status == "starting"

        router.get(f"{BASE_URL}/instances/555/").mock(return_value=httpx.Response(200, json={"instances": running_instance(actual_status="stopped")}))
        assert (await prov.status(_dep())).status == "paused"
        router.get(f"{BASE_URL}/instances/555/").mock(return_value=httpx.Response(404))
        assert (await prov.status(_dep())).status == "deleted"


async def test_wait_ready_waits_for_port_then_models(env, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-mantis-k")
    with respx.mock() as router:
        router.get(f"{BASE_URL}/instances/555/").mock(side_effect=[
            httpx.Response(200, json={"instances": running_instance(ports={}, public_ipaddr=None)}),
            httpx.Response(200, json={"instances": running_instance()}),
        ])
        models = router.get("http://1.2.3.4:41234/v1/models").mock(side_effect=[
            httpx.Response(503), httpx.Response(200, json={"data": [{"id": MODEL}]}),
        ])
        dep = await VastAIProvider().wait_ready(_dep(endpoint_url=None), timeout_s=300)
    assert dep.status == "running" and dep.endpoint_url == "http://1.2.3.4:41234/v1"
    assert models.call_count == 2
    assert models.calls[0].request.headers["Authorization"] == "Bearer sk-mantis-k"


async def test_wait_ready_deleted_instance_fails(env):
    with respx.mock() as router:
        router.get(f"{BASE_URL}/instances/555/").mock(return_value=httpx.Response(404))
        with pytest.raises(DeployError) as ei:
            await VastAIProvider().wait_ready(_dep(endpoint_url=None), timeout_s=30)
    assert "deleted" in str(ei.value)


# ---------------------------------------------------------------------------
# logs() / delete() / list_deployments() / cost()
# ---------------------------------------------------------------------------


async def test_logs_via_request_logs_url(env):
    with respx.mock() as router:
        req = router.put(f"{BASE_URL}/instances/request_logs/555/").mock(
            return_value=httpx.Response(200, json={"success": True, "result_url": "https://s3.example.com/logs/555.txt"}),
        )
        router.get("https://s3.example.com/logs/555.txt").mock(side_effect=[
            httpx.Response(404), httpx.Response(200, text="a\nb\nc\n"),
        ])
        lines = [ln async for ln in VastAIProvider().logs(_dep(), tail=2)]
    assert lines == ["b", "c"]
    assert _json.loads(req.calls[0].request.content) == {"tail": "2"}


async def test_logs_not_supported_when_shape_differs(env):
    with respx.mock() as router:
        router.put(f"{BASE_URL}/instances/request_logs/555/").mock(return_value=httpx.Response(200, json={"success": True}))
        with pytest.raises(NotSupported):
            _ = [ln async for ln in VastAIProvider().logs(_dep())]


async def test_delete_destroys_and_drops_key(env, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk")
    with respx.mock() as router:
        route = router.delete(f"{BASE_URL}/instances/555/").mock(return_value=httpx.Response(200, json={"success": True}))
        await VastAIProvider().delete(_dep())
        assert route.called and KEY_ENV not in os.environ
        route.mock(return_value=httpx.Response(404))
        await VastAIProvider().delete(_dep())  # already gone is fine


async def test_list_deployments_filters_label(env):
    with respx.mock() as router:
        router.get(f"{BASE_URL}/instances/").mock(return_value=httpx.Response(200, json={"instances": [
            running_instance(),
            running_instance(id=777, label="my-jupyter-box"),
            running_instance(id=778, label="mantis-llama", actual_status="loading", ports={}, public_ipaddr=None),
        ]}))
        deps = await VastAIProvider().list_deployments()
    assert [d.id for d in deps] == ["555", "778"]
    assert deps[0].endpoint_url == "http://1.2.3.4:41234/v1" and deps[0].status == "running"
    assert deps[0].model == "qwen-qwen3-8b" and deps[0].auth_env == KEY_ENV
    assert deps[0].gpu.family == "H100" and deps[0].gpu.price_per_hour == 1.512
    assert deps[1].status == "starting" and deps[1].endpoint_url is None
    assert "extra_env" not in deps[0].raw["instance"]


async def test_cost_is_dph_total_no_scale_to_zero():
    est = await VastAIProvider().cost(_dep(raw={"dph_total": 1.512}))
    assert est.per_hour_usd == 1.512 and est.idle_per_hour_usd == 1.512
    assert "storage" in est.basis
    est = await VastAIProvider().cost(_dep(gpu=offer(price_per_hour=0.9)))
    assert est.per_hour_usd == 0.9


def test_registered():
    from mantis_agent.deploy.base import DEPLOY_PROVIDERS, get_provider

    assert DEPLOY_PROVIDERS["vastai"] is VastAIProvider
    prov = get_provider("vastai")
    assert prov.display_name == "Vast.ai" and prov.scale_to_zero is False and prov.public_by_default is True
    assert [f.env for f in prov.credential_fields] == ["VAST_API_KEY", "HF_TOKEN"]
