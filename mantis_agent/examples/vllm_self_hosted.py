"""Self-hosted vLLM example via ``query()``.

Demonstrates a two-turn flow against a vLLM server you run on your own
GPU box:

  1. parent emits a tool call to ``search_docs``;
  2. parent receives the tool result and emits a one-line answer.

Run modes
---------

**Real vLLM** (default — requires a running OpenAI-compatible server)::

    pip install vllm
    python -m vllm.entrypoints.openai.api_server \\
        --model Qwen/Qwen2.5-7B-Instruct --port 8000

    python -m mantis_agent.examples.vllm_self_hosted

**Override the base URL / model**::

    VLLM_BASE_URL=https://gpu-box.internal:8000/v1 \\
    VLLM_MODEL=Qwen/Qwen2.5-72B-Instruct \\
        python -m mantis_agent.examples.vllm_self_hosted

**Offline smoke test** — no network, scripted ``MockProvider`` exercises
the same tool-call → tool-result → final-answer path the live server
would take. Use this in CI / on a plane::

    MANTIS_AGENT_MOCK=1 python -m mantis_agent.examples.vllm_self_hosted

vLLM speaks OpenAI-compat at ``/v1/chat/completions``, so the OpenAI-compat
adapter is the right call. Capability resolution picks Path A (native
tools). The mock path skips the network entirely but goes through the
exact same Agent loop, so regressions in tool dispatch / result threading
/ message extraction show up immediately.
"""

from __future__ import annotations

import asyncio
import os
import sys
import urllib.error
import urllib.request

from mantis_agent import query, tool


# Default location of the vLLM server in the docs above.
_DEFAULT_BASE_URL = "http://localhost:8000/v1"
_DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"


# ---------------------------------------------------------------------------
# Tool the example exercises
# ---------------------------------------------------------------------------


@tool
async def search_docs(query_str: str) -> str:
    """Return a canned one-line snippet for a docs query."""

    canned = {
        "retries": (
            "Retries: exponential backoff with jitter, capped at 30s, "
            "from /docs/playbooks/retries.md."
        ),
        "rate limits": (
            "Rate limits: 60 req/min per token, see /docs/api/rate-limits.md."
        ),
        "auth": (
            "Auth: rotate API keys every 90 days, see /docs/security/keys.md."
        ),
    }
    key = (query_str or "").strip().lower()
    for k, v in canned.items():
        if k in key:
            return v
    return f"No matching docs for {query_str!r}."


# ---------------------------------------------------------------------------
# Reachability preflight — fail fast if vLLM isn't up
# ---------------------------------------------------------------------------


def _vllm_is_reachable(base_url: str, *, timeout: float = 2.0) -> bool:
    """``GET {base_url}/models`` — the OpenAI-compat shape vLLM serves.

    Returns True for any 2xx. We don't try to parse the body because
    the point is "the server is answering on this port".
    """

    url = base_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Mock-mode wiring — lets the example run with zero network in CI
# ---------------------------------------------------------------------------


def _build_mock_provider():
    """Build a deterministic MockProvider that scripts two parent turns.

    Turn 1 emits a ``search_docs`` tool call (Path A native shape).
    Turn 2 emits a final text answer that quotes the tool result.

    The MockProvider replays each script on successive ``stream()`` calls;
    the Agent loop dispatches the tool between turns the same way a real
    vLLM response would trigger.
    """

    from mantis_agent.events import (
        ContentBlockDelta,
        ContentBlockStart,
        ContentBlockStop,
        InputJsonDelta,
        MessageDelta,
        MessageStart,
        MessageStop,
        TextDelta,
    )
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.types import TextBlock, ToolUseBlock, Usage

    def _hdr(model: str = "mock-vllm") -> list:
        return [MessageStart(message_id="mock-vllm-1", model=model)]

    def _text(idx: int, txt: str) -> list:
        return [
            ContentBlockStart(index=idx, block=TextBlock(text="")),
            ContentBlockDelta(index=idx, delta=TextDelta(text=txt)),
            ContentBlockStop(index=idx),
        ]

    def _tool_call(idx: int, cid: str, name: str, json_args: str) -> list:
        return [
            ContentBlockStart(
                index=idx,
                block=ToolUseBlock(id=cid, name=name, input={}),
            ),
            ContentBlockDelta(index=idx, delta=InputJsonDelta(partial_json=json_args)),
            ContentBlockStop(index=idx),
        ]

    def _stop(reason: str = "end_turn") -> list:
        return [
            MessageDelta(
                stop_reason=reason,
                usage=Usage(input_tokens=18, output_tokens=22),
            ),
            MessageStop(),
        ]

    turn_1 = (
        _hdr()
        + _tool_call(
            0,
            "vllm-call-1",
            "search_docs",
            '{"query_str": "retries"}',
        )
        + _stop("tool_use")
    )
    turn_2 = (
        _hdr()
        + _text(
            0,
            "From the docs: exponential backoff with jitter, capped at 30s.",
        )
        + _stop()
    )

    scripts = [turn_1, turn_2]

    class _ScriptedMock(MockProvider):
        """Walks ``scripts`` in order on each ``stream()`` call.

        Any call beyond the script list replays the last one — defensive
        in case the model emits an extra empty turn under retries.
        """

        def __init__(self) -> None:
            super().__init__()
            self._i = 0

        async def stream(self, **_kw):
            script = scripts[self._i] if self._i < len(scripts) else scripts[-1]
            self._i += 1
            for ev in script:
                yield ev

    return _ScriptedMock()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    mock_mode = os.environ.get("MANTIS_AGENT_MOCK") == "1"
    backend = os.environ.get("VLLM_BASE_URL", _DEFAULT_BASE_URL)
    model = os.environ.get("VLLM_MODEL", _DEFAULT_MODEL)

    # Live-mode guard: don't hand off to httpx if the server isn't up —
    # users get a clear actionable message instead of a connection-
    # refused stack trace. ``MANTIS_AGENT_NO_PREFLIGHT=1`` opts out for
    # weird network setups where the preflight URL might be blocked but
    # the chat endpoint isn't.
    if not mock_mode and os.environ.get("MANTIS_AGENT_NO_PREFLIGHT") != "1":
        if not _vllm_is_reachable(backend):
            raise SystemExit(
                f"vLLM not reachable at {backend}. Start it with:\n"
                "    python -m vllm.entrypoints.openai.api_server "
                f"--model {model} --port 8000\n"
                "Or set VLLM_BASE_URL=... to point at a remote box.\n"
                "For an offline smoke test: MANTIS_AGENT_MOCK=1"
            )

    options: dict = {
        "model": "mock-vllm" if mock_mode else model,
        "tools": [search_docs],
        "system": (
            "You are a documentation assistant. Use the search_docs tool "
            "when the user asks about docs, then quote the result."
        ),
        "max_tokens": 512,
        "max_turns": 3,
        "persist": False if mock_mode else True,
        "include_memory": False,
    }
    if mock_mode:
        options["provider"] = _build_mock_provider()
    else:
        options["backend"] = backend

    final_text: list[str] = []
    tool_calls: list[str] = []

    async for msg in query(
        prompt="What's in our docs about retries?",
        options=options,
    ):
        if msg.type == "assistant":
            for block in msg.message.content:
                if hasattr(block, "text") and block.text:
                    print(f"[assistant] {block.text}")
                    final_text.append(block.text)
                elif getattr(block, "name", None) == "search_docs":
                    tool_calls.append(block.name)
                    print("[assistant → search_docs] dispatching")
        elif msg.type == "result":
            cost = getattr(msg, "total_cost_usd", 0.0) or 0.0
            print(
                f"\n[result] {msg.subtype} · {msg.num_turns} turns · "
                f"${cost:.4f} (in={msg.usage.input_tokens}, "
                f"out={msg.usage.output_tokens})"
            )

    if mock_mode:
        assert tool_calls, "expected the parent to call search_docs in mock mode"
        assert any("backoff" in t for t in final_text), (
            "expected the final parent text to incorporate the tool result"
        )
        print("\n[ok] mock-mode smoke test passed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
