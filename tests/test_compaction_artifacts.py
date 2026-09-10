"""Recoverable compaction keeps exact evidence without changing tool pairing."""
import json
import re

import anyio
import pytest

from mantis_agent.agent import _assert_message_invariants
from mantis_agent.artifacts import ArtifactStore, make_artifact_tool
from mantis_agent.compact import SimpleCompactor
from mantis_agent.types import (
    AssistantMessage, ImageBlock, TextBlock, ThinkingBlock, ToolResultBlock,
    ToolUseBlock, UserMessage,
)


async def summarize(prompt):
    return "Summary of prior conversation: ran tools."


def artifact_id(text):
    return re.search(r"artifact:([0-9a-f]{64})", text).group(1)


def transcript(payload):
    return [
        UserMessage(content="original task"),
        AssistantMessage(content=[ThinkingBlock(thinking="PRIVATE SECRET"),
                                  ToolUseBlock(id="a", name="bash", input={})]),
        UserMessage(content=[ToolResultBlock(tool_use_id="a", content=payload, is_error=True)]),
        AssistantMessage(content=[ToolUseBlock(id="b", name="bash", input={})]),
        UserMessage(content=[ToolResultBlock(tool_use_id="b", content="recent")]),
        AssistantMessage(content=[TextBlock(text="next")]),
    ]


def recover(store, id):
    offset = 0
    chunks = []
    while True:
        result = store.read(id, offset, 137)
        chunks.append(result["content"])
        if result["eof"]:
            return "".join(chunks)
        offset = result["next_offset"]


@pytest.mark.parametrize("method", ["microcompact", "emergency_clear"])
@pytest.mark.parametrize("structured", [False, True])
def test_strip_exact_recovery_and_idempotence(tmp_path, method, structured):
    store = ArtifactStore(tmp_path / "artifacts")
    text = "α\r\nlarge output\x00" * 2000
    image = ImageBlock(source={"type": "base64", "data": "abc" * 1000})
    payload = [TextBlock(text=text), image, ThinkingBlock(thinking="PRIVATE SECRET")] if structured else text
    messages = transcript(payload)
    comp = SimpleCompactor(summarize, artifact_store=store, micro_min_chars=0,
                           micro_keep_tool_results=1)
    assert getattr(comp, method)(messages)
    _assert_message_invariants(messages)
    block = messages[2].content[0]
    assert block.tool_use_id == "a" and block.is_error
    id = artifact_id(block.content)
    archived = recover(store, id)
    if structured:
        data = json.loads(archived)
        assert data[0]["text"] == text
        assert data[1]["source"] == image.source
        assert len(data) == 2
    else:
        assert archived == text
    assert "PRIVATE SECRET" not in archived
    snapshot = list(messages)
    assert not getattr(comp, method)(messages)
    assert messages == snapshot
    assert messages[4].content[0].content == "recent"
    # Same ID and full payload survive reopening the store.
    assert recover(ArtifactStore(store.root), id) == archived


def test_bare_image_emergency(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    image = ImageBlock(source={"data": "x" * 9000})
    messages = [UserMessage(content=[image])]
    comp = SimpleCompactor(summarize, artifact_store=store, micro_min_chars=0)
    assert comp.emergency_clear(messages, keep_last=0)
    assert json.loads(recover(store, artifact_id(messages[0].content[0].text)))["source"] == image.source
    assert not comp.emergency_clear(messages, keep_last=0)


def test_full_archive_before_summary_and_repeated_compaction(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    text = "exact evidence" * 5000
    messages = transcript(text)

    async def summary(prompt):
        assert list(store.root.iterdir()), "archive must exist before summarizer runs"
        return "done"

    comp = SimpleCompactor(summary, artifact_store=store, keep_recent_turns=1)
    out = anyio.run(comp.compact, messages)
    _assert_message_invariants(out)
    assert out[0] is messages[0] and out[-1] is messages[-1]
    first_id = artifact_id(out[1].content)
    archived = json.loads(recover(store, first_id))
    assert archived[1]["content"][0]["content"] == text
    assert "PRIVATE SECRET" not in recover(store, first_id)
    assert archived[0]["content"][0]["type"] == "tool_use"
    out.extend([UserMessage(content="continue"), AssistantMessage(content=[TextBlock(text="ok")])])
    again = anyio.run(comp.compact, out)
    second_id = artifact_id(again[1].content)
    assert first_id in recover(store, second_id)
    assert json.loads(recover(store, first_id))[1]["content"][0]["content"] == text
    result = json.loads(anyio.run(lambda: make_artifact_tool(store).fn(id=first_id)))
    assert "PRIVATE SECRET" not in result["content"]


@pytest.mark.parametrize("method", ["microcompact", "emergency_clear", "compact"])
@pytest.mark.parametrize("error", [PermissionError, OSError, TypeError, ValueError])
def test_archive_failure_preserves_evidence(tmp_path, monkeypatch, method, error):
    store = ArtifactStore(tmp_path / "artifacts")
    messages = transcript("evidence" * 2000)
    before = list(messages)

    def fail(*args):
        raise error("archive failed")

    monkeypatch.setattr(store, "put_text", fail)
    comp = SimpleCompactor(summarize, artifact_store=store, keep_recent_turns=1,
                           micro_keep_tool_results=1)
    if method == "compact":
        assert anyio.run(comp.compact, messages) is messages
    else:
        assert not getattr(comp, method)(messages)
    assert messages == before


def test_bare_image_micro_and_failure(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "artifacts")
    image = UserMessage(content=[ImageBlock(source={"data": "x" * 9000})])
    messages = [image, *transcript("large" * 1000)]
    comp = SimpleCompactor(summarize, artifact_store=store, micro_keep_tool_results=1)
    assert comp.microcompact(messages)
    assert artifact_id(messages[0].content[0].text)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(store, "put_json", fail)
    messages = [image]
    assert not comp.emergency_clear(messages, keep_last=0)
    assert messages[0] is image
