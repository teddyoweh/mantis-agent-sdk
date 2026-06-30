"""Tests for Session fork + resume-from-arbitrary-checkpoint.

The matrix covers both stores (in-memory + sqlite) and both directions of
the contract:

  • store-level fork (legacy, full-copy) — still works
  • Session.fork(checkpoint=...) — high-level, truncated fork
  • Session.resume_from(checkpoint) — in-place rewind
  • fork_session() and resume_session() top-level helpers
  • Checkpoint discovery via Session.checkpoints() / make_checkpoints()

These tests deliberately use real ``Message`` types (UserMessage,
AssistantMessage) with mixed content (text + tool_use blocks) so we exercise
the msgspec encode/decode round-trip the stores depend on.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import anyio
import pytest

from mantis_agent import (
    AssistantMessage,
    Checkpoint,
    InMemorySessionStore,
    Session,
    SessionInfo,
    SessionNotFoundError,
    SqliteSessionStore,
    TextBlock,
    ToolUseBlock,
    UserMessage,
    fork_session,
    make_checkpoints,
    resume_session,
)


# ---------------------------------------------------------------------------
# Fixtures — both store types, side by side
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_store() -> InMemorySessionStore:
    return InMemorySessionStore()


@pytest.fixture
def sqlite_store(tmp_path: Path) -> SqliteSessionStore:
    return SqliteSessionStore(str(tmp_path / "sessions.db"))


def _both_stores() -> list:
    """Parametrize: both store types share the SessionStore protocol, so
    every test runs against both. We construct them lazily inside the test
    because pytest fixtures don't play nice with anyio.run().

    For sqlite, we hand out a temp-file path — ``:memory:`` doesn't work
    because the store opens a fresh connection per call, and each
    ``sqlite3.connect(":memory:")`` is its own empty database.
    """

    def _mk_memory() -> InMemorySessionStore:
        return InMemorySessionStore()

    def _mk_sqlite() -> SqliteSessionStore:
        td = tempfile.mkdtemp(prefix="mantis_agent_sess_")
        return SqliteSessionStore(str(Path(td) / "s.db"))

    return [
        pytest.param(_mk_memory, id="memory"),
        pytest.param(_mk_sqlite, id="sqlite"),
    ]


def _sample_messages() -> list:
    """A small but realistic conversation. Three exchanges, one tool use."""

    return [
        UserMessage(content="What is 2 + 2?"),
        AssistantMessage(
            content=[
                TextBlock(text="Let me compute that."),
                ToolUseBlock(id="t1", name="calc", input={"expr": "2+2"}),
            ]
        ),
        UserMessage(content="Use the result for the next question."),
        AssistantMessage(
            content=[TextBlock(text="The answer is 4.")]
        ),
        UserMessage(content="What about 3 + 3?"),
        AssistantMessage(
            content=[TextBlock(text="That's 6.")]
        ),
    ]


# ---------------------------------------------------------------------------
# Checkpoint discovery
# ---------------------------------------------------------------------------


def test_checkpoints_one_per_message() -> None:
    msgs = _sample_messages()
    cps = make_checkpoints(msgs)
    assert len(cps) == len(msgs)
    # Indices go 1..len(messages), never 0 (0 is the "before everything" cut).
    assert [c.index for c in cps] == list(range(1, len(msgs) + 1))


def test_checkpoint_role_and_summary() -> None:
    msgs = _sample_messages()
    cps = make_checkpoints(msgs)
    # First checkpoint is the user's first prompt.
    assert cps[0].role == "user"
    assert "2 + 2" in cps[0].summary
    # Tool-use assistant message gets a [tool_use:calc] marker.
    assert cps[1].role == "assistant"
    assert "[tool_use:calc]" in cps[1].summary


def test_checkpoint_summary_truncates_long_text() -> None:
    very_long = "x" * 500
    msgs = [UserMessage(content=very_long)]
    cps = make_checkpoints(msgs)
    # Summaries cap around 80 chars + role prefix; certainly not 500.
    assert len(cps[0].summary) < 120


def test_checkpoint_is_immutable_struct() -> None:
    cp = Checkpoint(index=1, role="user", summary="hi")
    with pytest.raises(AttributeError):
        cp.index = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Store-level fork (legacy contract, unchanged)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mk_store", _both_stores())
def test_store_fork_full_copy(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        msgs = _sample_messages()
        await store.save("src", msgs, {"title": "original"})

        await store.fork("src", "dst")

        # New row exists, has same messages, parent recorded in meta.
        loaded_msgs, loaded_meta = await store.load("dst")
        assert len(loaded_msgs) == len(msgs)
        assert loaded_meta.get("forked_from") == "src"
        assert loaded_meta.get("title") == "original"

        # Old row is untouched.
        src_msgs, src_meta = await store.load("src")
        assert len(src_msgs) == len(msgs)
        # forked_from is NOT added to the source.
        assert "forked_from" not in src_meta

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_store_fork_isolates_state(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        await store.save("src", _sample_messages(), {})
        await store.fork("src", "dst")

        # Mutate the fork — source should NOT see the change.
        dst_msgs, _ = await store.load("dst")
        dst_msgs.append(UserMessage(content="new question after fork"))
        await store.save("dst", dst_msgs, {})

        src_msgs, _ = await store.load("src")
        assert len(src_msgs) == len(_sample_messages())
        # Last message of src is still the assistant's "That's 6."
        last = src_msgs[-1]
        assert getattr(last, "role", None) == "assistant"

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_store_fork_missing_id_raises(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        with pytest.raises(SessionNotFoundError):
            await store.fork("nope", "dst")

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_store_fork_duplicate_id_raises(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        await store.save("src", _sample_messages(), {})
        await store.save("dst", [UserMessage(content="hi")], {})
        with pytest.raises(ValueError):
            await store.fork("src", "dst")

    anyio.run(main)


# ---------------------------------------------------------------------------
# Session.create / load / save round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_create_then_load(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        sess = await Session.create(store, "abc", title="t1", tags=["a", "b"])
        assert sess.id == "abc"
        assert sess.messages == []
        assert sess.meta.get("title") == "t1"
        assert sess.meta.get("tags") == ["a", "b"]

        # Persisted — list_sessions should see it.
        infos = await store.list_sessions()
        assert any(i.id == "abc" for i in infos)

        # Reload from a fresh handle.
        sess2 = await Session.load(store, "abc")
        assert sess2.id == "abc"
        assert sess2.meta.get("title") == "t1"

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_auto_id(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        s1 = await Session.create(store)
        s2 = await Session.create(store)
        assert s1.id != s2.id
        assert s1.id.startswith("sess_")

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_append_save_load(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        sess = await Session.create(store, "s1")
        sess.append(UserMessage(content="hello"))
        sess.append(AssistantMessage(content=[TextBlock(text="hi")]))
        await sess.save()

        sess2 = await Session.load(store, "s1")
        assert len(sess2.messages) == 2
        assert sess2.messages[0].role == "user"
        assert sess2.messages[1].role == "assistant"

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_reload_drops_in_memory_edits(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        sess = await Session.create(store, "s1")
        sess.append(UserMessage(content="persisted"))
        await sess.save()

        sess.append(UserMessage(content="unsaved"))
        assert len(sess.messages) == 2

        await sess.reload()
        assert len(sess.messages) == 1
        assert sess.messages[0].content == "persisted"

    anyio.run(main)


# ---------------------------------------------------------------------------
# Session.checkpoints()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_checkpoints_match_messages(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        sess = await Session.create(store, "s")
        sess.extend(_sample_messages())
        await sess.save()

        cps = sess.checkpoints()
        assert len(cps) == 6
        assert cps[-1].index == 6

    anyio.run(main)


# ---------------------------------------------------------------------------
# Session.fork — full and truncated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_fork_full(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        src = await Session.create(store, "src")
        src.extend(_sample_messages())
        await src.save()

        dst = await src.fork("dst")
        assert dst.id == "dst"
        assert len(dst.messages) == len(_sample_messages())
        assert dst.meta.get("forked_from") == "src"

        # Source unchanged.
        src2 = await Session.load(store, "src")
        assert "forked_from" not in src2.meta

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_fork_auto_id(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        src = await Session.create(store, "src")
        src.extend(_sample_messages())
        await src.save()

        f1 = await src.fork()
        f2 = await src.fork()
        assert f1.id != f2.id
        assert f1.id != src.id
        assert f1.id.startswith("sess_")

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_fork_at_checkpoint_truncates(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        src = await Session.create(store, "src")
        src.extend(_sample_messages())  # 6 messages
        await src.save()

        cps = src.checkpoints()
        # Pick checkpoint 3 = the boundary after message index 2 (third msg).
        cp = cps[2]
        assert cp.index == 3

        dst = await src.fork("dst", checkpoint=cp)
        assert len(dst.messages) == 3
        assert dst.meta.get("forked_from") == "src"
        assert dst.meta.get("forked_at_index") == 3

        # Persisted with the truncated tail.
        dst2 = await Session.load(store, "dst")
        assert len(dst2.messages) == 3

        # Source is unmodified.
        src2 = await Session.load(store, "src")
        assert len(src2.messages) == 6

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_fork_at_zero_yields_empty(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        src = await Session.create(store, "src")
        src.extend(_sample_messages())
        await src.save()

        # Passing the raw int 0 also works — checkpoint resolution is
        # polymorphic.
        dst = await src.fork(checkpoint=0)
        assert len(dst.messages) == 0
        assert dst.meta.get("forked_at_index") == 0

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_fork_at_head_equivalent_to_full(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        src = await Session.create(store, "src")
        src.extend(_sample_messages())
        await src.save()

        cps = src.checkpoints()
        dst = await src.fork(checkpoint=cps[-1])
        assert len(dst.messages) == len(_sample_messages())

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_fork_at_out_of_range_raises(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        src = await Session.create(store, "src")
        src.extend(_sample_messages())
        await src.save()

        with pytest.raises(ValueError):
            await src.fork(checkpoint=99)
        with pytest.raises(ValueError):
            await src.fork(checkpoint=-1)

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_fork_truncated_isolates_state(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        src = await Session.create(store, "src")
        src.extend(_sample_messages())
        await src.save()

        cps = src.checkpoints()
        dst = await src.fork("dst", checkpoint=cps[2])

        # Continue the fork — should NOT change the source.
        dst.append(UserMessage(content="forked branch question"))
        await dst.save()

        src2 = await Session.load(store, "src")
        assert len(src2.messages) == 6  # unchanged

        dst2 = await Session.load(store, "dst")
        assert len(dst2.messages) == 4

    anyio.run(main)


# ---------------------------------------------------------------------------
# Session.resume_from — in-place rewind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_resume_truncates_in_place(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        sess = await Session.create(store, "s")
        sess.extend(_sample_messages())  # 6 messages
        await sess.save()

        cps = sess.checkpoints()
        await sess.resume_from(cps[3])  # keep first 4 messages

        assert len(sess.messages) == 4
        # Persisted.
        sess2 = await Session.load(store, "s")
        assert len(sess2.messages) == 4
        # Resume history recorded.
        history = sess2.meta.get("resume_history")
        assert history and len(history) == 1
        assert history[0]["index"] == 4

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_resume_multiple_times_appends_history(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        sess = await Session.create(store, "s")
        sess.extend(_sample_messages())
        await sess.save()

        await sess.resume_from(5)  # rewind to 5 msgs
        sess.append(UserMessage(content="new branch a"))
        await sess.save()

        await sess.resume_from(3)  # rewind further to 3 msgs
        history = sess.meta.get("resume_history")
        assert len(history) == 2
        assert [h["index"] for h in history] == [5, 3]

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_resume_int_checkpoint(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        sess = await Session.create(store, "s")
        sess.extend(_sample_messages())
        await sess.save()

        # Pass a raw int instead of a Checkpoint object.
        await sess.resume_from(2)
        assert len(sess.messages) == 2

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_session_resume_out_of_range_raises(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        sess = await Session.create(store, "s")
        sess.extend(_sample_messages())
        await sess.save()

        with pytest.raises(ValueError):
            await sess.resume_from(99)
        with pytest.raises(ValueError):
            await sess.resume_from(-3)

    anyio.run(main)


# ---------------------------------------------------------------------------
# Top-level helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mk_store", _both_stores())
def test_fork_session_helper_full(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        await store.save("src", _sample_messages(), {"title": "x"})

        new_id = await fork_session(store, "src")
        assert new_id.startswith("sess_")

        msgs, meta = await store.load(new_id)
        assert len(msgs) == len(_sample_messages())
        assert meta.get("forked_from") == "src"

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_fork_session_helper_truncated(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        await store.save("src", _sample_messages(), {})

        new_id = await fork_session(store, "src", checkpoint=2)
        msgs, meta = await store.load(new_id)
        assert len(msgs) == 2
        assert meta.get("forked_at_index") == 2

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_fork_session_helper_explicit_id(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        await store.save("src", _sample_messages(), {})
        new_id = await fork_session(store, "src", "explicit", checkpoint=4)
        assert new_id == "explicit"
        msgs, _ = await store.load("explicit")
        assert len(msgs) == 4

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_fork_session_helper_invalid_checkpoint(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        await store.save("src", _sample_messages(), {})
        with pytest.raises(ValueError):
            await fork_session(store, "src", checkpoint=99)

    anyio.run(main)


@pytest.mark.parametrize("mk_store", _both_stores())
def test_resume_session_helper(mk_store) -> None:
    async def main() -> None:
        store = mk_store()
        await store.save("src", _sample_messages(), {})

        sess = await resume_session(store, "src", 3)
        assert sess.id == "src"
        assert len(sess.messages) == 3

        # Persisted.
        msgs, _ = await store.load("src")
        assert len(msgs) == 3

    anyio.run(main)


# ---------------------------------------------------------------------------
# Sqlite-specific persistence quirks
# ---------------------------------------------------------------------------


def test_sqlite_fork_survives_reopen(tmp_path: Path) -> None:
    """Forks written to a sqlite file should be visible after a fresh open."""

    async def main() -> None:
        db_path = tmp_path / "x.db"
        store = SqliteSessionStore(str(db_path))
        sess = await Session.create(store, "src")
        sess.extend(_sample_messages())
        await sess.save()
        await sess.fork("dst", checkpoint=4)

        # Reopen and verify both rows survived.
        store2 = SqliteSessionStore(str(db_path))
        infos = await store2.list_sessions()
        ids = {i.id for i in infos}
        assert {"src", "dst"} <= ids

        dst = await Session.load(store2, "dst")
        assert len(dst.messages) == 4
        assert dst.meta.get("forked_at_index") == 4

    anyio.run(main)


def test_sessioninfo_shape() -> None:
    """SessionInfo round-trips through msgspec encode/decode."""

    info = SessionInfo(
        id="abc",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-02T00:00:00Z",
        n_messages=5,
        title="hello",
        tags=["x"],
    )
    assert info.id == "abc"
    assert info.n_messages == 5
