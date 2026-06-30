"""Provider adapters.

Importing ``mantis_agent.providers`` doesn't pull any specific backend —
each adapter is imported lazily by ``resolve()`` so a user who only wants
Ollama doesn't pay the cost of importing the OpenAI-compat HTTP code.
"""

from __future__ import annotations

from .base import HTTPProviderMixin, Provider, detect_provider, register, resolve

__all__ = [
    "HTTPProviderMixin",
    "Provider",
    "detect_provider",
    "register",
    "resolve",
]
