"""`mantis-agent deploy ...` — grammar, JSON output, error envelope, lazy imports."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from mantis_agent.cli import _build_parser, main
from mantis_agent.deploy import store
from mantis_agent.deploy.base import DeployOpts, Deployment, GpuSpec, register_provider


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("CLIFAKE_API_KEY", "k")
    for v in ("MANTIS_AGENT_API_KEY", "MANTIS_AGENT_BASE_URL", "MANTIS_AGENT_MODEL"):
        monkeypatch.delenv(v, raising=False)
    yield tmp_path


@register_provider
class CliFake:
    id = "clifake"
    display_name = "CLI Fake"
    credential_fields = ()
    engines = ("vllm",)
    console_url = "https://x"
    scale_to_zero = False
    public_by_default = False

    def configured(self) -> bool:
        return True

    async def validate_credentials(self):
        from mantis_agent.deploy.base import Account

        return Account(ok=True, provider="clifake", user="u")

    async def list_gpus(self):
        return [GpuSpec(provider_id="g1", family="L4", vram_gb=24, price_per_hour=0.5)]

    async def deploy(self, model, gpu, engine, opts):
        return Deployment(id="c1", provider="clifake", model=model, engine=engine, status="running", gpu=gpu,
                          served_model_name=model, endpoint_url="https://c.example/v1", opts=opts)

    async def status(self, dep):
        dep.message = "fresh"
        return dep

    async def wait_ready(self, dep, timeout_s=1200):
        return dep

    async def logs(self, dep, tail=200):
        yield "l1"
        yield "l2"

    async def delete(self, dep):
        return None

    async def list_deployments(self):
        return []

    async def cost(self, dep):
        from mantis_agent.deploy.base import CostEstimate

        return CostEstimate(per_hour_usd=0.5, idle_per_hour_usd=0.5, basis="fake")


def _run(capsys, argv: list[str]) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out)


def test_grammar_parses():
    p = _build_parser()
    a = p.parse_args(["deploy", "up", "runpod", "Qwen/Qwen3-8B", "--gpu", "AMPERE_80", "--engine", "vllm",
                      "--max-model-len", "8192", "--tp", "1", "--min", "0", "--max", "2", "--idle", "120",
                      "--name", "q", "--no-wait", "--json"])
    assert a.cmd == "deploy" and a.deploy_cmd == "up" and a.gpu == "AMPERE_80" and a.max == 2 and a.json
    for argv in (["deploy", "providers"], ["deploy", "creds", "runpod", "--set", "RUNPOD_API_KEY=x"],
                 ["deploy", "gpus", "hf", "--min-vram", "40"], ["deploy", "models", "qwen", "--sort", "downloads"],
                 ["deploy", "inspect", "Qwen/Qwen3-8B"], ["deploy", "ls", "--refresh"], ["deploy", "status", "id"],
                 ["deploy", "logs", "id", "--tail", "50"], ["deploy", "connect", "id"], ["deploy", "down", "id", "--yes"]):
        assert p.parse_args(argv + ["--json"]).json is True
    with pytest.raises(SystemExit):
        p.parse_args(["deploy", "up", "runpod", "m"])  # --gpu is required


def test_providers_json(capsys):
    rc, obj = _run(capsys, ["deploy", "providers", "--json"])
    assert rc == 0 and obj["ok"] is True
    by_id = {p["id"]: p for p in obj["providers"]}
    assert "runpod" in by_id and by_id["clifake"]["configured"] is True
    assert by_id["runpod"]["credential_fields"][0]["env"] == "RUNPOD_API_KEY"


def test_gpus_and_creds_json(capsys):
    rc, obj = _run(capsys, ["deploy", "gpus", "clifake", "--json"])
    assert rc == 0 and obj["gpus"][0]["provider_id"] == "g1"
    rc, obj = _run(capsys, ["deploy", "creds", "clifake", "--json"])
    assert rc == 0 and obj["ok"] is True and obj["account"]["user"] == "u"


def test_up_ls_status_logs_connect_down_json(capsys, monkeypatch):
    from mantis_agent.deploy import manager
    from mantis_agent.deploy.base import ModelInfo

    async def _inspect(model, *, hf_token=None):
        return ModelInfo(id=model, source="hf", vllm_ok=True, est_vram_gb=10)

    monkeypatch.setattr(manager, "inspect_model", _inspect)

    async def _connect(dep_id, *, set_current=True):
        return {"model": "org/m", "backend": "https://c.example/v1", "api_key_env": None, "headers": {}}

    monkeypatch.setattr(manager, "connect", _connect)

    rc, obj = _run(capsys, ["deploy", "up", "clifake", "org/m", "--gpu", "g1", "--json"])
    assert rc == 0 and obj["deployment"]["id"] == "c1" and obj["deployment"]["status"] == "running"
    assert obj["connect"]["backend"] == "https://c.example/v1"
    assert isinstance(obj["deployment"]["created_at"], str)

    rc, obj = _run(capsys, ["deploy", "ls", "--json"])
    assert rc == 0 and [d["id"] for d in obj["deployments"]] == ["c1"]

    rc, obj = _run(capsys, ["deploy", "status", "c1", "--json"])
    assert rc == 0 and obj["deployment"]["message"] == "fresh" and obj["cost"]["per_hour_usd"] == 0.5

    rc, obj = _run(capsys, ["deploy", "logs", "c1", "--tail", "5", "--json"])
    assert rc == 0 and obj["lines"] == ["l1", "l2"]

    rc, obj = _run(capsys, ["deploy", "connect", "c1", "--json"])
    assert rc == 0 and obj["model"] == "org/m"

    rc, obj = _run(capsys, ["deploy", "down", "c1", "--yes", "--json"])
    assert rc == 0 and obj["deleted"] is True
    assert store.find("c1").status == "deleted"


def test_human_output_prints_launch_line(capsys, monkeypatch):
    from mantis_agent.deploy import manager

    async def _connect(dep_id, *, set_current=True):
        return {"model": "org/m", "backend": "https://c.example/v1", "api_key_env": "CLIFAKE_API_KEY", "headers": {}}

    monkeypatch.setattr(manager, "connect", _connect)
    store.upsert(Deployment(id="c1", provider="clifake", model="org/m", engine="vllm", status="running",
                            gpu=GpuSpec(provider_id="g1", family="L4", vram_gb=24), served_model_name="org/m",
                            endpoint_url="https://c.example/v1", opts=DeployOpts()))
    rc = main(["deploy", "connect", "c1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "MANTIS_AGENT_MODEL=org/m MANTIS_AGENT_BASE_URL=https://c.example/v1 MANTIS_AGENT_API_KEY=$CLIFAKE_API_KEY mantis" in out
    rc = main(["deploy", "ls"])
    out = capsys.readouterr().out
    assert rc == 0 and "c1" in out and "clifake" in out


def test_error_envelope(capsys):
    rc, obj = _run(capsys, ["deploy", "status", "does-not-exist", "--json"])
    assert rc == 1 and obj["ok"] is False and "no deployment" in obj["error"] and obj["hint"]
    rc = main(["deploy", "status", "does-not-exist"])
    err = capsys.readouterr().err
    assert rc == 1 and err.startswith("error:") and "hint:" in err


def test_not_supported_envelope(capsys, monkeypatch):
    from mantis_agent.deploy import manager
    from mantis_agent.deploy.base import NotSupported

    def _logs(dep_id, *, tail=200):
        raise NotSupported("no logs", hint="use the console")

    monkeypatch.setattr(manager, "logs", _logs)
    rc, obj = _run(capsys, ["deploy", "logs", "x", "--json"])
    assert rc == 2 and obj["supported"] is False


def test_cli_import_does_not_load_deploy_or_providers():
    code = ("import sys, mantis_agent.cli; "
            "print(int('mantis_agent.deploy' in sys.modules), int(any(m.startswith('mantis_agent.deploy.providers') for m in sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "PYTHONPATH": os.getcwd()}).stdout.strip()
    assert out == "0 0"
