"""Auto-route a model name to a backend URL.

The Claude SDK has one backend (Anthropic). We have many — Ollama,
Together, Fireworks, Groq, OpenRouter, vLLM, llama.cpp, TGI, OpenAI.
To preserve the two-line drop-in story (``import`` + ``model``) we
infer the backend from the model name shape when the user didn't
pass one explicitly.

Precedence (high → low):
  1. ``explicit`` (the ``backend=`` kwarg)
  2. ``$MANTIS_AGENT_BASE_URL``
  3. shape-based inference from the model name (see ``infer_backend``)
  4. ``http://localhost:11434`` (Ollama, lowest-effort install)

Inference rules (90% of the OSS catalog falls in one of these buckets):

  * ``deepseek-r1:1.5b``, ``qwen2.5:7b``, ``llama3.2:3b``
      → Ollama tag form (contains ``:`` and no ``/``) → Ollama
  * ``accounts/fireworks/models/...``
      → Fireworks path → Fireworks AI
  * ``Qwen/Qwen2.5-72B-Instruct-Turbo``, ``meta-llama/Meta-Llama-3.1-70B``,
    ``mistralai/Mixtral-8x7B``, ``deepseek-ai/...``, ``google/gemma-...``
      → HuggingFace org/repo shape → Together AI (most popular hosted OSS)
  * ``gpt-4o``, ``gpt-4o-mini``, ``o1-...``, ``o3-...``, ``o4-...``
      → OpenAI native
  * ``gemini-...``
      → Google Generative Language API (OpenAI-compat endpoint)
  * ``claude-...``
      → raise — we don't proxy Anthropic. Tell the user to use
        the real ``claude-agent-sdk`` for Claude models.
  * anything else → Ollama (the safe fallback; tags without ``:``
    like ``qwen2.5`` also pull happily)
"""

from __future__ import annotations

import os

__all__ = ["infer_backend", "resolve_backend", "BackendRoutingError"]


# Public URL constants — single source of truth so providers, docs, and
# tests reference the same string. Bump these here and the whole package
# follows.
OLLAMA_DEFAULT = "http://localhost:11434"
TOGETHER_DEFAULT = "https://api.together.xyz/v1"
FIREWORKS_DEFAULT = "https://api.fireworks.ai/inference/v1"
OPENAI_DEFAULT = "https://api.openai.com/v1"
GEMINI_DEFAULT = "https://generativelanguage.googleapis.com/v1beta/openai"
GROQ_DEFAULT = "https://api.groq.com/openai/v1"


class BackendRoutingError(ValueError):
    """Raised when a model name is unambiguously bound to a backend
    we don't (and won't) proxy — currently just Anthropic Claude.
    """


def resolve_backend(model: str, explicit: str | None = None) -> str:
    """Return the backend URL to use for ``model``.

    Precedence: ``explicit`` > ``$MANTIS_AGENT_BASE_URL`` > inferred from
    model name > Ollama default. Raises :class:`BackendRoutingError`
    only when the model name points at a backend we deliberately refuse
    (Anthropic Claude — the user should use ``claude-agent-sdk``
    directly for those).
    """

    if explicit:
        return explicit
    env_url = os.environ.get("MANTIS_AGENT_BASE_URL")
    if env_url:
        return env_url
    return infer_backend(model)


def infer_backend(model: str) -> str:
    """Pure model-name → backend URL mapping. No env or override
    consulted. Exposed for testing and for the rare caller that wants
    the inference without ``resolve_backend``'s precedence chain.
    """

    name = (model or "").strip()
    if not name:
        return OLLAMA_DEFAULT

    lower = name.lower()

    # Anthropic — refuse loudly. We don't proxy Claude.
    if lower.startswith("claude-") or lower.startswith("claude/"):
        raise BackendRoutingError(
            f"Model {model!r} looks like an Anthropic Claude model. "
            "mantis-agent-sdk doesn't proxy Anthropic — use the real "
            "claude-agent-sdk for Claude. If you meant a different "
            "model that happens to share the prefix, pass "
            "backend=... explicitly to bypass routing."
        )

    # Fireworks publishes models under accounts/fireworks/models/<id>.
    if name.startswith("accounts/fireworks/models/"):
        return FIREWORKS_DEFAULT

    # OpenAI native — gpt-* and o*-mini/o1/o3/o4 reasoning models.
    if lower.startswith("gpt-") or lower.startswith("o1-") or lower.startswith("o3-") or lower.startswith("o4-") or lower in {"o1", "o3", "o4"}:
        return OPENAI_DEFAULT

    # Gemini via Google Generative Language OpenAI-compat endpoint.
    if lower.startswith("gemini-") or lower.startswith("gemini/"):
        return GEMINI_DEFAULT

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
