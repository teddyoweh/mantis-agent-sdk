"""Vast.ai deploy adapter — the marketplace / raw-VM path.

Vast.ai rents individual hosts' GPUs by the hour. We search the offer book
(``POST /bundles/``), rent the offer the user picked (``PUT /asks/{id}/``)
with the upstream ``vllm/vllm-openai`` image and a launch line from
:mod:`mantis_agent.deploy._vm_bootstrap`, then poll the instance until it
is ``running`` with a public IP and the host port that maps container
port 8000. The result is a **plain-HTTP** OpenAI base URL on a shared
public IP, so the endpoint is protected only by the ``--api-key`` we
generate (``Authorization: Bearer``; the key lives in
``os.environ[MANTIS_DEPLOY_<SLUG>_KEY]`` — the store persists the env name).

No scale-to-zero: the instance bills ``dph_total`` every hour it exists
(storage even while stopped). ``opts.idle_timeout_s`` is recorded as an
SDK-side note in ``Deployment.message`` for now; auto-teardown is a
follow-up.
"""

from __future__ import annotations

import logging
import os
import shlex
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .. import _http
from .. import _vm_bootstrap as _boot
from .._http import DeployHttp, raise_for
from .._vm_bootstrap import (
    DEFAULT_PORT,
    auth_env_name,
    build_engine_args,
    build_onstart_script,
    generate_api_key,
    sglang_image,
    slugify,
    vllm_image,
    wait_for_openai,
)
from ..base import (
    Account,
    CostEstimate,
    CredentialField,
    DeployError,
    DeployOpts,
    Deployment,
    DeploymentStatus,
    Engine,
    GpuFamily,
    GpuSpec,
    NotSupported,
    register_provider,
)

__all__ = ["VastAIProvider", "gpu_family_from_name", "parse_host_port"]

_log = logging.getLogger(__name__)

BASE_URL = "https://console.vast.ai/api/v0"
LABEL_PREFIX = "mantis-"
MAX_OFFERS = 40
DEFAULT_DISK_GB = 80

# Fields the instance object may echo back that could carry our secrets
# (HF token / api key); scrubbed before anything lands in ``Deployment.raw``.
_SECRET_KEYS = frozenset({"extra_env", "env", "onstart", "args_str", "args", "ssh_key", "image_args"})


def gpu_family_from_name(gpu_name: str, gpu_ram_mb: int | None = None) -> GpuFamily:
    n = (gpu_name or "").upper().replace(" ", "_")
    if n.startswith("H100"):
        return "H100"
    if n.startswith("H200"):
        return "H200"
    if n.startswith("B200"):
        return "B200"
    if n.startswith("A100"):
        return "A100-80" if (gpu_ram_mb or 0) >= 70_000 else "A100-40"
    if n == "L40S":
        return "L40S"
    if n == "L4":
        return "L4"
    if n in ("A10", "A10G"):
        return "A10G"
    if n in ("TESLA_T4", "T4"):
        return "T4"
    return "other"


def parse_host_port(inst: dict[str, Any], container_port: int = DEFAULT_PORT) -> int | None:
    """Find the public host port mapped to ``container_port`` in a Vast
    instance object. The documented shape is
    ``{"ports": {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41234"}]}}``
    but we accept a few variants defensively."""

    ports = inst.get("ports")
    if isinstance(ports, dict):
        for key in (f"{container_port}/tcp", str(container_port), container_port):
            entry = ports.get(key)
            if isinstance(entry, list) and entry:
                entry = entry[0]
            if isinstance(entry, dict):
                hp = entry.get("HostPort") or entry.get("host_port")
                if hp:
                    try:
                        return int(hp)
                    except (TypeError, ValueError):
                        pass
            elif isinstance(entry, (int, str)) and str(entry).isdigit():
                return int(entry)
    elif isinstance(ports, list):
        for p in ports:
            if isinstance(p, dict) and str(p.get("container_port") or p.get("PrivatePort")) == str(container_port):
                hp = p.get("host_port") or p.get("PublicPort")
                if hp:
                    return int(hp)
    direct = inst.get("direct_port_start")
    if direct and inst.get("direct_port_end") is not None:
        # Fallback used by some hosts: contiguous direct-port range mapped in order.
        return int(direct)
    return None


def _status_from_instance(inst: dict[str, Any]) -> tuple[DeploymentStatus, str]:
    actual = str(inst.get("actual_status") or "").lower()
    intended = str(inst.get("intended_status") or "").lower()
    msg = str(inst.get("status_msg") or "")
    if actual == "running":
        return "running", msg or "instance running"
    if actual in ("loading", "created", "") and intended in ("running", ""):
        return "starting", msg or f"instance {actual or 'scheduling'}"
    if actual in ("exited", "error", "offline"):
        return "failed", msg or f"instance {actual}"
    if actual in ("stopped",) or intended == "stopped":
        return "paused", msg or "instance stopped (storage still billed)"
    return "unknown", msg or f"actual_status={actual!r}"


def _scrub(inst: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in inst.items() if k not in _SECRET_KEYS}


@register_provider
class VastAIProvider:
    id = "vastai"
    display_name = "Vast.ai"
    credential_fields = (
        CredentialField("VAST_API_KEY", "Vast.ai API key", secret=True, required=True,
                        help="cloud.vast.ai → Account → API Keys"),
        CredentialField("HF_TOKEN", "Hugging Face token", secret=True, required=False,
                        help="only for gated repos; passed to the container as HF_TOKEN"),
    )
    engines: tuple[Engine, ...] = ("vllm", "sglang")
    console_url = "https://cloud.vast.ai/instances/"
    scale_to_zero = False
    public_by_default = True   # plain HTTP on a shared public IP; only the generated key guards it

    # ----- HTTP ------------------------------------------------------------

    def configured(self) -> bool:
        return bool(os.environ.get("VAST_API_KEY"))

    def _http(self) -> DeployHttp:
        """A control-plane client bound to the key in ``VAST_API_KEY`` (read
        per call so ``save_credentials`` takes effect without a restart)."""

        key = os.environ.get("VAST_API_KEY")
        if not key:
            raise DeployError(
                "VAST_API_KEY is not set",
                hint="cloud.vast.ai → Account → API Keys, then export VAST_API_KEY=…",
                provider=self.id,
            )
        return DeployHttp(
            provider=self.id, base_url=BASE_URL,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            auth_env="VAST_API_KEY", console_url=self.console_url,
        )

    async def _request(self, method: str, path: str, *, json: Any = None, params: Any = None,
                       ok_404: bool = False, what: str = "") -> Any:
        """JSON round-trip with the two Vast-specific error shapes handled
        before the shared ``raise_for`` hints kick in: a tolerated 404
        (instance already gone) and 410 (offer rented from under us)."""

        resp = await self._http().request(
            method, path, json=json, params=params, raise_on_error=False, what=what,
        )
        if resp.status_code == 404 and ok_404:
            return None
        if resp.status_code == 410:
            raise DeployError(
                "that offer is gone (410) — Vast.ai offers vanish between search and rent",
                hint="re-run `gpus vastai` and pick another offer", provider=self.id,
            )
        raise_for(resp, provider=self.id, auth_env="VAST_API_KEY", console_url=self.console_url, what=what)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    # ----- credentials -----------------------------------------------------

    async def validate_credentials(self) -> Account:
        if not self.configured():
            return Account(ok=False, provider=self.id, message="VAST_API_KEY not set")
        data = await self._request("GET", "/users/current/")
        if not isinstance(data, dict):
            return Account(ok=True, provider=self.id, message="authenticated")
        balance = data.get("balance") if data.get("balance") is not None else data.get("credit")
        try:
            balance_f = float(balance) if balance is not None else None
        except (TypeError, ValueError):
            balance_f = None
        user = data.get("username") or data.get("email")
        msg = "authenticated" if balance_f is None else f"balance ${balance_f:.2f}"
        if balance_f is not None and balance_f <= 0:
            msg += " — add credit before renting (cloud.vast.ai → Billing)"
        return Account(ok=True, provider=self.id, user=user, balance_usd=balance_f, message=msg)

    # ----- catalogue ---------------------------------------------------------

    async def list_gpus(self, *, min_vram_gb: int | None = None) -> list[GpuSpec]:
        query: dict[str, Any] = {
            "type": "on-demand",
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "reliability2": {"gte": 0.98},
            "direct_port_count": {"gte": 1},
            "disk_space": {"gte": 40},
            "cuda_max_good": {"gte": 12.4},
            "order": [["dph_total", "asc"]],
            "limit": 250,
        }
        if min_vram_gb:
            query["gpu_ram"] = {"gte": int(min_vram_gb * 1024)}
        data = await self._request("POST", "/bundles/", json=query)
        offers = (data or {}).get("offers") if isinstance(data, dict) else data
        rows: list[GpuSpec] = []
        seen: set[tuple[str, int]] = set()
        for o in offers or []:
            if not isinstance(o, dict) or o.get("id") is None:
                continue
            name = str(o.get("gpu_name") or "GPU")
            count = int(o.get("num_gpus") or 1)
            key = (name, count)
            if key in seen:
                continue
            seen.add(key)
            ram_mb = int(o.get("gpu_ram") or 0)
            vram_gb = int(round(ram_mb / 1024)) if ram_mb else 0
            pretty = name.replace("_", " ")
            rows.append(GpuSpec(
                provider_id=str(o["id"]),
                family=gpu_family_from_name(name, ram_mb),
                vram_gb=vram_gb,
                count=count,
                price_per_hour=round(float(o.get("dph_total") or 0.0), 3) if o.get("dph_total") is not None else None,
                available=True,
                region=o.get("geolocation") or None,
                label=f"{pretty} ×{count} ({vram_gb} GB)" if count > 1 else f"{pretty} ({vram_gb} GB)",
            ))
        rows.sort(key=lambda g: (g.price_per_hour if g.price_per_hour is not None else 1e9))
        return rows[:MAX_OFFERS]

    # ----- deploy ------------------------------------------------------------

    @staticmethod
    def _disk_gb(opts: DeployOpts) -> int:
        if opts.extra.get("disk_gb"):
            return int(opts.extra["disk_gb"])
        est = opts.extra.get("est_weights_gb") or opts.extra.get("est_vram_gb")
        if est:
            return max(40, int(float(est) * 1.5) + 20)
        return DEFAULT_DISK_GB

    async def deploy(self, model: str, gpu: GpuSpec, engine: Engine, opts: DeployOpts) -> Deployment:
        if engine not in self.engines:
            raise NotSupported(
                f"Vast.ai adapter supports vllm and sglang, not {engine!r}",
                hint="pick engine='vllm' (default) or 'sglang'", provider=self.id,
            )
        try:
            offer_id = int(gpu.provider_id)
        except ValueError:
            raise DeployError(
                f"Vast.ai GPU id must be an offer id from list_gpus(), got {gpu.provider_id!r}",
                hint="run `gpus vastai` and pass one of the listed ids", provider=self.id,
            ) from None

        slug = slugify(opts.name or model)
        label = LABEL_PREFIX + slug
        if opts.tensor_parallel is None:
            opts.tensor_parallel = max(1, gpu.count)
        auth_env = auth_env_name(slug)
        api_key = os.environ.get(auth_env) or generate_api_key()
        os.environ[auth_env] = api_key     # the store persists the NAME only
        hf_token = opts.hf_token or os.environ.get("HF_TOKEN")

        flags = build_engine_args(model, engine, opts, api_key=api_key, port=DEFAULT_PORT)
        if engine == "vllm":
            image = vllm_image(opts)
            args_str = " ".join(shlex.quote(a) for a in ["--model", model, *flags])
        else:
            image = sglang_image(opts)
            args_str = " ".join(shlex.quote(a) for a in ["python", "-m", "sglang.launch_server", "--model-path", model, *flags])
        onstart = build_onstart_script(model, engine, opts, api_key=api_key, port=DEFAULT_PORT)

        env_parts = [f"-p {DEFAULT_PORT}:{DEFAULT_PORT}", f"-e VLLM_API_KEY={shlex.quote(api_key)}"]
        if hf_token:
            env_parts.append(f"-e HF_TOKEN={shlex.quote(hf_token)}")
            env_parts.append(f"-e HUGGING_FACE_HUB_TOKEN={shlex.quote(hf_token)}")
        runtype = str(opts.extra.get("runtype", "args"))
        body: dict[str, Any] = {
            "client_id": "me",
            "image": image,
            "disk": self._disk_gb(opts),
            "label": label,
            "runtype": runtype,
            "env": " ".join(env_parts),
            "args_str": args_str,
            "cancel_unavail": True,
        }
        if runtype != "args":
            body["onstart"] = onstart
        if opts.extra.get("bid_price"):
            body["price"] = float(opts.extra["bid_price"])

        data = await self._request("PUT", f"/asks/{offer_id}/", json=body, what="rent offer")
        if not isinstance(data, dict) or not data.get("success", True) or data.get("new_contract") is None:
            msg = (data or {}).get("msg") if isinstance(data, dict) else data
            raise DeployError(
                f"Vast.ai did not create the instance: {msg or data}",
                hint="the offer may have been rented meanwhile — re-run `gpus vastai`", provider=self.id,
            )
        inst_id = str(data["new_contract"])
        note = (
            f"Vast.ai has no scale-to-zero: ${gpu.price_per_hour or 0:.3f}/h accrues until you tear it down"
            f" (idle_timeout_s={opts.idle_timeout_s} is recorded but not enforced yet). "
            "Endpoint is plain HTTP on a shared public IP; only the generated key guards it."
        )
        dep = Deployment(
            id=inst_id, provider=self.id, model=model, engine=engine, status="pending", gpu=gpu,
            served_model_name=opts.served_model_name or model, endpoint_url=None, name=label,
            opts=opts, auth_env=auth_env, auth_headers={}, message=note,
            raw={"offer_id": offer_id, "image": image, "disk_gb": body["disk"], "label": label},
        )

        # Short bounded poll so the caller usually gets the URL back from deploy();
        # wait_ready() keeps going if the host is slow to schedule.
        deadline = _boot._clock() + float(opts.extra.get("create_timeout_s", 180))
        delay = 3.0
        while True:
            dep = await self._refresh(dep, check_health=False)
            if dep.endpoint_url or dep.status == "failed" or _boot._clock() >= deadline:
                break
            await _http.sleep(min(delay, max(0.0, deadline - _boot._clock())))
            delay = min(delay * 1.5, 15.0)
        if dep.status == "failed":
            raise DeployError(
                f"Vast.ai instance {inst_id} failed to start: {dep.message}",
                hint="destroy it in cloud.vast.ai (storage bills while it exists) and try another offer",
                provider=self.id,
            )
        if not dep.message.startswith("Vast.ai has no scale-to-zero"):
            dep.message = f"{dep.message} — {note}"
        return dep

    # ----- status --------------------------------------------------------------

    async def _get_instance(self, inst_id: str) -> dict[str, Any] | None:
        data = await self._request("GET", f"/instances/{inst_id}/", ok_404=True)
        if data is None:
            return None
        inst = data.get("instances") if isinstance(data, dict) and "instances" in data else data
        if isinstance(inst, list):
            inst = next((i for i in inst if str(i.get("id")) == str(inst_id)), inst[0] if inst else None)
        return inst if isinstance(inst, dict) else None

    def _bearer(self, dep: Deployment) -> dict[str, str]:
        key = os.environ.get(dep.auth_env or "")
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def _refresh(self, dep: Deployment, *, check_health: bool = True) -> Deployment:
        inst = await self._get_instance(dep.id)
        if inst is None:
            dep.status, dep.message = "deleted", "instance no longer exists on Vast.ai"
            return dep
        status, msg = _status_from_instance(inst)
        ip = inst.get("public_ipaddr")
        port = parse_host_port(inst, DEFAULT_PORT)
        if ip and port:
            dep.endpoint_url = f"http://{ip}:{port}/v1"
        dep.raw.update({"instance": _scrub(inst)})
        if inst.get("dph_total") is not None:
            dep.raw["dph_total"] = inst["dph_total"]
        if status == "running" and check_health and dep.endpoint_url:
            base = dep.endpoint_url[:-3]
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0)) as client:
                    r = await client.get(f"{base}/health", headers=self._bearer(dep))
                if r.status_code != 200:
                    status, msg = "starting", f"instance up, engine not ready (/health {r.status_code})"
                else:
                    msg = "healthy"
            except httpx.HTTPError as e:
                status, msg = "starting", f"instance up, engine not answering yet ({type(e).__name__})"
        elif status == "running" and not dep.endpoint_url:
            status, msg = "starting", "running but port mapping not published yet"
        dep.status, dep.message = status, msg
        return dep

    async def status(self, dep: Deployment) -> Deployment:
        return await self._refresh(dep, check_health=True)

    async def wait_ready(self, dep: Deployment, timeout_s: int = 1200) -> Deployment:
        deadline = _boot._clock() + timeout_s
        delay = 5.0
        while not dep.endpoint_url:
            dep = await self._refresh(dep, check_health=False)
            if dep.status in ("failed", "deleted"):
                raise DeployError(f"instance {dep.id} is {dep.status}: {dep.message}", provider=self.id)
            if dep.endpoint_url:
                break
            if _boot._clock() >= deadline:
                raise DeployError(
                    f"instance {dep.id} has no public port after {timeout_s}s ({dep.message})",
                    hint="the host may be stuck scheduling — destroy it and pick another offer",
                    provider=self.id,
                )
            await _http.sleep(min(delay, max(0.0, deadline - _boot._clock())))
            delay = min(delay * 1.5, 20.0)
        remaining = max(1, int(deadline - _boot._clock()))
        await wait_for_openai(dep.endpoint_url, self._bearer(dep), remaining, provider=self.id)
        dep.status, dep.message = "running", "healthy"
        return dep

    # ----- logs / delete / list / cost -------------------------------------------

    async def logs(self, dep: Deployment, tail: int = 200) -> AsyncIterator[str]:
        data = await self._request("PUT", f"/instances/request_logs/{dep.id}/", json={"tail": str(tail)})
        url = data.get("result_url") if isinstance(data, dict) else None
        if not url:
            raise NotSupported(
                "Vast.ai did not return a log URL for this instance",
                hint=f"see {self.console_url} → instance {dep.id} → Logs", provider=self.id,
            )
        text: str | None = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            for _ in range(8):   # the S3 object appears a few seconds after the request
                try:
                    r = await client.get(url)
                except httpx.HTTPError:
                    r = None
                if r is not None and r.status_code == 200:
                    text = r.text
                    break
                await _http.sleep(2.0)
        if text is None:
            raise DeployError(
                "Vast.ai log file was not ready in time", hint="retry in a few seconds", provider=self.id,
            )
        lines = text.splitlines()
        for line in lines[-tail:] if tail > 0 else lines:
            yield line

    async def delete(self, dep: Deployment) -> None:
        await self._request("DELETE", f"/instances/{dep.id}/", ok_404=True)
        if dep.auth_env:
            os.environ.pop(dep.auth_env, None)

    async def list_deployments(self) -> list[Deployment]:
        data = await self._request("GET", "/instances/", params={"owner": "me"})
        items = data.get("instances") if isinstance(data, dict) else data
        deps: list[Deployment] = []
        for inst in items or []:
            if not isinstance(inst, dict):
                continue
            label = str(inst.get("label") or "")
            if not label.startswith(LABEL_PREFIX):
                continue
            status, msg = _status_from_instance(inst)
            ip, port = inst.get("public_ipaddr"), parse_host_port(inst, DEFAULT_PORT)
            slug = label[len(LABEL_PREFIX):]
            name = str(inst.get("gpu_name") or "GPU")
            ram_mb = int(inst.get("gpu_ram") or 0)
            count = int(inst.get("num_gpus") or 1)
            gpu = GpuSpec(
                provider_id=str(inst.get("ask_contract_id") or inst.get("id")),
                family=gpu_family_from_name(name, ram_mb), vram_gb=int(round(ram_mb / 1024)) if ram_mb else 0,
                count=count, price_per_hour=inst.get("dph_total"), available=True,
                region=inst.get("geolocation"), label=f"{name.replace('_', ' ')} ×{count}",
            )
            deps.append(Deployment(
                id=str(inst["id"]), provider=self.id, model=slug, engine="vllm", status=status, gpu=gpu,
                served_model_name=slug, endpoint_url=f"http://{ip}:{port}/v1" if ip and port else None,
                name=label, auth_env=auth_env_name(slug), message=msg, raw={"instance": _scrub(inst)},
            ))
        return deps

    async def cost(self, dep: Deployment) -> CostEstimate:
        price = dep.raw.get("dph_total")
        if price is None:
            price = dep.gpu.price_per_hour
        per_hour = round(float(price), 3) if price is not None else None
        return CostEstimate(
            per_hour_usd=per_hour,
            idle_per_hour_usd=per_hour or 0.0,
            basis="Vast.ai dph_total (GPU + host); no scale-to-zero, storage + bandwidth bill "
            "even while stopped — tear down when idle",
        )
