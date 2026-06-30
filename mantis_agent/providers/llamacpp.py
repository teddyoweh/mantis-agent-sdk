"""llama.cpp server adapter.

llama.cpp ships an HTTP server (``llama-server``) with two relevant modes:

**Mode A — Jinja-on (OpenAI-compatible).**
When the server is started with ``--jinja``, it applies the model's chat
template server-side and exposes ``POST /v1/chat/completions`` with the
standard OpenAI streaming shape. In this mode the entire wire format is the
same as vLLM / Together / Fireworks — we delegate to
:class:`OpenAICompatProvider` and just rewrite the base URL.

**Mode B — Raw ``/completion``.**
When started without ``--jinja``, the server only speaks its native
completion API: ``POST /completion`` with ``{"prompt": "<rendered>", "stream":
true, "n_predict": ..., "grammar": "..."?}``. The chat template has to be
rendered **client-side** via :mod:`mantis_agent.templates.jinja` and the
output stream is plain text deltas (no native tool calls — Path B/C
territory).

Mode B is parked for M1 (see ``docs/plan.md`` §7.3). Mode A is what we ship.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any

from ..capabilities import HOSTED_PROFILES, BackendCapability, ModelCapability
from ..events import StreamEvent
from ..types import Message

DEFAULT_BASE_URL = "http://localhost:8080"


class LlamaCppProvider:
    """llama.cpp ``llama-server`` adapter.

    Currently a thin shim over :class:`OpenAICompatProvider` for the
    ``--jinja`` mode. Raw ``/completion`` lands in M1 once the client-side
    Jinja template engine is wired up.
    """

    name = "llamacpp"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        mode: str = "jinja",
        default_headers: dict[str, str] | None = None,
    ) -> None:
        if mode not in ("jinja", "raw"):
            raise ValueError(f"unknown llama.cpp mode {mode!r}; expected 'jinja' or 'raw'")
        if mode == "raw":
            # Mode B — client-side templates. Parked until M1.
            raise NotImplementedError(
                "llama.cpp raw /completion mode (client-side chat templates) "
                "lands in M1; start llama-server with --jinja to use the "
                "OpenAI-compatible /v1/chat/completions endpoint for now."
            )

        # Mode A — delegate to the OpenAI-compat provider with the /v1 prefix.
        # Imported lazily so the cold-start cost of the OpenAI-compat module
        # is only paid when someone actually instantiates this provider.
        from .openai_compat import OpenAICompatProvider  # noqa: PLC0415

        base = (base_url or DEFAULT_BASE_URL).rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"

        self._inner = OpenAICompatProvider(
            api_key=api_key or "sk-no-key",  # llama.cpp ignores auth by default
            base_url=base,
            default_headers=default_headers,
        )
        # Surface llama.cpp's true backend capability — the inner provider's
        # heuristic would mis-detect this as a generic openai_compat backend.
        self.backend_capability: BackendCapability = HOSTED_PROFILES["llamacpp"]

    async def stream(
        self,
        *,
        model: str,
        messages: Iterable[Message],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
        extra: dict[str, Any] | None = None,
        model_capability: ModelCapability | None = None,
    ) -> AsyncIterator[StreamEvent]:
        async for ev in self._inner.stream(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            extra=extra,
            model_capability=model_capability,
        ):
            yield ev

    async def aclose(self) -> None:
        await self._inner.aclose()
