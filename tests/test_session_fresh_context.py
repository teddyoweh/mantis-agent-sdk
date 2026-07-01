"""Session-resume context freshness: the stale isMeta context head is dropped on
load so the agent re-derives current env/git/memory on the next run."""

from __future__ import annotations

import anyio

from mantis_agent import AssistantMessage, TextBlock, UserMessage
from mantis_agent.session import (
    InMemorySessionStore,
    Session,
    strip_context_messages,
)


def _msgs() -> list:
    return [
        UserMessage(content="<system-reminder>stale env: branch=old</system-reminder>", isMeta=True),
        UserMessage(content="do the thing"),
        AssistantMessage(content=[TextBlock(text="done")]),
        UserMessage(content="[Current todo list] ...", isMeta=True),  # a reminder mid-history
    ]


def test_strip_removes_meta_keeps_real() -> None:
    out = strip_context_messages(_msgs())
    assert all(not getattr(m, "isMeta", False) for m in out)
    assert [type(m).__name__ for m in out] == ["UserMessage", "AssistantMessage"]
    assert out[0].content == "do the thing"


def test_load_defaults_to_fresh_context() -> None:
    async def main():
        store = InMemorySessionStore()
        await store.save("s1", _msgs(), {})
        sess = await Session.load(store, "s1")            # fresh_context defaults True
        assert not any(getattr(m, "isMeta", False) for m in sess.messages)
        assert len(sess.messages) == 2                    # only the real turns

    anyio.run(main)


def test_load_can_keep_frozen_head() -> None:
    async def main():
        store = InMemorySessionStore()
        await store.save("s1", _msgs(), {})
        sess = await Session.load(store, "s1", fresh_context=False)
        assert any(getattr(m, "isMeta", False) for m in sess.messages)
        assert len(sess.messages) == 4                    # verbatim

    anyio.run(main)
