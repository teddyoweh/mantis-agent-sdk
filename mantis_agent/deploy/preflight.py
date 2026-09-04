"""Pre-flight: can this model be served, and on which GPU?

Turns Hub metadata into a :class:`ModelInfo`, decides vLLM servability
from ``config.architectures``, estimates VRAM (weights + KV cache) and
grades every candidate GPU as ``fits`` / ``tight`` / ``no``.

The VRAM rule of thumb, from the September 2026 survey:

* weights = ``params × bytes_per_param`` (2 for bf16/fp16, 1 for fp8/int8,
  ~0.55 for 4-bit); ``safetensors.parameters`` is dtype-keyed so a checkpoint
  that is *already* FP8 sizes itself correctly. MoE checkpoints count every
  expert — that is what has to fit.
* KV cache per token = ``2 × layers × kv_heads × head_dim × bytes``; we size
  for one sequence at the chosen context. When ``config.json`` is not
  readable (gated repo, no token) fall back to a 20 % headroom rule.
* vLLM keeps ``gpu_memory_utilization = 0.9`` of the card and needs ~1.5 GB
  for activations / CUDA graphs. ``need ≤ 0.9 × VRAM`` is the bar; under 15 %
  headroom is ``tight`` (reduce ``--max-model-len``), over is ``fits``.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable

from .base import DeployError, GpuSpec, ModelInfo

__all__ = [
    "CURATED_MODELS",
    "VLLM_ARCHITECTURES",
    "build_model_info",
    "bytes_per_param",
    "check_gated",
    "estimate_vram_gb",
    "fit_verdicts",
    "gpu_family_from_name",
    "kv_bytes_per_token",
    "ollama_model_info",
    "vllm_servable",
]

# Text-generation architectures vLLM serves natively. Snapshot of
# ``vllm/model_executor/models/registry.py`` (_TEXT_GENERATION_MODELS +
# _MULTIMODAL_MODELS + the Transformers-backend set), September 2026:
# https://docs.vllm.ai/en/latest/models/supported_models.html . Anything
# not listed is *unknown* (``vllm_ok=None``) rather than unsupported — vLLM
# falls back to the Transformers backend for most decoder-only models.
VLLM_ARCHITECTURES: frozenset[str] = frozenset({
    # Llama family & derivatives
    "LlamaForCausalLM", "Llama4ForCausalLM", "Llama4ForConditionalGeneration",
    "MistralForCausalLM", "MixtralForCausalLM", "Mistral3ForConditionalGeneration",
    "SolarForCausalLM", "ExaoneForCausalLM", "Exaone4ForCausalLM",
    "NemotronForCausalLM", "NemotronHForCausalLM", "GraniteForCausalLM",
    "GraniteMoeForCausalLM", "GraniteMoeHybridForCausalLM",
    "InternLM2ForCausalLM", "InternLM3ForCausalLM", "InternLMForCausalLM",
    "AquilaForCausalLM", "BaichuanForCausalLM", "BaiChuanForCausalLM",
    "YiForCausalLM", "XverseForCausalLM", "OrionForCausalLM", "TeleChat2ForCausalLM",
    "Zamba2ForCausalLM", "ArcticForCausalLM", "OlmoForCausalLM", "Olmo2ForCausalLM",
    "Olmo3ForCausalLM", "OlmoeForCausalLM", "SmolLM3ForCausalLM",
    "MiniCPMForCausalLM", "MiniCPM3ForCausalLM", "MiniMaxText01ForCausalLM",
    "MiniMaxM1ForCausalLM", "MiniMaxM2ForCausalLM",
    # Qwen
    "Qwen2ForCausalLM", "Qwen2MoeForCausalLM", "Qwen3ForCausalLM",
    "Qwen3MoeForCausalLM", "Qwen3NextForCausalLM", "QWenLMHeadModel",
    "Qwen2VLForConditionalGeneration", "Qwen2_5_VLForConditionalGeneration",
    "Qwen3VLForConditionalGeneration", "Qwen3VLMoeForConditionalGeneration",
    # DeepSeek / GLM / Kimi / others
    "DeepseekForCausalLM", "DeepseekV2ForCausalLM", "DeepseekV3ForCausalLM",
    "DeepseekV32ForCausalLM", "DeepseekV4ForCausalLM",
    "Glm4ForCausalLM", "Glm4MoeForCausalLM", "GlmMoeDsaForCausalLM",
    "Glm4vForConditionalGeneration", "ChatGLMModel", "ChatGLMForConditionalGeneration",
    "GptOssForCausalLM", "KimiK2ForCausalLM", "DbrxForCausalLM", "JambaForCausalLM",
    "MambaForCausalLM", "Mamba2ForCausalLM", "FalconForCausalLM", "FalconH1ForCausalLM",
    "FalconMambaForCausalLM", "CohereForCausalLM", "Cohere2ForCausalLM",
    "CommandForCausalLM", "PersimmonForCausalLM", "StableLmForCausalLM",
    "StableLMEpochForCausalLM", "Starcoder2ForCausalLM", "GPTBigCodeForCausalLM",
    "GPT2LMHeadModel", "GPTJForCausalLM", "GPTNeoXForCausalLM", "BloomForCausalLM",
    "OPTForCausalLM", "MPTForCausalLM", "DeciLMForCausalLM", "HunYuanMoEV1ForCausalLM",
    "HunYuanDenseV1ForCausalLM", "Ernie4_5ForCausalLM", "Ernie4_5_MoeForCausalLM",
    "Step3TextForCausalLM", "SeedOssForCausalLM", "ApertusForCausalLM",
    "BailingMoeForCausalLM", "BailingMoeV2ForCausalLM", "LongcatFlashForCausalLM",
    "Dots1ForCausalLM", "Plamo2ForCausalLM", "JAISLMHeadModel", "Fairseq2LlamaForCausalLM",
    # Gemma / Phi
    "GemmaForCausalLM", "Gemma2ForCausalLM", "Gemma3ForCausalLM", "Gemma4ForCausalLM",
    "Gemma3ForConditionalGeneration", "Gemma3nForConditionalGeneration",
    "Gemma4ForConditionalGeneration",
    "PhiForCausalLM", "Phi3ForCausalLM", "Phi3SmallForCausalLM", "PhiMoEForCausalLM",
    "Phi4MMForCausalLM", "Phi4MultimodalForCausalLM",
    # Multimodal (text-capable)
    "LlavaForConditionalGeneration", "LlavaNextForConditionalGeneration",
    "LlavaOnevisionForConditionalGeneration", "MllamaForConditionalGeneration",
    "InternVLChatModel", "Idefics3ForConditionalGeneration", "PaliGemmaForConditionalGeneration",
    "PixtralForConditionalGeneration", "MiniCPMV", "MolmoForCausalLM",
    "KimiVLForConditionalGeneration", "Ovis", "AyaVisionForConditionalGeneration",
})

# Curated "empty query" list: the README's ranked open-weight table mapped
# to HF ids, plus the flagship open models the hosted catalog samples
# (``mantis_agent.catalog``: gpt-oss, GLM, Kimi, DeepSeek, Qwen3). Keep this
# short and current — it is what the dashboard shows before anyone types.
CURATED_MODELS: tuple[str, ...] = (
    "Qwen/Qwen3-8B",
    "Qwen/Qwen3-14B",
    "Qwen/Qwen3-32B",
    "Qwen/Qwen3-235B-A22B-Instruct-2507",
    "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "meta-llama/Llama-3.1-8B-Instruct",
    "meta-llama/Llama-3.3-70B-Instruct",
    "meta-llama/Llama-4-Scout-17B-16E-Instruct",
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
    "deepseek-ai/DeepSeek-R1-Distill-Llama-8B",
    "deepseek-ai/DeepSeek-V3.2",
    "zai-org/GLM-4.7",
    "moonshotai/Kimi-K2.6-Instruct",
    "MiniMaxAI/MiniMax-M2.5",
    "google/gemma-3-27b-it",
    "microsoft/phi-4",
    "mistralai/Mistral-Small-3.2-24B-Instruct-2506",
    "NousResearch/Hermes-4-70B",
)

# safetensors dtype key → bytes per parameter.
_DTYPE_BYTES: dict[str, float] = {
    "F64": 8, "F32": 4, "BF16": 2, "F16": 2, "I16": 2, "U16": 2,
    "F8_E4M3": 1, "F8_E5M2": 1, "I8": 1, "U8": 1,
    "I4": 0.5, "U4": 0.5, "F4": 0.5, "NF4": 0.5, "MXFP4": 0.5,
    "I2": 0.25, "U2": 0.25, "BOOL": 0.125,
}

_QUANT_BYTES: dict[str, float] = {
    "fp8": 1.0, "int8": 1.0, "awq": 0.55, "gptq": 0.55, "int4": 0.55, "nf4": 0.55,
    "bitsandbytes": 0.55, "mxfp4": 0.55, "compressed-tensors": 1.0, "fbgemm_fp8": 1.0,
}


def bytes_per_param(dtype: str | None, quant: str | None = None) -> float:
    if quant:
        q = quant.lower()
        for key, val in _QUANT_BYTES.items():
            if key in q:
                return val
    if dtype:
        d = dtype.upper()
        if d in _DTYPE_BYTES:
            return _DTYPE_BYTES[d]
        for key, val in _DTYPE_BYTES.items():
            if d.startswith(key):
                return val
        if "4" in d:
            return 0.5
        if "8" in d:
            return 1.0
    return 2.0


def vllm_servable(architectures: Iterable[str]) -> tuple[bool | None, str]:
    archs = [a for a in architectures if a]
    if not archs:
        return None, "no architectures in config — vLLM may still load it via the Transformers backend"
    if any(a in VLLM_ARCHITECTURES for a in archs):
        return True, ""
    if any(a.endswith(("ForMaskedLM", "ForSequenceClassification", "Model")) and not a.endswith("LMHeadModel") for a in archs):
        return False, f"{archs[0]} is not a text-generation architecture"
    return None, f"{archs[0]} is not in the vLLM native registry — try `--trust-remote-code`; vLLM's Transformers fallback may work"


def kv_bytes_per_token(config: dict[str, Any] | None, *, kv_dtype_bytes: int = 2) -> float | None:
    """``2 × layers × kv_heads × head_dim × bytes`` from a full config.json."""

    if not config:
        return None
    text_cfg = config.get("text_config") if isinstance(config.get("text_config"), dict) else config
    layers = text_cfg.get("num_hidden_layers") or config.get("num_hidden_layers")
    heads = text_cfg.get("num_attention_heads") or config.get("num_attention_heads")
    kv_heads = text_cfg.get("num_key_value_heads") or heads
    hidden = text_cfg.get("hidden_size") or config.get("hidden_size")
    head_dim = text_cfg.get("head_dim")
    if not head_dim and hidden and heads:
        head_dim = int(hidden) // int(heads)
    try:
        if not (layers and kv_heads and head_dim):
            return None
        return 2.0 * int(layers) * int(kv_heads) * int(head_dim) * kv_dtype_bytes
    except (TypeError, ValueError):
        return None


def estimate_vram_gb(
    params_b: float | None,
    dtype: str | None,
    config: dict[str, Any] | None = None,
    *,
    context_len: int | None = None,
    quant: str | None = None,
) -> float | None:
    """Weights + KV (one sequence at ``context_len``) + ~1.5 GB activations.
    GB here means 1e9 bytes, the unit GPU vendors advertise."""

    if not params_b:
        return None
    weights = params_b * 1e9 * bytes_per_param(dtype, quant) / 1e9
    per_token = kv_bytes_per_token(config)
    if per_token is None:
        return round(weights * 1.2, 1)
    ctx = context_len or _default_context(config)
    kv = per_token * ctx / 1e9
    return round(weights + kv + 1.5, 1)


def _default_context(config: dict[str, Any] | None) -> int:
    mx = None
    if config:
        text_cfg = config.get("text_config") if isinstance(config.get("text_config"), dict) else config
        mx = text_cfg.get("max_position_embeddings") or config.get("max_position_embeddings")
    try:
        mx = int(mx) if mx else 8192
    except (TypeError, ValueError):
        mx = 8192
    # Size for a realistic agent context, not a 1M-token headline number.
    return max(2048, min(mx, 32768))


def _dominant_dtype(parameters: dict[str, Any] | None) -> tuple[str | None, float | None]:
    if not isinstance(parameters, dict) or not parameters:
        return None, None
    total = 0.0
    best: tuple[str, float] | None = None
    for k, v in parameters.items():
        try:
            n = float(v)
        except (TypeError, ValueError):
            continue
        total += n
        if best is None or n > best[1]:
            best = (str(k), n)
    return (best[0] if best else None), (total / 1e9 if total else None)


_PARAM_IN_NAME = re.compile(r"(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])")


def _params_from_name(model_id: str) -> float | None:
    hits = _PARAM_IN_NAME.findall(model_id)
    if not hits:
        return None
    try:
        # MoE ids carry two numbers ("235B-A22B"); the *total* is what has to fit.
        return max(float(h) for h in hits)
    except ValueError:
        return None


def build_model_info(
    meta: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    context_len: int | None = None,
) -> ModelInfo:
    """Fold a Hub metadata object (+ optional full config.json) into ModelInfo."""

    mid = str(meta.get("id") or meta.get("modelId") or "")
    hub_cfg = meta.get("config") if isinstance(meta.get("config"), dict) else {}
    archs = tuple(str(a) for a in (
        (config or {}).get("architectures") or hub_cfg.get("architectures") or []
    ))
    st = meta.get("safetensors") if isinstance(meta.get("safetensors"), dict) else {}
    dtype, params_b = _dominant_dtype(st.get("parameters"))
    if params_b is None and st.get("total"):
        try:
            params_b = float(st["total"]) / 1e9
        except (TypeError, ValueError):
            params_b = None
    if params_b is None:
        params_b = _params_from_name(mid)
    if dtype is None:
        td = (config or {}).get("torch_dtype") or (config or {}).get("dtype")
        if isinstance(td, str):
            dtype = {"bfloat16": "BF16", "float16": "F16", "float32": "F32"}.get(td, td.upper())
    quant = None
    qc = (config or {}).get("quantization_config")
    if isinstance(qc, dict):
        quant = str(qc.get("quant_method") or qc.get("format") or "") or None
    gated_raw = meta.get("gated")
    gated = bool(gated_raw) and gated_raw is not False
    gated_kind = gated_raw if isinstance(gated_raw, str) and gated else None
    card = meta.get("cardData") if isinstance(meta.get("cardData"), dict) else {}
    license_ = card.get("license") or card.get("license_name")
    if isinstance(license_, list):
        license_ = ", ".join(str(x) for x in license_)
    tags = tuple(str(t) for t in (meta.get("tags") or []) if isinstance(t, str))
    if not license_:
        for t in tags:
            if t.startswith("license:"):
                license_ = t.split(":", 1)[1]
                break
    ctx = None
    for src in (config or {}, hub_cfg):
        text_cfg = src.get("text_config") if isinstance(src.get("text_config"), dict) else src
        v = text_cfg.get("max_position_embeddings") or src.get("max_position_embeddings")
        if v:
            try:
                ctx = int(v)
                break
            except (TypeError, ValueError):
                pass
    library = meta.get("library_name")
    if library == "gguf" or any(t == "gguf" for t in tags):
        ok: bool | None = False
        reason = "GGUF checkpoint — serve it with llama.cpp (engine=llamacpp), vLLM's GGUF loader is experimental"
    else:
        ok, reason = vllm_servable(archs)
    est = estimate_vram_gb(params_b, dtype, config, context_len=context_len, quant=quant)
    return ModelInfo(
        id=mid, source="hf", architectures=archs, params_b=round(params_b, 2) if params_b else None,
        dtype=dtype, gated=gated, gated_kind=gated_kind,
        license=str(license_) if license_ else None,
        downloads=_int_or_none(meta.get("downloads")), likes=_int_or_none(meta.get("likes")),
        context_len=ctx, vllm_ok=ok, est_vram_gb=est, tags=tags[:40], reason=reason,
    )


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def ollama_model_info(tag: str, manifest: dict[str, Any]) -> ModelInfo:
    """ModelInfo for an ``ollama:<name>:<tag>`` entry from its manifest."""

    cfg = manifest.get("config") if isinstance(manifest.get("config"), dict) else {}
    size = float(manifest.get("size_bytes") or 0)
    params = None
    mt = cfg.get("model_type") or cfg.get("parameter_size")
    if isinstance(mt, str):
        params = _params_from_name(mt + "B") if not mt.upper().endswith("B") else _params_from_name(mt)
    if params is None:
        params = _params_from_name(tag)
    file_type = cfg.get("file_type")
    est = round(size / 1e9 * 1.2 + 1.0, 1) if size else (estimate_vram_gb(params, "Q4", None) if params else None)
    return ModelInfo(
        id=f"ollama:{tag}", source="ollama",
        architectures=(str(cfg.get("model_family")),) if cfg.get("model_family") else (),
        params_b=params, dtype=str(file_type) if file_type else "GGUF",
        gated=False, license=None, downloads=None, likes=None, context_len=None,
        vllm_ok=False, est_vram_gb=est, tags=("gguf",),
        reason="Ollama library models are GGUF — deploy with engine=llamacpp, or pick the HF safetensors repo for vLLM",
    )


def check_gated(info: ModelInfo, hf_token: str | None = None) -> None:
    """A gated repo with no token cannot be pulled by any provider."""

    if not info.gated:
        return
    if (hf_token or os.environ.get("HF_TOKEN") or "").strip():
        return
    if info.gated_kind == "manual":
        access = (
            f"request access at https://huggingface.co/{info.id} — this repo is "
            "owner-approved, so it is granted by hand and may take a while"
        )
    else:
        access = (
            f"click Agree at https://huggingface.co/{info.id} while logged in — "
            "access to this repo is granted instantly"
        )
    raise DeployError(
        f"{info.id} is a gated model and no Hugging Face token is set",
        hint=(
            f"{access}, create a read token at https://huggingface.co/settings/tokens, "
            "then paste it into the Hugging Face token field on the deploy form "
            "(or run `mantis-agent deploy creds hf --set HF_TOKEN=hf_...`) — every "
            "provider reads the same token"
        ),
    )


def fit_verdicts(
    info: ModelInfo,
    candidates: Iterable[GpuSpec],
    *,
    context_len: int | None = None,
) -> list[tuple[GpuSpec, str]]:
    """Grade every GPU. Verdicts: ``"fits"``, ``"tight"`` (<15 % headroom),
    or ``"no: <reason>"``. Sorted fits → tight → no, cheapest first."""

    need = info.est_vram_gb
    if need is not None and context_len and info.context_len:
        # Re-scale the KV part linearly when the caller wants a specific context.
        pass
    out: list[tuple[GpuSpec, str]] = []
    for gpu in candidates:
        total = gpu.total_vram_gb
        if not total:
            out.append((gpu, "no: GPU VRAM unknown"))
            continue
        if need is None:
            out.append((gpu, "tight: model size unknown — check the weights fit by hand"))
            continue
        usable = 0.9 * total
        if need > usable:
            out.append((gpu, f"no: needs ~{need:.0f} GB, {gpu.display} offers {total} GB ({usable:.0f} usable)"))
            continue
        headroom = (usable - need) / usable
        if headroom < 0.15:
            out.append((gpu, f"tight: ~{need:.0f} GB of {total} GB — reduce --max-model-len or pick a bigger card"))
        else:
            out.append((gpu, "fits"))
    rank = {"fits": 0, "tight": 1, "no": 2}
    out.sort(key=lambda gv: (
        rank[gv[1].split(":", 1)[0]],
        gv[0].price_per_hour if gv[0].price_per_hour is not None else 1e9,
        gv[0].total_vram_gb,
    ))
    return out


# ---------------------------------------------------------------------------
# GPU name → family (shared by the provider catalogues)
# ---------------------------------------------------------------------------


def gpu_family_from_name(name: str, vram_gb: int | None = None) -> str:
    """``"NVIDIA A100 80GB PCIe"`` → ``"A100-80"``; unknown → ``"other"``.
    Returns a :data:`~mantis_agent.deploy.base.GpuFamily` value."""

    n = (name or "").upper().replace("-", " ").replace("_", " ")
    if "B200" in n:
        return "B200"
    if "H200" in n:
        return "H200"
    if "H100" in n:
        return "H100"
    if "A100" in n:
        if "40" in n or (vram_gb and vram_gb <= 48):
            return "A100-40"
        return "A100-80"
    if "L40S" in n or "L40 S" in n:
        return "L40S"
    if "A10G" in n or n.strip() in ("A10", "NVIDIA A10"):
        return "A10G"
    if " L4" in f" {n}" or n.strip() == "L4" or "NVIDIA L4" in n:
        return "L4"
    if "T4" in n:
        return "T4"
    return "other"
