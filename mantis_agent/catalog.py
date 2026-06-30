"""Model catalog for the ``mantis`` terminal — three ways to run a model:

1. **Local (Ollama)** — open-weight models pulled onto your machine. Free, no
   key. See :data:`SUGGESTED_PULLS`.
2. **Self-host** — the *full* open weights on your own GPU server (vLLM /
   llama.cpp / TGI). No vendor key; you point ``mantis`` at your URL.
3. **Hosted API** — a provider runs the model for you. Needs an API key (that
   key is your billing account for *their compute*, not a licence for the
   model — the weights themselves are open). See :data:`CATALOG`.

A hosted provider is **enabled** once we can find its key — in the environment
or saved by ``/enable`` to ``~/.mantis-agent/models.json`` (chmod ``600``).
Disabled providers still appear in ``/models`` so the whole menu is visible.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .paths import get_mantis_agent_dir


# ---------------------------------------------------------------------------
# 1. Local (Ollama) — curated open-weight models you can pull for free
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pull:
    tag: str  # the `ollama pull <tag>` argument, e.g. "deepseek-r1:7b"
    note: str  # one-line description


# Flagship open models that run comfortably on a laptop/desktop via Ollama.
SUGGESTED_PULLS: tuple[Pull, ...] = (
    Pull("gpt-oss:20b", "OpenAI gpt-oss 20B — open-weight (Apache-2.0)"),
    Pull("qwen3:8b", "Qwen3 8B — strong all-rounder"),
    Pull("qwen2.5-coder:7b", "Qwen2.5-Coder 7B — code"),
    Pull("deepseek-r1:8b", "DeepSeek-R1 8B — reasoning"),
    Pull("llama3.2:3b", "Llama 3.2 3B — small & fast"),
    Pull("llama3.1:8b", "Llama 3.1 8B — general"),
    Pull("glm4:9b", "GLM-4 9B — Zhipu's open model"),
    Pull("gemma2:9b", "Gemma 2 9B — Google"),
    Pull("mistral:7b", "Mistral 7B — general"),
    Pull("phi4", "Phi-4 14B — Microsoft"),
)


# ---------------------------------------------------------------------------
# 2. Self-host — the open weights on your own GPU, OpenAI-compatible
# ---------------------------------------------------------------------------

SELF_HOST_NOTE = (
    "Run the full open weights yourself (vLLM / llama.cpp --server / TGI) and "
    "connect with [white]/connect <url> [model][/] — e.g. "
    "[white]/connect http://gpu-box:8000/v1 deepseek-ai/DeepSeek-V3[/]. "
    "No vendor key; it's your hardware."
)


# ---------------------------------------------------------------------------
# 3. Hosted APIs — provider runs it; needs a key (international endpoints)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Provider:
    id: str  # short slug, e.g. "deepseek"
    label: str  # display name
    base_url: str  # OpenAI-compat base URL
    api_key_env: str  # env var holding the key
    models: tuple[str, ...]  # a few flagship model ids
    note: str = ""  # signup hint


# Model ids verified against provider docs, June 2026. Hosted endpoints are
# OpenAI-compatible; when a provider is enabled the selector also fetches its
# live /v1/models, so these flagship lists are a starting menu, not a ceiling.
CATALOG: tuple[Provider, ...] = (
    Provider(
        "deepseek", "DeepSeek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY",
        ("deepseek-chat", "deepseek-reasoner"),
        "platform.deepseek.com · chat=V3.2, reasoner=thinking",
    ),
    Provider(
        "moonshot", "Kimi (Moonshot)", "https://api.moonshot.ai/v1", "MOONSHOT_API_KEY",
        ("kimi-latest", "kimi-k2-0905-preview", "moonshot-v1-128k", "moonshot-v1-32k"),
        "platform.moonshot.ai · K2.6",
    ),
    Provider(
        "glm", "GLM (Zhipu)", "https://api.z.ai/api/paas/v4", "ZHIPUAI_API_KEY",
        ("glm-4.7", "glm-4.6", "glm-4-plus", "glm-4-flash"),
        "z.ai",
    ),
    Provider(
        "qwen", "Qwen (DashScope)",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY",
        ("qwen-max", "qwen-plus", "qwen3-235b-a22b", "qwen3-coder-plus"),
        "dashscope-intl.aliyuncs.com · Qwen3",
    ),
    Provider(
        "groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
        ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "moonshotai/kimi-k2-instruct-0905",
         "qwen/qwen3-32b", "llama-3.3-70b-versatile"),
        "console.groq.com · very fast · hosts gpt-oss",
    ),
    Provider(
        "openai", "OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY",
        ("gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4-pro"),
        "platform.openai.com · GPT-5.4",
    ),
    Provider(
        "gemini", "Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY",
        ("gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"),
        "aistudio.google.com",
    ),
    Provider(
        "openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
        ("openai/gpt-oss-120b", "z-ai/glm-4.7", "moonshotai/kimi-k2",
         "deepseek/deepseek-chat", "qwen/qwen3-235b-a22b"),
        "openrouter.ai · one key, every model",
    ),
    Provider(
        "together", "Together", "https://api.together.xyz/v1", "TOGETHER_API_KEY",
        ("openai/gpt-oss-120b", "zai-org/GLM-4.7", "deepseek-ai/DeepSeek-V3",
         "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
        "api.together.xyz",
    ),
    Provider(
        "fireworks", "Fireworks", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY",
        ("accounts/fireworks/models/gpt-oss-120b",
         "accounts/fireworks/models/deepseek-v3",
         "accounts/fireworks/models/qwen3-235b-a22b"),
        "fireworks.ai",
    ),
    Provider(
        "cerebras", "Cerebras", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY",
        ("gpt-oss-120b", "zai-glm-4.7", "llama-3.3-70b", "gemma-4-31b"),
        "cloud.cerebras.ai · very fast · hosts OpenAI gpt-oss",
    ),
)

BY_ID = {p.id: p for p in CATALOG}


# ---------------------------------------------------------------------------
# Saved-key store (~/.mantis-agent/models.json)
# ---------------------------------------------------------------------------


def _store_path() -> Any:
    return get_mantis_agent_dir() / "models.json"


def _load_store() -> dict[str, Any]:
    try:
        return json.loads(_store_path().read_text())
    except Exception:  # noqa: BLE001 — missing / corrupt → empty
        return {}


def _save_store(data: dict[str, Any]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def saved_key(provider_id: str) -> str | None:
    return (_load_store().get("keys") or {}).get(provider_id)


def set_key(provider_id: str, key: str) -> None:
    """Persist a key and make it live for this process."""
    data = _load_store()
    data.setdefault("keys", {})[provider_id] = key
    _save_store(data)
    prov = BY_ID.get(provider_id)
    if prov and prov.api_key_env:
        os.environ[prov.api_key_env] = key


def clear_key(provider_id: str) -> bool:
    data = _load_store()
    keys = data.get("keys") or {}
    if provider_id in keys:
        del keys[provider_id]
        _save_store(data)
        return True
    return False


def api_key_for(provider: Provider) -> str | None:
    """Key from the environment first, then the saved store."""
    if provider.api_key_env and os.environ.get(provider.api_key_env):
        return os.environ[provider.api_key_env]
    return saved_key(provider.id)


def is_enabled(provider: Provider) -> bool:
    return bool(api_key_for(provider))


def provider_for_model(model_id: str) -> Provider | None:
    """Best-effort: which hosted provider serves ``model_id`` (exact id, then a
    prefix heuristic). Returns ``None`` for local/self-hosted/unknown ids."""
    for p in CATALOG:
        if model_id in p.models:
            return p
    low = model_id.lower()
    prefixes = {
        "accounts/fireworks/": "fireworks",
        "z-ai/": "openrouter",
        "moonshotai/": "groq",
        "glm-": "glm",
        "kimi-": "moonshot",
        "moonshot-": "moonshot",
        "deepseek-chat": "deepseek",
        "deepseek-reasoner": "deepseek",
        "qwen-max": "qwen",
        "qwen-plus": "qwen",
        "gpt-": "openai",
        "o1": "openai",
        "o3": "openai",
        "o4": "openai",
        "gemini-": "gemini",
    }
    for pre, pid in prefixes.items():
        if low.startswith(pre):
            return BY_ID.get(pid)
    return None


__all__ = [
    "Pull",
    "SUGGESTED_PULLS",
    "SELF_HOST_NOTE",
    "Provider",
    "CATALOG",
    "BY_ID",
    "saved_key",
    "set_key",
    "clear_key",
    "api_key_for",
    "is_enabled",
    "provider_for_model",
]
