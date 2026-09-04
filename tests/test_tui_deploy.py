"""``/deploy`` — bring-your-own GPU from inside the terminal.

The backend contract (``mantis_agent.deploy.manager``) is monkeypatched with
fakes; what is tested is the terminal's side: the status panel, the ``up``
grammar, the connect flow switching the session's model exactly like
``/model``, the down confirmation naming the $/h, and the /dash facts.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

import pytest
from rich.console import Console

from mantis_agent import catalog
from mantis_agent.deploy import manager as dm
from mantis_agent.deploy.base import (
    Account,
    CredentialField,
    DeployError,
    Deployment,
    GpuSpec,
    ModelInfo,
)
from mantis_agent.tui import (
    DEPLOY_USAGE,
    SLASH_COMMANDS,
    MantisTUI,
    build_help_lines,
    dashboard_line,
    deploy_cost_per_hour,
    parse_deploy_up,
    render_dashboard,
    render_deploy_panel,
)

# -- fakes -------------------------------------------------------------------------

H100 = GpuSpec(provider_id="NVIDIA H100 80GB", family="H100", vram_gb=80, price_per_hour=2.49,
               available=True, label="H100 80GB")


def _dep(id: str = "ep-123", status: str = "running", provider: str = "runpod",
         model: str = "meta-llama/Llama-3.1-8B-Instruct", endpoint: str | None = "https://api.runpod.ai/v2/ep-123/openai/v1") -> Deployment:
    return Deployment(id=id, provider=provider, model=model, engine="vllm", status=status,  # type: ignore[arg-type]
                      gpu=H100, served_model_name=model, endpoint_url=endpoint,
                      auth_env="RUNPOD_API_KEY")


def _providers() -> list[dict]:
    return [
        {"id": "runpod", "display_name": "RunPod Serverless", "configured": True,
         "credential_fields": [CredentialField("RUNPOD_API_KEY", "RunPod API key",
                                               help="console.runpod.io → Settings → API Keys")],
         "engines": ("vllm", "sglang"), "console_url": "https://console.runpod.io",
         "scale_to_zero": True, "public_by_default": False},
        {"id": "modal", "display_name": "Modal", "configured": False,
         "credential_fields": [CredentialField("MODAL_TOKEN_ID", "Modal token id", secret=False),
                               CredentialField("MODAL_TOKEN_SECRET", "Modal token secret")],
         "engines": ("vllm",), "console_url": "https://modal.com", "scale_to_zero": True,
         "public_by_default": False},
    ]


class _UI:
    """A scripted UI adapter: prints go to the console, secrets and confirms
    come from queues, jobs run inline so a test can await them."""

    can_prompt_after_job = True

    def __init__(self, tui: MantisTUI, secrets: list[str] | None = None,
                 confirms: list[bool] | None = None) -> None:
        self.tui = tui
        self.secrets = list(secrets or [])
        self.confirms = list(confirms or [])
        self.asked: list[str] = []
        self.switched: list[tuple[str, str, str | None]] = []
        self.jobs: list[Any] = []
        self.progress_lines: list[str] = []

    async def emit(self, fn: Any) -> None:
        fn()

    async def secret(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.secrets.pop(0) if self.secrets else ""

    async def confirm(self, question: str) -> bool:
        self.asked.append(question)
        return self.confirms.pop(0) if self.confirms else False

    def spawn(self, coro: Any, desc: str) -> Any:
        task = asyncio.ensure_future(coro)
        task.id = len(self.jobs) + 1  # type: ignore[attr-defined]
        self.jobs.append(task)
        return task

    async def switch(self, model: str, backend: str, api_key: str | None) -> None:
        self.switched.append((model, backend, api_key))
        self.tui.model, self.tui.backend, self.tui.api_key = model, backend, api_key

    def progress(self, label: str) -> Any:
        ui = self

        class _P:
            def line(self, text: str) -> None:
                ui.progress_lines.append(text)

            async def finish(self, dep: Any, err: BaseException | None) -> None:
                ui.tui._deploy_print_outcome(dep, err, 3.0)
        return _P()


def _tui(width: int = 80) -> MantisTUI:
    t = MantisTUI(model="qwen3:8b", backend="http://localhost:11434/v1", api_key=None,
                  system=None, max_tokens=1, temperature=None, max_turns=1)
    t.console = Console(width=width, file=io.StringIO(), force_terminal=False, color_system=None)
    return t


def _out(t: MantisTUI) -> str:
    return t.console.file.getvalue()


async def _run(t: MantisTUI, arg: str, ui: _UI) -> None:
    await t._cmd_deploy(arg, ui=ui)
    for job in ui.jobs:
        await job


@pytest.fixture(autouse=True)
def _fake_manager(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls: dict[str, list] = {"teardown": [], "deploy": [], "connect": [], "creds": []}

    async def providers() -> list:
        return _providers()

    async def list_deployments(*, refresh: bool = False, provider_id: str | None = None) -> list:
        return [_dep(), _dep("ep-zero", status="scaled_to_zero", model="Qwen/Qwen3-8B")]

    async def status(dep_id: str, *, refresh: bool = True) -> Deployment:
        if dep_id == "nope":
            raise DeployError("no deployment 'nope'", hint="see /deploy ls")
        return _dep(dep_id)

    async def connect(dep_id: str, *, set_current: bool = True) -> dict:
        calls["connect"].append(dep_id)
        return {"model": "meta-llama/Llama-3.1-8B-Instruct",
                "backend": "https://api.runpod.ai/v2/ep-123/openai/v1",
                "api_key_env": "RUNPOD_API_KEY", "headers": {}}

    async def teardown(dep_id: str, *, progress: Any = None) -> None:
        calls["teardown"].append(dep_id)
        if progress:
            progress("deleted endpoint")

    async def deploy(provider_id: str, model: str, *, gpu: Any, engine: str = "vllm",
                     opts: Any = None, wait: bool = True, progress: Any = None) -> Deployment:
        calls["deploy"].append((provider_id, model, gpu, engine, opts))
        for ln in ("pre-flight: 8.0B · BF16 · ~20 GB", "creating endpoint", "cold start… 503",
                   "ready"):
            if progress:
                progress(ln)
        return _dep(model=model, provider=provider_id)

    async def save_credentials(provider_id: str, values: dict) -> Account:
        calls["creds"].append((provider_id, values))
        return Account(ok=True, provider=provider_id, user="teddy", balance_usd=42.0)

    async def gpus(provider_id: str, *, min_vram_gb: int | None = None) -> list:
        return [H100]

    async def search_models(query: str = "", *, limit: int = 25, sort: str = "trending") -> list:
        return [ModelInfo(id="Qwen/Qwen3-8B", source="hf", params_b=8.2, dtype="BF16",
                          vllm_ok=True, est_vram_gb=20.5, context_len=32768)]

    for name, fn in (("providers", providers), ("list_deployments", list_deployments),
                     ("status", status), ("connect", connect), ("teardown", teardown),
                     ("deploy", deploy), ("save_credentials", save_credentials),
                     ("gpus", gpus), ("search_models", search_models)):
        monkeypatch.setattr(dm, name, fn)
    monkeypatch.setenv("RUNPOD_API_KEY", "rp-secret")
    return calls


# -- the status panel ----------------------------------------------------------------


def test_panel_lists_providers_deployments_and_cost() -> None:
    c = Console(width=80, record=True, force_terminal=False, color_system=None)
    c.print(render_deploy_panel(_providers(), [_dep(), _dep("ep-zero", status="scaled_to_zero")], 80))
    out = c.export_text()
    assert "mantis · deploy" in out
    assert "● RunPod Serverless" in out
    assert "○ Modal  run /deploy creds modal" in out
    assert "2 deployments · 1 running · $2.49/h" in out
    assert "ep-123  runpod · meta-llama/Llama-3.1-8B-Instruct · H100 80GB" in out
    assert "ep-zero  runpod" in out
    assert max(len(ln) for ln in out.splitlines()) <= 80
    # The full row — provider · model · GPU · status · $/h · endpoint — when it fits.
    from mantis_agent.tui import deployment_row

    assert deployment_row(_dep(), 200).plain == (
        "ep-123  runpod · meta-llama/Llama-3.1-8B-Instruct · H100 80GB · running · $2.49/h"
        " · https://api.runpod.ai/v2/ep-123/openai/v1")
    assert "scaled_to_zero · $0.00/h" in deployment_row(_dep("ep-zero", status="scaled_to_zero"), 200).plain


def test_panel_empty_shows_the_three_steps() -> None:
    c = Console(width=80, record=True, force_terminal=False, color_system=None)
    c.print(render_deploy_panel([{"id": "runpod", "display_name": "RunPod", "configured": False}], [], 80))
    out = c.export_text()
    assert "0 deployments" in out
    assert "1  /deploy creds <provider>" in out
    assert "2  /deploy gpus <provider>" in out
    assert "3  /deploy up <provider> <model> --gpu <id>" in out


def test_bare_deploy_prints_the_panel_and_refreshes_the_snapshot() -> None:
    t = _tui()
    asyncio.run(_run(t, "", _UI(t)))
    assert "mantis · deploy" in _out(t) and "ep-123" in _out(t)
    assert [d.id for d in t._deploy_snapshot] == ["ep-123", "ep-zero"]


# -- grammar -----------------------------------------------------------------------------


def test_parse_up_full_grammar() -> None:
    got = parse_deploy_up(["runpod", "Qwen/Qwen3-8B", "--gpu", "NVIDIA H100 80GB", "--engine",
                           "sglang", "--max-model-len", "32768", "--tp", "2", "--min", "1",
                           "--max=3", "--idle", "120", "--name", "q3"])
    assert got == {"provider": "runpod", "model": "Qwen/Qwen3-8B", "gpu": "NVIDIA H100 80GB",
                   "engine": "sglang", "max_model_len": 32768, "tensor_parallel": 2,
                   "min_replicas": 1, "max_replicas": 3, "idle_timeout_s": 120, "name": "q3",
                   "trust_remote_code": False}


def test_parse_up_defaults_and_errors() -> None:
    got = parse_deploy_up(["runpod", "m", "--gpu", "g"])
    assert got["engine"] == "vllm" and got["min_replicas"] == 0 and got["max_replicas"] == 1
    assert got["idle_timeout_s"] == 300
    assert "usage" in parse_deploy_up(["runpod"])
    assert "--gpu" in parse_deploy_up(["runpod", "m"])
    assert "number" in parse_deploy_up(["runpod", "m", "--gpu", "g", "--tp", "two"])
    assert "unknown flag" in parse_deploy_up(["runpod", "m", "--gpu", "g", "--bogus", "1"])
    assert "--engine" in parse_deploy_up(["runpod", "m", "--gpu", "g", "--engine", "torch"])
    assert "needs a value" in parse_deploy_up(["runpod", "m", "--gpu"])


def test_up_runs_as_a_job_with_progress_and_the_connect_hint(_fake_manager: dict) -> None:
    t = _tui()
    ui = _UI(t, confirms=[False])
    asyncio.run(_run(t, "up runpod Qwen/Qwen3-8B --gpu 'NVIDIA H100 80GB' --max-model-len 8192", ui))
    provider, model, gpu, engine, opts = _fake_manager["deploy"][0]
    assert (provider, model, gpu, engine) == ("runpod", "Qwen/Qwen3-8B", "NVIDIA H100 80GB", "vllm")
    assert opts.max_model_len == 8192 and opts.min_replicas == 0
    assert ui.progress_lines == ["pre-flight: 8.0B · BF16 · ~20 GB", "creating endpoint",
                                 "cold start… 503", "ready"]
    out = _out(t)
    assert "job #1" in out
    assert "└ ep-123 · running · 3s · $2.49/h" in out
    assert "/deploy connect ep-123" in out
    assert ui.asked == ["Use Qwen/Qwen3-8B on runpod now?"]     # the one-key offer
    assert not ui.switched


def test_up_yes_connects_right_away(_fake_manager: dict) -> None:
    t = _tui()
    ui = _UI(t, confirms=[True])
    asyncio.run(_run(t, "up runpod Qwen/Qwen3-8B --gpu H100", ui))
    assert _fake_manager["connect"] == ["ep-123"]
    assert ui.switched == [("meta-llama/Llama-3.1-8B-Instruct",
                            "https://api.runpod.ai/v2/ep-123/openai/v1", "rp-secret")]


def test_up_failure_prints_the_hint_not_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    async def deploy(*a: Any, **k: Any) -> Deployment:
        raise DeployError("GPU sold out", hint="try --gpu 'NVIDIA A100 80GB'")
    monkeypatch.setattr(dm, "deploy", deploy)
    t = _tui()
    ui = _UI(t)

    async def go() -> None:
        await t._cmd_deploy("up runpod m --gpu H100", ui=ui)
        with pytest.raises(DeployError):
            await ui.jobs[0]
    asyncio.run(go())
    out = _out(t)
    assert "deploy failed after 3s: GPU sold out" in out
    assert "→ try --gpu 'NVIDIA A100 80GB'" in out
    assert "Traceback" not in out


# -- connect switches the session like /model ------------------------------------------


def test_connect_switches_model_backend_and_key(_fake_manager: dict) -> None:
    t = _tui()
    ui = _UI(t)
    asyncio.run(_run(t, "connect ep-123", ui))
    assert _fake_manager["connect"] == ["ep-123"]
    assert ui.switched == [("meta-llama/Llama-3.1-8B-Instruct",
                            "https://api.runpod.ai/v2/ep-123/openai/v1", "rp-secret")]
    assert t.model == "meta-llama/Llama-3.1-8B-Instruct"


def test_classic_connect_prints_the_same_routing_note(monkeypatch: pytest.MonkeyPatch) -> None:
    t = _tui()
    monkeypatch.setattr(t, "_build_agent", lambda: None)
    monkeypatch.setattr(catalog, "set_last_model", lambda *a, **k: None)
    monkeypatch.setattr(catalog, "push_recent_model", lambda *a, **k: None)
    asyncio.run(t._cmd_deploy("connect ep-123"))          # the default classic adapter
    out = _out(t)
    assert "model → meta-llama/Llama-3.1-8B-Instruct · ⚙ Self-host · via" in out
    assert len(out.splitlines()) == 1                       # one line, cut — never wrapped
    assert t.backend == "https://api.runpod.ai/v2/ep-123/openai/v1"


# -- down confirms and names the money --------------------------------------------------


def test_down_confirm_names_the_hourly_cost_and_tears_down(_fake_manager: dict) -> None:
    t = _tui()
    ui = _UI(t, confirms=[True])
    asyncio.run(_run(t, "down ep-123", ui))
    assert ui.asked == ["Tear down ep-123: meta-llama/Llama-3.1-8B-Instruct on runpod (H100 80GB)"
                        " — stops $2.49/h from accruing?"]
    assert _fake_manager["teardown"] == ["ep-123"]
    assert "✓ ep-123 torn down · deleted endpoint" in _out(t)


def test_down_declined_keeps_it(_fake_manager: dict) -> None:
    t = _tui()
    asyncio.run(_run(t, "down ep-123", _UI(t, confirms=[False])))
    assert _fake_manager["teardown"] == []
    assert "(kept)" in _out(t)


# -- creds, gpus, models, errors, usage ----------------------------------------------------


def test_creds_prompts_each_field_masked_then_validates(_fake_manager: dict) -> None:
    t = _tui()
    ui = _UI(t, secrets=["tok-id", "tok-secret"])
    asyncio.run(_run(t, "creds modal", ui))
    assert ui.asked[0].startswith("Modal token id ($MODAL_TOKEN_ID)")
    assert ui.asked[1].startswith("Modal token secret ($MODAL_TOKEN_SECRET)")
    assert _fake_manager["creds"] == [("modal", {"MODAL_TOKEN_ID": "tok-id",
                                                 "MODAL_TOKEN_SECRET": "tok-secret"})]
    out = _out(t)
    assert "✓ modal user teddy · balance $42.00" in out
    assert "next: /deploy gpus modal" in out


def test_creds_cancelled_saves_nothing(_fake_manager: dict) -> None:
    t = _tui()
    asyncio.run(_run(t, "creds modal", _UI(t, secrets=[""])))
    assert _fake_manager["creds"] == []
    assert "cancelled" in _out(t)


def test_gpus_and_models_render() -> None:
    t = _tui()
    asyncio.run(_run(t, "gpus runpod --min-vram 40", _UI(t)))
    assert "NVIDIA H100 80GB" in _out(t) and "H100 · 80 GB · $2.49/h" in _out(t)
    t = _tui()
    asyncio.run(_run(t, "models qwen", _UI(t)))
    assert "Qwen/Qwen3-8B" in _out(t) and "vllm ✓" in _out(t) and "8.2B · BF16 · ~20 GB · ctx 32k" in _out(t)


def test_deploy_error_is_one_line_with_hint() -> None:
    t = _tui()
    asyncio.run(_run(t, "status nope", _UI(t)))
    out = _out(t)
    assert "✗ deploy: no deployment 'nope'" in out and "→ see /deploy ls" in out


def test_not_implemented_backend_is_reported_kindly(monkeypatch: pytest.MonkeyPatch) -> None:
    async def providers() -> list:
        raise NotImplementedError
    monkeypatch.setattr(dm, "providers", providers)
    t = _tui()
    asyncio.run(_run(t, "", _UI(t)))
    assert "isn't wired in this build yet" in _out(t)


def test_unknown_subcommand_prints_usage() -> None:
    t = _tui()
    asyncio.run(_run(t, "frobnicate", _UI(t)))
    out = _out(t)
    for cmd, _ in DEPLOY_USAGE:
        assert cmd.split(" [")[0][:30] in out


def test_deploy_is_registered_and_categorised() -> None:
    assert "/deploy" in SLASH_COMMANDS
    rows = {c: cat for cat, c, _ in build_help_lines(SLASH_COMMANDS)}
    assert rows["/deploy"] == "model"


# -- /dash carries the deploy facts -----------------------------------------------------------


def test_dash_has_a_deploy_row_only_when_something_is_deployed() -> None:
    t = _tui()
    facts = t._dash_facts(light=True)
    assert facts["deploy"]["count"] == 0
    c = Console(width=80, record=True, force_terminal=False, color_system=None)
    c.print(render_dashboard(facts, 80))
    assert "deploy" not in c.export_text().replace("dashboard", "")
    assert "deploy" not in dashboard_line(facts)

    t._deploy_snapshot = [_dep(), _dep("ep-zero", status="scaled_to_zero")]
    facts = t._dash_facts(light=True)
    assert facts["deploy"] == {"count": 2, "running": 1, "per_hour": 2.49,
                               "rows": ["ep-123 · meta-llama/Llama-3.1-8B-Instruct · running",
                                        "ep-zero · meta-llama/Llama-3.1-8B-Instruct · scaled_to_zero"]}
    c = Console(width=80, record=True, force_terminal=False, color_system=None)
    c.print(render_dashboard(facts, 80))
    out = c.export_text()
    assert "deploy   2 live · 1 running · $2.49/h" in out
    assert max(len(ln) for ln in out.splitlines()) <= 80
    assert "2 deploy · $2.49/h" in dashboard_line(facts)


def test_cost_per_hour_rules() -> None:
    assert deploy_cost_per_hour(_dep()) == 2.49
    assert deploy_cost_per_hour(_dep(status="scaled_to_zero")) == 0.0
    assert deploy_cost_per_hour(_dep(status="starting")) is None
    free = _dep()
    free.gpu = GpuSpec(provider_id="x", family="other", vram_gb=1)
    assert deploy_cost_per_hour(free) is None
