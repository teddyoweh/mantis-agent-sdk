"""Tests for the ``<system-reminder>`` context-injection convention.

Pins the wire format to match the Claude SDK source byte-for-byte
(``src/utils/api.ts:463``, ``src/utils/messages.ts:3098``,
``src/utils/queryHelpers.ts:432``). Any drift here will surface as a
transcript-compatibility break.
"""

from __future__ import annotations

from datetime import datetime, timezone

from mantis_agent import (
    UserMessage,
    build_live_context_block,
    is_system_reminder,
    prepend_user_context,
    render_user_context,
    strip_system_reminders,
    wrap_system_reminder,
)
from mantis_agent.system_reminder import (
    CLOSE_TAG,
    OPEN_TAG,
    USER_CONTEXT_PREAMBLE,
    USER_CONTEXT_TRAILER,
)


# ---------------------------------------------------------------------------
# Tag constants and wire format
# ---------------------------------------------------------------------------


def test_tag_constants_match_claude_source() -> None:
    """Pinned literals — anything that touches the wire format breaks compat."""

    assert OPEN_TAG == "<system-reminder>"
    assert CLOSE_TAG == "</system-reminder>"


def test_preamble_matches_claude_source() -> None:
    """The canonical 'As you answer…' line is the byte-for-byte trigger
    Claude models are trained on."""

    assert USER_CONTEXT_PREAMBLE == (
        "As you answer the user's questions, "
        "you can use the following context:"
    )


def test_trailer_matches_claude_source_including_whitespace() -> None:
    """The trailer has leading whitespace verbatim from Claude's source.
    Token-level diff tooling depends on the byte-equality."""

    assert USER_CONTEXT_TRAILER.startswith("      IMPORTANT:")


# ---------------------------------------------------------------------------
# wrap / detect / strip
# ---------------------------------------------------------------------------


def test_wrap_system_reminder() -> None:
    out = wrap_system_reminder("hello")
    assert out == "<system-reminder>\nhello\n</system-reminder>"


def test_wrap_empty_returns_empty() -> None:
    assert wrap_system_reminder("") == ""


def test_is_system_reminder_full_message() -> None:
    msg = wrap_system_reminder("context block")
    assert is_system_reminder(msg) is True


def test_is_system_reminder_negatives() -> None:
    assert is_system_reminder("just text") is False
    assert is_system_reminder("") is False
    # Embedded but not whole message: NOT a full reminder.
    assert (
        is_system_reminder(
            f"prefix {OPEN_TAG}\ninner\n{CLOSE_TAG} suffix"
        )
        is False
    )


def test_strip_system_reminders_removes_embedded() -> None:
    text = f"hi {OPEN_TAG}\nctx\n{CLOSE_TAG} bye {OPEN_TAG}\nmore\n{CLOSE_TAG}!"
    out = strip_system_reminders(text)
    assert "<system-reminder>" not in out
    assert "hi " in out and " bye " in out and "!" in out


# ---------------------------------------------------------------------------
# render_user_context
# ---------------------------------------------------------------------------


def test_render_user_context_canonical_format() -> None:
    out = render_user_context({"memory": "M1\nM2", "claudeMd": "rule"})
    assert USER_CONTEXT_PREAMBLE in out
    assert "# memory\nM1\nM2" in out
    assert "# claudeMd\nrule" in out
    assert USER_CONTEXT_TRAILER in out


def test_render_user_context_empty_dict_returns_empty() -> None:
    assert render_user_context({}) == ""


def test_render_user_context_skips_empty_values() -> None:
    out = render_user_context({"memory": "M", "skills": ""})
    assert "# memory\nM" in out
    assert "# skills" not in out


# ---------------------------------------------------------------------------
# prepend_user_context — the integration point
# ---------------------------------------------------------------------------


def test_prepend_user_context_inserts_isMeta_synthetic() -> None:
    messages = [UserMessage(content="real question")]
    out = prepend_user_context(messages, {"memory": "X"})

    assert len(out) == 2
    first = out[0]
    assert isinstance(first, UserMessage)
    assert first.isMeta is True
    assert "<system-reminder>" in first.content
    assert "# memory\nX" in first.content

    # Original message is untouched and still in position [1].
    assert out[1].content == "real question"
    assert out[1].isMeta is False


def test_prepend_user_context_empty_context_is_noop() -> None:
    messages = [UserMessage(content="x")]
    out = prepend_user_context(messages, {})
    assert out == messages
    assert len(out) == 1


def test_prepend_user_context_in_place_mutates() -> None:
    messages = [UserMessage(content="x")]
    out = prepend_user_context(messages, {"k": "v"}, in_place=True)
    assert out is messages
    assert len(messages) == 2


# ---------------------------------------------------------------------------
# build_live_context_block — the "[Live context — at turn start]" form
# ---------------------------------------------------------------------------


def test_build_live_context_block_basic() -> None:
    ts = datetime(2026, 5, 16, 11, 47, tzinfo=timezone.utc)
    block = build_live_context_block(
        local_time=ts,
        timezone_name="America/New_York",
        location="San Francisco",
        locale="en-US",
    )
    assert "[Live context — at turn start]" in block
    assert "Saturday" in block
    assert "America/New_York" in block
    assert "San Francisco" in block
    assert "en-US" in block
    assert "Use this for reasoning" in block


def test_build_live_context_block_extras() -> None:
    block = build_live_context_block(extra={"build": "v0.1.0", "host": "modal"})
    assert "- build: v0.1.0" in block
    assert "- host: modal" in block


# ---------------------------------------------------------------------------
# user_message_isMeta wire roundtrip
# ---------------------------------------------------------------------------


def test_user_message_ismeta_field_default_false() -> None:
    m = UserMessage(content="x")
    assert m.isMeta is False


def test_user_message_ismeta_field_true_when_set() -> None:
    m = UserMessage(content="x", isMeta=True)
    assert m.isMeta is True
