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
    Pull("qwen2.5:7b", "Qwen2.5 7B — strong all-rounder"),
    Pull("qwen2.5-coder:7b", "Qwen2.5-Coder 7B — code"),
    Pull("deepseek-r1:7b", "DeepSeek-R1 7B — reasoning"),
    Pull("deepseek-r1:14b", "DeepSeek-R1 14B — reasoning (bigger)"),
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


CATALOG: tuple[Provider, ...] = (
    Provider(
        "deepseek", "DeepSeek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY",
        ("deepseek-chat", "deepseek-reasoner"),
        "platform.deepseek.com",
    ),
    Provider(
        "moonshot", "Kimi (Moonshot)", "https://api.moonshot.ai/v1", "MOONSHOT_API_KEY",
        ("kimi-k2-0711-preview", "moonshot-v1-128k", "moonshot-v1-32k", "moonshot-v1-8k"),
        "platform.moonshot.ai",
    ),
    Provider(
        "glm", "GLM (Zhipu)", "https://api.z.ai/api/paas/v4", "ZHIPUAI_API_KEY",
        ("glm-4.6", "glm-4-plus", "glm-4-air", "glm-4-flash"),
        "z.ai",
    ),
    Provider(
        "qwen", "Qwen (DashScope)",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY",
        ("qwen-max", "qwen-plus", "qwen2.5-72b-instruct", "qwen2.5-coder-32b-instruct"),
        "dashscope-intl.aliyuncs.com",
    ),
    Provider(
        "groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
        ("llama-3.3-70b-versatile", "deepseek-r1-distill-llama-70b", "moonshotai/kimi-k2-instruct"),
        "console.groq.com · very fast",
    ),
    Provider(
        "openai", "OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY",
        ("gpt-4o", "gpt-4o-mini", "o3", "o4-mini"),
        "platform.openai.com",
    ),
    Provider(
        "gemini", "Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY",
        ("gemini-2.0-flash", "gemini-1.5-pro"),
        "aistudio.google.com",
    ),
    Provider(
        "openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
        ("z-ai/glm-4.6", "moonshotai/kimi-k2", "deepseek/deepseek-chat", "qwen/qwen-2.5-72b-instruct"),
        "openrouter.ai · one key, every model",
    ),
    Provider(
        "together", "Together", "https://api.together.xyz/v1", "TOGETHER_API_KEY",
        ("deepseek-ai/DeepSeek-V3", "meta-llama/Llama-3.3-70B-Instruct-Turbo",
         "Qwen/Qwen2.5-72B-Instruct-Turbo"),
        "api.together.xyz",
    ),
    Provider(
        "fireworks", "Fireworks", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY",
        ("accounts/fireworks/models/deepseek-v3",
         "accounts/fireworks/models/llama-v3p3-70b-instruct",
         "accounts/fireworks/models/qwen2p5-72b-instruct"),
        "fireworks.ai",
    ),
    Provider(
        "cerebras", "Cerebras", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY",
        ("llama-3.3-70b", "qwen-3-32b"),
        "cloud.cerebras.ai · very fast",
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
