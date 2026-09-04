"""Modal deploy adapter — template rendering, subprocess deploy, health polling, CLI parsing.

No Modal SDK is needed: ``_import_modal`` is stubbed and every subprocess
goes through the module-level ``_runner`` hook, which the tests replace with
a recorder. HTTP (the endpoint's ``/health``) is mocked with respx.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest
import respx

from mantis_agent.deploy import _http
from mantis_agent.deploy import _vm_bootstrap as _boot
from mantis_agent.deploy.base import DeployError, DeployOpts, Deployment, GpuSpec, NotSupported
from mantis_agent.deploy.providers import modal_deploy
from mantis_agent.deploy.providers.modal_deploy import (
    MODAL_GPUS,
    ModalDeployProvider,
    render_modal_app,
)

_REAL_IMPORT_MODAL = modal_deploy._import_modal

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


MODEL = "Qwen/Qwen3-8B"
URL = "https://ws--mantis-qwen-qwen3-8b-server.us-east.modal.direct"


class Recorder:
    """Stand-in for ``_run_subprocess``: records calls, replays scripted results."""

    def __init__(self, *results: tuple[int, str, str]):
        self.results = list(results)
        self.calls: list[tuple[list[str], dict[str, str], float]] = []

    async def __call__(self, cmd, env, timeout_s):
        self.calls.append((list(cmd), dict(env), timeout_s))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-test")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-test")
    for var in ("MODAL_PROXY_TOKEN_ID", "MODAL_PROXY_TOKEN_SECRET", "HF_TOKEN", "MODAL_CONFIG_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(modal_deploy, "_import_modal", lambda: object())

    async def _nosleep(_s):
        return None

    monkeypatch.setattr(_http, "sleep", _nosleep)
    return tmp_path


def gpu(pid: str = "H100", count: int = 1, price: float = 3.95) -> GpuSpec:
    return GpuSpec(provider_id=pid, family="H100", vram_gb=80, count=count, price_per_hour=price)


def _dep(**kw) -> Deployment:
    base = dict(
        id="mantis-qwen-qwen3-8b", provider="modal", model=MODEL, engine="vllm", status="starting",
        gpu=gpu(), served_model_name=MODEL, endpoint_url=URL + "/v1",
        auth_headers={"Modal-Key": "${MODAL_PROXY_TOKEN_ID}", "Modal-Secret": "${MODAL_PROXY_TOKEN_SECRET}"},
    )
    base.update(kw)
    return Deployment(**base)


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def _render(**kw) -> str:
    args = dict(
        app_name="mantis-qwen-qwen3-8b", model=MODEL, engine="vllm", gpu="H100:2", gpu_count=2,
        opts=DeployOpts(tensor_parallel=2), filename="qwen-qwen3-8b.py",
        has_hf_token=False, unauthenticated=False,
    )
    args.update(kw)
    return render_modal_app(**args)


def test_template_has_gpu_model_and_engine_flags():
    src = _render(opts=DeployOpts(
        tensor_parallel=2, max_model_len=8192, quantization="fp8", trust_remote_code=True,
        extra_engine_args=["--enforce-eager"], idle_timeout_s=120, min_replicas=1, max_replicas=3,
    ))
    compile(src, "generated.py", "exec")  # valid Python
    assert "gpu='H100:2'" in src
    assert "APP_NAME = 'mantis-qwen-qwen3-8b'" in src
    assert "'vllm', 'serve', 'Qwen/Qwen3-8B'" in src.replace("\n            ", " ")
    assert "'--served-model-name',\n            'Qwen/Qwen3-8B'" in src
    assert "'--tensor-parallel-size',\n            '2'" in src
    assert "'--max-model-len',\n            '8192'" in src
    assert "'--quantization',\n            'fp8'" in src
    assert "'--trust-remote-code'" in src
    assert "'--enforce-eager'" in src
    assert "uv_pip_install('vllm==0.21.0')" in src
    assert "scaledown_window=120" in src
    assert "startup_timeout=600" in src
    assert "min_containers=1" in src and "max_containers=3" in src
    assert "unauthenticated=False" in src
    assert "--api-key" not in src
    assert "SECRETS = []" in src and "'HF_TOKEN'" not in src  # the docstring mentions the name; the secret line must not
    assert 'json.dumps({"url": Server.get_url(), "app": APP_NAME})' in src


def test_template_secrets_line_only_with_token():
    src = _render(has_hf_token=True)
    assert "SECRETS = [modal.Secret.from_dict({'HF_TOKEN': os.environ['MANTIS_HF_TOKEN']})]" in src
    assert "'VLLM_API_KEY'" not in src and "--api-key" not in src
    src = _render(has_hf_token=True, unauthenticated=True)
    assert "'HF_TOKEN': os.environ['MANTIS_HF_TOKEN'], 'VLLM_API_KEY': os.environ['MANTIS_VLLM_API_KEY']" in src
    assert "unauthenticated=True" in src
    assert '"--api-key",\n            os.environ["VLLM_API_KEY"]' in src
    compile(src, "generated.py", "exec")


def test_template_engine_version_pin_and_sglang():
    src = _render(opts=DeployOpts(engine_version="v0.22.1"))
    assert "uv_pip_install('vllm==0.22.1')" in src
    src = _render(engine="sglang", opts=DeployOpts(max_model_len=4096))
    assert "uv_pip_install('sglang[all]')" in src
    assert "'sglang.launch_server'" in src and "'--model-path'" in src
    assert "'--tp',\n            '1'" in src
    assert "'--context-length',\n            '4096'" in src
    assert "mantis-sglang-cache" in src


def test_template_rejects_tgi_and_llamacpp():
    for engine in ("tgi", "llamacpp"):
        with pytest.raises(NotSupported):
            _render(engine=engine)


# ---------------------------------------------------------------------------
# configured() / validate_credentials()
# ---------------------------------------------------------------------------


def test_configured_from_env(env, monkeypatch):
    assert ModalDeployProvider().configured() is True
    monkeypatch.delenv("MODAL_TOKEN_SECRET")
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(env / "nope.toml"))
    assert ModalDeployProvider().configured() is False


def test_configured_from_modal_toml(env, monkeypatch):
    monkeypatch.delenv("MODAL_TOKEN_ID")
    monkeypatch.delenv("MODAL_TOKEN_SECRET")
    toml = env / "modal.toml"
    toml.write_text('[default]\ntoken_id = "ak-abc"\ntoken_secret = "as-xyz"\nactive = true\n')
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(toml))
    assert ModalDeployProvider().configured() is True
    toml.write_text('[default]\ntoken_id = "ak-abc"\n')  # half a pair is not configured
    assert ModalDeployProvider().configured() is False


async def test_missing_sdk_hint(env, monkeypatch):
    monkeypatch.setattr(modal_deploy, "_import_modal", _REAL_IMPORT_MODAL)
    monkeypatch.setitem(sys.modules, "modal", None)  # makes `import modal` raise ImportError
    with pytest.raises(DeployError) as ei:
        await ModalDeployProvider().validate_credentials()
    assert ei.value.hint == "pip install mantis-agent-sdk[modal]"


async def test_validate_credentials_uses_app_list(env, monkeypatch):
    rec = Recorder((0, json.dumps([
        {"App ID": "ap-1", "Description": "mantis-qwen-qwen3-8b", "State": "deployed"},
        {"App ID": "ap-2", "Description": "someone-else", "State": "deployed"},
    ]), ""))
    monkeypatch.setattr(modal_deploy, "_runner", rec)
    acct = await ModalDeployProvider().validate_credentials()
    assert acct.ok and acct.provider == "modal"
    assert "2 app(s)" in acct.message and "1 deployed by mantis" in acct.message
    cmd, cmd_env, _ = rec.calls[0]
    assert cmd == [sys.executable, "-m", "modal", "app", "list", "--json"]
    assert cmd_env["MODAL_TOKEN_ID"] == "ak-test"


async def test_validate_credentials_auth_failure_hint(env, monkeypatch):
    monkeypatch.setattr(modal_deploy, "_runner", Recorder((1, "", "Error: Token missing. Could not authenticate client.")))
    with pytest.raises(DeployError) as ei:
        await ModalDeployProvider().validate_credentials()
    assert "MODAL_TOKEN_ID" in (ei.value.hint or "")


# ---------------------------------------------------------------------------
# list_gpus()
# ---------------------------------------------------------------------------


async def test_list_gpus_static_table():
    rows = await ModalDeployProvider().list_gpus()
    assert len(rows) == len(MODAL_GPUS) * 8
    assert rows[0].provider_id == "T4" and rows[0].price_per_hour == 0.59
    h100x4 = next(r for r in rows if r.provider_id == "H100:4")
    assert h100x4.count == 4 and h100x4.family == "H100" and h100x4.price_per_hour == 15.8
    assert h100x4.total_vram_gb == 320 and h100x4.available is True
    prices = [r.price_per_hour for r in rows]
    assert prices == sorted(prices)
    assert {r.family for r in rows} == {"T4", "L4", "A10G", "L40S", "A100-40", "A100-80", "H100", "H200", "B200"}


# ---------------------------------------------------------------------------
# deploy()
# ---------------------------------------------------------------------------


def _ok_result(url: str = URL) -> tuple[int, str, str]:
    return 0, "✓ Created objects.\n" + json.dumps({"url": url, "app": "mantis-qwen-qwen3-8b"}) + "\n", "warnings…"


async def test_deploy_with_proxy_token_records_header_refs(env, monkeypatch):
    monkeypatch.setenv("MODAL_PROXY_TOKEN_ID", "wk-1")
    monkeypatch.setenv("MODAL_PROXY_TOKEN_SECRET", "ws-1")
    rec = Recorder(_ok_result())
    monkeypatch.setattr(modal_deploy, "_runner", rec)

    opts = DeployOpts()
    dep = await ModalDeployProvider().deploy(MODEL, gpu("H100:2", count=2), "vllm", opts)

    cmd, cmd_env, timeout = rec.calls[0]
    path = Path(cmd[1])
    assert cmd[0] == sys.executable
    assert path == env / "home" / "deploy" / "modal" / "qwen-qwen3-8b.py"
    assert path.exists()
    src = path.read_text()
    assert "gpu='H100:2'" in src and "unauthenticated=False" in src
    assert opts.tensor_parallel == 2  # defaulted from the gpu count
    assert cmd_env["MODAL_TOKEN_ID"] == "ak-test" and cmd_env["MODAL_TOKEN_SECRET"] == "as-test"
    assert "MANTIS_VLLM_API_KEY" not in cmd_env and "MANTIS_HF_TOKEN" not in cmd_env
    assert timeout == 900

    assert dep.id == "mantis-qwen-qwen3-8b" and dep.provider == "modal"
    assert dep.endpoint_url == URL + "/v1"
    assert dep.served_model_name == MODEL and dep.status == "starting"
    assert dep.auth_env is None
    assert dep.auth_headers == {"Modal-Key": "${MODAL_PROXY_TOKEN_ID}", "Modal-Secret": "${MODAL_PROXY_TOKEN_SECRET}"}
    assert dep.raw["unauthenticated"] is False
    assert "wk-1" not in json.dumps(dep.raw)


async def test_deploy_without_proxy_token_generates_key(env, monkeypatch):
    rec = Recorder(_ok_result())
    monkeypatch.setattr(modal_deploy, "_runner", rec)
    monkeypatch.delenv("MANTIS_DEPLOY_QWEN_QWEN3_8B_KEY", raising=False)

    dep = await ModalDeployProvider().deploy(
        MODEL, gpu(), "vllm", DeployOpts(hf_token="hf_secret123"),
    )
    assert dep.auth_env == "MANTIS_DEPLOY_QWEN_QWEN3_8B_KEY"
    key = os.environ[dep.auth_env]
    assert key.startswith("sk-mantis-") and len(key) > 30
    assert dep.auth_headers == {}
    _, cmd_env, _ = rec.calls[0]
    assert cmd_env["MANTIS_VLLM_API_KEY"] == key
    assert cmd_env["MANTIS_HF_TOKEN"] == "hf_secret123"
    src = Path(rec.calls[0][0][1]).read_text()
    assert "unauthenticated=True" in src and "--api-key" in src
    assert key not in src and "hf_secret123" not in src  # secrets never hit disk
    assert "'HF_TOKEN': os.environ['MANTIS_HF_TOKEN']" in src
    assert "MANTIS_DEPLOY_QWEN_QWEN3_8B_KEY" in dep.message
    monkeypatch.delenv(dep.auth_env)


async def test_deploy_explicit_unauthenticated_even_with_proxy_token(env, monkeypatch):
    monkeypatch.setenv("MODAL_PROXY_TOKEN_ID", "wk-1")
    monkeypatch.setenv("MODAL_PROXY_TOKEN_SECRET", "ws-1")
    monkeypatch.setattr(modal_deploy, "_runner", Recorder(_ok_result()))
    dep = await ModalDeployProvider().deploy(MODEL, gpu(), "vllm", DeployOpts(extra={"unauthenticated": True}))
    assert dep.auth_env == "MANTIS_DEPLOY_QWEN_QWEN3_8B_KEY" and dep.auth_headers == {}
    monkeypatch.delenv(dep.auth_env)


async def test_deploy_subprocess_failure_is_deploy_error(env, monkeypatch):
    monkeypatch.setattr(modal_deploy, "_runner", Recorder((1, "", "Traceback…\nmodal.exception.AuthError: bad token")))
    with pytest.raises(DeployError) as ei:
        await ModalDeployProvider().deploy(MODEL, gpu(), "vllm", DeployOpts(extra={"unauthenticated": True}))
    assert "exit 1" in str(ei.value)
    assert "qwen-qwen3-8b.py" in (ei.value.hint or "") and "AuthError" in (ei.value.hint or "")


async def test_deploy_rejects_unsupported_engine(env):
    with pytest.raises(NotSupported):
        await ModalDeployProvider().deploy(MODEL, gpu(), "tgi", DeployOpts())


async def test_deploy_requires_credentials(env, monkeypatch):
    monkeypatch.delenv("MODAL_TOKEN_ID")
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(env / "missing.toml"))
    with pytest.raises(DeployError) as ei:
        await ModalDeployProvider().deploy(MODEL, gpu(), "vllm", DeployOpts())
    assert "MODAL_TOKEN_ID" in (ei.value.hint or "")


# ---------------------------------------------------------------------------
# status() / wait_ready()
# ---------------------------------------------------------------------------


async def test_status_and_wait_ready_503_then_200(env, monkeypatch):
    monkeypatch.setenv("MODAL_PROXY_TOKEN_ID", "wk-1")
    monkeypatch.setenv("MODAL_PROXY_TOKEN_SECRET", "ws-1")
    prov = ModalDeployProvider()
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL + "/health").mock(side_effect=[
            httpx.Response(503), httpx.Response(503), httpx.Response(200, text="OK"),
        ])
        dep = await prov.status(_dep())
        assert dep.status == "scaled_to_zero" and "503" in dep.message
        sent = route.calls[0].request.headers
        assert sent["Modal-Key"] == "wk-1" and sent["Modal-Secret"] == "ws-1"

        dep = await prov.wait_ready(dep, timeout_s=60)
        assert dep.status == "running" and dep.message == "healthy"
        assert route.call_count == 3


async def test_status_warm_pool_503_is_starting_and_errors(env):
    prov = ModalDeployProvider()
    with respx.mock(assert_all_called=False) as router:
        router.get(URL + "/health").mock(return_value=httpx.Response(503))
        dep = await prov.status(_dep(opts=DeployOpts(min_replicas=1)))
        assert dep.status == "starting"
        router.get(URL + "/health").mock(return_value=httpx.Response(401))
        dep = await prov.status(_dep())
        assert dep.status == "unknown" and "MODAL_PROXY_TOKEN_ID" in dep.message
        router.get(URL + "/health").mock(side_effect=httpx.ConnectError("boom"))
        dep = await prov.status(_dep())
        assert dep.status == "unknown" and "ConnectError" in dep.message
    dep = await prov.status(_dep(endpoint_url=None))
    assert dep.status == "unknown"


async def test_status_sends_bearer_for_unauthenticated_deploy(env, monkeypatch):
    monkeypatch.setenv("MANTIS_DEPLOY_X_KEY", "sk-mantis-abc")
    with respx.mock() as router:
        route = router.get(URL + "/health").mock(return_value=httpx.Response(200))
        await ModalDeployProvider().status(_dep(auth_headers={}, auth_env="MANTIS_DEPLOY_X_KEY"))
        assert route.calls[0].request.headers["Authorization"] == "Bearer sk-mantis-abc"
        assert "Modal-Key" not in route.calls[0].request.headers


async def test_wait_ready_times_out_with_logs_hint(env, monkeypatch):
    t = [0.0]

    def clock():
        t[0] += 30.0
        return t[0]

    monkeypatch.setattr(_boot, "_clock", clock)
    with respx.mock() as router:
        router.get(URL + "/health").mock(return_value=httpx.Response(503))
        with pytest.raises(DeployError) as ei:
            await ModalDeployProvider().wait_ready(_dep(), timeout_s=50)
    assert "modal app logs mantis-qwen-qwen3-8b" in (ei.value.hint or "")


# ---------------------------------------------------------------------------
# logs() / list_deployments() / delete() / cost()
# ---------------------------------------------------------------------------


async def test_logs_shell_out_non_follow(env, monkeypatch):
    rec = Recorder((0, "line1\nline2\nline3\n", ""))
    monkeypatch.setattr(modal_deploy, "_runner", rec)
    lines = [ln async for ln in ModalDeployProvider().logs(_dep(), tail=2)]
    assert lines == ["line2", "line3"]
    assert rec.calls[0][0][:3] == [sys.executable, "-m", "modal"]
    assert rec.calls[0][0][3:] == ["app", "logs", "mantis-qwen-qwen3-8b", "-n", "2"]


async def test_list_deployments_filters_mantis_prefix(env, monkeypatch):
    out = json.dumps([
        {"App ID": "ap-1", "Description": "mantis-qwen-qwen3-8b", "State": "deployed", "Created at": "2026-09-04"},
        {"App ID": "ap-2", "Description": "mantis-old", "State": "stopped"},
        {"App ID": "ap-3", "Description": "unrelated-app", "State": "deployed"},
        {"app_id": "ap-4", "name": "mantis-new-shape", "state": "ephemeral"},
    ])
    monkeypatch.setattr(modal_deploy, "_runner", Recorder((0, out, "")))
    deps = await ModalDeployProvider().list_deployments()
    assert [d.id for d in deps] == ["mantis-qwen-qwen3-8b", "mantis-old", "mantis-new-shape"]
    assert [d.status for d in deps] == ["running", "deleted", "unknown"]
    assert deps[0].model == "qwen-qwen3-8b" and deps[0].raw["app_id"] == "ap-1"


async def test_list_deployments_bad_json(env, monkeypatch):
    monkeypatch.setattr(modal_deploy, "_runner", Recorder((0, "not json", "")))
    with pytest.raises(DeployError):
        await ModalDeployProvider().list_deployments()


async def test_delete_stops_app_and_drops_key(env, monkeypatch):
    rec = Recorder((0, "", ""))
    monkeypatch.setattr(modal_deploy, "_runner", rec)
    monkeypatch.setenv("MANTIS_DEPLOY_X_KEY", "sk")
    await ModalDeployProvider().delete(_dep(auth_env="MANTIS_DEPLOY_X_KEY"))
    assert rec.calls[0][0][3:] == ["app", "stop", "mantis-qwen-qwen3-8b", "-y"]
    assert "MANTIS_DEPLOY_X_KEY" not in os.environ


async def test_delete_tolerates_missing_app(env, monkeypatch):
    monkeypatch.setattr(modal_deploy, "_runner", Recorder((1, "", "Error: App not found")))
    await ModalDeployProvider().delete(_dep())


async def test_cost_scales_with_containers():
    est = await ModalDeployProvider().cost(_dep(gpu=gpu(price=3.95), opts=DeployOpts(min_replicas=1, max_replicas=3)))
    assert est.per_hour_usd == pytest.approx(11.85) and est.idle_per_hour_usd == pytest.approx(3.95)
    est = await ModalDeployProvider().cost(_dep(gpu=gpu(price=3.95)))
    assert est.idle_per_hour_usd == 0.0
    # price unknown on the spec → looked up from the static table by family
    est = await ModalDeployProvider().cost(_dep(gpu=GpuSpec(provider_id="H100:2", family="H100", vram_gb=80, count=2)))
    assert est.per_hour_usd == pytest.approx(7.9)


def test_registered():
    from mantis_agent.deploy.base import DEPLOY_PROVIDERS, get_provider

    assert DEPLOY_PROVIDERS["modal"] is ModalDeployProvider
    prov = get_provider("modal")
    assert prov.display_name == "Modal" and prov.scale_to_zero is True and prov.public_by_default is False
    assert [f.env for f in prov.credential_fields] == [
        "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "MODAL_PROXY_TOKEN_ID", "MODAL_PROXY_TOKEN_SECRET", "HF_TOKEN",
    ]
    assert [f.required for f in prov.credential_fields] == [True, True, False, False, False]
