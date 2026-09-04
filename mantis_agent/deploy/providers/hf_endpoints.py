"""Hugging Face Inference Endpoints adapter.

The lowest-friction path for HF-centric users: one ``hf_`` token
authenticates the control plane, the deployed endpoint *and* gated repos,
and HF pulls the weights server-side. Engine is chosen by keying
``model.image`` with ``vLLM`` / ``sGLang`` / ``tgi`` / ``llamacpp``.

Shapes (api.endpoints.huggingface.cloud/openapi.json, September 2026):

* ``POST /v2/endpoint/{namespace}`` create, ``GET`` list; ``GET/DELETE
  /v2/endpoint/{ns}/{name}``; ``GET /v3/endpoint/{ns}/{name}/logs``.
* ``GET /v2/provider/{namespace}`` — vendors → regions → computes with
  ``instanceType`` / ``instanceSize`` / ``pricePerHour`` / ``status``.
* ``GET https://huggingface.co/api/whoami-v2`` → ``{"name": ...}``.
* Status enum: ``pending, initializing, updating, updateFailed, running,
  paused, failed, scaledToZero``. ``status.url`` is the base; OpenAI routes
  live under ``{url}/v1``.
* A scaled-to-zero endpoint answers **503** while it wakes; send
  ``X-Scale-Up-Timeout: 600`` to hold the request.

GPU ids here are ``vendor/region/instanceType/instanceSize`` so a pick from
``list_gpus`` round-trips into the create body verbatim.
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
    register_provider,
    utcnow,
)
from ..preflight import gpu_family_from_name

__all__ = ["HFEndpointsProvider", "API_BASE", "STATIC_HARDWARE", "ENGINE_IMAGES"]

API_BASE = "https://api.endpoints.huggingface.cloud"
HUB_API = "https://huggingface.co/api"
CONSOLE_URL = "https://ui.endpoints.huggingface.co"

#: engine → (image key in ``model.image``, default container image)
ENGINE_IMAGES: dict[str, tuple[str, str]] = {
    "vllm": ("vLLM", "vllm/vllm-openai:v0.23.0"),
    "sglang": ("sGLang", "lmsysorg/sglang:latest"),
    "tgi": ("tgi", "ghcr.io/huggingface/text-generation-inference:latest"),
    "llamacpp": ("llamacpp", "ghcr.io/ggml-org/llama.cpp:server-cuda"),
}

#: Fallback catalogue (huggingface.co/docs/inference-endpoints/pricing, Sept 2026)
#: (vendor, region, instance_type, instance_size, count, vram, $/h)
STATIC_HARDWARE: tuple[tuple[str, str, str, str, int, int, float], ...] = (
    ("aws", "us-east-1", "nvidia-t4", "x1", 1, 16, 0.50),
    ("aws", "us-east-1", "nvidia-l4", "x1", 1, 24, 0.80),
    ("aws", "us-east-1", "nvidia-l4", "x4", 4, 24, 3.80),
    ("aws", "us-east-1", "nvidia-a10g", "x1", 1, 24, 1.00),
    ("aws", "us-east-1", "nvidia-a10g", "x4", 4, 24, 5.00),
    ("aws", "us-east-1", "nvidia-l40s", "x1", 1, 48, 1.80),
    ("aws", "us-east-1", "nvidia-l40s", "x4", 4, 48, 8.30),
    ("aws", "us-east-1", "nvidia-a100", "x1", 1, 80, 2.50),
    ("aws", "us-east-1", "nvidia-a100", "x2", 2, 80, 5.00),
    ("aws", "us-east-1", "nvidia-a100", "x4", 4, 80, 10.00),
    ("aws", "us-east-1", "nvidia-h200", "x1", 1, 141, 5.00),
    ("gcp", "us-east4", "nvidia-h100", "x1", 1, 80, 10.00),
)

_STATUS: dict[str, DeploymentStatus] = {
    "pending": "pending", "initializing": "starting", "updating": "starting",
    "updatefailed": "failed", "running": "running", "paused": "paused",
    "failed": "failed", "scaledtozero": "scaled_to_zero",
}

_VRAM_BY_TYPE: dict[str, int] = {
    "nvidia-t4": 16, "nvidia-l4": 24, "nvidia-a10g": 24, "nvidia-l40s": 48,
    "nvidia-a100": 80, "nvidia-h100": 80, "nvidia-h200": 141,
}


def _gpu_id(vendor: str, region: str, itype: str, isize: str) -> str:
    return f"{vendor}/{region}/{itype}/{isize}"


def _parse_gpu_id(pid: str) -> tuple[str, str, str, str]:
    parts = pid.split("/")
    if len(parts) != 4:
        raise DeployError(
            f"hf: GPU id must be vendor/region/instance-type/size (got {pid!r})",
            hint="pick one from `mantis-agent deploy gpus hf`, e.g. aws/us-east-1/nvidia-a100/x1", provider="hf",
        )
    return parts[0], parts[1], parts[2], parts[3]


@register_provider
class HFEndpointsProvider:
    id = "hf"
    display_name = "Hugging Face Inference Endpoints"
    credential_fields = (
        CredentialField("HF_TOKEN", "Hugging Face token (write scope for endpoints)",
                        help="huggingface.co/settings/tokens — the same token unlocks gated repos"),
    )
    engines: tuple[Engine, ...] = ("vllm", "sglang", "tgi", "llamacpp")
    console_url = CONSOLE_URL
    scale_to_zero = True
    public_by_default = False  # we create type=protected (token required)

    def __init__(self) -> None:
        self._namespace: str | None = None

    # ------------------------------------------------------------------

    def configured(self) -> bool:
        return require_env("HF_TOKEN")

    def _token(self) -> str:
        tok = (os.environ.get("HF_TOKEN") or "").strip()
        if not tok:
            raise DeployError(
                "HF_TOKEN is not set",
                hint="create a token at huggingface.co/settings/tokens, then `mantis-agent deploy creds hf --set HF_TOKEN=hf_...`",
                provider=self.id,
            )
        return tok

    def _http(self, base: str = API_BASE, **kw: Any) -> DeployHttp:
        return DeployHttp(
            provider=self.id, base_url=base, auth_env="HF_TOKEN", console_url=CONSOLE_URL,
            headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}, **kw,
        )

    async def _whoami(self) -> dict[str, Any]:
        data = await self._http(HUB_API).json("GET", "/whoami-v2", what="whoami")
        return data if isinstance(data, dict) else {}

    async def namespace(self) -> str:
        ns = os.environ.get("HF_ENDPOINTS_NAMESPACE")
        if ns:
            return ns
        if self._namespace is None:
            who = await self._whoami()
            self._namespace = str(who.get("name") or "")
            if not self._namespace:
                raise DeployError("hf: could not determine the account namespace from whoami-v2", provider=self.id)
        return self._namespace

    async def validate_credentials(self) -> Account:
        who = await self._whoami()
        ns = str(who.get("name") or "")
        self._namespace = ns or self._namespace
        orgs = [o.get("name") for o in (who.get("orgs") or []) if isinstance(o, dict)]
        return Account(ok=True, provider=self.id, user=ns or None,
                       message="namespace " + ns + (f"; orgs: {', '.join(map(str, orgs))}" if orgs else ""))

    # ------------------------------------------------------------------

    async def list_gpus(self) -> list[GpuSpec]:
        ns = await self.namespace()
        out: list[GpuSpec] = []
        try:
            data = await self._http().json("GET", f"/v2/provider/{ns}", what="hardware catalogue")
            for vendor in _as_list(data, "vendors"):
                vname = str(vendor.get("name") or vendor.get("vendor") or "")
                for region in _as_list(vendor, "regions"):
                    rname = str(region.get("name") or region.get("region") or "")
                    for c in _as_list(region, "computes"):
                        if str(c.get("accelerator") or "gpu").lower() != "gpu":
                            continue
                        itype = str(c.get("instanceType") or c.get("instance_type") or "")
                        isize = str(c.get("instanceSize") or c.get("instance_size") or "x1")
                        if not itype:
                            continue
                        count = int(c.get("numberOfAccelerators") or c.get("numAccelerators") or _size_count(isize))
                        price = c.get("pricePerHour") or c.get("price_per_hour")
                        try:
                            price_f = float(price) if price is not None else None
                        except (TypeError, ValueError):
                            price_f = None
                        st = str(c.get("status") or "available").lower()
                        vram = _VRAM_BY_TYPE.get(itype, 0)
                        mem = c.get("memoryGb") or c.get("acceleratorMemoryGb")
                        if not vram and mem:
                            try:
                                vram = int(float(mem))
                            except (TypeError, ValueError):
                                vram = 0
                        out.append(GpuSpec(
                            provider_id=_gpu_id(vname, rname, itype, isize),
                            family=gpu_family_from_name(itype, vram),  # type: ignore[arg-type]
                            vram_gb=vram, count=max(1, count), price_per_hour=price_f,
                            available=st not in ("not_available", "unavailable"), region=rname,
                            label=f"{itype} {isize} ({vname}/{rname})",
                        ))
        except DeployError:
            out = []
        if not out:
            for vendor, region, itype, isize, count, vram, price in STATIC_HARDWARE:
                out.append(GpuSpec(
                    provider_id=_gpu_id(vendor, region, itype, isize),
                    family=gpu_family_from_name(itype, vram), vram_gb=vram, count=count,  # type: ignore[arg-type]
                    price_per_hour=price, available=None, region=region,
                    label=f"{itype} {isize} ({vendor}/{region})",
                ))
        out.sort(key=lambda g: (g.price_per_hour if g.price_per_hour is not None else 1e9, g.total_vram_gb))
        return out

    # ------------------------------------------------------------------

    async def deploy(self, model: str, gpu: GpuSpec, engine: Engine, opts: DeployOpts) -> Deployment:
        ns = await self.namespace()
        if engine not in ENGINE_IMAGES:
            raise DeployError(f"hf: unsupported engine {engine}", hint=f"one of {', '.join(ENGINE_IMAGES)}", provider=self.id)
        vendor, region, itype, isize = _parse_gpu_id(gpu.provider_id)
        name = slugify(opts.name or f"mantis-{slugify(model, max_len=24)}", max_len=32)
        key, default_image = ENGINE_IMAGES[engine]
        image_url = opts.engine_version or default_image
        if ":" not in image_url and "/" not in image_url:
            image_url = f"{default_image.split(':', 1)[0]}:{image_url}"
        tp = opts.tensor_parallel or gpu.count or 1
        image_cfg: dict[str, Any] = {"url": image_url, "port": 8000, "healthRoute": "/health"}
        if key in ("vLLM", "sGLang"):
            image_cfg["tensorParallelSize"] = tp
        if key == "vLLM" and opts.max_model_len:
            image_cfg["maxModelLen"] = opts.max_model_len
        if key == "tgi" and opts.max_model_len:
            image_cfg["maxTotalTokens"] = opts.max_model_len
        if opts.quantization and key in ("vLLM", "tgi"):
            image_cfg["quantize"] = opts.quantization
        container_args: list[str] = list(opts.extra_engine_args)
        if opts.trust_remote_code and key in ("vLLM", "sGLang", "tgi"):
            container_args.append("--trust-remote-code")
        if opts.served_model_name and key in ("vLLM", "sGLang"):
            container_args += ["--served-model-name", opts.served_model_name]
        secrets: dict[str, str] = {}
        hf_token = opts.hf_token or os.environ.get("HF_TOKEN")
        if hf_token:
            secrets["HF_TOKEN"] = hf_token
        body: dict[str, Any] = {
            "name": name,
            "type": "protected",
            "provider": {"vendor": vendor, "region": region},
            "compute": {
                "accelerator": "gpu",
                "instanceType": itype,
                "instanceSize": isize,
                "scaling": {
                    "minReplica": max(0, opts.min_replicas),
                    "maxReplica": max(1, opts.max_replicas),
                    "scaleToZeroTimeout": max(1, opts.idle_timeout_s // 60),
                },
            },
            "model": {
                "repository": model,
                "revision": str(opts.extra.get("revision") or "main"),
                "framework": "pytorch",
                "task": "text-generation",
                "image": {key: image_cfg},
                "env": {str(k): str(v) for k, v in (opts.extra.get("env") or {}).items()},
                "secrets": secrets,
            },
            "tags": ["mantis"],
        }
        if container_args:
            body["model"]["args"] = container_args
        data = await self._http(timeout=90.0).json("POST", f"/v2/endpoint/{ns}", json=body, what="create endpoint")
        dep = Deployment(
            id=name, provider=self.id, model=model, engine=engine, status="pending", gpu=gpu,
            served_model_name=opts.served_model_name or model, endpoint_url=None, name=name, opts=opts,
            auth_env="HF_TOKEN", message="endpoint requested", raw={"namespace": ns, "image": image_url},
        )
        return self._apply(dep, data)

    # ------------------------------------------------------------------

    def _apply(self, dep: Deployment, data: dict[str, Any]) -> Deployment:
        status = data.get("status") if isinstance(data.get("status"), dict) else {}
        state = str(status.get("state") or "").lower()
        dep.status = _STATUS.get(state, "unknown" if state else dep.status)
        url = status.get("url")
        if url:
            dep.endpoint_url = str(url).rstrip("/") + "/v1"
        msg = status.get("message") or status.get("errorMessage") or ""
        dep.message = str(msg) if msg else f"state={state or '?'}"
        dep.raw = {**dep.raw, "endpoint": mask_secrets(data)}
        dep.updated_at = utcnow()
        return dep

    async def status(self, dep: Deployment) -> Deployment:
        ns = str(dep.raw.get("namespace") or await self.namespace())
        resp = await self._http().request("GET", f"/v2/endpoint/{ns}/{dep.id}", raise_on_error=False, what="status")
        if resp.status_code == 404:
            dep.status = "deleted"
            dep.message = "endpoint no longer exists"
            dep.updated_at = utcnow()
            return dep
        raise_for(resp, provider=self.id, auth_env="HF_TOKEN", console_url=CONSOLE_URL, what="status")
        return self._apply(dep, resp.json())

    async def wait_ready(self, dep: Deployment, timeout_s: int = 1200) -> Deployment:
        async def check_state() -> tuple[bool, Any]:
            cur = await self.status(dep)
            if cur.status in ("running", "scaled_to_zero"):
                return True, cur
            if cur.status in ("failed", "deleted"):
                raise DeployError(f"hf endpoint {dep.id} {cur.status}: {cur.message}",
                                  hint=f"see {CONSOLE_URL}/{dep.id}", provider=self.id)
            return False, cur

        await poll_until(check_state, timeout_s=timeout_s, interval_s=10.0,
                         what=f"hf endpoint {dep.id} to initialise", provider=self.id)
        if not dep.endpoint_url:
            raise DeployError(f"hf endpoint {dep.id} is running but reported no URL", provider=self.id)
        http = self._http()

        async def check_models() -> tuple[bool, Any]:
            resp = await http.request(
                "GET", f"{dep.endpoint_url}/models", headers={"X-Scale-Up-Timeout": "600"},
                raise_on_error=False, retry_statuses=(), timeout=620.0,
            )
            if resp.status_code == 200:
                return True, resp
            if resp.status_code in (401, 403):
                raise_for(resp, provider=self.id, auth_env="HF_TOKEN", console_url=CONSOLE_URL)
            return False, resp  # 503 = warming

        await poll_until(check_models, timeout_s=timeout_s, interval_s=10.0,
                         what=f"hf endpoint {dep.id} to answer /v1/models", provider=self.id)
        dep.status = "running"
        dep.message = "endpoint answered /v1/models"
        dep.updated_at = utcnow()
        return dep

    async def logs(self, dep: Deployment, tail: int = 200) -> AsyncIterator[str]:
        ns = str(dep.raw.get("namespace") or await self.namespace())
        data = await self._http().json(
            "GET", f"/v3/endpoint/{ns}/{dep.id}/logs",
            params={"limit": max(1, min(tail, 5000)), "order": "asc"}, what="logs",
        )
        entries = _as_list(data, "logs") or _as_list(data, "items")
        for e in entries[-tail:]:
            if isinstance(e, dict):
                ts = e.get("timestamp") or e.get("time") or ""
                msg = e.get("message") or e.get("line") or e.get("log") or ""
                yield f"{ts} {msg}".strip()
            else:
                yield str(e)

    async def delete(self, dep: Deployment) -> None:
        ns = str(dep.raw.get("namespace") or await self.namespace())
        resp = await self._http().request("DELETE", f"/v2/endpoint/{ns}/{dep.id}", raise_on_error=False, what="delete")
        if resp.status_code in (200, 202, 204, 404):
            return
        raise_for(resp, provider=self.id, auth_env="HF_TOKEN", console_url=CONSOLE_URL, what="delete")

    async def list_deployments(self) -> list[Deployment]:
        ns = await self.namespace()
        data = await self._http().json("GET", f"/v2/endpoint/{ns}", what="list")
        out: list[Deployment] = []
        for ep in _as_list(data, "items"):
            model = ep.get("model") if isinstance(ep.get("model"), dict) else {}
            image = model.get("image") if isinstance(model.get("image"), dict) else {}
            engine: Engine = "vllm"
            for eng, (key, _) in ENGINE_IMAGES.items():
                if key in image:
                    engine = eng  # type: ignore[assignment]
                    break
            compute = ep.get("compute") if isinstance(ep.get("compute"), dict) else {}
            prov = ep.get("provider") if isinstance(ep.get("provider"), dict) else {}
            itype = str(compute.get("instanceType") or "")
            isize = str(compute.get("instanceSize") or "x1")
            scaling = compute.get("scaling") if isinstance(compute.get("scaling"), dict) else {}
            vram = _VRAM_BY_TYPE.get(itype, 0)
            gpu = GpuSpec(
                provider_id=_gpu_id(str(prov.get("vendor") or ""), str(prov.get("region") or ""), itype, isize),
                family=gpu_family_from_name(itype, vram), vram_gb=vram, count=_size_count(isize),  # type: ignore[arg-type]
                price_per_hour=_static_price(itype, isize), region=str(prov.get("region") or "") or None,
                label=f"{itype} {isize}",
            )
            name = str(ep.get("name") or "")
            dep = Deployment(
                id=name, provider=self.id, model=str(model.get("repository") or ""), engine=engine,
                status="unknown", gpu=gpu, served_model_name=str(model.get("repository") or ""),
                name=name, auth_env="HF_TOKEN",
                opts=DeployOpts(min_replicas=int(scaling.get("minReplica") or 0),
                                max_replicas=int(scaling.get("maxReplica") or 1),
                                idle_timeout_s=int(scaling.get("scaleToZeroTimeout") or 15) * 60),
                raw={"namespace": ns},
            )
            out.append(self._apply(dep, ep))
        return out

    async def cost(self, dep: Deployment) -> CostEstimate:
        price = dep.gpu.price_per_hour
        per_hour = price * max(1, dep.opts.max_replicas) if price else None
        idle = price * dep.opts.min_replicas if (price and dep.opts.min_replicas > 0) else 0.0
        return CostEstimate(
            per_hour_usd=round(per_hour, 3) if per_hour else None, idle_per_hour_usd=round(idle, 3),
            basis="list price × max replicas; $0 while scaled to zero or paused (scaled-to-zero still holds quota)",
        )


def _as_list(data: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        v = data.get(key)
        if isinstance(v, list):
            return [d for d in v if isinstance(d, dict)]
    return []


def _size_count(isize: str) -> int:
    try:
        return max(1, int(str(isize).lstrip("x")))
    except ValueError:
        return 1


def _static_price(itype: str, isize: str) -> float | None:
    for _v, _r, t, s, _c, _vr, price in STATIC_HARDWARE:
        if t == itype and s == isize:
            return price
    return None
