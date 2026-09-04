"""VRAM maths, vLLM servability, fit verdicts and the gated-model gate."""

from __future__ import annotations

import pytest

from mantis_agent.deploy import preflight
from mantis_agent.deploy.base import DeployError, GpuSpec, ModelInfo

# Qwen/Qwen3-8B as the Hub and config.json describe it (September 2026).
QWEN3_8B_META = {
    "id": "Qwen/Qwen3-8B",
    "gated": False,
    "downloads": 1_234_567,
    "likes": 2500,
    "tags": ["safetensors", "qwen3", "license:apache-2.0"],
    "cardData": {"license": "apache-2.0"},
    "config": {"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3"},
    "safetensors": {"parameters": {"BF16": 8_190_735_360}, "total": 8_190_735_360},
}
QWEN3_8B_CONFIG = {
    "architectures": ["Qwen3ForCausalLM"], "torch_dtype": "bfloat16",
    "num_hidden_layers": 36, "hidden_size": 4096, "num_attention_heads": 32,
    "num_key_value_heads": 8, "head_dim": 128, "max_position_embeddings": 40960,
}


def _gpu(pid: str, vram: int, price: float | None = None, count: int = 1) -> GpuSpec:
    return GpuSpec(provider_id=pid, family="other", vram_gb=vram, count=count, price_per_hour=price)


def test_8b_bf16_weights_are_about_16_gb():
    weights = 8.19 * preflight.bytes_per_param("BF16")
    assert 16.0 < weights < 17.0
    assert preflight.bytes_per_param("F8_E4M3") == 1
    assert preflight.bytes_per_param("BF16", quant="awq") == 0.55
    assert preflight.bytes_per_param(None) == 2.0


def test_kv_cache_formula_matches_the_survey():
    # 2 × 36 layers × 8 kv heads × 128 head_dim × 2 bytes = 147 456 B/token
    assert preflight.kv_bytes_per_token(QWEN3_8B_CONFIG) == 147_456
    assert preflight.kv_bytes_per_token({"num_hidden_layers": 80, "num_key_value_heads": 8,
                                         "hidden_size": 8192, "num_attention_heads": 64}) == 2 * 80 * 8 * 128 * 2
    assert preflight.kv_bytes_per_token(None) is None
    assert preflight.kv_bytes_per_token({"hidden_size": 1}) is None


def test_estimate_with_and_without_config():
    with_kv = preflight.estimate_vram_gb(8.19, "BF16", QWEN3_8B_CONFIG)
    # 16.38 weights + 4.83 KV (32k ctx, sized below the 40k max) + 1.5 activations
    assert with_kv == pytest.approx(22.7, abs=0.2)
    fallback = preflight.estimate_vram_gb(8.19, "BF16", None)
    assert fallback == pytest.approx(16.38 * 1.2, abs=0.1)
    small_ctx = preflight.estimate_vram_gb(8.19, "BF16", QWEN3_8B_CONFIG, context_len=4096)
    assert small_ctx < with_kv
    assert preflight.estimate_vram_gb(None, "BF16", None) is None


def test_build_model_info_from_hub_metadata():
    info = preflight.build_model_info(QWEN3_8B_META, QWEN3_8B_CONFIG)
    assert info.id == "Qwen/Qwen3-8B" and info.source == "hf"
    assert info.architectures == ("Qwen3ForCausalLM",)
    assert info.params_b == pytest.approx(8.19, abs=0.01)
    assert info.dtype == "BF16" and info.license == "apache-2.0"
    assert info.gated is False and info.vllm_ok is True and info.reason == ""
    assert info.context_len == 40960 and info.downloads == 1_234_567
    assert info.est_vram_gb == pytest.approx(22.7, abs=0.2)


def test_gated_manual_and_license_from_tags():
    meta = {**QWEN3_8B_META, "id": "meta-llama/Llama-3.1-8B-Instruct", "gated": "manual",
            "cardData": {}, "tags": ["license:llama3.1"]}
    info = preflight.build_model_info(meta, None)
    assert info.gated is True and info.license == "llama3.1"
    # No config.json (gated, no token) → headroom fallback still gives a number.
    assert info.est_vram_gb == pytest.approx(16.38 * 1.2, abs=0.1)


def test_vllm_servability_classes():
    assert preflight.vllm_servable(["LlamaForCausalLM"]) == (True, "")
    ok, reason = preflight.vllm_servable(["TotallyNewForCausalLM"])
    assert ok is None and "trust-remote-code" in reason
    ok, reason = preflight.vllm_servable(["BertForMaskedLM"])
    assert ok is False
    ok, _ = preflight.vllm_servable([])
    assert ok is None
    gguf = preflight.build_model_info({"id": "x/y-GGUF", "library_name": "gguf", "tags": ["gguf"]}, None)
    assert gguf.vllm_ok is False and "llama.cpp" in gguf.reason


def test_params_from_name_uses_total_for_moe():
    assert preflight._params_from_name("Qwen/Qwen3-235B-A22B-Instruct") == 235.0
    assert preflight._params_from_name("openai/gpt-oss-20b") == 20.0
    assert preflight._params_from_name("microsoft/phi-4") is None


def test_fit_verdicts_format_and_order():
    info = ModelInfo(id="x", source="hf", params_b=8.19, dtype="BF16", est_vram_gb=22.7)
    verdicts = preflight.fit_verdicts(info, [
        _gpu("small", 24, 0.69), _gpu("mid", 48, 1.9), _gpu("big", 80, 2.7), _gpu("cheap-big", 80, 2.5),
    ])
    kinds = [(g.provider_id, v.split(":", 1)[0]) for g, v in verdicts]
    # fits first, cheapest first within a tier; the 24 GB card is out.
    assert kinds == [("mid", "fits"), ("cheap-big", "fits"), ("big", "fits"), ("small", "no")]
    assert verdicts[-1][1].startswith("no: needs ~23 GB")
    for _g, v in verdicts:
        assert v == "fits" or v.split(":", 1)[0] in ("tight", "no")


def test_fit_tight_when_headroom_under_15_percent():
    info = ModelInfo(id="x", source="hf", est_vram_gb=40.0)
    ((gpu, verdict),) = preflight.fit_verdicts(info, [_gpu("l40s", 48)])
    assert verdict.startswith("tight:") and "max-model-len" in verdict
    ((_, unknown),) = preflight.fit_verdicts(ModelInfo(id="y", source="hf"), [_gpu("a", 80)])
    assert unknown.startswith("tight:")
    ((_, novram),) = preflight.fit_verdicts(info, [_gpu("raw", 0)])
    assert novram.startswith("no:")
    # Multi-GPU counts total VRAM.
    ((_, multi),) = preflight.fit_verdicts(ModelInfo(id="z", source="hf", est_vram_gb=100), [_gpu("h100", 80, count=2)])
    assert multi == "fits"
    ((_, edge),) = preflight.fit_verdicts(ModelInfo(id="z", source="hf", est_vram_gb=140), [_gpu("h100", 80, count=2)])
    assert edge.startswith("tight:")


def test_gated_without_token_is_a_hinted_error(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    info = ModelInfo(id="meta-llama/Llama-3.1-8B-Instruct", source="hf",
                     gated=True, gated_kind="manual")
    with pytest.raises(DeployError) as ei:
        preflight.check_gated(info)
    # The hint names the access path (this repo is owner-approved) and the
    # token env var every provider reads.
    assert "request access" in ei.value.hint and "HF_TOKEN" in ei.value.hint
    preflight.check_gated(info, hf_token="hf_x")  # explicit token passes
    monkeypatch.setenv("HF_TOKEN", "hf_env")
    preflight.check_gated(info)  # env token passes
    preflight.check_gated(ModelInfo(id="open/model", source="hf", gated=False))


def test_ollama_model_info_from_manifest():
    info = preflight.ollama_model_info("qwen3:8b", {
        "size_bytes": 5_200_000_000, "config": {"model_family": "qwen3", "model_type": "8.2B", "file_type": "Q4_K_M"},
    })
    assert info.id == "ollama:qwen3:8b" and info.source == "ollama"
    assert info.vllm_ok is False and info.dtype == "Q4_K_M" and info.params_b == 8.2
    assert info.est_vram_gb == pytest.approx(5.2 * 1.2 + 1.0, abs=0.1)


def test_gpu_family_from_name():
    f = preflight.gpu_family_from_name
    assert f("NVIDIA A100 80GB PCIe") == "A100-80"
    assert f("NVIDIA A100-SXM4-40GB") == "A100-40"
    assert f("nvidia-l4") == "L4" and f("NVIDIA L40S") == "L40S"
    assert f("H100 80GB HBM3") == "H100" and f("H200") == "H200" and f("B200") == "B200"
    assert f("nvidia-a10g") == "A10G" and f("Tesla T4") == "T4"
    assert f("RTX 4090") == "other"


def test_curated_list_is_small_and_hf_shaped():
    assert 10 <= len(preflight.CURATED_MODELS) <= 30
    assert all("/" in m for m in preflight.CURATED_MODELS)
    assert "Qwen/Qwen3-8B" in preflight.CURATED_MODELS


# ---------------------------------------------------------------------------
# Gated repos: the Hub reports false | "auto" | "manual", and the two gated
# kinds need different things from the user.
# ---------------------------------------------------------------------------


def _gated_info(kind):
    from mantis_agent.deploy.base import ModelInfo

    return ModelInfo(id="org/repo", source="hf", gated=bool(kind), gated_kind=kind)


def test_auto_gated_says_access_is_instant() -> None:
    from mantis_agent.deploy.base import DeployError
    from mantis_agent.deploy.preflight import check_gated

    with pytest.raises(DeployError) as ei:
        check_gated(_gated_info("auto"))
    assert "instantly" in ei.value.hint
    assert "owner-approved" not in ei.value.hint


def test_manual_gated_warns_the_owner_must_approve() -> None:
    from mantis_agent.deploy.base import DeployError
    from mantis_agent.deploy.preflight import check_gated

    with pytest.raises(DeployError) as ei:
        check_gated(_gated_info("manual"))
    assert "owner-approved" in ei.value.hint
    assert "request access" in ei.value.hint


def test_a_token_clears_the_gate_whichever_kind() -> None:
    from mantis_agent.deploy.preflight import check_gated

    check_gated(_gated_info("auto"), "hf_token")
    check_gated(_gated_info("manual"), "hf_token")
    check_gated(_gated_info(None))  # ungated needs nothing
