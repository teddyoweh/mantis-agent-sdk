"""Claude on Amazon Bedrock.

Same Messages body as the direct API; three things differ:

* the endpoint is
  ``POST https://bedrock-runtime.{region}.amazonaws.com/model/{modelId}/invoke-with-response-stream``
  — the model is in the path, so the body carries no ``model``, and no
  ``stream`` flag either (the endpoint *is* the streaming one).
* the body carries ``anthropic_version: "bedrock-2023-05-31"``.
* requests are signed with AWS SigV4 (service ``bedrock``), and the response
  is an ``vnd.amazon.eventstream`` byte stream rather than SSE — each frame's
  payload is ``{"bytes": "<base64 of one Anthropic SSE event>"}``, so after
  unwrapping, the event taxonomy is exactly the direct API's and the
  translation is imported from the passthrough adapter.

Model ids are Bedrock's: ``anthropic.claude-sonnet-4-5-20250929-v1:0``, or an
**inference profile** id like ``us.anthropic.claude-opus-4-5-20251101-v1:0``.
Current Claude models are only reachable through a cross-region profile, so
:func:`to_bedrock_model` adds the geography prefix by default; anything that
already looks like a Bedrock id (contains a dot or a colon) passes through
untouched.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx
import msgspec

from ..anthropic_auth import sigv4_headers
from ..capabilities import HOSTED_PROFILES, BackendCapability, ModelCapability
from ..errors import AuthError, ProviderError
from ..events import StreamEvent
from ..http import make_client
from ..types import Message
from .anthropic_passthrough import _frame_to_events, build_messages_payload
from .aws_eventstream import EventStreamError, decode_frames
from .cloud_credentials import AwsCredentials, aws_credentials, aws_region

__all__ = [
    "BEDROCK_ANTHROPIC_VERSION",
    "AnthropicBedrockProvider",
    "bedrock_url",
    "to_bedrock_body",
    "to_bedrock_model",
]

#: The literal Bedrock expects in the body in place of ``model``.
BEDROCK_ANTHROPIC_VERSION = "bedrock-2023-05-31"

#: Our id → the Bedrock base model id (no geography prefix; that is added by
#: :func:`to_bedrock_model` unless the caller opted out). Only ids whose
#: Bedrock spelling we know need a row — everything else passes through, so a
#: model released after this table still works when spelled Bedrock's way.
_BEDROCK_MODEL_IDS: dict[str, str] = {
    "claude-opus-4-1": "anthropic.claude-opus-4-1-20250805-v1:0",
    "claude-opus-4": "anthropic.claude-opus-4-20250514-v1:0",
    "claude-sonnet-4": "anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-3-7-sonnet": "anthropic.claude-3-7-sonnet-20250219-v1:0",
    "claude-3-5-sonnet": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-haiku": "anthropic.claude-3-5-haiku-20241022-v1:0",
    "claude-3-haiku": "anthropic.claude-3-haiku-20240307-v1:0",
}

#: Region prefix → cross-region inference profile geography.
_GEOGRAPHY_BY_PREFIX = {
    "us": "us", "ca": "us", "eu": "eu", "ap": "apac", "sa": "us", "me": "eu",
    "af": "eu", "il": "eu",
}

_PAYLOAD_ENCODER = msgspec.json.Encoder()


def inference_geography(region: str) -> str:
    """The inference-profile prefix for a region (``us-east-1`` → ``us``)."""

    return _GEOGRAPHY_BY_PREFIX.get((region or "").split("-", 1)[0].lower(), "us")


def to_bedrock_model(model: str, *, region: str = "", profile: bool = True) -> str:
    """Our model id in Bedrock's spelling.

    ``profile=True`` (the default) prefixes the cross-region inference profile
    geography, which is how every current Claude model is invoked; pass
    ``profile=False`` for an on-demand base model id. An id that already looks
    like Bedrock's (has a ``.`` or a ``:``) is returned unchanged.
    """

    bare = (model or "").strip()
    if not bare:
        return bare
    if "." in bare or ":" in bare:
        return bare
    mapped = _BEDROCK_MODEL_IDS.get(bare.lower())
    if mapped is None:
        # Unknown but clearly ours: Bedrock ids are the Anthropic id with an
        # ``anthropic.`` prefix and a ``-v1:0`` suffix. Guessing the undated
        # form is better than sending a name Bedrock certainly rejects, and
        # the 400 it produces names the model.
        mapped = f"anthropic.{bare}-v1:0"
    if profile:
        return f"{inference_geography(region)}.{mapped}"
    return mapped


def bedrock_url(*, region: str, model: str, stream: bool = True, base_url: str = "") -> str:
    """The runtime endpoint for one request."""

    verb = "invoke-with-response-stream" if stream else "invoke"
    root = (base_url or f"https://bedrock-runtime.{region}.amazonaws.com").rstrip("/")
    return f"{root}/model/{to_bedrock_model(model, region=region)}/{verb}"


def to_bedrock_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn a direct-API Messages body into Bedrock's form.

    Drops ``model`` (it is in the path) and ``stream`` (the endpoint decides),
    and adds ``anthropic_version``.
    """

    body = {k: v for k, v in payload.items() if k not in ("model", "stream")}
    body["anthropic_version"] = BEDROCK_ANTHROPIC_VERSION
    return body


class AnthropicBedrockProvider:
    """Claude over Bedrock's runtime endpoint, signed with SigV4.

    Credentials come from :func:`cloud_credentials.aws_credentials` — the
    environment, a named profile in ``~/.aws``, or boto3's chain when boto3 is
    installed — and are re-resolved per request so a rotated session token is
    picked up without a restart.
    """

    name = "anthropic_bedrock"
    backend_capability: BackendCapability

    def __init__(
        self,
        *,
        region: str | None = None,
        profile: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        backend_capability: BackendCapability | None = None,
        model_capability: ModelCapability | None = None,
    ) -> None:
        self.region = aws_region(region or "")
        self._profile = profile or os.environ.get("AWS_PROFILE") or None
        self._explicit: AwsCredentials | None = None
        if access_key_id and secret_access_key:
            self._explicit = AwsCredentials(
                access_key_id, secret_access_key, session_token or "", self.region, "explicit"
            )
        self.base_url = (base_url or "").rstrip("/")
        self._default_headers = dict(default_headers or {})
        self.backend_capability = backend_capability or HOSTED_PROFILES["anthropic"]
        self._default_model_capability = model_capability
        self.client = make_client()

    def _credentials(self) -> AwsCredentials:
        creds = self._explicit or aws_credentials(profile=self._profile)
        if creds is None:
            raise AuthError(
                "Claude on Bedrock needs AWS credentials — export "
                "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (plus AWS_REGION), or "
                "set AWS_PROFILE to a profile in ~/.aws/credentials."
            )
        return creds

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
            raise ProviderError("AnthropicBedrockProvider.stream needs a model name.")
        creds = self._credentials()
        payload = build_messages_payload(
            model=model, messages=messages, system=system, tools=tools,
            max_tokens=max_tokens, temperature=temperature, extra=extra,
            thinking=thinking,
            # Bedrock supports prompt caching on current Claude models, but
            # rejects the marker on older ones with an error that reads like a
            # malformed request; the direct API is where caching earns its
            # keep, so default it off here.
            cache=getattr(self, "cache_prompts", False),
        )
        url = bedrock_url(region=self.region, model=model, base_url=self.base_url)
        body = _PAYLOAD_ENCODER.encode(to_bedrock_body(payload))
        headers = sigv4_headers(
            method="POST", url=url, body=body, region=self.region, service="bedrock",
            access_key_id=creds.access_key_id,
            secret_access_key=creds.secret_access_key,
            session_token=creds.session_token,
            extra_headers={
                "content-type": "application/json",
                "accept": "application/vnd.amazon.eventstream",
            },
        )
        headers.update(self._default_headers)

        async with self.client.stream("POST", url, content=body, headers=headers) as response:
            if response.status_code >= 400:
                await response.aread()
                _raise_bedrock_error(response, self.region, model)
            buffer = b""
            state: dict[int, dict[str, Any]] = {}
            async for chunk in response.aiter_bytes():
                buffer += chunk
                try:
                    frames, buffer = decode_frames(buffer)
                except EventStreamError as exc:
                    raise ProviderError(f"Bedrock event-stream error: {exc}") from exc
                for frame in frames:
                    for payload_str in _frame_payloads(frame):
                        async for ev in _frame_to_events(None, payload_str, state):
                            yield ev

    async def aclose(self) -> None:
        await self.client.aclose()


def _frame_payloads(frame: Any) -> list[str]:
    """The Anthropic event JSON carried by one event-stream frame.

    A ``chunk`` frame wraps the event as base64 under ``bytes``; an
    ``exception`` frame carries the error directly and must not be swallowed —
    Bedrock reports mid-stream throttling and validation failures that way,
    and dropping them ends the turn silently with a partial answer.
    """

    message_type = frame.message_type or "event"
    raw = frame.payload.decode("utf-8", "replace")
    if message_type in ("exception", "error"):
        try:
            detail = json.loads(raw)
            message = detail.get("message") or detail.get("Message") or raw
        except ValueError:
            message = raw
        raise ProviderError(
            f"Bedrock stream error ({frame.headers.get(':exception-type') or 'error'}): {message}"
        )
    try:
        wrapper = json.loads(raw)
    except ValueError:
        return []
    encoded = wrapper.get("bytes") if isinstance(wrapper, dict) else None
    if encoded is None:
        # Some frames (``:event-type: metadata``) carry no model event.
        return []
    try:
        return [base64.b64decode(encoded).decode("utf-8", "replace")]
    except (ValueError, TypeError):
        return []


def _raise_bedrock_error(response: httpx.Response, region: str, model: str) -> None:
    """Translate a Bedrock HTTP failure, with the region/model hint its own
    error message leaves out."""

    text = response.content.decode("utf-8", "replace")
    try:
        detail = json.loads(text)
        message = detail.get("message") or detail.get("Message") or text
    except ValueError:
        message = text or response.reason_phrase
    status = response.status_code
    if status in (401, 403):
        raise AuthError(
            f"Bedrock rejected the request ({status}): {message} — check the IAM "
            f"principal has bedrock:InvokeModelWithResponseStream on "
            f"{to_bedrock_model(model, region=region)} in {region}."
        )
    if status == 404:
        raise ProviderError(
            f"Bedrock has no model {to_bedrock_model(model, region=region)!r} in "
            f"{region} ({status}): {message} — model access is per-region and "
            "must be requested once in the Bedrock console."
        )
    raise ProviderError(f"Bedrock API error ({status}): {message}")
