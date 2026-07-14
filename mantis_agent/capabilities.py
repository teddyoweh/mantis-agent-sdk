"""ModelCapability registry + BackendCapability probing.

The single source of truth about what a (model, backend) pair can do.
Drives the tool-use path resolver (A/B/C), gates the thinking parser, picks
chat templates, sets default sampling, and feeds the budget tracker.

Capability lookup is O(1) — at Agent init we resolve once and freeze the
result onto the agent. The model table here is hand-maintained for the top
30 OSS models we explicitly support at GA; everything else falls through to
a family-based heuristic.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

# ---------------------------------------------------------------------------
# Model capability — what a given model can do
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelCapability:
    """Per-model facts that drive runtime behavior.

    Fields
    ------
    name:                  canonical slug (lowercase, hyphenated)
    family:                family id for chat-template lookup
    supports_native_tools: model knows how to emit tool_calls when served via
                           an OpenAI-compatible tools[] interface
    supports_grammar:      server-side grammar/JSON-schema-constrained sampling
                           is available when this model is served on a grammar-
                           capable backend (vLLM guided_json, llama.cpp GBNF,
                           TGI grammar). Per-MODEL bit is a hint; the actual
                           grammar path is gated on backend capability too.
    supports_reasoning_effort:
                           the model accepts a REQUEST-SIDE reasoning knob —
                           OpenAI's ``reasoning_effort`` / a ``think`` flag /
                           an adaptive-thinking budget. Providers gate on this
                           before sending a thinking config so a model that
                           400s on the field (e.g. plain gpt-4o, most local
                           chat models) is never handed one. Distinct from
                           ``emits_thinking_blocks`` / ``emits_inline_thinking``,
                           which describe the RESPONSE side (does it emit
                           reasoning) — a model can emit inline ``<think>`` tags
                           yet accept no request-side switch.
    emits_thinking_blocks: server emits an out-of-band thinking field
    emits_inline_thinking: model emits <think>...</think> in content
    context_window:        max input tokens
    max_output_tokens:     max output tokens per turn (sane default)
    chat_template_id:      key into templates/bundled/
    recommended_temperature: default if user doesn't override
    family_specific_stops: extra stop tokens beyond the chat template
    """

    name: str
    family: str
    supports_native_tools: bool = False
    supports_grammar: bool = True  # most modern OSS backends support some form
    supports_reasoning_effort: bool = False
    emits_thinking_blocks: bool = False
    emits_inline_thinking: bool = False
    # Inline reasoning-tag pairs this model uses. The default covers the
    # five tag conventions in the wild — providers/normalizers can pass
    # this directly to ``ThinkingParser(tags=…)``. Only consulted when
    # ``emits_inline_thinking`` is True.
    inline_thinking_tags: tuple[tuple[str, str], ...] = (
        ("<think>", "</think>"),
        ("<thought>", "</thought>"),
        ("<reasoning>", "</reasoning>"),
        ("<thinking>", "</thinking>"),
        ("<reflection>", "</reflection>"),
    )
    context_window: int = 8192
    max_output_tokens: int = 4096
    chat_template_id: str = "chatml"
    recommended_temperature: float = 0.7
    family_specific_stops: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# The table — 30 explicitly supported OSS models
# ---------------------------------------------------------------------------

_TABLE: dict[str, ModelCapability] = {
    # GLM self-hosted checkpoints
    "glm-4-9b-0414": ModelCapability(
        name="glm-4-9b-0414",
        family="glm",
        supports_native_tools=True,
        context_window=16384,
        max_output_tokens=4096,
        chat_template_id="chatml",
    ),
    # Llama 3.3
    "llama-3.3-70b-instruct": ModelCapability(
        name="llama-3.3-70b-instruct",
        family="llama3",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="llama3",
        family_specific_stops=("<|eot_id|>", "<|end_of_text|>"),
    ),
    # Llama 3.1
    "llama-3.1-70b-instruct": ModelCapability(
        name="llama-3.1-70b-instruct",
        family="llama3",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="llama3",
        family_specific_stops=("<|eot_id|>", "<|end_of_text|>"),
    ),
    "llama-3.1-8b-instruct": ModelCapability(
        name="llama-3.1-8b-instruct",
        family="llama3",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="llama3",
        family_specific_stops=("<|eot_id|>", "<|end_of_text|>"),
    ),
    # Llama 3 (no native tools — Path C territory)
    "llama-3-70b-instruct": ModelCapability(
        name="llama-3-70b-instruct",
        family="llama3",
        supports_native_tools=False,
        context_window=8192,
        chat_template_id="llama3",
        family_specific_stops=("<|eot_id|>", "<|end_of_text|>"),
    ),
    "llama-3-8b-instruct": ModelCapability(
        name="llama-3-8b-instruct",
        family="llama3",
        supports_native_tools=False,
        context_window=8192,
        chat_template_id="llama3",
        family_specific_stops=("<|eot_id|>", "<|end_of_text|>"),
    ),
    # Qwen 2.5 — strongest open tool use
    "qwen2.5-72b-instruct": ModelCapability(
        name="qwen2.5-72b-instruct",
        family="qwen2.5",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="qwen2",
        family_specific_stops=("<|im_end|>",),
    ),
    "qwen2.5-32b-instruct": ModelCapability(
        name="qwen2.5-32b-instruct",
        family="qwen2.5",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="qwen2",
        family_specific_stops=("<|im_end|>",),
    ),
    "qwen2.5-14b-instruct": ModelCapability(
        name="qwen2.5-14b-instruct",
        family="qwen2.5",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="qwen2",
        family_specific_stops=("<|im_end|>",),
    ),
    "qwen2.5-7b-instruct": ModelCapability(
        name="qwen2.5-7b-instruct",
        family="qwen2.5",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="qwen2",
        family_specific_stops=("<|im_end|>",),
    ),
    "qwen2.5-coder-32b-instruct": ModelCapability(
        name="qwen2.5-coder-32b-instruct",
        family="qwen2.5",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="qwen2",
        recommended_temperature=0.2,
        family_specific_stops=("<|im_end|>",),
    ),
    "qwq-32b-preview": ModelCapability(
        name="qwq-32b-preview",
        family="qwen2.5",
        supports_native_tools=False,  # reasoning models often unreliable with tools
        emits_inline_thinking=True,
        context_window=32768,
        chat_template_id="qwen2",
        recommended_temperature=0.6,
        family_specific_stops=("<|im_end|>",),
    ),
    # DeepSeek
    "deepseek-v3": ModelCapability(
        name="deepseek-v3",
        family="deepseek",
        supports_native_tools=True,
        context_window=65536,
        chat_template_id="deepseek",
    ),
    "deepseek-r1": ModelCapability(
        name="deepseek-r1",
        family="deepseek",
        supports_native_tools=False,
        emits_inline_thinking=True,
        emits_thinking_blocks=True,
        context_window=65536,
        chat_template_id="deepseek",
        recommended_temperature=0.6,
    ),
    # DeepSeek's official reasoner id (R1-class). Emits <think>/reasoning_content
    # and is unreliable with tools — must mirror deepseek-r1, NOT the plain
    # deepseek chat default it would otherwise resolve to ("deepseek-r1" is not a
    # substring of "deepseek-reasoner").
    "deepseek-reasoner": ModelCapability(
        name="deepseek-reasoner",
        family="deepseek",
        supports_native_tools=False,
        emits_inline_thinking=True,
        emits_thinking_blocks=True,
        context_window=65536,
        chat_template_id="deepseek",
        recommended_temperature=0.6,
    ),
    "deepseek-r1-distill-llama-70b": ModelCapability(
        name="deepseek-r1-distill-llama-70b",
        family="llama3",
        supports_native_tools=False,
        emits_inline_thinking=True,
        context_window=131072,
        chat_template_id="llama3",
        recommended_temperature=0.6,
        family_specific_stops=("<|eot_id|>",),
    ),
    "deepseek-r1-distill-qwen-32b": ModelCapability(
        name="deepseek-r1-distill-qwen-32b",
        family="qwen2.5",
        supports_native_tools=False,
        emits_inline_thinking=True,
        context_window=131072,
        chat_template_id="qwen2",
        recommended_temperature=0.6,
        family_specific_stops=("<|im_end|>",),
    ),
    # Mixtral / Mistral
    "mixtral-8x22b-instruct": ModelCapability(
        name="mixtral-8x22b-instruct",
        family="mistral",
        supports_native_tools=True,
        context_window=65536,
        chat_template_id="mistral",
    ),
    "mixtral-8x7b-instruct": ModelCapability(
        name="mixtral-8x7b-instruct",
        family="mistral",
        supports_native_tools=True,
        context_window=32768,
        chat_template_id="mistral",
    ),
    "mistral-large-2": ModelCapability(
        name="mistral-large-2",
        family="mistral",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="mistral",
    ),
    "mistral-nemo-12b": ModelCapability(
        name="mistral-nemo-12b",
        family="mistral",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="mistral",
    ),
    # Hermes / Functionary
    "hermes-3-llama-3.1-70b": ModelCapability(
        name="hermes-3-llama-3.1-70b",
        family="llama3",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="llama3",
        family_specific_stops=("<|eot_id|>",),
    ),
    "hermes-3-llama-3.1-8b": ModelCapability(
        name="hermes-3-llama-3.1-8b",
        family="llama3",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="llama3",
        family_specific_stops=("<|eot_id|>",),
    ),
    "hermes-2-pro-llama-3.1-8b": ModelCapability(
        name="hermes-2-pro-llama-3.1-8b",
        family="llama3",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="llama3",
        family_specific_stops=("<|eot_id|>",),
    ),
    "functionary-v3.2": ModelCapability(
        name="functionary-v3.2",
        family="llama3",
        supports_native_tools=True,
        context_window=8192,
        chat_template_id="llama3",
        family_specific_stops=("<|eot_id|>",),
    ),
    # Cohere
    "command-r-plus": ModelCapability(
        name="command-r-plus",
        family="cohere",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="cohere",
    ),
    "aya-expanse-32b": ModelCapability(
        name="aya-expanse-32b",
        family="cohere",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="cohere",
    ),
    # Phi / Gemma — Path C
    "phi-4-14b": ModelCapability(
        name="phi-4-14b",
        family="phi",
        supports_native_tools=False,
        context_window=16384,
        chat_template_id="phi",
    ),
    "gemma-2-27b-it": ModelCapability(
        name="gemma-2-27b-it",
        family="gemma",
        supports_native_tools=False,
        context_window=8192,
        chat_template_id="gemma",
    ),
    "gemma-2-9b-it": ModelCapability(
        name="gemma-2-9b-it",
        family="gemma",
        supports_native_tools=False,
        context_window=8192,
        chat_template_id="gemma",
    ),
    # Yi / InternLM / Granite
    "yi-large": ModelCapability(
        name="yi-large",
        family="yi",
        supports_native_tools=True,
        context_window=32768,
        chat_template_id="chatml",
    ),
    "internlm2.5-20b-chat": ModelCapability(
        name="internlm2.5-20b-chat",
        family="internlm",
        supports_native_tools=True,
        context_window=32768,
        chat_template_id="chatml",
    ),
    "granite-3.1-8b-instruct": ModelCapability(
        name="granite-3.1-8b-instruct",
        family="granite",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="chatml",
    ),
    # Moonshot Kimi
    "kimi-k2-instruct": ModelCapability(
        name="kimi-k2-instruct",
        family="kimi",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="kimi",
        recommended_temperature=0.6,
        family_specific_stops=("<|im_end|>",),  # Kimi uses ChatML-derived tokens
    ),
    "kimi-k1.5-instruct": ModelCapability(
        name="kimi-k1.5-instruct",
        family="kimi",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="kimi",
        recommended_temperature=0.6,
        family_specific_stops=("<|im_end|>",),
    ),
    "moonshot-v1-128k": ModelCapability(
        name="moonshot-v1-128k",
        family="kimi",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="kimi",
        recommended_temperature=0.3,
    ),
    "moonshot-v1-32k": ModelCapability(
        name="moonshot-v1-32k",
        family="kimi",
        supports_native_tools=True,
        context_window=32768,
        chat_template_id="kimi",
        recommended_temperature=0.3,
    ),
    "moonshot-v1-8k": ModelCapability(
        name="moonshot-v1-8k",
        family="kimi",
        supports_native_tools=True,
        context_window=8192,
        chat_template_id="kimi",
        recommended_temperature=0.3,
    ),
}


# ---------------------------------------------------------------------------
# Family-level fallback heuristics
# ---------------------------------------------------------------------------

_FAMILY_DEFAULTS: dict[str, ModelCapability] = {
    "llama3": ModelCapability(
        name="llama3-unknown",
        family="llama3",
        supports_native_tools=False,
        context_window=8192,
        chat_template_id="llama3",
        family_specific_stops=("<|eot_id|>",),
    ),
    # Llama 3.1 / 3.2 / 3.3 DO support native function-calling (Ollama serves
    # tool_calls for them). The bare "llama3" default above is for 3.0 only;
    # this entry catches the modern point releases — including Ollama tag forms
    # like ``llama3.2:latest`` / ``llama3.1:8b`` that never match the hyphenated
    # _TABLE keys and would otherwise fall through to the non-tool default.
    "llama3.1": ModelCapability(
        name="llama3.1-unknown",
        family="llama3",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="llama3",
        family_specific_stops=("<|eot_id|>",),
    ),
    "qwen2.5": ModelCapability(
        name="qwen2.5-unknown",
        family="qwen2.5",
        supports_native_tools=True,
        context_window=32768,
        chat_template_id="qwen2",
        family_specific_stops=("<|im_end|>",),
    ),
    # Qwen3 (qwen3:8b, qwen3-235b-a22b, qwen3-coder-plus, qwen3-32b, …). Ships
    # native tool_calls and a 40k–128k window; the ChatML-shaped qwen2 template
    # + <|im_end|> stop still apply. Without this the ids fall through to the
    # generic no-tools / 8192 fallback (tools disabled, premature compaction).
    "qwen3": ModelCapability(
        name="qwen3-unknown",
        family="qwen3",
        supports_native_tools=True,
        context_window=131072,
        max_output_tokens=8192,
        chat_template_id="qwen2",
        family_specific_stops=("<|im_end|>",),
    ),
    "deepseek": ModelCapability(
        name="deepseek-unknown",
        family="deepseek",
        supports_native_tools=True,
        context_window=65536,  # DeepSeek V3/R1 ship a 64k window (was understated at 32k)
        chat_template_id="deepseek",
    ),
    "mistral": ModelCapability(
        name="mistral-unknown",
        family="mistral",
        supports_native_tools=True,
        context_window=32768,
        chat_template_id="mistral",
    ),
    "phi": ModelCapability(
        name="phi-unknown",
        family="phi",
        supports_native_tools=False,
        context_window=16384,
        chat_template_id="phi",
    ),
    "gemma": ModelCapability(
        name="gemma-unknown",
        family="gemma",
        supports_native_tools=False,
        context_window=8192,
        chat_template_id="gemma",
    ),
    "cohere": ModelCapability(
        name="cohere-unknown",
        family="cohere",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="cohere",
    ),
    "kimi": ModelCapability(
        name="kimi-unknown",
        family="kimi",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="kimi",
        family_specific_stops=("<|im_end|>",),
    ),
    # IBM Granite uses a ChatML-style template with <|im_end|>-style delimiters
    # (see the in-table granite row), NOT llama3 headers/<|eot_id|>. Give it its
    # own default so unknown variants (granite-3.2-2b-instruct, …) template right.
    "granite": ModelCapability(
        name="granite-unknown",
        family="granite",
        supports_native_tools=True,
        context_window=131072,
        chat_template_id="chatml",
    ),
    # Hosted flagships (OpenAI / Anthropic / Google / Zhipu). These are served
    # over their provider APIs (or the anthropic passthrough), so the chat
    # template is unused — but the capability drives native-tool routing (path A)
    # and the context-window indicator / auto-compaction. Windows are safe floors
    # (underestimate compacts a little early; overestimate risks a 413).
    "openai": ModelCapability(
        name="openai-unknown", family="openai",
        supports_native_tools=True, context_window=128000,
    ),
    # o-series reasoning models (o1/o3/o4) have a 200k context, unlike the 128k
    # gpt-4o family — so track it accurately (understating it compacts early).
    # They also take a request-side ``reasoning_effort`` (as does gpt-5.x, which
    # the openai_compat adapter detects by name — the bare "openai" family below
    # stays False so plain gpt-4o is never handed the field).
    "openai_reasoning": ModelCapability(
        name="openai-reasoning-unknown", family="openai",
        supports_native_tools=True, supports_reasoning_effort=True,
        context_window=200000,
    ),
    "claude": ModelCapability(
        name="claude-unknown", family="claude",
        supports_native_tools=True, supports_reasoning_effort=True,
        context_window=200000,
    ),
    "gemini": ModelCapability(
        name="gemini-unknown", family="gemini",
        supports_native_tools=True, supports_reasoning_effort=True,
        context_window=1000000,
    ),
    "glm": ModelCapability(
        name="glm-unknown", family="glm",
        supports_native_tools=True, supports_reasoning_effort=True,
        context_window=128000,
    ),
}

# Heuristic: substrings to family
_FAMILY_HINTS: tuple[tuple[str, str], ...] = (
    # Modern tool-capable Llamas first — these must win over the generic
    # "llama3" hint below (substring order matters; "llama3.2" contains "llama3").
    ("llama-3.1", "llama3.1"),
    ("llama3.1", "llama3.1"),
    ("llama-3.2", "llama3.1"),
    ("llama3.2", "llama3.1"),
    ("llama-3.3", "llama3.1"),
    ("llama3.3", "llama3.1"),
    ("llama-3", "llama3"),
    ("llama3", "llama3"),
    ("hermes", "llama3"),
    ("functionary", "llama3"),
    # Qwen3 before the qwen2 hints — "qwen2" is not a substring of "qwen3", so
    # without these the qwen3 ids fall through to the generic no-tools / 8192
    # default (tools disabled, premature compaction).
    ("qwen3", "qwen3"),
    ("qwen-3", "qwen3"),
    ("qwen2.5", "qwen2.5"),
    ("qwen-2.5", "qwen2.5"),
    ("qwq", "qwen2.5"),
    ("qwen2", "qwen2.5"),
    ("deepseek", "deepseek"),
    ("mixtral", "mistral"),
    ("mistral", "mistral"),
    ("phi", "phi"),
    ("gemma", "gemma"),
    ("command-r", "cohere"),
    ("aya", "cohere"),
    ("kimi", "kimi"),
    ("moonshot", "kimi"),
    ("internlm", "qwen2.5"),  # ChatML-shaped
    ("granite", "granite"),
    ("yi", "qwen2.5"),
    # Hosted flagships — so gpt-4o/gpt-5.x, claude-*, o1/o3/o4, gemini, glm don't
    # fall through to the tiny 8192 / no-native-tools generic default.
    ("claude", "claude"),
    ("gpt-", "openai"),
    ("o1", "openai_reasoning"),
    ("o3", "openai_reasoning"),
    ("o4", "openai_reasoning"),
    ("gemini", "gemini"),
    ("glm", "glm"),
)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def lookup_model(model_id: str) -> ModelCapability:
    """Resolve a model id to a capability record.

    Order of resolution:
      1. Exact match (case-insensitive, slashes stripped to last component).
      2. Substring family hint → family default.
      3. Generic ChatML fallback.
    """

    key = _normalize(model_id)
    if key in _TABLE:
        return _TABLE[key]

    # Strip provider prefix like "meta-llama/Llama-3.1-70B-Instruct"
    if "/" in model_id:
        tail = _normalize(model_id.rsplit("/", 1)[-1])
        if tail in _TABLE:
            return _TABLE[tail]
        key = tail

    # DeepSeek-R1 distills wear a Llama or Qwen backbone, so their chat template
    # and stop tokens must follow that backbone — NOT the "deepseek-r1" prefix
    # the generic substring match below would greedily hit first. Only reached
    # for sizes not already in _TABLE (the 70b/32b distills match exactly above);
    # reuse those rows so the untabled sizes inherit the correct template.
    if "distill" in key and "deepseek" in key:
        if "qwen" in key:
            return replace(_TABLE["deepseek-r1-distill-qwen-32b"], name=key)
        if "llama" in key:
            return replace(_TABLE["deepseek-r1-distill-llama-70b"], name=key)

    # Substring fuzzy match against the table keys.
    for table_key, cap in _TABLE.items():
        if table_key in key or key in table_key:
            return cap

    # Family hint fallback.
    for hint, family in _FAMILY_HINTS:
        if hint in key:
            base = _FAMILY_DEFAULTS.get(family)
            if base is not None:
                return replace(base, name=key)

    # Generic fallback. Path C territory.
    return ModelCapability(
        name=key,
        family="unknown",
        supports_native_tools=False,
        supports_grammar=True,
        emits_inline_thinking=False,
        context_window=8192,
        chat_template_id="chatml",
    )


def _normalize(s: str) -> str:
    """Lowercase, strip whitespace, normalize separators."""

    return s.strip().lower().replace("_", "-")


# ---------------------------------------------------------------------------
# Backend capability — what the server supports
# ---------------------------------------------------------------------------


BackendKind = Literal[
    "openai_compat",
    "ollama",
    "llamacpp",
    "tgi",
    "modal",
    "anthropic",
    "raw",
    "mock",
]


@dataclass(frozen=True, slots=True)
class BackendCapability:
    """Probed at adapter init. Cached for the connection lifetime."""

    kind: BackendKind
    supports_native_tools: bool
    supports_grammar: bool
    supports_logprobs: bool = False
    supports_prefix_caching: bool = False
    supports_streaming: bool = True
    max_concurrent_requests: int = 64
    # Provider-specific hints surfaced for adapters.
    provider_hint: str = ""  # e.g. "together", "fireworks", "groq", "openrouter"


# Pre-canned profiles for the well-known hosted providers. The OpenAI-compat
# adapter picks one of these based on base_url; raw vLLM probes live.
HOSTED_PROFILES: dict[str, BackendCapability] = {
    "together": BackendCapability(
        kind="openai_compat",
        supports_native_tools=True,
        supports_grammar=True,
        supports_logprobs=True,
        provider_hint="together",
    ),
    "fireworks": BackendCapability(
        kind="openai_compat",
        supports_native_tools=True,
        supports_grammar=True,
        supports_logprobs=True,
        provider_hint="fireworks",
    ),
    "groq": BackendCapability(
        kind="openai_compat",
        supports_native_tools=True,
        supports_grammar=False,  # Groq doesn't expose grammar
        supports_logprobs=True,
        max_concurrent_requests=30,
        provider_hint="groq",
    ),
    "openrouter": BackendCapability(
        kind="openai_compat",
        supports_native_tools=True,
        supports_grammar=False,
        supports_logprobs=False,
        provider_hint="openrouter",
    ),
    "deepinfra": BackendCapability(
        kind="openai_compat",
        supports_native_tools=True,
        supports_grammar=True,
        provider_hint="deepinfra",
    ),
    "cerebras": BackendCapability(
        kind="openai_compat",
        supports_native_tools=True,
        supports_grammar=False,
        provider_hint="cerebras",
    ),
    "anyscale": BackendCapability(
        kind="openai_compat",
        supports_native_tools=True,
        supports_grammar=True,
        provider_hint="anyscale",
    ),
    "deepseek": BackendCapability(
        kind="openai_compat",
        supports_native_tools=True,
        supports_grammar=False,
        provider_hint="deepseek",
    ),
    "moonshot": BackendCapability(
        kind="openai_compat",
        supports_native_tools=True,
        supports_grammar=False,
        provider_hint="moonshot",
    ),
    "vllm": BackendCapability(
        kind="openai_compat",
        supports_native_tools=True,
        supports_grammar=True,
        supports_logprobs=True,
        supports_prefix_caching=True,
        provider_hint="vllm",
    ),
    "ollama": BackendCapability(
        kind="ollama",
        supports_native_tools=True,  # since 0.3
        supports_grammar=True,  # format=json since 0.x
        supports_prefix_caching=True,
        provider_hint="ollama",
    ),
    "llamacpp": BackendCapability(
        kind="llamacpp",
        supports_native_tools=False,  # default; --jinja flag makes it true
        supports_grammar=True,  # GBNF
        supports_logprobs=True,
        supports_prefix_caching=True,
        provider_hint="llamacpp",
    ),
    "tgi": BackendCapability(
        kind="tgi",
        supports_native_tools=False,
        supports_grammar=True,
        supports_logprobs=True,
        provider_hint="tgi",
    ),
    "modal": BackendCapability(
        kind="modal",
        supports_native_tools=True,
        supports_grammar=True,
        provider_hint="modal",
    ),
    "anthropic": BackendCapability(
        kind="anthropic",
        supports_native_tools=True,
        supports_grammar=False,  # Anthropic doesn't expose grammar-constrained sampling
        supports_logprobs=False,
        supports_prefix_caching=True,  # implicit prompt caching with cache_control blocks
        provider_hint="anthropic",
    ),
    # OpenAI + other first-party hosted APIs. Without these they fell through to
    # the generic vLLM fallback, which tags provider_hint="vllm" — so budget/cost
    # tracking looked up the wrong pricing. Native tools + no local grammar.
    "openai": BackendCapability(
        kind="openai_compat",
        supports_native_tools=True,
        supports_grammar=False,
        supports_logprobs=True,
        supports_prefix_caching=True,  # OpenAI does automatic prompt caching
        provider_hint="openai",
    ),
    "gemini": BackendCapability(
        kind="openai_compat",
        supports_native_tools=True,
        supports_grammar=False,
        supports_logprobs=False,
        provider_hint="gemini",
    ),
    "glm": BackendCapability(
        kind="openai_compat",
        supports_native_tools=True,
        supports_grammar=False,
        provider_hint="glm",
    ),
    "qwen": BackendCapability(
        kind="openai_compat",
        supports_native_tools=True,
        supports_grammar=False,
        provider_hint="qwen",
    ),
    "mock": BackendCapability(
        kind="mock",
        supports_native_tools=True,
        supports_grammar=True,
        provider_hint="mock",
    ),
}


def hosted_profile_from_url(base_url: str) -> BackendCapability | None:
    """Heuristic: match a base_url to a hosted profile."""

    url = base_url.lower()
    if "api.anthropic.com" in url:
        return HOSTED_PROFILES["anthropic"]
    if ".modal.run" in url:
        return HOSTED_PROFILES["modal"]
    if "api.together." in url:  # api.together.xyz / api.together.ai
        return HOSTED_PROFILES["together"]
    if "fireworks" in url:
        return HOSTED_PROFILES["fireworks"]
    if "groq" in url:
        return HOSTED_PROFILES["groq"]
    if "openrouter" in url:
        return HOSTED_PROFILES["openrouter"]
    if "deepinfra" in url:
        return HOSTED_PROFILES["deepinfra"]
    if "cerebras" in url:
        return HOSTED_PROFILES["cerebras"]
    if "anyscale" in url:
        return HOSTED_PROFILES["anyscale"]
    # Anchor to the real API host, not the bare vendor word: a self-hosted
    # (vLLM/llama.cpp) box named after the model — e.g. http://deepseek-box:8000
    # — must fall through to a grammar-capable profile, not the hosted deepseek
    # profile (supports_grammar=False), which would downgrade its tool-use path.
    if "api.deepseek.com" in url:
        return HOSTED_PROFILES["deepseek"]
    # Match Moonshot's real domains (api.moonshot.ai/.cn, kimi.moonshot.ai) —
    # NOT a bare "kimi", which is a model name: a self-hosted Kimi box on
    # vLLM/llama.cpp (e.g. http://kimi-box:8000/v1) must fall through to a
    # grammar-capable profile, not the hosted moonshot profile (grammar=False).
    if "moonshot.ai" in url or "moonshot.cn" in url:
        return HOSTED_PROFILES["moonshot"]
    if "api.openai.com" in url:
        return HOSTED_PROFILES["openai"]
    if "generativelanguage.googleapis" in url:
        return HOSTED_PROFILES["gemini"]
    if "z.ai" in url or "bigmodel.cn" in url or "zhipu" in url:
        return HOSTED_PROFILES["glm"]
    if "dashscope" in url or "aliyuncs" in url:
        return HOSTED_PROFILES["qwen"]
    if "11434" in url or "ollama" in url:
        return HOSTED_PROFILES["ollama"]
    # llama.cpp's canonical base_url is http://localhost:8080/v1 — it carries the
    # port but not the word "llama", so match on the port alone. This branch is
    # last, after every provider-specific host check has already returned, so a
    # bare :8080 endpoint is the llama.cpp server by elimination.
    if "8080" in url:
        return HOSTED_PROFILES["llamacpp"]
    return None


# ---------------------------------------------------------------------------
# Tool-use path resolution (Path A / B / C)
# ---------------------------------------------------------------------------


ToolUsePath = Literal["A", "B", "C"]


def resolve_tool_use_path(
    model: ModelCapability, backend: BackendCapability
) -> ToolUsePath:
    """Pick the tool-use strategy for this (model, backend) pair.

    - A: native tool calling via OpenAI-compat tools[] (best path)
    - B: prompt-engineered <tool_call> XML, parsed from text stream
    - C: prompt-engineered + grammar-constrained sampling (server enforces JSON)
    """

    if model.supports_native_tools and backend.supports_native_tools:
        return "A"
    # Path C (server-enforced grammar/JSON) requires BOTH the backend to offer
    # grammar-constrained sampling and the model to be able to honor it — as the
    # supports_grammar docstring promises. Gating on backend alone left the
    # per-model bit dead and would route a grammar-incapable model to C anyway.
    if model.supports_grammar and backend.supports_grammar:
        return "C"
    return "B"


__all__ = [
    "BackendCapability",
    "BackendKind",
    "HOSTED_PROFILES",
    "ModelCapability",
    "ToolUsePath",
    "hosted_profile_from_url",
    "lookup_model",
    "resolve_tool_use_path",
]
