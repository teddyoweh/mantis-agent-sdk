"""High-level deploy operations — the API the dashboard, CLI and ``/deploy`` call.

This file is the CONTRACT. Signatures and docstrings are fixed; the bodies
are implemented by the deploy-core work. Every function is async and raises
:class:`~mantis_agent.deploy.base.DeployError` on failure.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

import httpx

from .store import _import_builtin_providers
from .base import (
    Account,
    CostEstimate,
    DeployError,
    DeployOpts,
    Deployment,
    DeployProvider,
    Engine,
    GpuSpec,
    ModelInfo,
    NotSupported,
    get_provider,
    utcnow,
)

__all__ = [
    "ProviderSummary",
    "boot_budget_s",
    "reasoning_parser_for",
    "tool_parser_for",
    "connect",
    "cost",
    "deploy",
    "find_models",
    "fit",
    "gpus",
    "inspect_model",
    "list_deployments",
    "logs",
    "providers",
    "save_credentials",
    "search_models",
    "status",
    "teardown",
    "try_endpoint",
    "validate",
    "wake_budget_s",
]

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import cost off callers
    from .smart_search import SmartSearchResult

ProgressFn = Callable[[str], Any]  # called with human-readable progress lines; sync or async
#: Structured progress for a UI that wants more than text: ``on_event(kind, data)``
#: with kind ``"stage"`` (data ``{"stage": "prepare"|"create"|"boot"|"ready"}``),
#: ``"created"`` (data ``{"provider", "id"}`` — the moment a billable resource
#: exists) and ``"heartbeat"`` (data ``{"elapsed_s"}``, every ~15s while booting).
EventFn = Callable[[str, dict[str, Any]], Any]

#: How often ``deploy`` reports that a booting endpoint is still booting.
HEARTBEAT_S = 15.0

#: How long ``connect`` keeps re-asking ``/models`` while a cold replica boots.
CONNECT_TIMEOUT_S = 120.0
CONNECT_INTERVAL_S = 5.0


class ProviderSummary(dict):
    """``{"id", "display_name", "configured", "credential_fields": [CredentialField...],
    "engines", "console_url", "scale_to_zero", "public_by_default"}`` — a plain
    dict so it serialises straight to JSON for the dashboard."""


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


#: Reasoning models emit their chain of thought inside the completion unless
#: vLLM is told which parser splits it into ``reasoning_content``. Without
#: this, GLM-4.7 answered "what are you?" with "1. Analyze the user's
#: request…" as the message — and an agent would take that as the answer.
_REASONING_PARSERS: tuple[tuple[str, str], ...] = (
    ("Glm4MoeForCausalLM", "glm45"), ("Glm4vMoeForConditionalGeneration", "glm45"),
    ("Qwen3ForCausalLM", "qwen3"), ("Qwen3MoeForCausalLM", "qwen3"),
    ("DeepseekV3ForCausalLM", "deepseek_r1"), ("DeepseekV2ForCausalLM", "deepseek_r1"),
)


def reasoning_parser_for(info: ModelInfo) -> str | None:
    """The vLLM ``--reasoning-parser`` a model needs, or ``None``. Keyed on the
    architecture (an R1 distill of Llama still thinks, so its name is checked
    too). A parser is harmless on a model that never opens a think block."""
    archs = set(info.architectures or ())
    for arch, parser in _REASONING_PARSERS:
        if arch in archs:
            return parser
    if "r1" in (info.id or "").lower().split("/")[-1].lower().replace("-", " ").split():
        return "deepseek_r1"
    return None


#: mantis is an agent: every request carries tools with ``tool_choice: auto``,
#: which vLLM rejects outright (400) unless the server was started with
#: ``--enable-auto-tool-choice`` and the parser that reads this model family's
#: tool-call format. Keyed on architecture, like the reasoning parser.
_TOOL_PARSERS: tuple[tuple[str, str], ...] = (
    ("Glm4MoeForCausalLM", "glm45"), ("Glm4vMoeForConditionalGeneration", "glm45"),
    ("Qwen3ForCausalLM", "hermes"), ("Qwen3MoeForCausalLM", "hermes"),
    ("Qwen2ForCausalLM", "hermes"), ("Qwen2MoeForCausalLM", "hermes"),
    ("DeepseekV3ForCausalLM", "deepseek_v3"),
    ("LlamaForCausalLM", "llama3_json"), ("Llama4ForConditionalGeneration", "llama4_pythonic"),
    ("MistralForCausalLM", "mistral"), ("Mistral3ForConditionalGeneration", "mistral"),
    ("Gemma3ForConditionalGeneration", "pythonic"), ("Gemma3ForCausalLM", "pythonic"),
    ("MiniMaxM2ForCausalLM", "minimax_m2"), ("KimiK2ForCausalLM", "kimi_k2"),
)


def tool_parser_for(info: ModelInfo) -> str | None:
    """The vLLM ``--tool-call-parser`` a model needs, or ``None`` when its family
    has no known format (it then deploys without auto tool choice). GLM-4.7
    changed its tool format and ships its own parser; DeepSeek-V3-shaped Kimi
    K2 checkpoints use Kimi's."""
    mid = (info.id or "").lower()
    archs = set(info.architectures or ())
    if "Glm4MoeForCausalLM" in archs and "glm-4.7" in mid:
        return "glm47"
    if "DeepseekV3ForCausalLM" in archs and "kimi" in mid:
        return "kimi_k2"
    for arch, parser in _TOOL_PARSERS:
        if arch in archs:
            return parser
    return None


def boot_budget_s(est_vram_gb: float | None) -> int:
    """How long a first boot is allowed to take. Weights are pulled from the
    Hub onto the GPU before the server can answer, and a 358B model is ~300 GB
    of them: at a conservative ~150 MB/s that is 35 minutes, which is nowhere
    near the 600 s Modal allows by default — the container was killed and
    restarted every ten minutes, forever, at the GPU's hourly rate. 15 minutes
    plus 8 s per GB, never less than 15 minutes, never more than an hour."""
    gb = float(est_vram_gb or 0) * 0.85          # weights are most of the estimate
    return int(max(900, min(3600, 900 + gb * 8)))


async def _emit(on_event: EventFn | None, kind: str, data: dict[str, Any]) -> None:
    """Structured twin of :func:`_say` — never lets a broken sink abort a deploy."""
    if on_event is None:
        return
    try:
        r = on_event(kind, data)
        if inspect.isawaitable(r):
            await r
    except Exception:  # noqa: BLE001
        pass


async def _say(progress: ProgressFn | None, line: str) -> None:
    if progress is None:
        return
    try:
        r = progress(line)
        if inspect.isawaitable(r):
            await r
    except Exception:  # noqa: BLE001 — a broken progress sink must not abort a deploy
        pass


def _provider(provider_id: str) -> DeployProvider:
    from . import store  # noqa: PLC0415

    store.load_credentials_into_env()
    return get_provider(provider_id)


def _summary(cls: type[DeployProvider]) -> ProviderSummary:
    inst = cls()
    # An adapter may need more than credentials (Modal deploys through its own
    # SDK). Optional, so adapters without a requirement say nothing.
    req = getattr(inst, "requirements", None)
    req_ok, req_hint = (True, "")
    if callable(req):
        try:
            req_ok, req_hint = req()
        except Exception:  # noqa: BLE001 - a broken check never hides a provider
            req_ok, req_hint = True, ""
    return ProviderSummary(
        id=cls.id,
        display_name=cls.display_name,
        configured=bool(inst.configured()),
        credential_fields=list(cls.credential_fields),
        engines=list(cls.engines),
        console_url=cls.console_url,
        scale_to_zero=bool(cls.scale_to_zero),
        public_by_default=bool(cls.public_by_default),
        requirements_ok=bool(req_ok),
        requirements_hint=str(req_hint or ""),
    )


def _auth_headers(dep: Deployment) -> dict[str, str]:
    from ._http import env_expand  # noqa: PLC0415

    headers: dict[str, str] = {}
    if dep.auth_env:
        val = (os.environ.get(dep.auth_env) or "").strip()
        if val:
            headers["Authorization"] = f"Bearer {val}"
    for k, v in (dep.auth_headers or {}).items():
        expanded = env_expand(str(v))
        if expanded:
            headers[str(k)] = expanded
    return headers


async def _refresh(dep: Deployment, provider: DeployProvider | None = None) -> Deployment:
    from . import store  # noqa: PLC0415

    prov = provider or _provider(dep.provider)
    try:
        fresh = await prov.status(dep)
    except NotSupported:
        return dep
    store.upsert(fresh)
    return fresh


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


async def providers() -> list[ProviderSummary]:
    """Every registered deploy provider with whether it's configured (no network)."""
    _import_builtin_providers()
    from . import store  # noqa: PLC0415
    from .base import DEPLOY_PROVIDERS  # noqa: PLC0415

    store.load_credentials_into_env()
    return [_summary(cls) for _id, cls in sorted(DEPLOY_PROVIDERS.items())]


async def save_credentials(provider_id: str, values: dict[str, str]) -> Account:
    """Persist credential values (keyed by ``CredentialField.env``) into the
    user settings env (same mechanism as ``catalog.set_key``), export them
    into ``os.environ``, then :func:`validate` and return the account."""
    from . import store  # noqa: PLC0415

    get_provider(provider_id)  # validates the id before anything is written
    store.save_credentials(provider_id, values)
    return await validate(provider_id)


async def validate(provider_id: str) -> Account:
    """Network check of the saved credentials."""
    prov = _provider(provider_id)
    if not prov.configured():
        missing = [f.env for f in prov.credential_fields if f.required and not (os.environ.get(f.env) or "").strip()]
        return Account(ok=False, provider=provider_id,
                       message="missing " + ", ".join(missing) if missing else "not configured")
    try:
        return await prov.validate_credentials()
    except DeployError as e:
        return Account(ok=False, provider=provider_id, message=str(e) + (f" — {e.hint}" if e.hint else ""))


async def gpus(provider_id: str, *, min_vram_gb: int | None = None) -> list[GpuSpec]:
    """Provider GPU catalogue, cheapest first, optionally filtered by total VRAM."""
    prov = _provider(provider_id)
    rows = list(await prov.list_gpus())
    if min_vram_gb:
        rows = [g for g in rows if g.total_vram_gb >= min_vram_gb]
    rows.sort(key=lambda g: (g.price_per_hour if g.price_per_hour is not None else 1e9, g.total_vram_gb))
    return rows


async def inspect_model(model: str, *, hf_token: str | None = None) -> ModelInfo:
    """Pre-flight facts: architectures, params, dtype, gated, license, vLLM
    servability and a VRAM estimate. ``model`` is an HF id or ``ollama:<tag>``."""
    from . import hf_hub, preflight  # noqa: PLC0415

    model = (model or "").strip()
    if not model:
        raise DeployError("model id required", hint="e.g. Qwen/Qwen3-8B")
    if model.startswith("ollama:"):
        tag = model[len("ollama:"):]
        manifest = await hf_hub.ollama_manifest(tag)
        return preflight.ollama_model_info(tag, manifest)
    meta = await hf_hub.model_info(model, token=hf_token)
    config = await hf_hub.fetch_config(model, token=hf_token)
    return preflight.build_model_info(meta, config)


async def search_models(query: str = "", *, limit: int = 25, sort: str = "trending",
                        source: str = "auto") -> list[ModelInfo]:
    """Search the HF Hub for text-generation models (plus the curated list
    from the catalog when ``query`` is empty).

    ``source`` narrows it: ``"curated"`` is the hand-picked list and nothing
    else (never topped up from whatever is trending — that is how gpt2 and
    abliterated fine-tunes ended up under a "Curated" heading); ``"hub"`` is
    the Hub alone, trending when there is no query. ``"auto"`` keeps the
    mixed behaviour the CLI relies on."""
    from . import hf_hub, preflight  # noqa: PLC0415

    limit = max(1, int(limit))
    out: list[ModelInfo] = []
    seen: set[str] = set()
    if source == "curated" and not (query or "").strip():
        return [ModelInfo(id=mid, source="hf", params_b=preflight._params_from_name(mid),
                          tags=("curated",), reason="") for mid in preflight.CURATED_MODELS[:limit]]
    if source != "hub" and not (query or "").strip():
        for mid in preflight.CURATED_MODELS[:limit]:
            out.append(ModelInfo(id=mid, source="hf", params_b=preflight._params_from_name(mid),
                                 tags=("curated",), reason=""))
            seen.add(mid.lower())
    remaining = limit - len(out)
    if remaining > 0:
        rows = await hf_hub.search(query, limit=remaining if query.strip() else limit, sort=sort)
        for row in rows:
            info = preflight.build_model_info(row)
            if info.id and info.id.lower() not in seen:
                seen.add(info.id.lower())
                out.append(info)
            if len(out) >= limit:
                break
    return out[:limit]


async def find_models(
    query: str,
    *,
    provider_id: str | None = None,
    limit: int = 24,
    hf_token: str | None = None,
    use_agent: bool = True,
    progress: ProgressFn | None = None,
) -> "SmartSearchResult":
    """Natural-language model search: ask a question, get grouped answers.

    ``query`` is what a person typed — "best open coding model under 40B,
    2025", "cheapest model that fits 24GB", "strongest reasoning model I can
    run on an A100". The reply is a
    :class:`~mantis_agent.deploy.smart_search.SmartSearchResult`: an
    ``interpretation`` line, labelled ``groups`` of
    :class:`~mantis_agent.deploy.base.ModelInfo` (each with a one-line
    ``reason`` and a ``best`` id), the ``columns`` worth rendering for *this*
    question, the ``filters`` that were derived (editable by the UI), whether
    the ``agent`` or the ``rules`` tier answered, and ``notes`` for every
    caveat.

    Two tiers. With ``use_agent`` (the default) and a model already configured
    (``deploy connect``, ``mantis setup``, or a saved auth method) an agent on
    *that* model interprets the question and groups a pre-fetched candidate
    list, answering through a response schema — never parsed prose. With no
    model configured, ``use_agent=False``, or any agent failure, the same
    question is answered by mantis's deterministic parser + grouping, and a
    note says so.

    Nothing here invents a benchmark. Groups are justified from Hub metadata
    (params, dtype, tags, licence, downloads, likes, ``lastModified``,
    architectures, ``gated``), mantis's VRAM estimate, and — when
    ``provider_id`` is given — that provider's GPU catalogue and prices, which
    also unlocks the ``fit`` and ``price`` columns.
    """
    from .smart_search import find_models as _find  # noqa: PLC0415

    return await _find(query, provider_id=provider_id, limit=limit, hf_token=hf_token,
                       use_agent=use_agent, progress=progress)


async def fit(info: ModelInfo, candidates: list[GpuSpec], *, context_len: int | None = None) -> list[tuple[GpuSpec, str]]:
    """Which GPUs fit this model: ``[(gpu, verdict)]`` where verdict is
    "fits", "tight" (<15% headroom) or "no" with the reason. Sorted best-value first."""
    from . import preflight  # noqa: PLC0415

    return preflight.fit_verdicts(info, candidates, context_len=context_len)


async def _resolve_gpu(prov: DeployProvider, gpu: GpuSpec | str) -> GpuSpec:
    if isinstance(gpu, GpuSpec):
        return gpu
    want = (gpu or "").strip()
    if not want:
        raise DeployError("gpu required", hint=f"pick one from `mantis-agent deploy gpus {prov.id}`")
    rows = list(await prov.list_gpus())
    for g in rows:
        if g.provider_id == want:
            return g
    lowered = want.lower()
    for g in rows:
        if g.provider_id.lower() == lowered or g.display.lower() == lowered or g.label.lower() == lowered:
            return g
    # ``<id>x<count>`` / ``<id>:<count>`` shorthand for multi-GPU shapes.
    for sep in ("x", ":", "×"):
        head, s, tail = want.rpartition(sep)
        if s and tail.isdigit() and head:
            for g in rows:
                if g.provider_id.lower() == head.lower():
                    return GpuSpec(provider_id=g.provider_id, family=g.family, vram_gb=g.vram_gb,
                                   count=int(tail), price_per_hour=(g.price_per_hour * int(tail)) if g.price_per_hour else None,
                                   available=g.available, region=g.region, label=f"{g.display} ×{tail}")
    known = ", ".join(g.provider_id for g in rows[:10])
    raise DeployError(f"{prov.id} has no GPU {want!r}", hint=f"known: {known}", provider=prov.id)


async def deploy(
    provider_id: str,
    model: str,
    *,
    gpu: GpuSpec | str,
    engine: Engine = "vllm",
    opts: DeployOpts | None = None,
    wait: bool = True,
    progress: ProgressFn | None = None,
    on_event: EventFn | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Deployment:
    """Pre-flight, deploy, persist to the store, optionally wait until ready.
    ``gpu`` may be a provider GPU id string. Progress lines go to ``progress``;
    structured stage / created / heartbeat events go to ``on_event``.
    ``cancelled`` is polled at the one point before anything is rented and
    every second of the boot wait; when it says yes the deploy stops with a
    DeployError (what was already created is the caller's to tear down)."""
    import asyncio  # noqa: PLC0415
    import time  # noqa: PLC0415

    from . import preflight, store  # noqa: PLC0415

    await _emit(on_event, "stage", {"stage": "prepare"})

    opts = opts or DeployOpts()
    model = (model or "").strip()
    prov = _provider(provider_id)
    if not prov.configured():
        missing = ", ".join(f.env for f in prov.credential_fields if f.required)
        raise DeployError(f"{provider_id} is not configured ({missing} missing)",
                          hint=f"`mantis-agent deploy creds {provider_id} --set {missing.split(', ')[0]}=...`",
                          provider=provider_id)
    if engine not in prov.engines:
        raise DeployError(f"{provider_id} does not serve engine {engine!r}",
                          hint=f"supported: {', '.join(prov.engines)}", provider=provider_id)
    gpu_spec = await _resolve_gpu(prov, gpu)
    await _say(progress, f"GPU: {gpu_spec.display} ({gpu_spec.total_vram_gb} GB"
               + (f", ${gpu_spec.price_per_hour:.2f}/h" if gpu_spec.price_per_hour else "") + ")")

    force = bool(opts.extra.get("force"))
    if model.startswith("ollama:") and engine != "llamacpp":
        raise DeployError("Ollama library models are GGUF and need engine=llamacpp",
                          hint="pick the HF safetensors repo for vLLM, or --engine llamacpp", provider=provider_id)
    await _say(progress, f"Inspecting {model} on the Hub…")
    try:
        info = await inspect_model(model, hf_token=opts.hf_token)
    except DeployError as e:
        if not force:
            raise
        await _say(progress, f"pre-flight skipped (--force): {e}")
        info = ModelInfo(id=model, source="hf")
    preflight.check_gated(info, opts.hf_token)
    if info.vllm_ok is False and engine in ("vllm", "sglang") and not force:
        raise DeployError(f"{model} is not servable by {engine}: {info.reason}",
                          hint="choose another engine, or pass extra={'force': True} / --force to try anyway",
                          provider=provider_id)
    if info.vllm_ok is None and info.reason:
        await _say(progress, f"note: {info.reason}")
    desc = []
    if info.params_b:
        desc.append(f"{info.params_b:g}B params")
    if info.dtype:
        desc.append(info.dtype)
    if info.est_vram_gb:
        desc.append(f"~{info.est_vram_gb:g} GB VRAM at {opts.max_model_len or 'default'} ctx")
    if desc:
        await _say(progress, "Model: " + ", ".join(desc))
    if gpu_spec.total_vram_gb and info.est_vram_gb is not None:
        (_g, verdict), = preflight.fit_verdicts(info, [gpu_spec], context_len=opts.max_model_len)
        if verdict.startswith("no"):
            if not force:
                raise DeployError(f"{model} does not fit {gpu_spec.display}: {verdict[3:].strip()}",
                                  hint=f"`mantis-agent deploy gpus {provider_id} --min-vram {int(info.est_vram_gb / 0.9) + 1}` lists cards that do; --force overrides",
                                  provider=provider_id)
            await _say(progress, f"warning (--force): {verdict}")
        elif verdict.startswith("tight"):
            await _say(progress, f"warning: {verdict}")
        else:
            await _say(progress, "Fit: ok")
    if prov.public_by_default:
        await _say(progress, "note: this provider's endpoints are reachable without our auth header — the engine's --api-key is the only lock")

    if cancelled and cancelled():
        raise DeployError("cancelled before anything was created", provider=provider_id)
    # the adapter sizes its startup timeout to the model; the wait below must
    # outlast it, or the manager gives up on a boot that is still going fine
    opts.extra.setdefault("boot_budget_s", boot_budget_s(info.est_vram_gb))
    # A model that takes 30 minutes to wake must not scale to zero after five
    # idle minutes — every pause would cost a cold start. Unless the caller
    # set the idle timeout, keep it warm at least as long as a boot takes.
    if opts.idle_timeout_s == 300 and opts.extra["boot_budget_s"] > 600:
        opts.idle_timeout_s = int(opts.extra["boot_budget_s"])
        await _say(progress, f"Idle timeout raised to {opts.idle_timeout_s // 60} min — a cold start of this model takes about that long")
    parser = reasoning_parser_for(info) if engine == "vllm" else None
    if parser and "--reasoning-parser" not in (opts.extra_engine_args or []):
        opts.extra_engine_args = list(opts.extra_engine_args or []) + ["--reasoning-parser", parser]
        await _say(progress, f"Reasoning model: vLLM will split its thinking out (--reasoning-parser {parser})")
    tparser = tool_parser_for(info) if engine == "vllm" else None
    if tparser and "--tool-call-parser" not in (opts.extra_engine_args or []):
        opts.extra_engine_args = list(opts.extra_engine_args or []) + [
            "--enable-auto-tool-choice", "--tool-call-parser", tparser]
        await _say(progress, f"Tool calling on — mantis sends tools with every request (--tool-call-parser {tparser})")
    elif engine == "vllm" and not tparser:
        await _say(progress, "note: no known tool-call parser for this architecture — the agent's tool calls will be refused")
    await _emit(on_event, "stage", {"stage": "create"})
    await _say(progress, f"Creating {engine} deployment on {prov.display_name}…")
    dep = await prov.deploy(model, gpu_spec, engine, opts)
    dep.opts.hf_token = None  # never keep the token on the object we persist
    if cancelled and cancelled():
        # cancelled while the provider was creating it: nobody else knows it
        # exists yet, so this is the only place that can stop it billing
        await _say(progress, f"Cancelled — deleting {dep.provider}:{dep.id} it had just created…")
        try:
            await prov.delete(dep)
        finally:
            store.upsert(dep)
            store.mark_deleted(dep.id)
        raise DeployError("cancelled — the endpoint it had just created was deleted", provider=provider_id)
    store.upsert(dep)
    await _say(progress, f"Created {dep.provider}:{dep.id} ({dep.status})")
    # from here on something is billable: a UI must be able to show it, read
    # its logs and tear it down even if the wait below never finishes
    await _emit(on_event, "created", {"provider": dep.provider, "id": dep.id})
    if wait:
        await _emit(on_event, "stage", {"stage": "boot"})
        await _say(progress, "Waiting for the endpoint to come up (this can take several minutes)…")
        # The provider's wait loop is silent for as long as the boot takes —
        # minutes. Run it beside a heartbeat so a watcher can tell "still
        # booting" from "hung".
        t0 = time.monotonic()
        waiter = asyncio.ensure_future(
            prov.wait_ready(dep, timeout_s=int(opts.extra.get("wait_timeout_s",
                                                              int(opts.extra.get("boot_budget_s", 1200)) + 300))))
        next_beat = HEARTBEAT_S
        try:
            while True:
                done, _ = await asyncio.wait({waiter}, timeout=min(1.0, HEARTBEAT_S))
                if done:
                    break
                if cancelled and cancelled():
                    raise DeployError("cancelled while it was starting", provider=provider_id)
                if time.monotonic() - t0 >= next_beat:
                    next_beat += HEARTBEAT_S
                    await _emit(on_event, "heartbeat", {"elapsed_s": round(time.monotonic() - t0)})
            dep = waiter.result()
        except DeployError as e:
            # a cancel means someone is tearing this down right now — writing
            # it back as "starting" would race the delete and resurrect it
            if not (cancelled and cancelled()):
                dep.message = str(e)
                store.upsert(dep)
            raise
        finally:
            if not waiter.done():
                waiter.cancel()
        store.upsert(dep)
        await _say(progress, f"Ready: {dep.endpoint_url} (model={dep.served_model_name})")
        await _emit(on_event, "stage", {"stage": "ready"})
    return dep


async def status(dep_id: str, *, refresh: bool = True) -> Deployment:
    """Stored deployment, refreshed from the provider when ``refresh``."""
    from . import store  # noqa: PLC0415

    dep = store.find(dep_id)
    if not refresh or dep.status == "deleted":
        return dep
    return await _refresh(dep)


async def list_deployments(*, refresh: bool = False, provider_id: str | None = None) -> list[Deployment]:
    """All stored deployments (optionally refreshed and filtered)."""
    from . import store  # noqa: PLC0415

    deps = store.list_deployments(provider_id=provider_id)
    if not refresh:
        return deps
    by_provider: dict[str, list[Deployment]] = {}
    for d in deps:
        by_provider.setdefault(d.provider, []).append(d)
    out: list[Deployment] = []
    provider_ids = [provider_id] if provider_id else sorted({*by_provider} | {
        pid for pid, cls in _registered().items() if cls().configured()})
    for pid in provider_ids:
        try:
            prov = _provider(pid)
        except DeployError:
            out.extend(by_provider.get(pid, []))
            continue
        if not prov.configured():
            out.extend(by_provider.get(pid, []))
            continue
        remote: dict[str, Deployment] = {}
        try:
            for r in await prov.list_deployments():
                remote[r.id] = r
        except (DeployError, NotSupported):
            remote = {}
            for d in by_provider.get(pid, []):
                out.append(await _refresh(d, prov))
            continue
        for d in by_provider.get(pid, []):
            r = remote.pop(d.id, None)
            if r is None:
                d.status = "deleted"
                d.message = "no longer present on the provider"
                d.updated_at = utcnow()
                store.upsert(d)
                continue
            out.append(await _refresh(d, prov))
        for r in remote.values():  # adopted: created outside mantis
            if not r.model:
                continue
            r.message = r.message or "adopted from the provider"
            store.upsert(r)
            out.append(r)
    out = [d for d in out if d.status != "deleted"]
    out.sort(key=lambda d: d.created_at, reverse=True)
    return out


def _registered() -> dict[str, type[DeployProvider]]:
    _import_builtin_providers()
    from .base import DEPLOY_PROVIDERS  # noqa: PLC0415

    return dict(DEPLOY_PROVIDERS)


def logs(dep_id: str, *, tail: int = 200) -> AsyncIterator[str]:
    """Async iterator of log lines; raises NotSupported where the provider has none."""
    from . import store  # noqa: PLC0415

    dep = store.find(dep_id)
    prov = _provider(dep.provider)
    return prov.logs(dep, tail=tail)


#: How long "try it" waits for an answer. A scaled-to-zero endpoint has to wake
#: a replica first, which on most providers is a minute or three.
TRY_TIMEOUT_S = 240.0


async def try_endpoint(dep_id: str, prompt: str = "Say hello in one short sentence.", *,
                       max_tokens: int = 256) -> dict[str, Any]:
    """Send one chat completion to a deployment and time it — the proof that
    it actually answers. Returns ``{"reply", "model", "latency_s",
    "completion_tokens", "tokens_per_s"}``; ``tokens_per_s`` is ``None`` when
    the engine reports no usage. Raises DeployError with a hint on refusal,
    timeout or a malformed answer."""
    import time  # noqa: PLC0415

    from . import store  # noqa: PLC0415

    dep = store.find(dep_id)
    if dep.status == "deleted":
        raise DeployError(f"{dep.provider}:{dep.id} was deleted")
    if not dep.endpoint_url:
        raise DeployError(f"{dep.provider}:{dep.id} has no endpoint yet ({dep.status})",
                          hint="wait for it to finish starting")
    headers = {**_auth_headers(dep), "X-Scale-Up-Timeout": "600", "Content-Type": "application/json"}
    if dep.auth_env and "Authorization" not in headers:
        raise DeployError(f"${dep.auth_env} is not set, so the endpoint cannot be authenticated",
                          hint=f"`mantis-agent deploy creds {dep.provider} --set {dep.auth_env}=...`")
    body = {"model": dep.served_model_name or dep.model,
            "messages": [{"role": "user", "content": (prompt or "").strip()[:2000] or "Say hello."}],
            "max_tokens": max(1, min(int(max_tokens), 512)), "temperature": 0.7}
    url = f"{dep.endpoint_url.rstrip('/')}/chat/completions"
    from ._http import sleep  # noqa: PLC0415

    # A scaled-to-zero endpoint answers 502/503 ("no upstreams available")
    # while a replica wakes — that is a cold start, not a failure. Keep asking
    # until it answers or the budget is spent, and report how long it took.
    t0 = time.monotonic()
    cold = 0.0
    while True:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=TRY_TIMEOUT_S,
                                                               write=15.0, pool=15.0)) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as e:
            raise DeployError(f"no answer after {int(TRY_TIMEOUT_S)}s",
                              hint="a cold replica can take longer to wake — try again in a minute",
                              provider=dep.provider) from e
        except httpx.HTTPError as e:
            raise DeployError(f"couldn't reach the endpoint ({type(e).__name__})", provider=dep.provider) from e
        if resp.status_code not in (502, 503, 504):
            break
        cold = time.monotonic() - t0
        if cold >= TRY_TIMEOUT_S:
            raise DeployError(f"still waking after {int(cold)}s (HTTP {resp.status_code})",
                              hint="the replica is cold-starting — big models take minutes; try again shortly",
                              provider=dep.provider)
        await sleep(5.0)
    elapsed = time.monotonic() - t0 - cold        # the answer's own time, not the wake
    if resp.status_code in (401, 403):
        raise DeployError(f"endpoint refused our credentials [HTTP {resp.status_code}]",
                          hint=f"${dep.auth_env or 'the auth header'} is wrong or expired for {dep.provider}",
                          provider=dep.provider)
    if resp.status_code != 200:
        raise DeployError(f"endpoint answered HTTP {resp.status_code}", hint=resp.text[:200] or None,
                          provider=dep.provider)
    try:
        data = resp.json()
        msg = data["choices"][0]["message"]
        reply = msg.get("content") or ""
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise DeployError("the endpoint answered, but not with a chat completion",
                          hint=resp.text[:200] or None, provider=dep.provider) from e
    # A reasoning model may spend the whole budget thinking: the answer is
    # empty but reasoning_content is not. That still proves it works — say
    # so, and show the thinking rather than a blank.
    thinking = (msg.get("reasoning_content") or msg.get("reasoning") or "") if isinstance(msg, dict) else ""
    thinking_only = not reply.strip() and bool(str(thinking).strip())
    if thinking_only:
        reply = "(still thinking when the token budget ran out) " + str(thinking).strip()
    toks = ((data.get("usage") or {}).get("completion_tokens")) if isinstance(data, dict) else None
    return {"reply": str(reply).strip(), "thinking_only": thinking_only,
            "model": data.get("model") or body["model"],
            "latency_s": round(elapsed, 2), "cold_start_s": round(cold, 1) if cold else None,
            "completion_tokens": toks,
            "tokens_per_s": round(toks / elapsed, 1) if toks and elapsed > 0 else None}


def wake_budget_s(dep: Deployment) -> int:
    """How long ``connect`` may wait for a scaled-to-zero replica to wake: the
    boot budget the deploy was given (weights + compile), plus a margin."""
    try:
        return int((dep.opts.extra or {}).get("boot_budget_s") or 0) + 300 or int(CONNECT_TIMEOUT_S)
    except (TypeError, ValueError, AttributeError):
        return int(CONNECT_TIMEOUT_S)


async def connect(dep_id: str, *, set_current: bool = True, timeout_s: float | None = None,
                  progress: ProgressFn | None = None) -> dict[str, Any]:
    """Verify ``GET {endpoint}/models`` answers (retrying cold-start 503s),
    then make it the current model/backend for the SDK and terminal
    (``catalog.set_last_model`` + auth env). Returns ``{"model", "backend",
    "api_key_env", "headers"}`` — exactly what ``MantisAgentOptions`` needs.
    A scaled-to-zero endpoint is woken and waited for up to ``timeout_s``
    (default: the deployment's own wake budget), with progress lines."""
    from . import store  # noqa: PLC0415
    from ._http import sleep  # noqa: PLC0415

    dep = store.find(dep_id)
    if dep.status == "deleted":
        raise DeployError(f"{dep.provider}:{dep.id} was deleted", hint="`mantis-agent deploy up` a new one")
    prov = _provider(dep.provider)
    if not dep.endpoint_url:
        dep = await _refresh(dep, prov)
    if not dep.endpoint_url:
        raise DeployError(f"{dep.provider}:{dep.id} has no endpoint URL yet ({dep.status}: {dep.message})",
                          hint="wait for it to finish starting: `mantis-agent deploy status " + dep.id + "`")
    headers = _auth_headers(dep)
    if dep.auth_env and "Authorization" not in headers:
        raise DeployError(f"${dep.auth_env} is not set, so the endpoint cannot be authenticated",
                          hint=f"`mantis-agent deploy creds {dep.provider} --set {dep.auth_env}=...`")
    url = f"{dep.endpoint_url.rstrip('/')}/models"
    probe_headers = {**headers, "X-Scale-Up-Timeout": "600"}
    deadline = float(timeout_s) if timeout_s else float(wake_budget_s(dep))
    waited = 0.0
    last = ""
    if dep.status == "scaled_to_zero":
        await _say(progress, f"It scaled to zero — waking a replica (a cold start can take up to {int(deadline // 60)} min)…")
    else:
        await _say(progress, "Checking the endpoint answers…")
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=125.0, write=10.0, pool=10.0)) as client:
        while True:
            try:
                resp = await client.get(url, headers=probe_headers)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
                resp = None
            if resp is not None:
                if resp.status_code == 200:
                    break
                if resp.status_code in (401, 403):
                    raise DeployError(f"endpoint refused our credentials [HTTP {resp.status_code}]",
                                      hint=f"${dep.auth_env or 'the auth header'} is wrong or expired for {dep.provider}",
                                      provider=dep.provider)
                if resp.status_code == 404 and dep.provider != "runpod":
                    raise DeployError(f"{url} answered 404", hint="the endpoint may have been deleted; `mantis-agent deploy ls --refresh`")
                last = f"HTTP {resp.status_code}"
            if waited >= deadline:
                raise DeployError(f"{url} not ready after {int(deadline)}s ({last})",
                                  hint="a cold replica can take longer — retry `deploy connect` in a minute, or check `deploy status`",
                                  provider=dep.provider)
            await sleep(CONNECT_INTERVAL_S)
            waited += CONNECT_INTERVAL_S
            if int(waited) % 30 == 0:
                await _say(progress, f"still waking… {int(waited // 60)}:{int(waited % 60):02d} ({last})")
    if dep.status not in ("running",):
        dep.status = "running"
        dep.message = "answered /models"
        store.upsert(dep)
    await _say(progress, "Awake — it answers. Pointing mantis at it…" if waited else "It answers. Pointing mantis at it…")
    extra_headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
    result: dict[str, Any] = {
        "model": dep.served_model_name,
        "backend": dep.endpoint_url,
        "api_key_env": dep.auth_env,
        "headers": extra_headers,
    }
    if set_current:
        from .. import catalog  # noqa: PLC0415

        catalog.set_last_model(dep.served_model_name, dep.endpoint_url)
        catalog.push_recent_model(dep.served_model_name)
        os.environ["MANTIS_AGENT_BASE_URL"] = dep.endpoint_url
        os.environ["MANTIS_AGENT_MODEL"] = dep.served_model_name
        if extra_headers:
            # Non-bearer auth (Modal-Key / Modal-Secret) reaches ``Agent`` through
            # ``$MANTIS_AGENT_EXTRA_HEADERS`` (a JSON object) — the terminal reads it.
            import json as _json  # noqa: PLC0415

            os.environ["MANTIS_AGENT_EXTRA_HEADERS"] = _json.dumps(extra_headers)
        else:
            os.environ.pop("MANTIS_AGENT_EXTRA_HEADERS", None)
        key = headers.get("Authorization", "")
        if key.startswith("Bearer "):
            # Same mechanism as ``serve.connect_selfhost``: the generic key
            # slot is what the openai_compat adapter reads for a custom URL.
            os.environ["MANTIS_AGENT_API_KEY"] = key[len("Bearer "):]
            try:
                from ..settings import update_setting_source  # noqa: PLC0415

                update_setting_source("user", {"env": {"MANTIS_AGENT_API_KEY": key[len("Bearer "):]}})
            except Exception:  # noqa: BLE001 — a read-only settings file must not fail connect
                result["warning"] = "model set but MANTIS_AGENT_API_KEY could not be saved to settings"
    return result


async def cost(dep_id: str) -> CostEstimate:
    """Hourly / idle cost estimate for a stored deployment (no refresh)."""
    from . import store  # noqa: PLC0415

    dep = store.find(dep_id)
    prov = _provider(dep.provider)
    return await prov.cost(dep)


async def teardown(dep_id: str, *, progress: ProgressFn | None = None) -> None:
    """Delete on the provider and mark deleted in the store."""
    from . import store  # noqa: PLC0415

    dep = store.find(dep_id)
    if dep.status == "deleted":
        await _say(progress, f"{dep.provider}:{dep.id} already deleted")
        return
    prov = _provider(dep.provider)
    await _say(progress, f"Deleting {dep.provider}:{dep.id} ({dep.model})…")
    await prov.delete(dep)
    store.mark_deleted(dep.id)
    await _say(progress, "Deleted.")
