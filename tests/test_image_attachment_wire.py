"""Attached images must survive all the way to the wire, on every provider.

Encoders are easy to get right in isolation and easy to break in composition —
this asserts on the actual request bytes each provider sends, so a regression
shows up as "the model can't see the screenshot" in a test rather than in a
session.
"""

from __future__ import annotations

import base64
import json

import anyio
import httpx
import pytest

from mantis_agent.types import ImageBlock, TextBlock, UserMessage

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).decode()


def _msg() -> UserMessage:
    return UserMessage(content=[
        TextBlock(text="what is in this screenshot?"),
        ImageBlock(source={"type": "base64", "media_type": "image/png", "data": PNG}),
    ])


def _sse() -> httpx.Response:
    body = ('data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            'data: [DONE]\n\n')
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _capture(provider, base_url: str, response_fn, **stream_kw) -> dict:
    """Run one turn against a mock transport and return the parsed request body."""
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return response_fn()

    provider.client = httpx.AsyncClient(
        base_url=base_url, transport=httpx.MockTransport(handler))

    async def go() -> None:
        async for _ev in provider.stream(messages=[_msg()], **stream_kw):
            pass

    anyio.run(go)
    assert seen, "provider never issued a request"
    return json.loads(seen[0])


# --- OpenAI-compatible: OpenAI, xAI, Gemini-compat, Modal, TGI, llama.cpp ----


def test_openai_compatible_sends_data_uri_image_part() -> None:
    from mantis_agent.providers.openai_compat import OpenAICompatProvider

    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    body = _capture(p, "https://api.openai.com/v1", _sse, model="gpt-5.4", max_tokens=16)

    parts = body["messages"][-1]["content"]
    assert isinstance(parts, list), "image turns must use the multipart content shape"
    image = next(p for p in parts if p["type"] == "image_url")
    assert image["image_url"]["url"] == f"data:image/png;base64,{PNG}"
    assert any(p["type"] == "text" for p in parts), "the question must ride along"


def test_openai_compatible_keeps_plain_text_turns_plain() -> None:
    """No image → no multipart shape. Some endpoints reject it needlessly."""
    from mantis_agent.providers.openai_compat import OpenAICompatProvider

    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return _sse()

    p.client = httpx.AsyncClient(base_url="https://api.openai.com/v1",
                                 transport=httpx.MockTransport(handler))

    async def go() -> None:
        async for _ev in p.stream(model="gpt-5.4", max_tokens=16,
                                  messages=[UserMessage(content="hi")]):
            pass

    anyio.run(go)
    assert json.loads(seen[0])["messages"][-1]["content"] == "hi"


# --- Anthropic ---------------------------------------------------------------


def test_anthropic_sends_native_image_block() -> None:
    from mantis_agent.providers.anthropic_passthrough import AnthropicPassthroughProvider

    def resp() -> httpx.Response:
        body = ('event: message_start\ndata: {"type":"message_start","message":'
                '{"id":"m","type":"message","role":"assistant","model":"c",'
                '"content":[],"stop_reason":null,"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
                'event: message_stop\ndata: {"type":"message_stop"}\n\n')
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    p = AnthropicPassthroughProvider(api_key="x", base_url="https://api.anthropic.com")
    body = _capture(p, "https://api.anthropic.com", resp,
                    model="claude-opus-5", max_tokens=16)

    blocks = body["messages"][-1]["content"]
    image = next(b for b in blocks if b["type"] == "image")
    assert image["source"] == {"type": "base64", "media_type": "image/png", "data": PNG}


# --- Ollama ------------------------------------------------------------------


def test_ollama_splits_images_into_the_images_array() -> None:
    """Ollama does NOT take inline image parts — base64 goes in ``images``."""
    from mantis_agent.providers.ollama import OllamaProvider

    def resp() -> httpx.Response:
        body = ('{"message":{"role":"assistant","content":"ok"},"done":false}\n'
                '{"message":{"role":"assistant","content":""},"done":true,'
                '"prompt_eval_count":1,"eval_count":1}\n')
        return httpx.Response(200, text=body,
                              headers={"content-type": "application/x-ndjson"})

    p = OllamaProvider(base_url="http://127.0.0.1:11434")
    body = _capture(p, "http://127.0.0.1:11434", resp, model="llava", max_tokens=16)

    turn = body["messages"][-1]
    assert turn["images"] == [PNG]
    assert "what is in this screenshot?" in turn["content"]
    assert PNG not in turn["content"], "base64 must not leak into the text slot"


# --- URL-sourced images ------------------------------------------------------


def test_openai_compatible_passes_url_images_through() -> None:
    from mantis_agent.providers.openai_compat import OpenAICompatProvider

    url = "https://example.com/a.png"
    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return _sse()

    p.client = httpx.AsyncClient(base_url="https://api.openai.com/v1",
                                 transport=httpx.MockTransport(handler))
    msg = UserMessage(content=[ImageBlock(source={"type": "url", "url": url})])

    async def go() -> None:
        async for _ev in p.stream(model="gpt-5.4", max_tokens=16, messages=[msg]):
            pass

    anyio.run(go)
    parts = json.loads(seen[0])["messages"][-1]["content"]
    assert parts[0]["image_url"]["url"] == url


@pytest.mark.parametrize("model", [
    "gpt-5.4", "claude-opus-5", "gemini-3-pro", "grok-4", "llava:13b",
    "qwen2.5vl:7b", "llama-4-scout", "pixtral-12b",
])
def test_vision_models_are_recognized(model: str) -> None:
    from mantis_agent.tui import model_supports_vision
    assert model_supports_vision(model) is True


@pytest.mark.parametrize("model", ["deepseek-v3", "qwen2.5-7b-instruct", "mistral-nemo-12b"])
def test_text_only_models_are_not_claimed_as_vision(model: str) -> None:
    from mantis_agent.tui import model_supports_vision
    assert model_supports_vision(model) is False
