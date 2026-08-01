"""Images must count toward the context estimate and be compactable.

Regression for a live wedge: a browser-automation run kept reading multi-MB
screenshots, the estimator scored every ``ImageBlock`` as 0 tokens, so neither
micro- nor full compaction ever fired. The provider then rejected a ~2M-token
prompt, and because both compaction paths protect the recent window — where the
screenshots were — nothing could shrink and every retry (including a manual
/compact) hit the same wall.
"""

from __future__ import annotations

import anyio

from mantis_agent import (
    AssistantMessage,
    ImageBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
)
from mantis_agent.compact import SimpleCompactor, _message_token_estimate

# ~1 MB of base64 — a modest screenshot.
BIG_B64 = "A" * 1_000_000


def _image(data: str = BIG_B64) -> ImageBlock:
    return ImageBlock(source={"type": "base64", "media_type": "image/png", "data": data})


async def _fake(_prompt: str) -> str:
    return "SUMMARY"


def _compactor(**kw) -> SimpleCompactor:
    kw.setdefault("micro_keep_tool_results", 2)
    kw.setdefault("micro_min_chars", 800)
    return SimpleCompactor(_fake, **kw)


def _screenshot_history(n: int) -> list:
    """n rounds of "read a PNG" — the tool result carries the image."""
    msgs: list = []
    for i in range(n):
        msgs.append(AssistantMessage(content=[ToolUseBlock(id=f"c{i}", name="Read", input={})]))
        msgs.append(
            UserMessage(content=[ToolResultBlock(tool_use_id=f"c{i}", content=[_image()])])
        )
    return msgs


def test_image_block_is_not_free() -> None:
    """The estimator used to return 0 here, which is what broke everything."""
    est = _message_token_estimate(UserMessage(content=[_image()]))
    assert est > 200_000


def test_structured_tool_result_counts_its_image() -> None:
    msg = UserMessage(content=[ToolResultBlock(tool_use_id="c0", content=[_image()])])
    assert _message_token_estimate(msg) > 200_000


def test_remote_image_url_gets_a_nonzero_floor() -> None:
    blk = ImageBlock(source={"type": "url", "url": "https://example.com/a.png"})
    assert _message_token_estimate(UserMessage(content=[blk])) > 0


def test_screenshots_trip_the_compaction_threshold() -> None:
    c = _compactor()
    msgs = _screenshot_history(4)
    over = anyio.run(c.should_compact, msgs, Usage(), 200_000)
    assert over is True
    assert c.should_microcompact(msgs, Usage(), 200_000) is True


def test_microcompact_clears_old_image_tool_results() -> None:
    c = _compactor()
    msgs = _screenshot_history(5)
    before = sum(_message_token_estimate(m) for m in msgs)
    assert c.microcompact(msgs) is True
    after = sum(_message_token_estimate(m) for m in msgs)
    # 3 of 5 rounds cleared (micro_keep=2).
    assert after < before / 2


def test_microcompact_clears_bare_images_outside_tool_results() -> None:
    """A pasted/attached image isn't a tool result and was never swept."""
    c = _compactor()
    msgs = _screenshot_history(5)
    msgs.insert(1, UserMessage(content=[_image()]))
    assert c.microcompact(msgs) is True
    assert _message_token_estimate(msgs[1]) < 100


def test_microcompact_is_idempotent_with_images() -> None:
    c = _compactor()
    msgs = _screenshot_history(5)
    assert c.microcompact(msgs) is True
    assert c.microcompact(msgs) is False


def test_emergency_clear_drops_the_recent_window_too() -> None:
    """The escalation that unwedges an already-rejected transcript."""
    c = _compactor()
    msgs = _screenshot_history(2)  # entirely inside the protected keep-window
    assert c.microcompact(msgs) is False
    before = sum(_message_token_estimate(m) for m in msgs)
    assert c.emergency_clear(msgs, keep_last=1) is True
    after = sum(_message_token_estimate(m) for m in msgs)
    # Exactly one image survives; the rest are placeholders.
    assert after < before * 0.6


def test_summarizer_prompt_never_carries_base64() -> None:
    seen: list[str] = []

    async def spy(prompt: str) -> str:
        seen.append(prompt)
        return "SUMMARY"

    c = SimpleCompactor(spy, keep_recent_turns=2, micro_keep_tool_results=2)
    msgs = [UserMessage(content="do the thing"), *_screenshot_history(6)]
    anyio.run(c.compact, msgs)
    assert seen, "summarizer should have been called"
    assert BIG_B64 not in seen[0]
    assert len(seen[0]) < 300_000
