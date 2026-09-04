"""Baseten adapter — the most complete REST control plane of the set.

Deploy is the documented pure-REST recipe (docs.baseten.co/examples/
create-a-model-with-rest, deploy-a-hugging-face-model):

1. ``POST /v1/secrets {name: hf_access_token, value}`` for gated repos;
2. ``POST /v1/prepare_model_upload`` → a presigned URL we ``PUT`` a tiny
   tar.gz to (one ``config.yaml``: upstream ``vllm/vllm-openai`` base image,
   ``weights: hf://…`` so Baseten mirrors the checkpoint once, a
   ``docker_server`` block running ``vllm serve``);
3. ``POST /v1/models {model_name, source:{kind: model_archive, ...},
   raw_config}`` → ``{model_id, model_deployment_id}``;
4. poll ``GET /v1/models/{id}/deployments/{dep}`` until ``ACTIVE``.

Inference: ``https://model-{model_id}.api.baseten.co/environments/production/sync/v1``
(any vLLM route under ``/sync/``), always ``Authorization: Bearer <key>``.
Both ids are kept in ``raw`` (``model_id`` / ``deployment_id``);
``Deployment.id`` is the model id because that is what the URL and every
management route key on. Delete = deactivate the deployment, then delete
the model. Prices from ``GET /v1/instance_type_prices`` (USD/minute) with
a static fallback from baseten.co/pricing.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from collections.abc import AsyncIterator
from typing import Any

import httpx

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

__all__ = ["BasetenProvider", "API_BASE", "STATIC_INSTANCES", "build_config", "build_archive"]

API_BASE = "https://api.baseten.co/v1"
CONSOLE_URL = "https://app.baseten.co/models"
HF_SECRET_NAME = "hf_access_token"

#: engine → (base image, serve command template)
ENGINE_IMAGES: dict[str, str] = {
    "vllm": "vllm/vllm-openai:v0.23.0",
    "sglang": "lmsysorg/sglang:latest",
}

#: accelerator → (family, vram GB, count, $/h) — baseten.co/pricing Sept 2026
STATIC_INSTANCES: dict[str, tuple[str, int, int, float]] = {
    "T4": ("T4", 16, 1, 0.631),
    "L4": ("L4", 24, 1, 0.848),
    "A10G": ("A10G", 24, 1, 1.207),
    "H100MIG": ("other", 40, 1, 3.75),
    "A100": ("A100-80", 80, 1, 4.00),
    "H100": ("H100", 80, 1, 6.50),
    "H100:2": ("H100", 80, 2, 13.00),
    "H100:4": ("H100", 80, 4, 26.00),
    "H200": ("H200", 141, 1, 8.00),
    "B200": ("B200", 180, 1, 9.98),
}

_STATUS: dict[str, DeploymentStatus] = {
    "BUILDING": "building", "DEPLOYING": "starting", "LOADING_MODEL": "starting",
    "WAKING_UP": "starting", "UPDATING": "starting", "MIGRATING": "starting",
    "ACTIVE": "running", "UNHEALTHY": "failed", "BUILD_FAILED": "failed",
    "BUILD_STOPPED": "failed", "FAILED": "failed", "DEACTIVATING": "deleting",
    "INACTIVE": "paused", "SCALED_TO_ZERO": "scaled_to_zero", "DELETING": "deleting",
}


def _endpoint_url(model_id: str) -> str:
    return f"https://model-{model_id}.api.baseten.co/environments/production/sync/v1"


def build_config(model: str, gpu: GpuSpec, engine: Engine, opts: DeployOpts, *, name: str,
                 use_hf_secret: bool) -> dict[str, Any]:
    """The Truss ``config.yaml`` as a dict (also sent as ``raw_config``)."""

    accel = gpu.provider_id
    if gpu.count > 1 and ":" not in accel:
        accel = f"{accel}:{gpu.count}"
    tp = opts.tensor_parallel or gpu.count or 1
    served = opts.served_model_name or model
    mount = "/app/checkpoint"
    if engine == "vllm":
        cmd = [
            "vllm", "serve", mount, "--served-model-name", served,
            "--tensor-parallel-size", str(tp), "--host", "0.0.0.0", "--port", "8000",
            "--enable-prefix-caching",
        ]
        if opts.max_model_len:
            cmd += ["--max-model-len", str(opts.max_model_len)]
        if opts.quantization:
            cmd += ["--quantization", opts.quantization]
        if opts.trust_remote_code:
            cmd.append("--trust-remote-code")
    else:  # sglang
        cmd = [
            "python3", "-m", "sglang.launch_server", "--model-path", mount,
            "--served-model-name", served, "--tp", str(tp), "--host", "0.0.0.0", "--port", "8000",
        ]
        if opts.max_model_len:
            cmd += ["--context-length", str(opts.max_model_len)]
        if opts.trust_remote_code:
            cmd.append("--trust-remote-code")
    cmd += list(opts.extra_engine_args)
    image = opts.engine_version or ENGINE_IMAGES[engine]
    if ":" not in image and "/" not in image:
        image = f"{ENGINE_IMAGES[engine].split(':', 1)[0]}:{image}"
    weights: dict[str, Any] = {
        "source": f"hf://{model}@{opts.extra.get('revision') or 'main'}",
        "mount_location": mount,
    }
    if use_hf_secret:
        weights["auth_secret_name"] = HF_SECRET_NAME
    cfg: dict[str, Any] = {
        "model_name": name,
        "model_metadata": {"tags": ["openai-compatible", "mantis"]},
        "base_image": {"image": image},
        "weights": [weights],
        "docker_server": {
            "start_command": " ".join(_shq(c) for c in cmd),
            "readiness_endpoint": "/health",
            "liveness_endpoint": "/health",
            "predict_endpoint": "/v1/chat/completions",
            "server_port": 8000,
        },
        "resources": {"accelerator": accel, "use_gpu": True},
        "runtime": {
            "predict_concurrency": int(opts.extra.get("predict_concurrency", 32)),
            "health_checks": {"startup_threshold_seconds": 1800, "restart_threshold_seconds": 300},
        },
        "model_cache": [],
        "environment_variables": {str(k): str(v) for k, v in (opts.extra.get("env") or {}).items()},
    }
    if use_hf_secret:
        cfg["secrets"] = {HF_SECRET_NAME: None}
    return cfg


def _shq(s: str) -> str:
    import shlex  # noqa: PLC0415

    return shlex.quote(s)


def _yaml(obj: Any, indent: int = 0) -> str:
    """Just enough YAML for ``config.yaml`` (no pyyaml at runtime)."""

    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        lines = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(_yaml(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {_yaml(v, indent + 1)}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return "[]"
        lines = []
        for v in obj:
            if isinstance(v, dict) and v:
                inner = _yaml(v, indent + 1).split("\n")
                lines.append(f"{pad}- {inner[0].lstrip()}")
                lines.extend(inner[1:])
            else:
                lines.append(f"{pad}- {_yaml(v, indent + 1)}")
        return "\n".join(lines)
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, (int, float)):
        return str(obj)
    return json.dumps(str(obj))


def build_archive(config: dict[str, Any]) -> bytes:
    """A tar.gz holding ``config.yaml`` (+ a stub ``model/`` dir Truss expects)."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = (_yaml(config) + "\n").encode("utf-8")
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        d = tarfile.TarInfo("model")
        d.type = tarfile.DIRTYPE
        d.mode = 0o755
        tar.addfile(d)
    return buf.getvalue()


@register_provider
class BasetenProvider:
    id = "baseten"
    display_name = "Baseten"
    credential_fields = (
        CredentialField("BASETEN_API_KEY", "Baseten API key", help="app.baseten.co → Settings → API keys"),
    )
    engines: tuple[Engine, ...] = ("vllm", "sglang")
    console_url = CONSOLE_URL
    scale_to_zero = True
    public_by_default = False

    # ------------------------------------------------------------------

    def configured(self) -> bool:
        return require_env("BASETEN_API_KEY")

    def _key(self) -> str:
        key = (os.environ.get("BASETEN_API_KEY") or "").strip()
        if not key:
            raise DeployError(
                "BASETEN_API_KEY is not set",
                hint="create a key at app.baseten.co → Settings → API keys, then `mantis-agent deploy creds baseten --set BASETEN_API_KEY=...`",
                provider=self.id,
            )
        return key

    def _http(self, base: str = API_BASE, **kw: Any) -> DeployHttp:
        return DeployHttp(
            provider=self.id, base_url=base, auth_env="BASETEN_API_KEY", console_url=CONSOLE_URL,
            headers={"Authorization": f"Bearer {self._key()}", "Content-Type": "application/json"}, **kw,
        )

    async def validate_credentials(self) -> Account:
        data = await self._http().json("GET", "/models", what="validate credentials")
        return Account(ok=True, provider=self.id, message=f"{len(_items(data, 'models'))} model(s) in the workspace")

    async def list_gpus(self) -> list[GpuSpec]:
        out: list[GpuSpec] = []
        try:
            data = await self._http().json("GET", "/instance_type_prices", what="instance prices")
            for row in _items(data, "instance_type_prices"):
                gpu_type = str(row.get("gpu_type") or row.get("accelerator") or "")
                if not gpu_type or gpu_type.upper() in ("NONE", "CPU"):
                    continue
                count = int(row.get("gpu_count") or 1)
                mib = row.get("gpu_memory_limit_mib") or row.get("gpu_memory_mib")
                try:
                    vram = int(round(float(mib) / 1024)) if mib else 0
                except (TypeError, ValueError):
                    vram = 0
                per_min = row.get("price_per_minute") or row.get("usd_per_minute") or row.get("price")
                try:
                    price = round(float(per_min) * 60, 3) if per_min is not None else None
                except (TypeError, ValueError):
                    price = None
                accel = gpu_type if count == 1 else f"{gpu_type}:{count}"
                fam, svram = _static_family(gpu_type, vram)
                out.append(GpuSpec(
                    provider_id=accel, family=fam, vram_gb=vram or svram, count=count,  # type: ignore[arg-type]
                    price_per_hour=price, available=None,
                    label=f"{accel} ({row.get('name') or row.get('instance_type') or ''})".replace(" ()", ""),
                ))
        except DeployError:
            out = []
        if not out:
            for accel, (fam, vram, count, price) in STATIC_INSTANCES.items():
                out.append(GpuSpec(provider_id=accel, family=fam, vram_gb=vram, count=count,  # type: ignore[arg-type]
                                   price_per_hour=price, available=None, label=accel))
        out.sort(key=lambda g: (g.price_per_hour if g.price_per_hour is not None else 1e9, g.total_vram_gb))
        return out

    # ------------------------------------------------------------------

    async def _ensure_secret(self, http: DeployHttp, value: str) -> None:
        resp = await http.request("POST", "/secrets", json={"name": HF_SECRET_NAME, "value": value},
                                  raise_on_error=False, what="hf secret")
        if resp.status_code < 400 or resp.status_code == 409:
            return
        raise_for(resp, provider=self.id, auth_env="BASETEN_API_KEY", console_url=CONSOLE_URL, what="hf secret")

    async def deploy(self, model: str, gpu: GpuSpec, engine: Engine, opts: DeployOpts) -> Deployment:
        if engine not in ENGINE_IMAGES:
            raise DeployError(f"baseten adapter serves {', '.join(ENGINE_IMAGES)} (got {engine})",
                              hint="use --engine vllm", provider=self.id)
        http = self._http(timeout=120.0)
        name = opts.name or f"mantis-{slugify(model, max_len=30)}"
        hf_token = opts.hf_token or os.environ.get("HF_TOKEN")
        if hf_token:
            await self._ensure_secret(http, hf_token)
        config = build_config(model, gpu, engine, opts, name=name, use_hf_secret=bool(hf_token))
        archive = build_archive(config)

        prep = await http.json("POST", "/prepare_model_upload", json={"model_name": name}, what="prepare upload")
        upload_url = prep.get("upload_url") or prep.get("url") or prep.get("presigned_url")
        if not upload_url:
            raise DeployError(f"baseten: prepare_model_upload returned no upload URL: {mask_secrets(prep)!r}",
                              provider=self.id)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
                up = await client.put(str(upload_url), content=archive,
                                      headers={"Content-Type": "application/gzip"})
        except httpx.HTTPError as e:
            raise DeployError(f"baseten: archive upload failed: {e}", provider=self.id) from e
        if up.status_code >= 400:
            raise DeployError(f"baseten: archive upload rejected [HTTP {up.status_code}]: {up.text[:200]}",
                              provider=self.id)
        source: dict[str, Any] = {"kind": "model_archive"}
        for k in ("model_archive_id", "id", "s3_key", "key", "archive_id"):
            if prep.get(k):
                source[k if k != "id" else "model_archive_id"] = prep[k]
        body: dict[str, Any] = {
            "model_name": name,
            "source": source,
            "raw_config": config,
            "publish": True,
            "promote": True,
        }
        if opts.extra.get("environment"):
            body["environment"] = str(opts.extra["environment"])
        data = await http.json("POST", "/models", json=body, what="create model")
        model_id = str(data.get("model_id") or data.get("id") or "")
        dep_id = str(data.get("model_deployment_id") or data.get("deployment_id") or "")
        if not model_id:
            raise DeployError(f"baseten: create returned no model id: {mask_secrets(data)!r}", provider=self.id)
        dep = Deployment(
            id=model_id, provider=self.id, model=model, engine=engine, status="building", gpu=gpu,
            served_model_name=opts.served_model_name or model, endpoint_url=_endpoint_url(model_id),
            name=name, opts=opts, auth_env="BASETEN_API_KEY", message="build submitted",
            raw={"model_id": model_id, "deployment_id": dep_id, "create": mask_secrets(data)},
        )
        await self._patch_autoscaling(http, dep)
        return dep

    async def _patch_autoscaling(self, http: DeployHttp, dep: Deployment) -> None:
        did = dep.raw.get("deployment_id")
        if not did:
            return
        body = {
            "min_replica": max(0, dep.opts.min_replicas),
            "max_replica": max(1, dep.opts.max_replicas),
            "scale_down_delay": max(30, dep.opts.idle_timeout_s),
        }
        resp = await http.request("PATCH", f"/models/{dep.id}/deployments/{did}/autoscaling_settings",
                                  json=body, raise_on_error=False, what="autoscaling")
        if resp.status_code >= 400:
            dep.message = f"build submitted (autoscaling not applied: HTTP {resp.status_code})"

    # ------------------------------------------------------------------

    async def _deployment_obj(self, dep: Deployment) -> dict[str, Any] | None:
        http = self._http()
        did = dep.raw.get("deployment_id")
        if did:
            resp = await http.request("GET", f"/models/{dep.id}/deployments/{did}", raise_on_error=False, what="status")
        else:
            resp = await http.request("GET", f"/models/{dep.id}/deployments/production", raise_on_error=False, what="status")
        if resp.status_code == 404:
            return None
        raise_for(resp, provider=self.id, auth_env="BASETEN_API_KEY", console_url=CONSOLE_URL, what="status")
        body = resp.json() if resp.content else {}
        return body if isinstance(body, dict) else {}

    def _apply(self, dep: Deployment, data: dict[str, Any]) -> Deployment:
        raw = str(data.get("status") or "").upper()
        dep.status = _STATUS.get(raw, "unknown") if raw else dep.status
        replicas = data.get("active_replica_count")
        dep.message = f"status={raw or '?'}" + (f", replicas={replicas}" if replicas is not None else "")
        if data.get("id") and not dep.raw.get("deployment_id"):
            dep.raw["deployment_id"] = str(data["id"])
        dep.endpoint_url = dep.endpoint_url or _endpoint_url(dep.id)
        dep.raw = {**dep.raw, "deployment": mask_secrets(data)}
        dep.updated_at = utcnow()
        return dep

    async def status(self, dep: Deployment) -> Deployment:
        data = await self._deployment_obj(dep)
        if data is None:
            dep.status = "deleted"
            dep.message = "model / deployment no longer exists"
            dep.updated_at = utcnow()
            return dep
        return self._apply(dep, data)

    async def wait_ready(self, dep: Deployment, timeout_s: int = 1200) -> Deployment:
        async def check_state() -> tuple[bool, Any]:
            cur = await self.status(dep)
            if cur.status in ("running", "scaled_to_zero"):
                return True, cur
            if cur.status in ("failed", "deleted"):
                raise DeployError(f"baseten deployment {dep.id} {cur.status}: {cur.message}",
                                  hint=f"build logs: {CONSOLE_URL}/{dep.id}", provider=self.id)
            return False, cur

        await poll_until(check_state, timeout_s=timeout_s, interval_s=15.0,
                         what=f"baseten model {dep.id} to become ACTIVE", provider=self.id)
        http = self._http()
        url = dep.endpoint_url or _endpoint_url(dep.id)

        async def check_models() -> tuple[bool, Any]:
            resp = await http.request("GET", f"{url}/models", raise_on_error=False, retry_statuses=(), timeout=120.0)
            if resp.status_code == 200:
                return True, resp
            if resp.status_code in (401, 403):
                raise_for(resp, provider=self.id, auth_env="BASETEN_API_KEY", console_url=CONSOLE_URL)
            return False, resp

        await poll_until(check_models, timeout_s=timeout_s, interval_s=10.0,
                         what=f"baseten model {dep.id} to answer /v1/models", provider=self.id)
        dep.status = "running"
        dep.endpoint_url = url
        dep.message = "endpoint answered /v1/models"
        dep.updated_at = utcnow()
        return dep

    async def logs(self, dep: Deployment, tail: int = 200) -> AsyncIterator[str]:
        did = dep.raw.get("deployment_id") or "production"
        data = await self._http().json(
            "GET", f"/models/{dep.id}/deployments/{did}/logs",
            params={"limit": max(1, min(tail, 1000))}, what="logs",
        )
        for e in _items(data, "logs")[-tail:]:
            ts = e.get("timestamp") or e.get("time") or ""
            lvl = e.get("level") or ""
            msg = e.get("message") or e.get("msg") or e.get("log") or ""
            yield " ".join(str(x) for x in (ts, lvl, msg) if x)

    async def delete(self, dep: Deployment) -> None:
        http = self._http()
        did = dep.raw.get("deployment_id")
        if did:
            await http.request("POST", f"/models/{dep.id}/deployments/{did}/deactivate",
                               raise_on_error=False, what="deactivate")
        resp = await http.request("DELETE", f"/models/{dep.id}", raise_on_error=False, what="delete")
        if resp.status_code in (200, 202, 204, 404):
            return
        raise_for(resp, provider=self.id, auth_env="BASETEN_API_KEY", console_url=CONSOLE_URL, what="delete")

    async def list_deployments(self) -> list[Deployment]:
        data = await self._http().json("GET", "/models", what="list")
        out: list[Deployment] = []
        for m in _items(data, "models"):
            model_id = str(m.get("id") or "")
            if not model_id:
                continue
            prod = m.get("production_deployment") if isinstance(m.get("production_deployment"), dict) else {}
            accel = str(prod.get("instance_type_name") or prod.get("accelerator") or "")
            fam, vram = _static_family(accel.split(":", 1)[0], 0)
            count = int(accel.split(":", 1)[1]) if ":" in accel and accel.split(":", 1)[1].isdigit() else 1
            dep = Deployment(
                id=model_id, provider=self.id, model=str(m.get("name") or ""), engine="vllm",
                status="unknown",
                gpu=GpuSpec(provider_id=accel or "unknown", family=fam, vram_gb=vram, count=count,  # type: ignore[arg-type]
                            price_per_hour=STATIC_INSTANCES.get(accel, ("", 0, 1, None))[3], label=accel),
                served_model_name=str(m.get("name") or ""), endpoint_url=_endpoint_url(model_id),
                name=str(m.get("name") or ""), auth_env="BASETEN_API_KEY",
                raw={"model_id": model_id, "deployment_id": str(prod.get("id") or ""), "model": mask_secrets(m)},
            )
            out.append(self._apply(dep, prod) if prod else dep)
        return out

    async def cost(self, dep: Deployment) -> CostEstimate:
        price = dep.gpu.price_per_hour
        if price is None:
            price = STATIC_INSTANCES.get(dep.gpu.provider_id, ("", 0, 1, None))[3]
        per_hour = price * max(1, dep.opts.max_replicas) if price else None
        idle = price * dep.opts.min_replicas if (price and dep.opts.min_replicas > 0) else 0.0
        return CostEstimate(
            per_hour_usd=round(per_hour, 3) if per_hour else None, idle_per_hour_usd=round(idle, 3),
            basis="instance list price × max replicas, billed per minute incl. build/scale-up; $0 at min_replica=0 while idle",
        )


def _static_family(gpu_type: str, vram: int) -> tuple[str, int]:
    base = STATIC_INSTANCES.get(gpu_type.upper()) or STATIC_INSTANCES.get(gpu_type)
    if base:
        return base[0], base[1]
    return gpu_family_from_name(gpu_type, vram or None), vram


def _items(data: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for k in (key, "items", "data", "results"):
            v = data.get(k)
            if isinstance(v, list):
                return [d for d in v if isinstance(d, dict)]
    return []
