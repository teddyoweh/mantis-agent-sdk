"""Bring-your-own GPU provider — the shared contract.

``mantis_agent.deploy`` turns "I have an account on a GPU cloud" into "an
OpenAI-compatible endpoint serving any open-weight model", from the
dashboard (``mantis serve`` → Deploy), the CLI (``mantis-agent deploy``) or
the terminal (``/deploy``). Every provider adapter implements
:class:`DeployProvider`; everything above it (pre-flight sizing, the
deployment store, the dashboard, the CLI) speaks only these types.

Design notes (from the September 2026 provider survey):

* The wire on the *inference* side is always OpenAI-compatible, so once a
  deployment is ``running`` the rest of the SDK treats it like any other
  backend: ``Agent(model=dep.served_model_name, backend=dep.endpoint_url,
  api_key=...)``. The one thing that differs per provider is what goes in
  ``model=`` — RunPod wants the HF id, DeepInfra wants ``deploy_id:<id>``,
  Fireworks wants ``accounts/.../deployments/...`` — so :class:`Deployment`
  carries ``served_model_name`` and callers never guess.
* Cold starts return **503** on Modal, HF Endpoints and Fireworks until a
  replica is up. :meth:`DeployProvider.wait_ready` owns that retry loop; the
  manager's ``connect`` step re-checks ``/v1/models`` before handing the
  endpoint to the terminal.
* Credentials are described, not hard-coded: :attr:`DeployProvider.credential_fields`
  lists the env vars a provider needs so the dashboard can render a form
  and the CLI can print the exact ``export`` lines.
* Everything here is async because every adapter is HTTP-bound; the CLI
  wraps calls with :func:`anyio.run`.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Literal, Protocol, runtime_checkable

__all__ = [
    "DEPLOY_PROVIDERS",
    "Account",
    "CostEstimate",
    "CredentialField",
    "DeployError",
    "DeployOpts",
    "DeployProvider",
    "Deployment",
    "DeploymentStatus",
    "Engine",
    "GpuFamily",
    "GpuSpec",
    "ModelInfo",
    "NotSupported",
    "get_provider",
    "register_provider",
    "utcnow",
]

Engine = Literal["vllm", "sglang", "tgi", "llamacpp"]
GpuFamily = Literal[
    "T4", "L4", "A10G", "L40S", "A100-40", "A100-80", "H100", "H200", "B200", "other",
]
DeploymentStatus = Literal[
    "pending", "building", "starting", "running", "scaled_to_zero",
    "paused", "failed", "deleting", "deleted", "unknown",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeployError(RuntimeError):
    """Any failure talking to a deploy provider. ``hint`` is user-facing."""

    def __init__(self, message: str, *, hint: str | None = None, provider: str | None = None):
        super().__init__(message)
        self.hint = hint
        self.provider = provider


class NotSupported(DeployError):
    """The provider has no API for this operation (e.g. DeepInfra logs)."""


@dataclass(frozen=True)
class CredentialField:
    """One value a provider needs. ``env`` is the variable we read and the
    key we persist under; ``secret`` decides masking in UIs."""

    env: str
    label: str
    secret: bool = True
    required: bool = True
    help: str = ""  # where to get it, e.g. "console.runpod.io → Settings → API Keys"


@dataclass(frozen=True)
class GpuSpec:
    """A normalised row of a provider's GPU catalogue."""

    provider_id: str          # the provider's own id, passed back verbatim on deploy
    family: GpuFamily
    vram_gb: int
    count: int = 1
    price_per_hour: float | None = None   # USD for the whole spec (all ``count`` GPUs)
    available: bool | None = None         # None = provider doesn't say
    region: str | None = None
    label: str = ""                       # display name, defaults to provider_id

    @property
    def total_vram_gb(self) -> int:
        return self.vram_gb * self.count

    @property
    def display(self) -> str:
        return self.label or (f"{self.provider_id}×{self.count}" if self.count > 1 else self.provider_id)


@dataclass
class DeployOpts:
    """Knobs common to every provider. Adapters ignore what they can't map
    and record the effective values in ``Deployment.raw``."""

    name: str | None = None                 # human name / slug; adapters derive one from the model
    hf_token: str | None = None             # for gated repos; adapters plumb it into the provider's secret store
    served_model_name: str | None = None    # override what the endpoint answers to
    max_model_len: int | None = None
    tensor_parallel: int | None = None      # defaults to gpu.count
    quantization: str | None = None         # "fp8", "awq", "gptq", ...
    min_replicas: int = 0                   # 0 = scale to zero where supported
    max_replicas: int = 1
    idle_timeout_s: int = 300
    trust_remote_code: bool = False
    region: str | None = None
    request_timeout_s: int = 600
    extra_engine_args: list[str] = field(default_factory=list)
    engine_version: str | None = None       # pin the vLLM/SGLang image tag
    extra: dict[str, Any] = field(default_factory=dict)  # provider-specific escape hatch


@dataclass
class Deployment:
    """One deployed endpoint. Persisted by ``deploy.store`` and returned by
    every provider call; ``raw`` keeps the provider's own object."""

    id: str                        # provider-native id (RunPod endpoint id, HF endpoint name, Modal app name, ...)
    provider: str                  # DeployProvider.id
    model: str                     # the HF id (or ollama tag) the user asked for
    engine: Engine
    status: DeploymentStatus
    gpu: GpuSpec
    served_model_name: str         # what goes in ``model=`` on inference calls
    endpoint_url: str | None = None  # OpenAI base URL INCLUDING ``/v1``; None until known
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    name: str = ""                 # human label
    opts: DeployOpts = field(default_factory=DeployOpts)
    auth_env: str | None = None    # env var whose value authenticates inference calls (Bearer), if any
    auth_headers: dict[str, str] = field(default_factory=dict)  # non-bearer auth (Modal-Key/Modal-Secret); values may be env refs "${VAR}"
    message: str = ""              # last human-readable status detail
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.status in ("running", "scaled_to_zero")


@dataclass(frozen=True)
class Account:
    """What ``validate_credentials`` learned. All fields optional except ok."""

    ok: bool
    provider: str
    user: str | None = None
    balance_usd: float | None = None
    credits_usd: float | None = None
    message: str = ""


@dataclass(frozen=True)
class CostEstimate:
    per_hour_usd: float | None      # while running, all replicas
    idle_per_hour_usd: float        # 0.0 when scale-to-zero, else the warm-pool cost
    accrued_usd: float | None = None  # from the provider's billing API when it has one
    basis: str = ""                 # "list price × replicas", "provider billing API", ...


@dataclass(frozen=True)
class ModelInfo:
    """Pre-flight facts about a model, from the HF Hub (or Ollama manifest)."""

    id: str
    source: Literal["hf", "ollama"]
    architectures: tuple[str, ...] = ()
    params_b: float | None = None        # billions of parameters
    dtype: str | None = None             # dominant safetensors dtype ("BF16", "F8_E4M3", ...)
    gated: bool = False
    license: str | None = None
    downloads: int | None = None
    likes: int | None = None
    context_len: int | None = None       # max_position_embeddings when known
    vllm_ok: bool | None = None          # None = unknown architecture
    est_vram_gb: float | None = None     # weights + KV headroom at the default context
    tags: tuple[str, ...] = ()
    reason: str = ""                     # why vllm_ok is False / what's missing


@runtime_checkable
class DeployProvider(Protocol):
    """Everything a GPU cloud adapter must implement.

    Adapters are constructed with no arguments and read credentials from
    the environment (the store exports saved keys into ``os.environ`` on
    load, the same way ``catalog.set_key`` does). Anything not supported
    raises :class:`NotSupported` — callers render that, they don't crash.
    """

    id: str                      # "runpod", "hf", "modal", "deepinfra", "baseten", "vastai"
    display_name: str            # "RunPod Serverless"
    credential_fields: tuple[CredentialField, ...]
    engines: tuple[Engine, ...]  # what deploy() accepts
    console_url: str             # where the user manages things by hand
    scale_to_zero: bool          # whether min_replicas=0 is honoured
    public_by_default: bool      # True = endpoint is reachable without our auth headers (warn)

    def configured(self) -> bool:
        """All required credential env vars are present (no network)."""

    async def validate_credentials(self) -> Account: ...
    async def list_gpus(self) -> list[GpuSpec]: ...
    async def deploy(self, model: str, gpu: GpuSpec, engine: Engine, opts: DeployOpts) -> Deployment: ...
    async def status(self, dep: Deployment) -> Deployment: ...
    async def wait_ready(self, dep: Deployment, timeout_s: int = 1200) -> Deployment:
        """Poll until ``running`` (or fail). Must tolerate cold-start 503s."""

    def logs(self, dep: Deployment, tail: int = 200) -> AsyncIterator[str]: ...
    async def delete(self, dep: Deployment) -> None: ...
    async def list_deployments(self) -> list[Deployment]: ...
    async def cost(self, dep: Deployment) -> CostEstimate: ...


DEPLOY_PROVIDERS: dict[str, type[DeployProvider]] = {}


def register_provider(cls: type[DeployProvider]) -> type[DeployProvider]:
    """Class decorator: ``@register_provider`` on each adapter."""

    DEPLOY_PROVIDERS[cls.id] = cls
    return cls


def get_provider(provider_id: str) -> DeployProvider:
    """Instantiate an adapter by id, importing the built-ins lazily."""

    # ``from . import providers`` would resolve to the *function*
    # ``deploy.providers`` re-exported by the package ``__init__`` — not the
    # adapters subpackage — so import the subpackage by its full name.
    importlib.import_module("mantis_agent.deploy.providers")  # registers on import

    try:
        cls = DEPLOY_PROVIDERS[provider_id]
    except KeyError:
        known = ", ".join(sorted(DEPLOY_PROVIDERS)) or "none"
        raise DeployError(
            f"unknown deploy provider {provider_id!r}", hint=f"known: {known}"
        ) from None
    return cls()
