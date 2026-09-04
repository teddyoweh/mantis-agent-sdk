"""Claude on Google Vertex AI.

Same Messages body as the direct API, different envelope:

* the model lives in the **URL**, not the body::

      POST https://{region}-aiplatform.googleapis.com/v1/projects/{project}
           /locations/{region}/publishers/anthropic/models/{model}:streamRawPredict

  (``:rawPredict`` for the non-streaming form, which this adapter doesn't use —
  the SDK always streams.)
* the body carries ``anthropic_version: "vertex-2023-10-16"`` and **no**
  ``model`` field. Sending ``model`` is a 400; omitting ``anthropic_version``
  is a 400.
* auth is a Google OAuth2 bearer token (ADC), not ``x-api-key``, and there is
  no ``anthropic-version`` *header*.
* model ids are Vertex's own spelling — ``claude-opus-4-5@20251101`` — which
  is the Anthropic id with an ``@`` date suffix. :func:`to_vertex_model` maps
  the ids we know and passes anything else through unchanged, so a model
  released after this table still works if the caller spells it Vertex's way.

Everything downstream — the SSE event taxonomy, block assembly, usage — is
identical to the direct API, so the response translation is imported wholesale
from :mod:`mantis_agent.providers.anthropic_passthrough`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable
from typing import Any

import msgspec

from ..capabilities import HOSTED_PROFILES, BackendCapability, ModelCapability
from ..errors import AuthError, ProviderError
from ..events import StreamEvent
from ..http import make_client
from ..types import Message
from .anthropic_passthrough import (
    _iter_normalized_events,
    _raise_if_error,
    build_messages_payload,
)
from .cloud_credentials import CloudCredentialError, google_access_token, google_project

__all__ = [
    "VERTEX_ANTHROPIC_VERSION",
    "AnthropicVertexProvider",
    "to_vertex_body",
    "to_vertex_model",
    "vertex_url",
]

#: The literal Vertex expects in the body in place of ``model``.
VERTEX_ANTHROPIC_VERSION = "vertex-2023-10-16"

#: Anthropic's Vertex docs list the models with an ``@``-dated suffix. Only ids
#: whose Vertex spelling differs from ours need a row; everything else passes
#: through (including anything already carrying an ``@``).
_VERTEX_MODEL_IDS: dict[str, str] = {
    "claude-opus-4-1": "claude-opus-4-1@20250805",
    "claude-opus-4": "claude-opus-4@20250514",
    "claude-sonnet-4": "claude-sonnet-4@20250514",
    "claude-3-7-sonnet": "claude-3-7-sonnet@20250219",
    "claude-3-5-sonnet-v2": "claude-3-5-sonnet-v2@20241022",
    "claude-3-5-haiku": "claude-3-5-haiku@20241022",
}

#: Vertex serves Claude from a handful of regions; ``us-east5`` is the one
#: Anthropic's own docs use for every current model.
DEFAULT_VERTEX_REGION = "us-east5"

_PAYLOAD_ENCODER = msgspec.json.Encoder()


def vertex_region(explicit: str = "") -> str:
    """Region for Vertex: explicit → ``CLOUD_ML_REGION`` → ``VERTEX_REGION``
    → ``GOOGLE_CLOUD_REGION`` → ``us-east5``."""

    for candidate in (explicit, os.environ.get("CLOUD_ML_REGION"),
                      os.environ.get("VERTEX_REGION"),
                      os.environ.get("GOOGLE_CLOUD_REGION")):
        value = (candidate or "").strip()
        if value:
            return value
    return DEFAULT_VERTEX_REGION


def to_vertex_model(model: str) -> str:
    """Our model id in Vertex's spelling. Unknown ids pass through."""

    bare = (model or "").strip()
    if not bare:
        return bare
    if "@" in bare:  # already Vertex-shaped
        return bare
    return _VERTEX_MODEL_IDS.get(bare.lower(), bare)


def vertex_url(
    *, project: str, region: str, model: str, stream: bool = True, base_url: str = ""
) -> str:
    """The publisher-model endpoint for one request."""

    verb = "streamRawPredict" if stream else "rawPredict"
    root = (base_url or f"https://{region}-aiplatform.googleapis.com").rstrip("/")
    return (
        f"{root}/v1/projects/{project}/locations/{region}"
        f"/publishers/anthropic/models/{to_vertex_model(model)}:{verb}"
    )


def to_vertex_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn a direct-API Messages body into Vertex's form.

    Drops ``model`` (it is in the URL) and adds ``anthropic_version``. Both
    halves are load-bearing: Vertex 400s on the extra field and on the missing
    one, and the errors name neither clearly.
    """

    body = {k: v for k, v in payload.items() if k != "model"}
    body["anthropic_version"] = VERTEX_ANTHROPIC_VERSION
    return body


class AnthropicVertexProvider:
    """Claude over Vertex AI's ``publishers/anthropic`` endpoint.

    Credentials come from :func:`cloud_credentials.google_access_token` — an
    exported token, a service-account key, or ``gcloud`` — and are re-read per
    request so a token that expires mid-session refreshes without a restart.
    """

    name = "anthropic_vertex"
    backend_capability: BackendCapability

    def __init__(
        self,
        *,
        project: str | None = None,
        region: str | None = None,
        access_token: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        backend_capability: BackendCapability | None = None,
        model_capability: ModelCapability | None = None,
    ) -> None:
        self.project = (project or google_project() or "").strip()
        if not self.project:
            raise AuthError(
                "Claude on Vertex needs a GCP project — set GOOGLE_CLOUD_PROJECT "
                "(or pass project=…), and a region in CLOUD_ML_REGION."
            )
        self.region = vertex_region(region or "")
        self.base_url = (base_url or "").rstrip("/")
        self._explicit_token = (access_token or "").strip()
        self._default_headers = dict(default_headers or {})
        self.backend_capability = backend_capability or HOSTED_PROFILES["anthropic"]
        self._default_model_capability = model_capability
        # No base_url on the client: each request URL is absolute because the
        # model is part of the path.
        self.client = make_client(headers={"content-type": "application/json"})

    def _headers(self) -> dict[str, str]:
        token = self._explicit_token
        if not token:
            try:
                token = google_access_token()
            except CloudCredentialError as exc:
                raise AuthError(str(exc)) from exc
        if not token:
            raise AuthError(
                "Claude on Vertex needs Google credentials — run "
                "`gcloud auth application-default login`, or set "
                "GOOGLE_APPLICATION_CREDENTIALS to a service-account key."
            )
        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        headers.update(self._default_headers)
        return headers

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
        thinking: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if not model:
            raise ProviderError("AnthropicVertexProvider.stream needs a model name.")
        payload = build_messages_payload(
            model=model, messages=messages, system=system, tools=tools,
            max_tokens=max_tokens, temperature=temperature, extra=extra,
            thinking=thinking, cache=getattr(self, "cache_prompts", True),
        )
        url = vertex_url(project=self.project, region=self.region, model=model,
                         base_url=self.base_url)
        body = _PAYLOAD_ENCODER.encode(to_vertex_body(payload))
        async with self.client.stream(
            "POST", url, content=body, headers=self._headers()
        ) as response:
            await _raise_if_error(response)
            async for ev in _iter_normalized_events(response):
                yield ev

    async def aclose(self) -> None:
        await self.client.aclose()
