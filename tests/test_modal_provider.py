"""Modal serverless adapter — URL construction, auth, end-to-end streaming.

What this suite proves:

* ``build_modal_url`` produces the right pattern for every shape (no
  function, with function, with label, custom api_path) and validates
  each segment as a Modal-compatible slug.
* ``parse_modal_model_spec`` handles all four documented forms and
  rejects unparseable input.
* ``detect_provider`` routes ``modal:...`` specs and ``modal.run``
  URLs to the modal adapter before the openai_compat fallback can
  catch them.
* The registry resolves ``"modal"`` to ``ModalProvider`` (the lazy
  registration in ``providers/base.py`` actually imports the module).
* Constructor auth handling: env-var fallback, explicit args, the
  half-configured failure mode.
* ``from_model_spec`` round-trips workspace/app/function and carries
  the served model through to ``stream()``.
* End-to-end: a ``stream()`` call hits the correct Modal URL with
  ``Modal-Key`` + ``Modal-Secret`` headers, no ``Authorization``
  Bearer, and the streamed response decodes through the standard
  OpenAI-compat translation path (we get a ``MessageStart`` and a
  ``TextDelta`` back).
* ``backend_capability`` defaults to the Modal hosted profile —
  important so prompt-engineered tool paths aren't accidentally
  selected against a vLLM-style native-tool backend.
* ``stream()`` falls back to the constructor-time ``inner_model``
  when the caller passes an empty model name.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from mantis_agent.capabilities import HOSTED_PROFILES
from mantis_agent.events import (
    ContentBlockDelta,
    MessageStart,
    MessageStop,
    StreamEvent,
    TextDelta,
)
from mantis_agent.providers import detect_provider, resolve
from mantis_agent.providers.modal_provider import (
    MODAL_HOST_SUFFIX,
    ModalProvider,
    ModalProviderError,
    build_modal_url,
    parse_modal_model_spec,
)
from mantis_agent.types import UserMessage


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Pure helpers (no I/O, no env)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,expected",
    [
        (
            dict(workspace="alice", app="my-llm"),
            "https://alice--my-llm.modal.run/v1",
        ),
        (
            dict(workspace="alice", app="my-llm", function="serve"),
            "https://alice--my-llm-serve.modal.run/v1",
        ),
        (
            dict(workspace="alice", app="my-llm", function="serve", label="staging"),
            "https://alice--my-llm-serve-staging.modal.run/v1",
        ),
        (
            dict(workspace="alice", app="my-llm", api_path=""),
            "https://alice--my-llm.modal.run",
        ),
        (
            dict(workspace="alice", app="my-llm", api_path="/openai/v1"),
            "https://alice--my-llm.modal.run/openai/v1",
        ),
        (
            # Trailing slash on api_path: normalized away.
            dict(workspace="alice", app="my-llm", api_path="/v1/"),
            "https://alice--my-llm.modal.run/v1",
        ),
        (
            # Missing leading slash on api_path: added.
            dict(workspace="alice", app="my-llm", api_path="v1"),
            "https://alice--my-llm.modal.run/v1",
        ),
    ],
)
def test_build_modal_url_shapes(args: dict, expected: str) -> None:
    assert build_modal_url(**args) == expected


def test_build_modal_url_uses_canonical_host() -> None:
    # Single source of truth — make sure the constant actually shows up
    # in the assembled URL so renames don't silently drift.
    url = build_modal_url(workspace="w", app="a")
    assert f".{MODAL_HOST_SUFFIX}/" in url


@pytest.mark.parametrize(
    "args,bad_field",
    [
        (dict(workspace="", app="my-llm"), "workspace"),
        (dict(workspace="alice", app=""), "app"),
        # Spaces aren't a valid Modal slug.
        (dict(workspace="alice cool", app="my-llm"), "workspace"),
        (dict(workspace="alice", app="my llm"), "app"),
        (dict(workspace="alice", app="my-llm", function="bad/fn"), "function"),
        (dict(workspace="alice", app="my-llm", label="bad label"), "label"),
    ],
)
def test_build_modal_url_validates_slugs(args: dict, bad_field: str) -> None:
    with pytest.raises(ModalProviderError) as exc:
        build_modal_url(**args)
    assert bad_field in str(exc.value).lower()


@pytest.mark.parametrize(
    "spec,expected",
    [
        (
            "modal:alice/my-llm",
            {"workspace": "alice", "app": "my-llm", "function": None, "served_model": None},
        ),
        (
            "modal:alice/my-llm/serve",
            {"workspace": "alice", "app": "my-llm", "function": "serve", "served_model": None},
        ),
        (
            "modal:alice/my-llm@meta-llama/Llama-3",
            {
                "workspace": "alice",
                "app": "my-llm",
                "function": None,
                "served_model": "meta-llama/Llama-3",
            },
        ),
        (
            "modal:alice/my-llm/serve@Qwen/Qwen2.5-7B-Instruct",
            {
                "workspace": "alice",
                "app": "my-llm",
                "function": "serve",
                "served_model": "Qwen/Qwen2.5-7B-Instruct",
            },
        ),
        (
            # Case-insensitive scheme.
            "MODAL:alice/my-llm",
            {"workspace": "alice", "app": "my-llm", "function": None, "served_model": None},
        ),
    ],
)
def test_parse_modal_model_spec(spec: str, expected: dict) -> None:
    assert parse_modal_model_spec(spec) == expected


@pytest.mark.parametrize(
    "spec",
    [
        # Missing scheme.
        "alice/my-llm",
        # Missing app.
        "modal:alice",
        # Empty body.
        "modal:",
    ],
)
def test_parse_modal_model_spec_rejects_bad_input(spec: str) -> None:
    with pytest.raises(ModalProviderError):
        parse_modal_model_spec(spec)


# ---------------------------------------------------------------------------
# detect_provider routes Modal correctly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "modal:alice/my-llm",
        "MODAL:alice/my-llm/serve@Qwen/Q3",
        "https://alice--my-llm.modal.run/v1",
        "https://alice--my-llm-serve.modal.run",
    ],
)
def test_detect_provider_routes_modal(subject: str) -> None:
    assert detect_provider(subject) == "modal"


def test_detect_provider_modal_precedence_over_openai_compat() -> None:
    """A modal.run URL must not be silently classified as openai_compat.

    Critical because the URL otherwise *looks* like a generic vLLM
    endpoint to the bare ``https://`` fallback, and we'd lose Modal-Key
    auth + the modal backend profile.
    """

    # https:// → would normally → openai_compat; modal.run beats it.
    assert detect_provider("https://alice--my-llm.modal.run") == "modal"


def test_detect_provider_explicit_hint_wins() -> None:
    assert (
        detect_provider("modal:alice/my-llm", backend_hint="openai_compat")
        == "openai_compat"
    )


# ---------------------------------------------------------------------------
# Lazy registry resolves "modal" to our class
# ---------------------------------------------------------------------------


def test_registry_resolves_modal() -> None:
    assert resolve("modal") is ModalProvider


# ---------------------------------------------------------------------------
# Construction + auth
# ---------------------------------------------------------------------------


def test_explicit_base_url_skips_url_builder() -> None:
    p = ModalProvider(base_url="https://custom--proxy.modal.run/proxy/v1")
    assert p.base_url == "https://custom--proxy.modal.run/proxy/v1"


def test_constructor_needs_url_or_workspace_app() -> None:
    with pytest.raises(ModalProviderError):
        ModalProvider()  # nothing supplied


def test_partial_auth_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both Modal-Key and Modal-Secret, or neither — never just one."""

    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)

    with pytest.raises(ModalProviderError):
        ModalProvider(workspace="a", app="b", token_id="ak-xxx")
    with pytest.raises(ModalProviderError):
        ModalProvider(workspace="a", app="b", token_secret="as-xxx")


def test_env_vars_populate_auth_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-env-id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-env-secret")

    p = ModalProvider(workspace="a", app="b")
    headers = p.inner_provider.client.headers
    assert headers.get("Modal-Key") == "ak-env-id"
    assert headers.get("Modal-Secret") == "as-env-secret"
    # No Bearer leak: even if OPENAI_API_KEY is set in env, Modal must not
    # attach it.
    assert "authorization" not in {k.lower() for k in headers.keys()}


async def test_constructor_args_beat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-env")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-env")

    p = ModalProvider(
        workspace="a", app="b", token_id="ak-arg", token_secret="as-arg"
    )
    try:
        headers = p.inner_provider.client.headers
        assert headers["Modal-Key"] == "ak-arg"
        assert headers["Modal-Secret"] == "as-arg"
    finally:
        await p.aclose()


def test_no_auth_means_no_modal_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare ModalProvider with no token pair sends no Modal-Key headers.

    Some Modal deployments are public — they shouldn't require auth, and
    we shouldn't invent placeholder values.
    """

    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    p = ModalProvider(workspace="a", app="b")
    headers = p.inner_provider.client.headers
    assert "modal-key" not in {k.lower() for k in headers}
    assert "modal-secret" not in {k.lower() for k in headers}


def test_extra_default_headers_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    p = ModalProvider(
        workspace="a",
        app="b",
        default_headers={"X-Trace-Id": "abc123"},
    )
    assert p.inner_provider.client.headers["X-Trace-Id"] == "abc123"


def test_backend_capability_defaults_to_modal_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    p = ModalProvider(workspace="a", app="b")
    assert p.backend_capability is HOSTED_PROFILES["modal"]
    # And the inner OpenAICompat client also believes it's Modal — not
    # the generic vllm fallback.
    assert p.inner_provider.backend_capability.provider_hint == "modal"


# ---------------------------------------------------------------------------
# from_model_spec
# ---------------------------------------------------------------------------


def test_from_model_spec_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    p = ModalProvider.from_model_spec("modal:alice/my-llm/serve@Qwen/Qwen2.5-7B")
    assert p.base_url == "https://alice--my-llm-serve.modal.run/v1"
    assert p.inner_model == "Qwen/Qwen2.5-7B"


def test_from_model_spec_without_served_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    p = ModalProvider.from_model_spec("modal:alice/my-llm")
    assert p.base_url == "https://alice--my-llm.modal.run/v1"
    assert p.inner_model is None


# ---------------------------------------------------------------------------
# End-to-end streaming through a mocked transport
# ---------------------------------------------------------------------------


def _sse_chunk(payload: str) -> bytes:
    return f"data: {payload}\n\n".encode()


def _sse_done() -> bytes:
    return b"data: [DONE]\n\n"


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Tiny transport that records the request and replays one SSE stream.

    We construct a fresh httpx.AsyncClient with this transport, then
    swap it in for the ModalProvider's inner ``client``. That way the
    real provider code path runs unchanged — payload encoding, header
    propagation, SSE parsing — and we just intercept the wire bytes.
    """

    def __init__(self, body: bytes) -> None:
        self.last_request: httpx.Request | None = None
        self._body = body

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        self.last_request = request
        # Build a streaming response. iter_bytes is what httpx feeds
        # aiter_lines on the consuming side.
        async def _stream() -> AsyncIterator[bytes]:
            # Split into multiple chunks so SSE framing is realistic.
            for line in self._body.splitlines(keepends=True):
                yield line

        return httpx.Response(
            200,
            stream=httpx.AsyncByteStream(),  # placeholder, replaced below
            headers={"content-type": "text/event-stream"},
        )

    # We override the simpler interface below: httpx will call
    # handle_async_request and we return a Response with an async byte
    # stream. The cleanest way is to subclass and provide a stream.


class _StaticAsyncByteStream(httpx.AsyncByteStream):
    """An async byte stream of a fixed payload, split into small chunks."""

    def __init__(self, payload: bytes, chunk_size: int = 64) -> None:
        self._payload = payload
        self._chunk_size = chunk_size

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for i in range(0, len(self._payload), self._chunk_size):
            yield self._payload[i : i + self._chunk_size]

    async def aclose(self) -> None:
        return None


class _ReplayTransport(httpx.AsyncBaseTransport):
    """Cleaner version: record the request, replay a fixed SSE body."""

    def __init__(self, body: bytes) -> None:
        self.last_request: httpx.Request | None = None
        self.last_body: bytes = b""
        self._payload = body

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        # Materialize body once for inspection (provider sent JSON).
        self.last_body = await _read_request_body(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_StaticAsyncByteStream(self._payload),
        )


async def _read_request_body(request: httpx.Request) -> bytes:
    if request.stream is None:
        return b""
    chunks: list[bytes] = []
    async for chunk in request.stream:
        chunks.append(chunk)
    return b"".join(chunks)


def _build_minimal_sse() -> bytes:
    """A tiny but real OpenAI-compat SSE stream: MessageStart -> 'hi' -> done."""

    # MessageStart equivalent — choices[0] with the model name.
    chunk1 = (
        b'{"id":"chatcmpl-test","model":"served-model","choices":[{"delta":{"content":""},'
        b'"index":0,"finish_reason":null}]}'
    )
    chunk2 = (
        b'{"id":"chatcmpl-test","model":"served-model","choices":[{"delta":{"content":"hi"},'
        b'"index":0,"finish_reason":null}]}'
    )
    chunk3 = (
        b'{"id":"chatcmpl-test","model":"served-model","choices":[{"delta":{},"index":0,'
        b'"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1}}'
    )
    return _sse_chunk(chunk1.decode()) + _sse_chunk(chunk2.decode()) + _sse_chunk(chunk3.decode()) + _sse_done()


async def _drain(stream: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    out: list[StreamEvent] = []
    async for ev in stream:
        out.append(ev)
    return out


async def test_stream_hits_modal_url_with_auth_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-trip: ModalProvider.stream sends the right URL + auth headers."""

    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-env-id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-env-secret")

    transport = _ReplayTransport(_build_minimal_sse())
    p = ModalProvider(workspace="alice", app="my-llm", function="serve")
    try:
        # Replace inner client with one wired to our transport while
        # preserving every header the provider configured.
        original_headers = dict(p.inner_provider.client.headers)
        await p.inner_provider.client.aclose()
        p.inner_provider.client = httpx.AsyncClient(
            base_url=p.base_url,
            headers=original_headers,
            transport=transport,
        )

        events = await _drain(
            p.stream(
                model="served-model",
                messages=[UserMessage(content="ping")],
                max_tokens=16,
            )
        )
    finally:
        await p.aclose()

    # The request actually went out.
    assert transport.last_request is not None
    sent_url = str(transport.last_request.url)
    assert sent_url.startswith("https://alice--my-llm-serve.modal.run/")
    assert sent_url.endswith("/chat/completions")

    headers = transport.last_request.headers
    assert headers.get("Modal-Key") == "ak-env-id"
    assert headers.get("Modal-Secret") == "as-env-secret"
    # And we should NOT have leaked any unrelated bearer token.
    assert "authorization" not in {k.lower() for k in headers.keys()}

    # The body should include the model + a streamed flag.
    assert b'"model":"served-model"' in transport.last_body
    assert b'"stream":true' in transport.last_body

    # The translated stream should produce MessageStart + a TextDelta('hi') + MessageStop.
    starts = [e for e in events if isinstance(e, MessageStart)]
    text_deltas = [
        e.delta.text for e in events
        if isinstance(e, ContentBlockDelta) and isinstance(e.delta, TextDelta)
    ]
    stops = [e for e in events if isinstance(e, MessageStop)]
    assert len(starts) == 1
    assert "hi" in "".join(text_deltas)
    assert len(stops) == 1


async def test_stream_uses_inner_model_when_caller_passes_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When constructed via from_model_spec(...@served-name), an empty
    ``model=`` on stream() falls back to the spec-supplied name."""

    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)

    transport = _ReplayTransport(_build_minimal_sse())
    p = ModalProvider.from_model_spec("modal:alice/my-llm@spec-default-model")
    try:
        original_headers = dict(p.inner_provider.client.headers)
        await p.inner_provider.client.aclose()
        p.inner_provider.client = httpx.AsyncClient(
            base_url=p.base_url,
            headers=original_headers,
            transport=transport,
        )

        await _drain(
            p.stream(model="", messages=[UserMessage(content="hello")])
        )
    finally:
        await p.aclose()

    assert transport.last_request is not None
    assert b'"model":"spec-default-model"' in transport.last_body


async def test_stream_raises_when_no_model_anywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    p = ModalProvider(workspace="a", app="b")  # no inner_model
    try:
        with pytest.raises(ModalProviderError):
            # Materialize the async iterator to trigger the check.
            async for _ in p.stream(
                model="", messages=[UserMessage(content="hi")]
            ):
                pass  # pragma: no cover — we expect the raise first
    finally:
        await p.aclose()


async def test_aclose_releases_inner_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    p = ModalProvider(workspace="a", app="b")
    inner_client = p.inner_provider.client
    await p.aclose()
    assert inner_client.is_closed
