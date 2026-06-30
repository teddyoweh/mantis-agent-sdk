"""Model catalog — the curated list of providers/models the ``mantis`` terminal
can talk to, plus the on/off ("enabled") state for each.

A *provider* is a hosted (or local) OpenAI-compatible backend: a base URL, the
environment variable that carries its API key, and a handful of flagship model
ids. A provider is **enabled** when we can find its key — either in the
environment or saved by the user via ``/enable`` — (local backends like Ollama
are enabled whenever they're reachable). Disabled providers still show up in
``/models`` so you can see the whole menu and turn one on.

Saved keys live in ``~/.mantis-agent/models.json`` (chmod ``600``). They are
stored in plaintext, same as a shell rc export — fine for a local dev tool, but
prefer real environment variables on shared machines.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .paths import get_mantis_agent_dir


@dataclass(frozen=True, slots=True)
class Provider:
    id: str  # short slug, e.g. "deepseek"
    label: str  # display name, e.g. "DeepSeek"
    base_url: str  # OpenAI-compat base URL ("" for local-dynamic)
    api_key_env: str  # env var that holds the key ("" for keyless/local)
    models: tuple[str, ...]  # a few flagship model ids
    note: str = ""  # one-line hint (signup URL, etc.)
    local: bool = False  # True → keyless, enabled when reachable


# Curated as of early 2026. Model ids are the strings each provider's
# OpenAI-compatible endpoint expects. Keep this list tight and flagship-only;
# `/models` is a menu, not an exhaustive index.
CATALOG: tuple[Provider, ...] = (
    Provider(
        "ollama", "Ollama", "http://localhost:11434", "",
        (), "local models — install with `ollama pull <name>`", local=True,
    ),
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
        "glm", "GLM (Zhipu)", "https://open.bigmodel.cn/api/paas/v4", "ZHIPUAI_API_KEY",
        ("glm-4.6", "glm-4-plus", "glm-4-air", "glm-4-flash"),
        "open.bigmodel.cn  ·  intl: api.z.ai",
    ),
    Provider(
        "qwen", "Qwen (DashScope)",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY",
        ("qwen-max", "qwen-plus", "qwen2.5-72b-instruct", "qwen2.5-coder-32b-instruct"),
        "dashscope.console.aliyun.com",
    ),
    Provider(
        "groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
        ("llama-3.3-70b-versatile", "deepseek-r1-distill-llama-70b", "moonshotai/kimi-k2-instruct"),
        "console.groq.com  ·  very fast",
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
        "openrouter.ai  ·  one key, every model",
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
        "cloud.cerebras.ai  ·  very fast",
    ),
)

BY_ID = {p.id: p for p in CATALOG}


# ---------------------------------------------------------------------------
# Saved-key store (~/.mantis-agent/models.json)
# ---------------------------------------------------------------------------


def _store_path() -> Any:
    return get_mantis_agent_dir() / "models.json"


def _load_store() -> dict[str, Any]:
    p = _store_path()
    try:
        return json.loads(p.read_text())
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
    """The user-saved key for a provider, if any."""
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
    """Forget a saved key. Returns True if one was removed."""
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


def is_enabled(provider: Provider, *, ollama_reachable: bool = False) -> bool:
    if provider.local:
        return ollama_reachable
    return bool(api_key_for(provider))


def provider_for_model(model_id: str) -> Provider | None:
    """Best-effort: which provider serves ``model_id``.

    Exact membership first, then a prefix heuristic (e.g. ``z-ai/...`` →
    OpenRouter, ``accounts/fireworks/...`` → Fireworks, ``glm-*`` → GLM).
    """
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
        "deepseek-": "deepseek",
        "qwen-": "qwen",
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
