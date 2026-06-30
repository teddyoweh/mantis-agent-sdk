"""Hugging Face Text Generation Inference (TGI) adapter.

TGI 2.x ships an OpenAI-compatible endpoint at ``POST /v1/chat/completions``
that handles native tool calls and applies the model's Jinja chat template
server-side. We treat that as the default path and delegate to
:class:`OpenAICompatProvider`.

TGI also accepts a non-standard ``grammar`` field on the request body that
constrains sampling to a JSON schema or a GBNF grammar (Path C). Callers
pass it through this adapter via the ``grammar=`` kwarg or the generic
``extra`` mapping; we forward it on the wire.

Default base URL is ``http://localhost:3000/v1`` matching TGI's container
default port.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any

from ..capabilities import HOSTED_PROFILES, BackendCapability, ModelCapability
from ..events import StreamEvent
from ..types import Message

DEFAULT_BASE_URL = "http://localhost:3000/v1"


class TGIProvider:
    """HF Text Generation Inference adapter.

    Thin shim over :class:`OpenAICompatProvider`. The only TGI-specific bit
    is grammar pass-through and the backend capability override (TGI
    advertises grammar support; raw OpenAI-compat heuristics would mark this
    as ``supports_grammar=False`` for an unknown URL).
    """

    name = "tgi"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        # Lazy import to keep cold-start fast — users who never instantiate
        # TGIProvider don't pay for OpenAICompatProvider's import.
        from .openai_compat import OpenAICompatProvider  # noqa: PLC0415

        base = (base_url or DEFAULT_BASE_URL).rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"

        self._inner = OpenAICompatProvider(
            api_key=api_key or "sk-no-key",  # TGI typically runs without auth
            base_url=base,
            default_headers=default_headers,
        )
        # Surface TGI's true capability (native grammar, no native_tools on
        # older builds; v2.x has both — caller can override).
        self.backend_capability: BackendCapability = HOSTED_PROFILES["tgi"]

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
        grammar: dict[str, Any] | str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream events from TGI.

        Parameters mirror the Provider protocol with one extension: ``grammar``
        is forwarded verbatim to TGI in the request body. Either a JSON-schema
        dict (``{"type": "json", "value": {...}}``) or a raw GBNF string
        (``{"type": "regex"|"json", ...}``) is accepted — we don't validate
        the shape because TGI itself accepts both depending on build flags.
        """

        # Merge grammar into ``extra`` so OpenAICompatProvider passes it
        # through on the body. We intentionally avoid mutating the caller's
        # dict.
        merged_extra: dict[str, Any] = dict(extra) if extra else {}
        if grammar is not None and "grammar" not in merged_extra:
            merged_extra["grammar"] = grammar

        async for ev in self._inner.stream(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            extra=merged_extra or None,
            model_capability=model_capability,
        ):
            yield ev

    async def aclose(self) -> None:
        await self._inner.aclose()
