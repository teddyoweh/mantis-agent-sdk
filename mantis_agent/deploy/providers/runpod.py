"""RunPod Serverless adapter — REST v2 only.

The reference "any HF model → OpenAI URL" backend: one ``POST /serverless``
with the official vLLM worker image and ``MODEL_NAME`` in the env, then the
endpoint answers OpenAI calls at ``https://api.runpod.ai/v2/{id}/openai/v1``
with the same API key. v1 REST + GraphQL (and the ``runpod`` SDK's control
plane) retire in November 2026, so nothing here touches them.

Shapes (docs.runpod.io/api-reference-v2, verified September 2026):

* ``POST /v2/serverless`` → ``{id, name, ...}``; body ``{name, type:"QUEUE",
  image, gpu:{pools:[...], count}, workers:{min,max,idleTimeout},
  scaling:{type:"REQUEST_COUNT", requestCount}, flashboot, timeout, env}``.
* ``GET /v2/serverless/{id}``, ``DELETE /v2/serverless/{id}``,
  ``GET /v2/serverless`` (list), ``GET /v2/serverless/{id}/workers`` and
  ``.../workers/{wid}/logs?source=container&tail=N`` (SSE).
* ``GET /v2/catalog/gpus?include=AVAILABILITY&product=SERVERLESS`` →
  ``id, name, memory, price.{serverless,...}, availability``.
* ``GET https://api.runpod.ai/v2/{id}/health`` → ``{jobs:{...}, workers:{idle, running}}``.

GPU *pools* are what an endpoint binds to (``AMPERE_80`` = A100 80 GB,
``ADA_80_PRO`` = H100, ...). ``list_gpus`` returns the pools with the
serverless flex price of the cheapest card in each pool, overlaid on a
static table so the list still renders when the catalogue call fails.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from .._http import DeployHttp, mask_secrets, poll_until, require_env, slugify
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

__all__ = ["RunPodProvider", "CONTROL_BASE", "INFERENCE_BASE", "VLLM_IMAGE", "POOLS"]

CONTROL_BASE = "https://api.runpod.io/v2"
INFERENCE_BASE = "https://api.runpod.ai/v2"
CONSOLE_URL = "https://console.runpod.io/serverless"

#: The official vLLM worker (github.com/runpod-workers/worker-vllm). The
#: ``stable-cuda12.1.0`` tag tracks the latest release; pin a specific
#: ``vX.Y.Z...`` tag with ``DeployOpts.engine_version`` or
#: ``MANTIS_RUNPOD_VLLM_IMAGE`` when reproducibility matters.
VLLM_IMAGE = "runpod/worker-v1-vllm:stable-cuda12.1.0"

#: pool id → (family, vram GB, static flex $/h from docs.runpod.io/serverless/pricing, label)
POOLS: dict[str, tuple[str, int, float, str]] = {
    "AMPERE_16": ("other", 16, 0.69, "A4000 / RTX 3080 16 GB"),
    "AMPERE_24": ("other", 24, 0.69, "A5000 / RTX 3090 24 GB"),
    "ADA_24": ("L4", 24, 0.69, "L4 / RTX 4090 24 GB"),
    "ADA_32_PRO": ("other", 32, 0.90, "RTX 5090 32 GB"),
    "AMPERE_48": ("other", 48, 1.22, "A40 / A6000 48 GB"),
    "ADA_48_PRO": ("L40S", 48, 1.91, "L40S 48 GB"),
    "AMPERE_80": ("A100-80", 80, 2.72, "A100 80 GB"),
    "ADA_80_PRO": ("H100", 80, 4.18, "H100 80 GB"),
    "BLACKWELL_96": ("other", 96, 4.50, "RTX PRO 6000 96 GB"),
    "HOPPER_141": ("H200", 141, 5.58, "H200 141 GB"),
    "BLACKWELL_180": ("B200", 180, 8.64, "B200 180 GB"),
}

# GPU catalogue name fragment → pool, for overlaying live prices.
_NAME_TO_POOL: tuple[tuple[str, str], ...] = (
    ("B200", "BLACKWELL_180"), ("H200", "HOPPER_141"), ("H100", "ADA_80_PRO"),
    ("A100", "AMPERE_80"), ("PRO 6000", "BLACKWELL_96"), ("L40S", "ADA_48_PRO"),
    ("A40", "AMPERE_48"), ("A6000", "AMPERE_48"), ("5090", "ADA_32_PRO"),
    ("4090", "ADA_24"), ("L4", "ADA_24"), ("A5000", "AMPERE_24"), ("3090", "AMPERE_24"),
    ("A4000", "AMPERE_16"), ("3080", "AMPERE_16"),
)


def _pool_for(name: str) -> str | None:
    n = (name or "").upper()
    for frag, pool in _NAME_TO_POOL:
        if frag in n:
            return pool
    return None


@register_provider
class RunPodProvider:
    id = "runpod"
    display_name = "RunPod Serverless"
    credential_fields = (
        CredentialField("RUNPOD_API_KEY", "RunPod API key",
                        help="console.runpod.io → Settings → API Keys (read/write)"),
    )
    engines: tuple[Engine, ...] = ("vllm",)
    console_url = CONSOLE_URL
    scale_to_zero = True
    public_by_default = False  # reachable by anyone *with the API key*; the key is the auth

    # ------------------------------------------------------------------

    def configured(self) -> bool:
        return require_env("RUNPOD_API_KEY")

    def _http(self, base: str = CONTROL_BASE, **kw: Any) -> DeployHttp:
        key = (os.environ.get("RUNPOD_API_KEY") or "").strip()
        return DeployHttp(
            provider=self.id, base_url=base, auth_env="RUNPOD_API_KEY", console_url=CONSOLE_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, **kw,
        )

    def _require(self) -> None:
        if not self.configured():
            raise DeployError(
                "RUNPOD_API_KEY is not set",
                hint="create a key at console.runpod.io → Settings → API Keys, then `mantis-agent deploy creds runpod --set RUNPOD_API_KEY=...`",
                provider=self.id,
            )

    async def validate_credentials(self) -> Account:
        self._require()
        data = await self._http().json("GET", "/serverless", what="validate credentials")
        n = len(_items(data))
        return Account(ok=True, provider=self.id, message=f"{n} serverless endpoint(s) visible")

    # ------------------------------------------------------------------

    async def list_gpus(self) -> list[GpuSpec]:
        self._require()
        live: dict[str, tuple[float, bool | None]] = {}
        try:
            data = await self._http().json(
                "GET", "/catalog/gpus", params={"include": "AVAILABILITY", "product": "SERVERLESS"},
                what="gpu catalogue",
            )
            for row in _items(data, "gpus"):
                pool = _pool_for(str(row.get("name") or row.get("id") or "")) or (
                    str(row.get("id")) if str(row.get("id")) in POOLS else None)
                if not pool:
                    continue
                price = row.get("price") if isinstance(row.get("price"), dict) else {}
                p = price.get("serverless") or price.get("flex") or row.get("serverlessPrice")
                try:
                    p = float(p) if p is not None else None
                except (TypeError, ValueError):
                    p = None
                avail = row.get("availability")
                available: bool | None
                if isinstance(avail, bool):
                    available = avail
                elif isinstance(avail, str):
                    available = avail.upper() not in ("UNAVAILABLE", "NONE", "NOT_AVAILABLE")
                elif isinstance(avail, dict):
                    available = bool(avail.get("available", True))
                else:
                    available = None
                # Prices from the catalogue are $/h for one GPU-second-billed worker.
                if p is not None and p < 0.01:  # per-second figure → per hour
                    p = p * 3600
                prev = live.get(pool)
                if prev is None or (p is not None and (prev[0] is None or p < prev[0])):
                    live[pool] = (p, available if prev is None else (prev[1] or available))
        except DeployError:
            live = {}
        out: list[GpuSpec] = []
        for pool, (family, vram, static_price, label) in POOLS.items():
            price, available = live.get(pool, (None, None))
            out.append(GpuSpec(
                provider_id=pool, family=family, vram_gb=vram, count=1,  # type: ignore[arg-type]
                price_per_hour=round(price if price is not None else static_price, 3),
                available=available, region=None, label=f"{label} [{pool}]",
            ))
        out.sort(key=lambda g: (g.price_per_hour or 0, g.vram_gb))
        return out

    # ------------------------------------------------------------------

    def _endpoint_url(self, endpoint_id: str) -> str:
        return f"{INFERENCE_BASE}/{endpoint_id}/openai/v1"

    async def deploy(self, model: str, gpu: GpuSpec, engine: Engine, opts: DeployOpts) -> Deployment:
        self._require()
        if engine not in self.engines:
            raise DeployError(f"runpod adapter serves {', '.join(self.engines)} only (got {engine})",
                              hint="use --engine vllm", provider=self.id)
        name = opts.name or f"mantis-{slugify(model)}"
        image = opts.engine_version or os.environ.get("MANTIS_RUNPOD_VLLM_IMAGE") or VLLM_IMAGE
        if ":" not in image:
            image = f"runpod/worker-v1-vllm:{image}"
        tp = opts.tensor_parallel or gpu.count or 1
        env: dict[str, str] = {
            "MODEL_NAME": model,
            "TENSOR_PARALLEL_SIZE": str(tp),
            "RAW_OPENAI_OUTPUT": "1",
        }
        if opts.max_model_len:
            env["MAX_MODEL_LEN"] = str(opts.max_model_len)
        hf_token = opts.hf_token or os.environ.get("HF_TOKEN")
        if hf_token:
            env["HF_TOKEN"] = hf_token
        if opts.trust_remote_code:
            env["TRUST_REMOTE_CODE"] = "1"
        if opts.quantization:
            env["QUANTIZATION"] = opts.quantization
        if opts.served_model_name:
            env["OPENAI_SERVED_MODEL_NAME_OVERRIDE"] = opts.served_model_name
        for k, v in (opts.extra.get("env") or {}).items() if isinstance(opts.extra.get("env"), dict) else ():
            env[str(k)] = str(v)
        body: dict[str, Any] = {
            "name": name,
            "type": "QUEUE",
            "image": image,
            "gpu": {"pools": [gpu.provider_id], "count": max(1, gpu.count)},
            "workers": {
                "min": max(0, opts.min_replicas),
                "max": max(1, opts.max_replicas),
                "idleTimeout": max(1, min(3600, opts.idle_timeout_s)),
            },
            "scaling": {"type": "REQUEST_COUNT", "requestCount": int(opts.extra.get("request_count", 4))},
            "flashboot": "FLASHBOOT" if opts.extra.get("flashboot", True) else "NONE",
            "timeout": int(opts.request_timeout_s) * 1000,
            "env": env,
        }
        if opts.extra.get("network_volume_id"):
            body["networkVolumeId"] = str(opts.extra["network_volume_id"])
            env["BASE_PATH"] = "/runpod-volume"
        if opts.region:
            body["dataCenterIds"] = [opts.region]
        data = await self._http(timeout=90.0).json("POST", "/serverless", json=body, what="create endpoint")
        ep_id = str(data.get("id") or "")
        if not ep_id:
            raise DeployError(f"runpod: create returned no endpoint id: {data!r}", provider=self.id)
        return Deployment(
            id=ep_id, provider=self.id, model=model, engine=engine,
            status="starting" if opts.min_replicas > 0 else "scaled_to_zero",
            gpu=gpu, served_model_name=opts.served_model_name or model,
            endpoint_url=self._endpoint_url(ep_id), name=name, opts=opts,
            auth_env="RUNPOD_API_KEY", message="endpoint created; first request boots a worker",
            raw={"endpoint": mask_secrets(data), "image": image},
        )

    # ------------------------------------------------------------------

    async def _health(self, endpoint_id: str) -> dict[str, Any] | None:
        resp = await self._http(INFERENCE_BASE).request(
            "GET", f"/{endpoint_id}/health", raise_on_error=False, retry_statuses=(429,), timeout=20.0,
        )
        if resp.status_code != 200:
            return None
        try:
            body = resp.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    async def status(self, dep: Deployment) -> Deployment:
        self._require()
        resp = await self._http().request("GET", f"/serverless/{dep.id}", raise_on_error=False, what="status")
        if resp.status_code == 404:
            dep.status = "deleted"
            dep.message = "endpoint no longer exists on RunPod"
            dep.updated_at = utcnow()
            return dep
        from .._http import raise_for  # noqa: PLC0415

        raise_for(resp, provider=self.id, auth_env="RUNPOD_API_KEY", console_url=CONSOLE_URL, what="status")
        ep = resp.json() if resp.content else {}
        health = await self._health(dep.id)
        workers = (health or {}).get("workers") or {}
        running = int(workers.get("running") or 0)
        idle = int(workers.get("idle") or 0)
        initializing = int(workers.get("initializing") or 0) + int(workers.get("ready") or 0)
        wmin = int(((ep.get("workers") or {}).get("min")) or dep.opts.min_replicas or 0)
        st: DeploymentStatus
        if running or idle:
            st = "running"
        elif initializing:
            st = "starting"
        elif wmin == 0:
            st = "scaled_to_zero"
        elif health is None:
            st = "unknown"
        else:
            st = "starting"
        dep.status = st
        dep.endpoint_url = dep.endpoint_url or self._endpoint_url(dep.id)
        dep.message = f"workers running={running} idle={idle}"
        dep.raw = {**dep.raw, "endpoint": mask_secrets(ep), "health": health or {}}
        dep.updated_at = utcnow()
        return dep

    async def wait_ready(self, dep: Deployment, timeout_s: int = 1200) -> Deployment:
        """A queue endpoint only boots a worker on demand, so readiness is
        proven by asking ``/openai/v1/models`` and waiting for the first
        worker (503 / 5xx / timeouts while it downloads and loads)."""

        self._require()
        url = dep.endpoint_url or self._endpoint_url(dep.id)
        http = self._http(INFERENCE_BASE)

        async def check() -> tuple[bool, Any]:
            resp = await http.request("GET", f"{url}/models", raise_on_error=False,
                                      retry_statuses=(), timeout=120.0)
            if resp.status_code == 200:
                return True, resp
            if resp.status_code in (401, 403):
                from .._http import raise_for  # noqa: PLC0415

                raise_for(resp, provider=self.id, auth_env="RUNPOD_API_KEY", console_url=CONSOLE_URL)
            if resp.status_code == 404:
                raise DeployError(f"runpod endpoint {dep.id} not found",
                                  hint="it may have been deleted from the console", provider=self.id)
            return False, resp

        await poll_until(check, timeout_s=timeout_s, interval_s=10.0,
                         what=f"runpod endpoint {dep.id} to boot a worker", provider=self.id)
        dep.status = "running"
        dep.endpoint_url = url
        dep.message = "worker answered /models"
        dep.updated_at = utcnow()
        return dep

    async def logs(self, dep: Deployment, tail: int = 200) -> AsyncIterator[str]:
        self._require()
        http = self._http()
        data = await http.json("GET", f"/serverless/{dep.id}/workers", what="workers")
        workers = _items(data, "workers")
        if not workers:
            yield "(no workers — the endpoint is scaled to zero; send a request to boot one)"
            return
        per = max(10, tail // max(1, len(workers)))
        for w in workers:
            wid = w.get("id") or w.get("workerId")
            if not wid:
                continue
            yield f"--- worker {wid} ({w.get('status') or w.get('state') or '?'}) ---"
            try:
                async for line in http.stream_lines(
                    "GET", f"/serverless/{dep.id}/workers/{wid}/logs",
                    params={"source": "container", "tail": per}, timeout=30.0, limit=per,
                ):
                    yield line
            except DeployError as e:
                yield f"(logs unavailable for worker {wid}: {e})"

    async def delete(self, dep: Deployment) -> None:
        self._require()
        resp = await self._http().request("DELETE", f"/serverless/{dep.id}", raise_on_error=False, what="delete")
        if resp.status_code in (404, 204, 200):
            return
        from .._http import raise_for  # noqa: PLC0415

        raise_for(resp, provider=self.id, auth_env="RUNPOD_API_KEY", console_url=CONSOLE_URL, what="delete")

    async def list_deployments(self) -> list[Deployment]:
        self._require()
        data = await self._http().json("GET", "/serverless", what="list")
        out: list[Deployment] = []
        for ep in _items(data):
            env = ep.get("env") if isinstance(ep.get("env"), dict) else {}
            model = str(env.get("MODEL_NAME") or ep.get("name") or "")
            image = str(ep.get("image") or ep.get("imageName") or "")
            if "vllm" not in image.lower() and not model:
                continue
            gpu_cfg = ep.get("gpu") if isinstance(ep.get("gpu"), dict) else {}
            pools = gpu_cfg.get("pools") or ep.get("gpuTypeIds") or []
            pool = str(pools[0]) if pools else "unknown"
            fam, vram, price, label = POOLS.get(pool, ("other", 0, None, pool))
            workers = ep.get("workers") if isinstance(ep.get("workers"), dict) else {}
            ep_id = str(ep.get("id"))
            out.append(Deployment(
                id=ep_id, provider=self.id, model=model, engine="vllm",
                status="scaled_to_zero" if int(workers.get("min") or 0) == 0 else "unknown",
                gpu=GpuSpec(provider_id=pool, family=fam, vram_gb=vram,  # type: ignore[arg-type]
                            count=int(gpu_cfg.get("count") or 1), price_per_hour=price, label=label),
                served_model_name=str(env.get("OPENAI_SERVED_MODEL_NAME_OVERRIDE") or model),
                endpoint_url=self._endpoint_url(ep_id), name=str(ep.get("name") or ""),
                opts=DeployOpts(min_replicas=int(workers.get("min") or 0),
                                max_replicas=int(workers.get("max") or 1),
                                idle_timeout_s=int(workers.get("idleTimeout") or 300)),
                auth_env="RUNPOD_API_KEY", raw={"endpoint": mask_secrets(ep)},
            ))
        return out

    async def cost(self, dep: Deployment) -> CostEstimate:
        price = dep.gpu.price_per_hour
        if price is None:
            price = POOLS.get(dep.gpu.provider_id, ("", 0, 0.0, ""))[2] or None
        per_hour = (price * max(1, dep.opts.max_replicas)) if price else None
        idle = (price * dep.opts.min_replicas) if (price and dep.opts.min_replicas > 0) else 0.0
        return CostEstimate(
            per_hour_usd=round(per_hour, 3) if per_hour else None, idle_per_hour_usd=round(idle, 3),
            basis=f"serverless flex list price × max workers ({dep.opts.max_replicas}); idle workers bill until idleTimeout={dep.opts.idle_timeout_s}s",
        )


def _items(data: Any, key: str | None = None) -> list[dict[str, Any]]:
    """RunPod v2 list replies are either a bare list or ``{<key>: [...]}``."""

    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for k in ((key,) if key else ()) + ("items", "data", "endpoints", "gpus", "workers", "results"):
            v = data.get(k) if k else None
            if isinstance(v, list):
                return [d for d in v if isinstance(d, dict)]
    return []
