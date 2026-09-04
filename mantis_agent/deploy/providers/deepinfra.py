"""DeepInfra custom-LLM adapter — genuinely one API call.

``POST https://api.deepinfra.com/deploy/llm`` with a GPU string, a GPU
count and an HF repo starts a vLLM deployment; inference goes to the shared
``https://api.deepinfra.com/v1/openai`` with ``model="deploy_id:<id>"`` and
the same Bearer token. Thin feature set: no logs API, no GPU catalogue API
(five GPU strings, hard-coded from docs.deepinfra.com/private-models/custom-llms
and deepinfra.com/pricing), quantised checkpoints rejected, four GPUs per
account by default.

Control-plane routes: ``POST /deploy/llm``, ``GET /deploy/list``,
``GET /deploy/{id}``, ``PUT /deploy/{id}``, ``DELETE /deploy/{id}``.
Gated repos: the docs don't say how the HF token reaches the deployer; we
pass ``hf.token`` when we have one, which mirrors the dashboard's private-repo
field — verify on first use with a gated model.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from .._http import DeployHttp, mask_secrets, poll_until, raise_for, require_env, slugify
from ..base import (
    Account,
    CostEstimate,
    CredentialField,
    DeployError,
    DeployOpts,
    Deployment,
    DeploymentStatus,
    Engine,
    GpuSpec,
    NotSupported,
    register_provider,
    utcnow,
)

__all__ = ["DeepInfraProvider", "API_BASE", "OPENAI_BASE", "GPUS"]

API_BASE = "https://api.deepinfra.com"
OPENAI_BASE = "https://api.deepinfra.com/v1/openai"
CONSOLE_URL = "https://deepinfra.com/dash/deployments"

#: gpu string → (family, vram GB, $/h dedicated, September 2026)
GPUS: dict[str, tuple[str, int, float]] = {
    "A100-80GB": ("A100-80", 80, 0.89),
    "H100-80GB": ("H100", 80, 2.20),
    "H200-141GB": ("H200", 141, 2.69),
    "B200-180GB": ("B200", 180, 3.69),
    "B300-288GB": ("other", 288, 4.89),
}

_STATUS: dict[str, DeploymentStatus] = {
    "initializing": "starting", "deploying": "starting", "starting": "starting",
    "pending": "pending", "queued": "pending", "building": "building",
    "running": "running", "ready": "running", "active": "running",
    "scaled_to_zero": "scaled_to_zero", "idle": "scaled_to_zero", "sleeping": "scaled_to_zero",
    "failed": "failed", "error": "failed", "deleting": "deleting", "deleted": "deleted",
    "stopped": "paused", "paused": "paused",
}


@register_provider
class DeepInfraProvider:
    id = "deepinfra"
    display_name = "DeepInfra"
    credential_fields = (
        CredentialField("DEEPINFRA_API_KEY", "DeepInfra API key",
                        help="deepinfra.com/dash/api_keys"),
    )
    engines: tuple[Engine, ...] = ("vllm",)
    console_url = CONSOLE_URL
    scale_to_zero = True
    public_by_default = False

    # ------------------------------------------------------------------

    def configured(self) -> bool:
        return require_env("DEEPINFRA_API_KEY")

    def _http(self, **kw: Any) -> DeployHttp:
        key = (os.environ.get("DEEPINFRA_API_KEY") or "").strip()
        if not key:
            raise DeployError(
                "DEEPINFRA_API_KEY is not set",
                hint="create a key at deepinfra.com/dash/api_keys, then `mantis-agent deploy creds deepinfra --set DEEPINFRA_API_KEY=...`",
                provider=self.id,
            )
        return DeployHttp(
            provider=self.id, base_url=API_BASE, auth_env="DEEPINFRA_API_KEY", console_url=CONSOLE_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, **kw,
        )

    async def validate_credentials(self) -> Account:
        data = await self._http().json("GET", "/deploy/list", what="validate credentials")
        return Account(ok=True, provider=self.id, message=f"{len(_items(data))} custom deployment(s)")

    async def list_gpus(self) -> list[GpuSpec]:
        out = [
            GpuSpec(provider_id=gid, family=fam, vram_gb=vram, count=1, price_per_hour=price,  # type: ignore[arg-type]
                    available=None, label=gid)
            for gid, (fam, vram, price) in GPUS.items()
        ]
        # DeepInfra caps num_gpus at 4 per user; expose the x2/x4 A100 & H100
        # shapes so 70B-class models have somewhere to go.
        for gid in ("A100-80GB", "H100-80GB", "H200-141GB"):
            fam, vram, price = GPUS[gid]
            for n in (2, 4):
                out.append(GpuSpec(provider_id=gid, family=fam, vram_gb=vram, count=n,  # type: ignore[arg-type]
                                   price_per_hour=round(price * n, 2), available=None, label=f"{gid} ×{n}"))
        out.sort(key=lambda g: (g.price_per_hour or 0, g.total_vram_gb))
        return out

    # ------------------------------------------------------------------

    async def deploy(self, model: str, gpu: GpuSpec, engine: Engine, opts: DeployOpts) -> Deployment:
        if engine != "vllm":
            raise DeployError("deepinfra serves vLLM only", hint="use --engine vllm", provider=self.id)
        if gpu.provider_id not in GPUS:
            raise DeployError(f"deepinfra: unknown GPU {gpu.provider_id!r}",
                              hint="one of " + ", ".join(GPUS), provider=self.id)
        if opts.quantization:
            raise DeployError("deepinfra custom LLMs do not support quantized checkpoints",
                              hint="deploy the bf16 repo, or pick another provider", provider=self.id)
        name = slugify(opts.name or f"mantis-{slugify(model, max_len=28)}", max_len=40)
        hf: dict[str, Any] = {"repo": model}
        hf_token = opts.hf_token or os.environ.get("HF_TOKEN")
        if hf_token:
            hf["token"] = hf_token
        body: dict[str, Any] = {
            "model_name": name,
            "gpu": gpu.provider_id,
            "num_gpus": max(1, min(4, gpu.count)),
            "max_batch_size": int(opts.extra.get("max_batch_size", 64)),
            "hf": hf,
            "settings": {
                "min_instances": max(0, opts.min_replicas),
                "max_instances": max(1, opts.max_replicas),
            },
        }
        if opts.max_model_len:
            body["settings"]["max_model_len"] = int(opts.max_model_len)
        data = await self._http(timeout=90.0).json("POST", "/deploy/llm", json=body, what="create deployment")
        dep_id = str(data.get("deploy_id") or data.get("id") or "")
        if not dep_id:
            raise DeployError(f"deepinfra: create returned no deploy id: {mask_secrets(data)!r}", provider=self.id)
        dep = Deployment(
            id=dep_id, provider=self.id, model=model, engine="vllm", status="starting", gpu=gpu,
            served_model_name=f"deploy_id:{dep_id}", endpoint_url=OPENAI_BASE, name=name, opts=opts,
            auth_env="DEEPINFRA_API_KEY", message="deployment requested",
            raw={"deploy": mask_secrets(data)},
        )
        return self._apply(dep, data)

    def _apply(self, dep: Deployment, data: dict[str, Any]) -> Deployment:
        raw_status = str(data.get("status") or data.get("state") or "").lower()
        if raw_status:
            dep.status = _STATUS.get(raw_status, "unknown")
        msg = data.get("status_message") or data.get("message") or data.get("error") or ""
        dep.message = str(msg) if msg else (f"status={raw_status}" if raw_status else dep.message)
        dep.endpoint_url = OPENAI_BASE
        dep.raw = {**dep.raw, "deploy": mask_secrets(data)}
        dep.updated_at = utcnow()
        return dep

    async def status(self, dep: Deployment) -> Deployment:
        resp = await self._http().request("GET", f"/deploy/{dep.id}", raise_on_error=False, what="status")
        if resp.status_code == 404:
            dep.status = "deleted"
            dep.message = "deployment no longer exists"
            dep.updated_at = utcnow()
            return dep
        raise_for(resp, provider=self.id, auth_env="DEEPINFRA_API_KEY", console_url=CONSOLE_URL, what="status")
        body = resp.json() if resp.content else {}
        return self._apply(dep, body if isinstance(body, dict) else {})

    async def wait_ready(self, dep: Deployment, timeout_s: int = 1200) -> Deployment:
        async def check() -> tuple[bool, Any]:
            cur = await self.status(dep)
            if cur.status in ("running", "scaled_to_zero"):
                return True, cur
            if cur.status in ("failed", "deleted"):
                raise DeployError(f"deepinfra deployment {dep.id} {cur.status}: {cur.message}",
                                  hint=f"see {CONSOLE_URL}", provider=self.id)
            return False, cur

        await poll_until(check, timeout_s=timeout_s, interval_s=15.0,
                         what=f"deepinfra deployment {dep.id} to start", provider=self.id)
        return dep

    def logs(self, dep: Deployment, tail: int = 200) -> AsyncIterator[str]:
        raise NotSupported(
            "DeepInfra has no logs API for custom deployments",
            hint=f"open {CONSOLE_URL} for the deployment's log view", provider=self.id,
        )

    async def delete(self, dep: Deployment) -> None:
        resp = await self._http().request("DELETE", f"/deploy/{dep.id}", raise_on_error=False, what="delete")
        if resp.status_code in (200, 202, 204, 404):
            return
        raise_for(resp, provider=self.id, auth_env="DEEPINFRA_API_KEY", console_url=CONSOLE_URL, what="delete")

    async def list_deployments(self) -> list[Deployment]:
        data = await self._http().json("GET", "/deploy/list", what="list")
        out: list[Deployment] = []
        for d in _items(data):
            dep_id = str(d.get("deploy_id") or d.get("id") or "")
            if not dep_id:
                continue
            hf = d.get("hf") if isinstance(d.get("hf"), dict) else {}
            model = str(hf.get("repo") or d.get("model") or d.get("model_name") or "")
            gid = str(d.get("gpu") or "")
            fam, vram, price = GPUS.get(gid, ("other", 0, None))  # type: ignore[assignment]
            n = int(d.get("num_gpus") or 1)
            settings = d.get("settings") if isinstance(d.get("settings"), dict) else {}
            dep = Deployment(
                id=dep_id, provider=self.id, model=model, engine="vllm", status="unknown",
                gpu=GpuSpec(provider_id=gid or "unknown", family=fam, vram_gb=vram, count=n,  # type: ignore[arg-type]
                            price_per_hour=(price * n) if price else None, label=f"{gid} ×{n}" if n > 1 else gid),
                served_model_name=f"deploy_id:{dep_id}", endpoint_url=OPENAI_BASE,
                name=str(d.get("model_name") or d.get("name") or ""), auth_env="DEEPINFRA_API_KEY",
                opts=DeployOpts(min_replicas=int(settings.get("min_instances") or 0),
                                max_replicas=int(settings.get("max_instances") or 1)),
            )
            out.append(self._apply(dep, d))
        return out

    async def cost(self, dep: Deployment) -> CostEstimate:
        price = dep.gpu.price_per_hour
        if price is None:
            base = GPUS.get(dep.gpu.provider_id)
            price = base[2] * max(1, dep.gpu.count) if base else None
        per_hour = price * max(1, dep.opts.max_replicas) if price else None
        idle = price * dep.opts.min_replicas if (price and dep.opts.min_replicas > 0) else 0.0
        return CostEstimate(
            per_hour_usd=round(per_hour, 3) if per_hour else None, idle_per_hour_usd=round(idle, 3),
            basis="dedicated list price × GPUs × max instances (minute granularity); $0 at min_instances=0 while idle",
        )


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for k in ("deployments", "items", "data", "results"):
            v = data.get(k)
            if isinstance(v, list):
                return [d for d in v if isinstance(d, dict)]
    return []
