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
import time
from dataclasses import dataclass
from typing import Any

from .paths import get_mantis_agent_dir


# ---------------------------------------------------------------------------
# 1. Local (Ollama) — curated open-weight models you can pull for free
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class Provider:
    id: str  # short slug, e.g. "deepseek"
    label: str  # display name
    base_url: str  # OpenAI-compat base URL
    api_key_env: str  # env var holding the key
    models: tuple[str, ...]  # a few flagship model ids
    note: str = ""  # signup hint
    key_env_aliases: tuple[str, ...] = ()  # alternate env var names also honored


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
        key_env_aliases=("ZAI_API_KEY", "ZHIPU_API_KEY"),
    ),
    Provider(
        "qwen", "Qwen (DashScope)",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY",
        ("qwen-max", "qwen-plus", "qwen3-235b-a22b", "qwen3-coder-plus"),
        "dashscope-intl.aliyuncs.com · Qwen3",
        key_env_aliases=("QWEN_API_KEY",),  # intuitive name for the "Qwen" label
    ),
    Provider(
        "groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
        ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "moonshotai/kimi-k2-instruct-0905",
         "qwen/qwen3-32b", "llama-3.3-70b-versatile"),
        "console.groq.com · very fast · hosts gpt-oss",
    ),
    Provider(
        "openai", "OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY",
        ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4-pro"),
        "platform.openai.com · GPT-5.6", 
    ),
    Provider(
        "gemini", "Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY",
        ("gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"),
        "aistudio.google.com",
        key_env_aliases=("GOOGLE_API_KEY",),
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
    # Anthropic is NOT OpenAI-compatible — mantis routes api.anthropic.com to the
    # anthropic_passthrough provider, and its /models + auth use x-api-key +
    # anthropic-version headers (handled by _models_headers below). Listed here
    # so it shows up in `mantis setup` and /models like any other provider.
    Provider(
        "anthropic", "Claude (Anthropic)", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY",
        ("claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-fable-5"),
        "console.anthropic.com · Claude (Opus/Sonnet/Haiku)",
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
    key = key.strip()  # defensive: never store a paste's stray whitespace/newline
    data = _load_store()
    data.setdefault("keys", {})[provider_id] = key
    _save_store(data)
    prov = BY_ID.get(provider_id)
    if prov and prov.api_key_env:
        os.environ[prov.api_key_env] = key


def clear_key(provider_id: str) -> bool:
    data = _load_store()
    keys = data.get("keys") or {}
    removed = provider_id in keys
    if removed:
        del keys[provider_id]
        _save_store(data)
    # Mirror set_key, which also exports the key into the process env — otherwise
    # api_key_for() would still find it there and the provider would stay
    # "enabled" after a /disable. (A shell-exported var reappears next launch,
    # which is the right behavior — that's the user's standing choice.)
    prov = BY_ID.get(provider_id)
    if prov and prov.api_key_env:
        os.environ.pop(prov.api_key_env, None)
    return removed


def looks_like_model_id(s: Any) -> bool:
    """Is ``s`` a syntactically plausible model id? Real ids are a single token of
    ``[A-Za-z0-9._:+/-]`` — no whitespace, no newlines, no empties. This is the
    gate that stops half-typed picker filters ('  m\\n', 'my-model\\n') and stray
    paste noise from being persisted as a 'model' and then blindly sent to a
    backend next launch (a silent 404). It is a *syntax* check, not a claim the
    model exists — 'newest' passes here but is caught by real resolution."""
    if not isinstance(s, str):
        return False
    t = s.strip()
    if not t or len(t) > 200:
        return False
    return all(c.isalnum() or c in "._:+/-@" for c in t)


def get_last_model() -> dict[str, Any] | None:
    """The model used at the end of the last session, if recorded:
    ``{"model": str, "backend": str | None}``. Lets ``mantis`` reopen on the
    model you left off with instead of always reverting to the default. A stored
    id that isn't a plausible model token (legacy garbage) is ignored."""
    rec = _load_store().get("last")
    if isinstance(rec, dict) and looks_like_model_id(rec.get("model")):
        return {**rec, "model": rec["model"].strip()}
    return None


def set_last_model(model: str, backend: str | None = None) -> None:
    """Remember the active model + backend for next launch (no key is stored
    here — hosted keys live under ``keys`` and are looked up by provider).
    Silently ignores an implausible id so garbage can never be persisted."""
    if not looks_like_model_id(model):
        return
    data = _load_store()
    data["last"] = {"model": model.strip(),
                    "backend": backend.strip() if isinstance(backend, str) else backend}
    _save_store(data)


RECENT_MAX = 8


def get_recent_models() -> list[str]:
    """Most-recently-used model ids, newest first (for a 'Recent' shortcut).
    Self-heals: strips and drops any implausible legacy entries."""
    rec = _load_store().get("recent")
    if not isinstance(rec, list):
        return []
    out: list[str] = []
    for m in rec:
        if looks_like_model_id(m) and m.strip() not in out:
            out.append(m.strip())
    return out


def push_recent_model(model: str) -> None:
    """Move ``model`` to the front of the MRU list (deduped, capped). Ignores an
    implausible id (so a stray picker filter never pollutes the recents)."""
    if not looks_like_model_id(model):
        return
    model = model.strip()
    data = _load_store()
    recent = [m for m in (data.get("recent") or [])
              if isinstance(m, str) and m.strip() and m.strip() != model]
    recent.insert(0, model)
    data["recent"] = recent[:RECENT_MAX]
    _save_store(data)


def api_key_for(provider: Provider) -> str | None:
    """Key from the environment first, then the saved store. Honors alternate env
    var names (e.g. GOOGLE_API_KEY for Gemini) so a direct-env user's key is found
    whichever common convention they used."""
    for var in (provider.api_key_env, *provider.key_env_aliases):
        v = os.environ.get(var) if var else None
        if v and v.strip():
            # Strip stray whitespace/newlines — .env files and copy-paste often
            # add a trailing \n, which would otherwise auth-fail confusingly.
            return v.strip()
    saved = saved_key(provider.id)
    return saved.strip() if saved else saved


def is_enabled(provider: Provider) -> bool:
    if api_key_for(provider):
        return True
    # Anthropic can also be authed by an OAuth / gateway Bearer token, which
    # rides ANTHROPIC_AUTH_TOKEN rather than the x-api-key store.
    if provider.id == "anthropic" and os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return False


def anthropic_bearer_backend(provider: Provider | None, current_backend: str | None = None) -> str | None:
    """Backend URL to wire for an Anthropic provider authed by an env OAuth/gateway
    Bearer token (``ANTHROPIC_AUTH_TOKEN``), which :func:`api_key_for` does not
    return. The caller must set ``api_key=None`` so the passthrough reads the
    Bearer token from the environment. Preserves an existing Anthropic backend
    (e.g. a Bedrock/Vertex gateway) rather than yanking it onto the default API.
    Returns ``None`` when this doesn't apply (non-Anthropic, or no token)."""
    if provider is None or provider.id != "anthropic":
        return None
    if not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return None
    from .providers.base import detect_provider  # noqa: PLC0415
    if current_backend and detect_provider(current_backend) == "anthropic_passthrough":
        return current_backend
    return provider.base_url


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
        "claude-": "anthropic",
        "claude/": "anthropic",
    }
    for pre, pid in prefixes.items():
        if low.startswith(pre):
            return BY_ID.get(pid)
    return None


def enabled_provider_models() -> list[tuple[str, str]]:
    """Every model from every ENABLED hosted provider (one with a saved or env
    key), as ``(model_id, provider_label)`` pairs. Prefers each provider's cached
    live ``/v1/models`` (fresh within the TTL) and falls back to its flagship
    list. Powers the unified in-app ``/models`` picker so a user can jump to any
    provider they've set up — not just the active backend. Deduplicated by id."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for p in CATALOG:
        if not is_enabled(p):
            continue
        models = cached_live_models(p.id) or list(p.models)
        for m in models:
            if m and m not in seen:
                seen.add(m)
                out.append((m, p.label))
    return out


def grouped_provider_models() -> list[dict[str, Any]]:
    """One group per hosted provider for the unified ``/models`` picker — ENABLED
    providers first, then disabled (so you can *see* every model and enable a
    provider on demand). Each group is ``{label, provider_id, enabled, note,
    models}``; models come from cached live ``/v1/models`` when fresh, else the
    flagship list."""
    enabled = [p for p in CATALOG if is_enabled(p)]
    disabled = [p for p in CATALOG if not is_enabled(p)]
    out: list[dict[str, Any]] = []
    for p in enabled + disabled:
        out.append({
            "label": p.label,
            "provider_id": p.id,
            "enabled": is_enabled(p),
            "note": p.note,
            "models": list(cached_live_models(p.id) or p.models),
        })
    return out


# ---------------------------------------------------------------------------
# Model-query resolution — turn a user's `/model <query>` into a real model
# ---------------------------------------------------------------------------
#
# The single brain behind `/model <x>` and the picker's type-to-jump. It makes
# the command forgiving the way Claude Code's model switch is: a number, an
# exact id, a provider name, a tier word, an alias, or a fuzzy fragment all
# land on a *real* model — and anything it can't pin down is reported, never
# silently sent to a backend as-is (that was the "newest"/404 bug).


# Words that name a whole provider → jump to that provider's flagship (models[0]).
# A provider word is friendlier than a substring match (e.g. "gpt" would be
# ambiguous across gpt-5.4 / -mini / -nano / gpt-oss; the user means "OpenAI").
_PROVIDER_ALIASES: dict[str, str] = {
    "openai": "openai", "gpt": "openai", "chatgpt": "openai",
    "anthropic": "anthropic", "claude": "anthropic",
    "gemini": "gemini", "google": "gemini",
    "deepseek": "deepseek",
    "moonshot": "moonshot", "kimi": "moonshot",
    "qwen": "qwen", "alibaba": "qwen", "dashscope": "qwen",
    "glm": "glm", "zai": "glm", "z-ai": "glm", "zhipu": "glm",
    "groq": "groq",
    "openrouter": "openrouter",
    "together": "together",
    "fireworks": "fireworks",
    "cerebras": "cerebras",
}

# Aliases meaning "the freshest model" — resolved against the active list first
# (the caller passes it newest-first), then the most recent model.
_NEWEST_ALIASES = frozenset({"newest", "latest", "new", "recent", "flagship"})

# Every word that is a *query alias* rather than a real model id. If one of these
# ever ends up persisted as "the model" (a legacy build that stored the raw
# `/model` arg), restore re-resolves it instead of sending it to a backend.
RESOLVER_ALIAS_WORDS: frozenset[str] = _NEWEST_ALIASES | frozenset(_PROVIDER_ALIASES)


@dataclass(frozen=True)
class ModelResolution:
    """Outcome of :func:`resolve_model_query`.

    * ``model`` set  → a concrete id to switch to (with ``provider_id`` when the
      provider is known, so the caller wires the right backend/key).
    * ``model`` None + ``candidates`` non-empty → ambiguous; the caller should
      open the picker filtered by ``query`` so the user disambiguates.
    * ``model`` None + no candidates → nothing matched at all.
    """
    model: str | None
    provider_id: str | None
    reason: str  # "index" | "exact" | "newest" | "provider" | "fuzzy" | "none"
    candidates: tuple[str, ...] = ()


def _model_universe(active_models: list[str] | None) -> list[tuple[str, str | None]]:
    """Every id we could resolve to, as ``(model_id, provider_id | None)``:
    the active backend's models first (provider_id None → keep current backend),
    then every catalog provider's live/flagship models. Order matters — earlier
    entries win ties so a fuzzy fragment prefers the model you're already on."""
    universe: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()

    def add(m: str, pid: str | None) -> None:
        m = m.strip()
        if m and (m, pid) not in seen:
            seen.add((m, pid))
            universe.append((m, pid))

    for m in active_models or []:
        if isinstance(m, str):
            add(m, None)
    for p in CATALOG:
        for m in (cached_live_models(p.id) or p.models):
            add(m, p.id)
    return universe


def resolve_model_query(query: str, active_models: list[str] | None = None) -> ModelResolution:
    """Resolve a free-form ``/model`` argument to a concrete model.

    Resolution order (first hit wins):
      1. **number** — 1-based index into ``active_models`` (the on-screen list).
      2. **exact id** — case-sensitive, then case-insensitive.
      3. **newest/latest** alias — freshest active (or most-recent) model.
      4. **provider name** — 'openai', 'claude', 'gemini'… → that provider's flagship.
      5. **fuzzy** — case-insensitive substring of an id; unique → that model,
         several → ambiguous (caller opens the filtered picker).
    Returns a :class:`ModelResolution`; ``.model is None`` means "couldn't pin
    it down" — never guess a literal string onto the backend."""
    q = (query or "").strip()
    if not q:
        return ModelResolution(None, None, "none")
    ql = q.lower()
    active = [m.strip() for m in (active_models or []) if isinstance(m, str) and m.strip()]

    # 1. numeric index into the visible list
    if q.isdigit() and active:
        i = int(q)
        if 1 <= i <= len(active):
            return ModelResolution(active[i - 1], None, "index")

    universe = _model_universe(active)

    # 2. exact id (case-sensitive wins, then case-insensitive)
    for m, pid in universe:
        if m == q:
            return ModelResolution(m, pid, "exact")
    for m, pid in universe:
        if m.lower() == ql:
            return ModelResolution(m, pid, "exact")

    # 3. "newest" / "latest" → freshest we know of
    if ql in _NEWEST_ALIASES:
        if active:
            return ModelResolution(active[0], None, "newest")
        for m in get_recent_models():
            prov = provider_for_model(m)
            return ModelResolution(m, prov.id if prov else None, "newest")

    # 4. provider name → that provider's flagship (first live/flagship id)
    pid = _PROVIDER_ALIASES.get(ql)
    if pid:
        p = BY_ID.get(pid)
        if p:
            models = cached_live_models(p.id) or list(p.models)
            if models:
                return ModelResolution(models[0].strip(), p.id, "provider")

    # 5. fuzzy substring — unique match resolves, several → let the user choose
    subs = [(m, mpid) for m, mpid in universe if ql in m.lower()]
    if len(subs) == 1:
        return ModelResolution(subs[0][0], subs[0][1], "fuzzy")
    if len(subs) > 1:
        # An active-backend hit is unambiguous even if the same id also lives
        # under a provider group — prefer it and resolve rather than nag.
        active_hit = next((x for x in subs if x[1] is None), None)
        if active_hit is not None:
            return ModelResolution(active_hit[0], None, "fuzzy")
        uniq: list[str] = []
        for m, _ in subs:
            if m not in uniq:
                uniq.append(m)
        if len(uniq) == 1:
            return ModelResolution(subs[0][0], subs[0][1], "fuzzy")
        return ModelResolution(None, None, "fuzzy", tuple(uniq[:20]))

    return ModelResolution(None, None, "none")


# ---------------------------------------------------------------------------
# Live-model cache (~/.mantis-agent/live_models.json)
#
# A provider's /v1/models is a network call (slow, sometimes 2.5s). Persisting
# the result with a TTL lets the selector open *instantly* off the last cached
# answer and refresh in the background — so it's both fast and current across
# sessions, not just within one.
# ---------------------------------------------------------------------------

LIVE_TTL_S = 24 * 3600  # a day; model menus barely change


def _live_path() -> Any:
    return get_mantis_agent_dir() / "live_models.json"


def _load_live() -> dict[str, Any]:
    try:
        return json.loads(_live_path().read_text())
    except Exception:  # noqa: BLE001 — missing / corrupt → empty
        return {}


def cached_live_models(provider_id: str, *, ttl_s: float = LIVE_TTL_S) -> list[str] | None:
    """The last fetched model list for a provider, if still fresh. Instant
    (disk read, no network). ``None`` when absent or stale."""
    rec = _load_live().get(provider_id)
    if not isinstance(rec, dict):
        return None
    if time.time() - float(rec.get("ts", 0)) > ttl_s:
        return None
    models = rec.get("models")
    if not isinstance(models, list):
        return None
    # Filter on read too, so a cache written by an older build (or hand-edited)
    # can't surface a bogus id as a real model.
    clean = [m for m in models if _is_cacheable_model_id(m)]
    return clean or None


def _is_cacheable_model_id(m: Any) -> bool:
    """A live-cache entry must be a plausible id AND not a bare query alias — no
    provider ships a model literally named 'newest' or 'claude', and caching one
    would make it win the resolver's exact-match step over the real alias."""
    return looks_like_model_id(m) and m.strip().lower() not in RESOLVER_ALIAS_WORDS


def store_live_models(provider_id: str, models: list[str]) -> None:
    # Only persist plausible ids: a flaky /v1/models response (or an id colliding
    # with a query alias like "newest") must never poison the cache and later
    # resolve as a real model.
    clean = [m.strip() for m in models if _is_cacheable_model_id(m)]
    data = _load_live()
    data[provider_id] = {"models": clean, "ts": time.time()}
    p = _live_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _models_headers(provider: Provider, key: str | None) -> dict[str, str]:
    """Auth headers for a provider's ``/models`` probe. Anthropic uses
    ``x-api-key`` + ``anthropic-version`` (not OpenAI's ``Bearer``)."""
    if not key:
        return {}
    if provider.id == "anthropic" or "anthropic.com" in provider.base_url:
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"}


def validate_provider(provider: Provider, *, timeout: float = 4.0) -> tuple[bool, str]:
    """Check that a provider's key + endpoint actually work, by hitting
    ``/v1/models``. Returns ``(ok, detail)`` — e.g. ``(True, "42 models")`` or
    ``(False, "invalid API key")`` — so a freshly-entered key surfaces a clear
    pass/fail immediately instead of silently failing on the first chat turn."""
    import httpx  # noqa: PLC0415

    key = api_key_for(provider)
    if not key:
        return False, "no API key set"
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{provider.base_url.rstrip('/')}/models",
                      headers=_models_headers(provider, key))
    except Exception:  # noqa: BLE001
        return False, "can't reach endpoint"
    if r.status_code in (401, 403):
        return False, "invalid API key"
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}"
    try:
        n = len(r.json().get("data", []))
    except Exception:  # noqa: BLE001
        n = 0
    return True, (f"{n} models available" if n else "key accepted")


def refresh_live_models(provider: Provider, *, timeout: float = 2.5) -> list[str] | None:
    """Fetch a provider's live /v1/models and persist it. Best-effort: returns
    the ids (and caches them) on success, ``None`` on any failure. Intended to
    run off the UI thread; pair with :func:`cached_live_models` for reads."""
    import httpx  # noqa: PLC0415

    key = api_key_for(provider)
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{provider.base_url.rstrip('/')}/models",
                      headers=_models_headers(provider, key))
            r.raise_for_status()
            data = r.json().get("data", [])
            # Newest first — OpenAI et al. return a "created" epoch, so the shiny
            # recent models surface at the top instead of buried under legacy
            # ones. Providers without "created" keep their original (stable) order.
            data.sort(key=lambda m: m.get("created", 0) or 0, reverse=True)
            ids = [m.get("id", "") for m in data if m.get("id")]
    except Exception:  # noqa: BLE001
        return None
    store_live_models(provider.id, ids)
    return ids or None


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
    "looks_like_model_id",
    "ModelResolution",
    "resolve_model_query",
    "RESOLVER_ALIAS_WORDS",
    "get_last_model",
    "set_last_model",
    "get_recent_models",
    "push_recent_model",
    "RECENT_MAX",
    "LIVE_TTL_S",
    "cached_live_models",
    "store_live_models",
    "refresh_live_models",
    "validate_provider",
]
