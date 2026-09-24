"""Fireworks on-demand (dedicated) deployments.

A dedicated deployment puts one model from the Fireworks library on GPUs that
are yours alone, behind the same OpenAI-compatible API as their serverless
models — so the key you already use for Fireworks inference (``FIREWORKS_API_KEY``)
is the whole credential, and a running deployment is just another ``model``
string: ``accounts/<account>/deployments/<id>`` against
``https://api.fireworks.ai/inference/v1``.

Control plane (docs.fireworks.ai/api-reference): ``GET /v1/accounts`` (which
account the key belongs to), ``GET /v1/accounts/fireworks/models`` (the library,
each entry with its ``huggingFaceUrl``), and ``POST|GET|DELETE
/v1/accounts/<account>/deployments[/<id>]``. States: CREATING, READY,
UPDATING, DELETING, FAILED, DELETED.

What it deploys: models in the Fireworks library, found by their Hugging Face
id (``zai-org/GLM-4.7`` → ``accounts/fireworks/models/glm-4p7``). A model that
isn't in the library needs its weights uploaded first (``firectl create
model``) — hundreds of GB through signed URLs, which is not a dashboard click —
so the error says exactly that. An ``accounts/…/models/…`` name is taken as-is,
which is how an uploaded model is deployed.

Hardware: ``deploymentShape: "default"`` lets Fireworks pick a validated
configuration for the model; the chosen accelerator type and count ride along,
and "the pick never silently overrides fields you set". When no validated shape
fits the count, the create is retried with the type alone and Fireworks picks
the count. Scale to zero is native (idle for an hour → 0 replicas → 503 on the
next request while one comes up); a deployment with min 0 is deleted by
Fireworks after 7 days without traffic.
"""

from __future__ import annotations

import os
import re
import time
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

__all__ = ["FireworksDedicatedProvider", "API_BASE", "INFERENCE_BASE", "GPUS", "library_match"]

API_BASE = "https://api.fireworks.ai"
INFERENCE_BASE = "https://api.fireworks.ai/inference/v1"
CONSOLE_URL = "https://app.fireworks.ai/dashboard/deployments"
PROVIDER_ID = "fireworks-dedicated"

#: accelerator enum → (family, vram GB, $/GPU-hour) — fireworks.ai/pricing, September 2026
GPUS: dict[str, tuple[str, int, float | None]] = {
    "NVIDIA_H100_80GB": ("H100", 80, 8.00),
    "NVIDIA_H200_141GB": ("H200", 141, 8.00),
    "NVIDIA_B200_180GB": ("B200", 180, 13.00),
    "NVIDIA_B300_288GB": ("other", 288, 15.00),
}
_COUNTS = (1, 2, 4, 8)

_STATUS: dict[str, DeploymentStatus] = {
    "creating": "starting", "updating": "starting", "ready": "running",
    "deleting": "deleting", "failed": "failed", "deleted": "deleted",
}

_LIBRARY_TTL_S = 3600.0
_library_cache: tuple[float, list[dict[str, Any]]] | None = None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _hf_id(url: str) -> str:
    """``https://huggingface.co/zai-org/GLM-4.7`` → ``zai-org/glm-4.7``."""
    m = re.search(r"huggingface\.co/([^/?#]+/[^/?#]+)", url or "")
    return m.group(1).lower() if m else ""


def library_match(hf_model: str, library: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The library entry for a Hugging Face id, or None.

    An exact ``huggingFaceUrl`` match wins. Otherwise the repo name is compared
    to the library's short names with the punctuation removed and Fireworks'
    ``p`` for a decimal point (``GLM-4.7`` ↔ ``glm-4p7``), and a trailing
    precision tag dropped (``-FP8`` — Fireworks picks the precision itself)."""
    want = hf_model.strip().lower()
    for m in library:
        if _hf_id(str(m.get("huggingFaceUrl") or "")) == want:
            return m
    repo = want.split("/", 1)[-1]
    repo = re.sub(r"[-_](fp8|fp4|bf16|fp16|int4|int8|awq|gptq)$", "", repo)
    keys = {_norm(repo), _norm(repo.replace(".", "p"))}
    for m in library:
        short = str(m.get("name") or "").rsplit("/", 1)[-1]
        if _norm(short) in keys or _norm(short.replace("p", ".")) in keys:
            return m
    return None


@register_provider
class FireworksDedicatedProvider:
    id = PROVIDER_ID
    display_name = "Fireworks"
    credential_fields = (
        CredentialField("FIREWORKS_API_KEY", "Fireworks API key",
                        help="app.fireworks.ai → Settings → API Keys (the same key serverless inference uses)"),
        CredentialField("FIREWORKS_ACCOUNT_ID", "Account id", required=False, secret=False,
                        help="optional — found from the key; set it when the key can see more than one account"),
    )
    # Fireworks runs its own serving stack; "vllm" is the OpenAI-compatible
    # contract mantis relies on, not a claim about what runs underneath.
    engines: tuple[Engine, ...] = ("vllm",)
    console_url = CONSOLE_URL
    scale_to_zero = True
    public_by_default = False

    def __init__(self) -> None:
        self._account: str | None = None

    # ------------------------------------------------------------------

    def configured(self) -> bool:
        return require_env("FIREWORKS_API_KEY")

    def _http(self, **kw: Any) -> DeployHttp:
        key = (os.environ.get("FIREWORKS_API_KEY") or "").strip()
        if not key:
            raise DeployError(
                "FIREWORKS_API_KEY is not set",
                hint="create a key at app.fireworks.ai/settings/users/api-keys, then "
                     f"`mantis-agent deploy creds {PROVIDER_ID} --set FIREWORKS_API_KEY=...`",
                provider=self.id,
            )
        return DeployHttp(
            provider=self.id, base_url=API_BASE, auth_env="FIREWORKS_API_KEY", console_url=CONSOLE_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, **kw,
        )

    async def _account_id(self) -> str:
        if self._account:
            return self._account
        env = (os.environ.get("FIREWORKS_ACCOUNT_ID") or "").strip()
        if env:
            self._account = env.split("/")[-1]
            return self._account
        data = await self._http().json("GET", "/v1/accounts", params={"pageSize": 50}, what="find your account")
        accts = [a for a in (data.get("accounts") or []) if isinstance(a, dict)] if isinstance(data, dict) else []
        if not accts:
            raise DeployError("the Fireworks key can't see any account",
                              hint="set FIREWORKS_ACCOUNT_ID to your account id (Settings → Account)",
                              provider=self.id)
        if len(accts) > 1:
            names = ", ".join(str(a.get("name", "")).split("/")[-1] for a in accts[:5])
            raise DeployError("the Fireworks key can see several accounts: " + names,
                              hint="set FIREWORKS_ACCOUNT_ID to the one to deploy into", provider=self.id)
        self._account = str(accts[0].get("name") or "").split("/")[-1]
        return self._account

    async def _library(self) -> list[dict[str, Any]]:
        global _library_cache
        now = time.monotonic()
        if _library_cache and now - _library_cache[0] < _LIBRARY_TTL_S:
            return _library_cache[1]
        out: list[dict[str, Any]] = []
        token = ""
        http = self._http(timeout=60.0)
        for _ in range(10):
            params: dict[str, Any] = {"pageSize": 200}
            if token:
                params["pageToken"] = token
            data = await http.json("GET", "/v1/accounts/fireworks/models", params=params, what="read the model library")
            out.extend(m for m in (data.get("models") or []) if isinstance(m, dict))
            token = str(data.get("nextPageToken") or "")
            if not token:
                break
        _library_cache = (now, out)
        return out

    async def validate_credentials(self) -> Account:
        acct = await self._account_id()
        data = await self._http().json("GET", f"/v1/accounts/{acct}/deployments", params={"pageSize": 200},
                                       what="validate credentials")
        n = len([d for d in (data.get("deployments") or []) if isinstance(d, dict)]) if isinstance(data, dict) else 0
        return Account(ok=True, provider=self.id, message=f"account {acct} · {n} deployment(s)")

    async def list_gpus(self) -> list[GpuSpec]:
        out = []
        for gid, (fam, vram, price) in GPUS.items():
            for n in _COUNTS:
                out.append(GpuSpec(
                    provider_id=gid, family=fam, vram_gb=vram, count=n,  # type: ignore[arg-type]
                    price_per_hour=round(price * n, 2) if price else None, available=None,
                    label=gid.replace("NVIDIA_", "").replace("_", " ") + (f" ×{n}" if n > 1 else ""),
                ))
        out.sort(key=lambda g: (g.price_per_hour or 0, g.total_vram_gb))
        return out

    # ------------------------------------------------------------------

    async def _base_model(self, model: str) -> tuple[str, dict[str, Any] | None]:
        if model.startswith("accounts/"):
            return model, None
        hit = library_match(model, await self._library())
        if hit is None:
            raise DeployError(
                f"{model} isn't in the Fireworks model library",
                hint="Fireworks deploys models from its library, or ones you upload yourself with "
                     "`firectl create model` — then deploy `accounts/<you>/models/<id>`. "
                     "RunPod, Modal and the others deploy any Hugging Face repo.",
                provider=self.id,
            )
        return str(hit.get("name")), hit

    async def deploy(self, model: str, gpu: GpuSpec, engine: Engine, opts: DeployOpts) -> Deployment:
        if gpu.provider_id not in GPUS:
            raise DeployError(f"fireworks: unknown accelerator {gpu.provider_id!r}",
                              hint="one of " + ", ".join(GPUS), provider=self.id)
        acct = await self._account_id()
        base, entry = await self._base_model(model)
        name = slugify(opts.name or f"mantis-{slugify(model, max_len=40)}", max_len=60)
        body: dict[str, Any] = {
            "baseModel": base,
            "deploymentShape": str(opts.extra.get("deployment_shape") or "default"),
            "displayName": name[:64],
            "acceleratorType": gpu.provider_id,
            "acceleratorCount": max(1, gpu.count),
            "minReplicaCount": max(0, opts.min_replicas),
            "maxReplicaCount": max(1, opts.max_replicas, opts.min_replicas),
        }
        if opts.max_model_len:
            body["maxContextLength"] = int(opts.max_model_len)
        http = self._http(timeout=120.0)
        path = f"/v1/accounts/{acct}/deployments"
        resp = await http.request("POST", path, json=body, params={"deploymentId": name},
                                  raise_on_error=False, what="create deployment")
        if resp.status_code == 400 and re.search(r"shape|accelerator", resp.text or "", re.I):
            # no validated shape runs this model on exactly that many GPUs:
            # keep the type, let Fireworks pick the count
            body.pop("acceleratorCount", None)
            resp = await http.request("POST", path, json=body, params={"deploymentId": name},
                                      raise_on_error=False, what="create deployment")
        raise_for(resp, provider=self.id, auth_env="FIREWORKS_API_KEY", console_url=CONSOLE_URL,
                  what="create deployment")
        data = resp.json() if resp.content else {}
        full = str(data.get("name") or f"accounts/{acct}/deployments/{name}")
        dep = Deployment(
            id=full.rsplit("/", 1)[-1], provider=self.id, model=model, engine="vllm", status="starting",
            gpu=gpu, served_model_name=full, endpoint_url=INFERENCE_BASE, name=name, opts=opts,
            auth_env="FIREWORKS_API_KEY", message="deployment requested",
            raw={"account": acct, "base_model": base,
                 "library": {k: entry.get(k) for k in ("name", "displayName", "contextLength")} if entry else None,
                 "deployment": mask_secrets(data)},
        )
        return self._apply(dep, data if isinstance(data, dict) else {})

    def _apply(self, dep: Deployment, data: dict[str, Any]) -> Deployment:
        state = str(data.get("state") or "").lower()
        if state:
            dep.status = _STATUS.get(state, "unknown")
        # READY with nothing running is a deployment scaled to zero
        if dep.status == "running" and data.get("replicaCount") == 0 and (data.get("minReplicaCount") or 0) == 0:
            dep.status = "scaled_to_zero"
        st = data.get("status") if isinstance(data.get("status"), dict) else {}
        msg = st.get("message") or ""
        dep.message = str(msg) if msg else (f"state={state}" if state else dep.message)
        # what Fireworks actually chose, when the shape picked the count
        n = data.get("acceleratorCount")
        if isinstance(n, int) and n > 0 and n != dep.gpu.count:
            g = dep.gpu
            price = GPUS.get(g.provider_id, (None, None, None))[2]
            dep.gpu = GpuSpec(provider_id=g.provider_id, family=g.family, vram_gb=g.vram_gb, count=n,
                              price_per_hour=round(price * n, 2) if price else None, available=g.available,
                              label=g.provider_id.replace("NVIDIA_", "").replace("_", " ") + (f" ×{n}" if n > 1 else ""))
        dep.endpoint_url = INFERENCE_BASE
        dep.raw = {**dep.raw, "deployment": mask_secrets(data)}
        dep.updated_at = utcnow()
        return dep

    async def status(self, dep: Deployment) -> Deployment:
        """Control plane only — a GET on the deployment never wakes it."""
        acct = dep.raw.get("account") or await self._account_id()
        resp = await self._http().request("GET", f"/v1/accounts/{acct}/deployments/{dep.id}",
                                          raise_on_error=False, what="status")
        if resp.status_code == 404:
            dep.status, dep.message = "deleted", "deployment no longer exists"
            dep.updated_at = utcnow()
            return dep
        raise_for(resp, provider=self.id, auth_env="FIREWORKS_API_KEY", console_url=CONSOLE_URL, what="status")
        body = resp.json() if resp.content else {}
        return self._apply(dep, body if isinstance(body, dict) else {})

    async def wait_ready(self, dep: Deployment, timeout_s: int = 1200) -> Deployment:
        async def check() -> tuple[bool, Any]:
            cur = await self.status(dep)
            if cur.status in ("running", "scaled_to_zero"):
                return True, cur
            if cur.status in ("failed", "deleted"):
                raise DeployError(f"fireworks deployment {dep.id} {cur.status}: {cur.message}",
                                  hint=f"see {CONSOLE_URL}", provider=self.id)
            return False, cur

        await poll_until(check, timeout_s=timeout_s, interval_s=15.0,
                         what=f"fireworks deployment {dep.id} to be ready", provider=self.id)
        return dep

    def logs(self, dep: Deployment, tail: int = 200) -> AsyncIterator[str]:
        raise NotSupported(
            "Fireworks has no logs API for deployments",
            hint=f"open {CONSOLE_URL} for the deployment's metrics", provider=self.id,
        )

    async def delete(self, dep: Deployment) -> None:
        acct = dep.raw.get("account") or await self._account_id()
        resp = await self._http().request("DELETE", f"/v1/accounts/{acct}/deployments/{dep.id}",
                                          raise_on_error=False, what="delete")
        if resp.status_code in (200, 202, 204, 404):
            return
        raise_for(resp, provider=self.id, auth_env="FIREWORKS_API_KEY", console_url=CONSOLE_URL, what="delete")

    async def list_deployments(self) -> list[Deployment]:
        acct = await self._account_id()
        data = await self._http().json("GET", f"/v1/accounts/{acct}/deployments", params={"pageSize": 200},
                                       what="list")
        out: list[Deployment] = []
        for d in (data.get("deployments") or []) if isinstance(data, dict) else []:
            if not isinstance(d, dict) or not d.get("name"):
                continue
            full = str(d["name"])
            gid = str(d.get("acceleratorType") or "")
            fam, vram, price = GPUS.get(gid, ("other", 0, None))
            n = int(d.get("acceleratorCount") or 1)
            base = str(d.get("baseModel") or "")
            dep = Deployment(
                id=full.rsplit("/", 1)[-1], provider=self.id, model=base, engine="vllm", status="unknown",
                gpu=GpuSpec(provider_id=gid or "unknown", family=fam, vram_gb=vram, count=n,  # type: ignore[arg-type]
                            price_per_hour=round(price * n, 2) if price else None,
                            label=gid.replace("NVIDIA_", "").replace("_", " ") + (f" ×{n}" if n > 1 else "")),
                served_model_name=full, endpoint_url=INFERENCE_BASE, name=str(d.get("displayName") or ""),
                auth_env="FIREWORKS_API_KEY",
                opts=DeployOpts(min_replicas=int(d.get("minReplicaCount") or 0),
                                max_replicas=int(d.get("maxReplicaCount") or 1)),
                raw={"account": acct, "base_model": base},
            )
            out.append(self._apply(dep, d))
        return out

    async def cost(self, dep: Deployment) -> CostEstimate:
        price = dep.gpu.price_per_hour
        if price is None:
            base = GPUS.get(dep.gpu.provider_id)
            price = base[2] * max(1, dep.gpu.count) if (base and base[2]) else None
        per_hour = price * max(1, dep.opts.max_replicas) if price else None
        idle = price * dep.opts.min_replicas if (price and dep.opts.min_replicas > 0) else 0.0
        return CostEstimate(
            per_hour_usd=round(per_hour, 3) if per_hour else None, idle_per_hour_usd=round(idle, 3),
            basis="list price per GPU-hour × GPUs × max replicas, billed per GPU-second; $0 at min 0 while scaled down",
        )
