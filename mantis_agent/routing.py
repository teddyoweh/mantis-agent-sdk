"""Auto-route a model name to a backend.

The Claude SDK has one backend (Anthropic). We have five provider families —
OpenAI, Anthropic Claude, Google Gemini, xAI Grok, and the open-source world
(Ollama / vLLM / llama.cpp / TGI / Together / Fireworks / Groq / OpenRouter).
To preserve the two-line drop-in story (``import`` + ``model``) we infer the
backend from the model name shape when the user didn't pass one explicitly.

Precedence (high → low):
  1. ``explicit`` (the ``backend=`` kwarg)
  2. ``$MANTIS_AGENT_BASE_URL``
  3. shape-based inference from the model name (see ``infer_backend``)
  4. ``http://localhost:11434`` (Ollama, lowest-effort install)

Inference rules (90% of the catalog falls in one of these buckets):

  * ``deepseek-r1:1.5b``, ``qwen2.5:7b``, ``llama3.2:3b``
      → Ollama tag form (contains ``:`` and no ``/``) → Ollama
  * ``accounts/fireworks/models/...``
      → Fireworks path → Fireworks AI
  * ``Qwen/Qwen2.5-72B-Instruct-Turbo``, ``meta-llama/Meta-Llama-3.1-70B``,
    ``mistralai/Mixtral-8x7B``, ``deepseek-ai/...``, ``google/gemma-...``
      → HuggingFace org/repo shape → Together AI (most popular hosted OSS)
  * ``gpt-4o``, ``gpt-5``, ``o1-...``, ``o3-...``, ``o4-...``
      → OpenAI native
  * ``gemini-...``
      → Google Generative Language API (OpenAI-compat endpoint)
  * ``grok-...``
      → xAI (OpenAI-compat endpoint at ``api.x.ai``)
  * ``claude-...``
      → the literal sentinel ``"anthropic"``: Claude speaks ``/v1/messages``,
        not ``/chat/completions``, so the value is not a URL but the name
        ``providers.base.detect_provider`` maps to the native Anthropic
        adapter. Claude is a first-class provider here — ``ANTHROPIC_API_KEY``
        (or a subscription OAuth token in ``ANTHROPIC_AUTH_TOKEN``) is all
        it needs.
  * anything else → Ollama (the safe fallback; tags without ``:``
    like ``qwen2.5`` also pull happily)
"""

from __future__ import annotations

import os

__all__ = [
    "infer_backend",
    "resolve_backend",
    "hosted_default_url",
    "BackendRoutingError",
]


# Public URL constants — single source of truth so providers, docs, and
# tests reference the same string. Bump these here and the whole package
# follows.
OLLAMA_DEFAULT = "http://localhost:11434"
TOGETHER_DEFAULT = "https://api.together.xyz/v1"
FIREWORKS_DEFAULT = "https://api.fireworks.ai/inference/v1"
OPENAI_DEFAULT = "https://api.openai.com/v1"
GEMINI_DEFAULT = "https://generativelanguage.googleapis.com/v1beta/openai"
GROQ_DEFAULT = "https://api.groq.com/openai/v1"
XAI_DEFAULT = "https://api.x.ai/v1"

#: Not a URL: the backend *name* ``providers.base.detect_provider`` resolves to
#: the native Anthropic Messages adapter. Returned for ``claude-*`` models.
ANTHROPIC_SENTINEL = "anthropic"


class BackendRoutingError(ValueError):
    """Raised when a model name is bound to a backend this SDK cannot reach.

    Kept for API compatibility — no model family is refused any more (Claude
    routes to the native Anthropic adapter), so nothing in the package raises
    it today. Callers that catch it keep working.
    """


def resolve_backend(model: str, explicit: str | None = None) -> str:
    """Return the backend to use for ``model`` — a URL, or the ``"anthropic"``
    sentinel for Claude models.

    Precedence: ``explicit`` > ``$MANTIS_AGENT_BASE_URL`` > inferred from
    model name > Ollama default.
    """

    if explicit:
        return explicit
    env_url = os.environ.get("MANTIS_AGENT_BASE_URL")
    if env_url:
        return env_url
    return infer_backend(model)


def _is_openai_native(lower: str) -> bool:
    return (
        lower.startswith("gpt-")
        or lower.startswith("o1-")
        or lower.startswith("o3-")
        or lower.startswith("o4-")
        or lower in {"o1", "o3", "o4"}
    )


def hosted_default_url(model: str) -> str | None:
    """The first-party hosted endpoint a *bare* model name implies, or ``None``.

    Only the families whose names are unambiguous — OpenAI (``gpt-*`` /
    o-series), Gemini, Grok — return a URL. Everything else (Ollama tags,
    HF ``org/repo`` ids, plain names) returns ``None`` so callers keep their
    own default (``Agent`` keeps vLLM's ``localhost:8000``; ``infer_backend``
    keeps its Ollama/Together rules). Claude is not a URL family — see
    :func:`infer_backend` and the ``"anthropic"`` sentinel.
    """

    lower = (model or "").strip().lower()
    if not lower or lower.startswith("gpt-oss"):
        return None
    if _is_openai_native(lower):
        return OPENAI_DEFAULT
    if lower.startswith("gemini-") or lower.startswith("gemini/"):
        return GEMINI_DEFAULT
    if lower.startswith("grok-") or lower.startswith("grok/"):
        return XAI_DEFAULT
    return None


def infer_backend(model: str) -> str:
    """Pure model-name → backend mapping. No env or override consulted.
    Exposed for testing and for the rare caller that wants the inference
    without ``resolve_backend``'s precedence chain.

    Returns a URL for every family except Claude, which returns the
    ``"anthropic"`` sentinel (the native Messages adapter has no OpenAI-compat
    URL to point at).
    """

    name = (model or "").strip()
    if not name:
        return OLLAMA_DEFAULT

    lower = name.lower()

    # Anthropic Claude — first-class. The sentinel selects the native
    # /v1/messages adapter; credentials come from ANTHROPIC_API_KEY or an
    # OAuth/gateway token in ANTHROPIC_AUTH_TOKEN.
    if lower.startswith("claude-") or lower.startswith("claude/"):
        return ANTHROPIC_SENTINEL

    # Fireworks publishes models under accounts/fireworks/models/<id>.
    if name.startswith("accounts/fireworks/models/"):
        return FIREWORKS_DEFAULT

    # gpt-oss is OpenAI's OPEN-WEIGHT model — it is NOT served on the OpenAI
    # API under that id (it runs on Ollama / vLLM / Groq / Together). The
    # org/repo form (openai/gpt-oss-120b) routes via the HF shape below; every
    # other spelling (bare gpt-oss-120b, colon form gpt-oss:20b) would otherwise
    # fall into the gpt- branch and 404 against api.openai.com. Send it to the
    # safe open-weight default (Ollama).
    if lower.startswith("gpt-oss"):
        return OLLAMA_DEFAULT

    # OpenAI native — gpt-* and o*-mini/o1/o3/o4 reasoning models.
    if _is_openai_native(lower):
        return OPENAI_DEFAULT

    # Gemini via Google Generative Language OpenAI-compat endpoint.
    if lower.startswith("gemini-") or lower.startswith("gemini/"):
        return GEMINI_DEFAULT

    # xAI Grok — OpenAI-compatible at api.x.ai (key: XAI_API_KEY / GROK_API_KEY).
    if lower.startswith("grok-") or lower.startswith("grok/"):
        return XAI_DEFAULT

    # Groq publishes a flat catalog (llama-3.3-70b-versatile,
    # mixtral-8x7b-32768, deepseek-r1-distill-llama-70b, ...). They
    # don't follow a unique shape, so don't infer Groq from name —
    # users opt in via $MANTIS_AGENT_BASE_URL or backend=.

    # HuggingFace org/repo shape → Together AI (most-used hosted OSS).
    # `Qwen/Qwen2.5-72B-Instruct-Turbo`, `meta-llama/...`, etc.
    # Distinguish from Ollama tag form by checking for a slash AND no colon.
    if "/" in name and ":" not in name:
        return TOGETHER_DEFAULT

    # Ollama tag form (`qwen2.5:7b`, `deepseek-r1:1.5b`) or bare model
    # name (`qwen2.5`, `mistral`) — both pull and serve from Ollama.
    return OLLAMA_DEFAULT
