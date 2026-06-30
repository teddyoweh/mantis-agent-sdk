"""Mock provider for tests and offline development.

Three usage modes:

1. **Scripted.** Pass ``scripted_events=[...]`` — the provider replays them
   verbatim on *every* ``stream()`` call. Good for unit tests of the agent
   loop, hook system, permission gates, and text-tool-parser fuzzing.

2. **Fixtures directory.** Pass ``fixtures_dir=Path(...)`` — the provider
   scans for files named ``{model}_{scenario}.json`` containing a list of
   ``StreamEvent`` variants (tagged-union form). The fixture for the current
   call is selected by exact model match; an optional ``scenario=`` kwarg
   (forwarded via ``extra``) picks a specific scenario when multiple exist.
   This is the recorded-VCR pattern that ``tests/recorded/`` will use.

3. **Trivial fallback.** Neither given — emit a single ``"OK."`` text
   response. Lets you wire up an Agent without any setup just to verify
   plumbing.

Between events we ``anyio.sleep(0)`` to yield to the scheduler so consumers
that ``await`` between events get realistic interleaving. The mock is *not*
optimized for benchmarking — it's for correctness.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import msgspec

from ..capabilities import HOSTED_PROFILES, BackendCapability, ModelCapability
from ..events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
    StreamEvent,
    TextDelta,
)
from ..types import Message, TextBlock, Usage

_EVENT_DECODER = msgspec.json.Decoder(list[StreamEvent])


class MockProvider:
    """Replay-based provider for tests and offline work."""

    name = "mock"

    def __init__(
        self,
        *,
        fixtures_dir: Path | str | None = None,
        scripted_events: list[StreamEvent] | None = None,
        default_text: str = "OK.",
    ) -> None:
        self._scripted: list[StreamEvent] | None = (
            list(scripted_events) if scripted_events is not None else None
        )
        self._fixtures_dir: Path | None = (
            Path(fixtures_dir) if fixtures_dir is not None else None
        )
        self._default_text = default_text
        self.backend_capability: BackendCapability = HOSTED_PROFILES["mock"]
        # Cache of (model, scenario) → events list to avoid re-reading
        # fixture files on every call.
        self._fixture_cache: dict[tuple[str, str | None], list[StreamEvent]] = {}
        # Observability for tests — capture the kwargs of the most recent
        # stream() call, plus a per-call append-only log. Lets tests assert
        # on what the Agent layer actually forwarded (e.g. ``response_format``
        # translation, tool wire shapes, custom headers via extra). Cheap;
        # only populated on calls so production users pay one dict each turn.
        self.last_extra: dict[str, Any] | None = None
        self.last_call_kwargs: dict[str, Any] | None = None
        self.calls: list[dict[str, Any]] = []

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
        # Materialize messages so callers passing generators don't burn them.
        # Tests sometimes inspect ``last_call_kwargs["messages"]`` to verify
        # that the agent loop built the wire-form messages correctly.
        msg_list = list(messages)

        # Snapshot the call kwargs for test introspection. Shallow copies
        # only — we don't want to deep-copy the entire stream event scripts.
        call_record: dict[str, Any] = {
            "model": model,
            "messages": msg_list,
            "system": system,
            "tools": list(tools) if tools else None,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "extra": dict(extra) if isinstance(extra, dict) else extra,
            "model_capability": model_capability,
        }
        self.last_extra = call_record["extra"]
        self.last_call_kwargs = call_record
        self.calls.append(call_record)

        events = self._pick_events(model, extra)
        for ev in events:
            yield ev
            await anyio.sleep(0)

    async def aclose(self) -> None:  # noqa: D401 — no resources
        """Nothing to release."""
        return None

    # ------------------------------------------------------------------
    # Event picking
    # ------------------------------------------------------------------

    def _pick_events(self, model: str, extra: dict[str, Any] | None) -> list[StreamEvent]:
        """Resolve the event list to replay for this call.

        Precedence:
          1. ``scripted_events`` constructor arg wins outright.
          2. ``fixtures_dir`` → look up ``{model}_{scenario}.json`` (or just
             ``{model}.json`` if no scenario passed). Cached after first read.
          3. Synthesize a trivial "OK." stream.
        """
        if self._scripted is not None:
            return self._scripted

        if self._fixtures_dir is not None:
            scenario = None
            if isinstance(extra, dict):
                s = extra.get("scenario")
                if isinstance(s, str):
                    scenario = s
            cache_key = (model, scenario)
            cached = self._fixture_cache.get(cache_key)
            if cached is not None:
                return cached
            loaded = self._load_fixture(model, scenario)
            if loaded is not None:
                self._fixture_cache[cache_key] = loaded
                return loaded
            # Fall through to trivial if no matching fixture found.

        return self._trivial_events()

    def _load_fixture(self, model: str, scenario: str | None) -> list[StreamEvent] | None:
        """Look up a fixture file by ``(model, scenario)``.

        Naming: ``{model}_{scenario}.json`` if scenario is given, else any
        file matching ``{model}*.json`` (first hit wins, sorted for
        determinism). Returns ``None`` when nothing matches.
        """
        assert self._fixtures_dir is not None
        d = self._fixtures_dir
        if not d.is_dir():
            return None

        safe_model = model.replace("/", "_").replace(":", "_")
        candidates: list[Path] = []
        if scenario is not None:
            exact = d / f"{safe_model}_{scenario}.json"
            if exact.is_file():
                candidates.append(exact)
        if not candidates:
            # Match any fixture for this model.
            candidates = sorted(d.glob(f"{safe_model}*.json"))
        if not candidates:
            return None

        raw = candidates[0].read_bytes()
        try:
            return _EVENT_DECODER.decode(raw)
        except msgspec.DecodeError:
            # Be loud — fixture authoring errors should not be papered over.
            raise

    def _trivial_events(self) -> list[StreamEvent]:
        """Build a minimal valid stream: MessageStart → text block → MessageStop."""
        mid = f"mock-{uuid4().hex[:8]}"
        return [
            MessageStart(message_id=mid, model="mock"),
            ContentBlockStart(index=0, block=TextBlock(text="")),
            ContentBlockDelta(index=0, delta=TextDelta(text=self._default_text)),
            ContentBlockStop(index=0),
            MessageDelta(
                stop_reason="end_turn",
                usage=Usage(input_tokens=0, output_tokens=len(self._default_text.split())),
            ),
            MessageStop(),
        ]
