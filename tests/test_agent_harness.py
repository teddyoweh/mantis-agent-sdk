from __future__ import annotations

import anyio
import pytest

from mantis_agent import Agent, tool
from mantis_agent.agent import _assert_message_invariants
from mantis_agent.events import (
    ContentBlockDelta, ContentBlockStart, ContentBlockStop, InputJsonDelta,
    MessageDelta, MessageStart, MessageStop, TextDelta,
)
from mantis_agent.providers.mock import MockProvider
from mantis_agent.tools import ToolRegistry
from mantis_agent.types import TextBlock, ToolUseBlock, Usage, UserMessage


class Scripted(MockProvider):
    def __init__(self, scripts):
        super().__init__()
        self.scripts = iter(scripts)
        self.snapshots = []

    async def stream(self, **kwargs):
        self.snapshots.append(list(kwargs['messages']))
        for event in next(self.scripts):
            yield event
            await anyio.sleep(0)


def call(name, call_id, args):
    import json
    return [
        MessageStart(message_id='message', model='mock'),
        ContentBlockStart(index=0, block=ToolUseBlock(id=call_id, name=name, input={})),
        ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json=json.dumps(args))),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason='tool_use', usage=Usage(input_tokens=5, output_tokens=5)),
        MessageStop(),
    ]


def answer():
    return [
        MessageStart(message_id='message', model='mock'), ContentBlockStart(index=0, block=TextBlock(text='')),
        ContentBlockDelta(index=0, delta=TextDelta(text='Done')),
        ContentBlockStop(index=0), MessageDelta(stop_reason='end_turn'), MessageStop(),
    ]


def build(provider, **kwargs):
    return Agent(model='mock', provider=provider, include_memory=False,
                 include_env=False, include_recall=False, max_steps=5, **kwargs)


def test_bound_tools_do_not_mutate_shared_registry(tmp_path):
    from mantis_agent.artifacts import ArtifactStore
    from mantis_agent.task_state import TaskState

    registry = ToolRegistry()
    agent = build(MockProvider(), tools=registry,
                  artifact_store=ArtifactStore(tmp_path / 'artifacts'), task_state=TaskState())
    assert len(registry) == 0
    assert agent.tools.resolve('read_artifact') is not None
    assert agent.tools.resolve('task_state') is not None
    sibling = build(MockProvider(), tools=registry)
    assert len(sibling.tools) == 0


def test_unused_task_state_preserves_todo_progress():
    from mantis_agent.task_state import TaskState

    agent = build(MockProvider(), task_state=TaskState(),
                  todos=[{'content': 'done', 'status': 'completed'}])
    assert agent._progress_value() == 1
    agent.task_state.add_check('test', 'test passes')
    assert agent._progress_value() == 0


def test_reserved_tool_collision_is_explicit(tmp_path):
    from mantis_agent.artifacts import ArtifactStore, make_artifact_tool

    store = ArtifactStore(tmp_path)
    with pytest.raises(ValueError, match='reserved'):
        build(MockProvider(), tools=[make_artifact_tool(store)], artifact_store=store)


def test_actual_execution_updates_task_context_without_breaking_pairs():
    from mantis_agent.task_state import TaskState

    @tool
    async def inspect_test() -> str:
        """Run a deterministic check."""
        return 'one test passed'

    state = TaskState()
    provider = Scripted([call('inspect_test', 'check-1', {}), answer()])
    agent = build(provider, tools=[inspect_test], task_state=state)
    messages = [UserMessage(content='Check the implementation')]
    anyio.run(agent.run, messages)
    assert 'check-1' in state.render()
    assert any('check-1' in str(getattr(m, 'content', '')) for m in provider.snapshots[1])
    for snapshot in provider.snapshots:
        _assert_message_invariants(snapshot)
    # Evidence is a per-request tail projection, never persisted history
    # (mid-history rewrites break local prefix caches).
    reminders = [m for m in messages if isinstance(m, UserMessage)
                 and isinstance(m.content, str) and m.content.startswith('[Current task evidence]')]
    assert reminders == []
    for snapshot in provider.snapshots:
        assert str(snapshot[-1].content).startswith('[Current task evidence]')


def test_recall_can_return_after_context_eviction(monkeypatch):
    import mantis_agent.memory_recall as recall

    seen = []
    path = '/memory/a.md'

    def fake_recall(query, *, already_surfaced):
        seen.append(already_surfaced)
        if path in already_surfaced:
            return '', []
        return f'<system-reminder>\nMemory: {path}:\nimportant fact\n</system-reminder>', [path]

    monkeypatch.setattr(recall, 'recall_block', fake_recall)
    monkeypatch.delenv('MANTIS_AGENT_NO_CONTEXT', raising=False)
    provider = Scripted([answer(), answer(), answer()])
    agent = build(provider, auto_compact=False)
    agent.include_recall = True
    messages = [UserMessage(content='Remember this')]
    async def exercise():
        await agent.run(messages)
        messages.append(UserMessage(content='Use the fact again'))
        await agent.run(messages)
        assert path in seen[1]
        messages[:] = [UserMessage(content='Need the fact after compaction')]
        await agent.run(messages)
        assert path not in seen[2]

    anyio.run(exercise)


def test_structured_output_is_observed_without_private_thinking():
    from mantis_agent.task_state import TaskState
    from mantis_agent.types import ThinkingBlock

    @tool
    async def structured_check():
        """Return structured evidence."""
        return [TextBlock(text='public result')]

    state = TaskState()
    agent = build(Scripted([call('structured_check', 'structured', {}), answer()]),
                  tools=[structured_check], task_state=state)
    anyio.run(agent.run, [UserMessage(content='Check')])
    evidence = state.inspect()['evidence']['structured']
    assert 'public result' in evidence['output_excerpt']
    from mantis_agent.types import ToolResultBlock
    state.observe([ToolUseBlock(id='private', name='probe', input={})], [
        ToolResultBlock(tool_use_id='private', content=[
            TextBlock(text='public'), ThinkingBlock(thinking='private detail')
        ])
    ])
    assert 'private detail' not in str(state.inspect())


def test_provider_projection_restores_evicted_state_and_recall():
    from mantis_agent.task_state import TaskState

    state = TaskState()
    state.set_objective('retain objective')
    provider = Scripted([answer()])
    agent = build(provider, task_state=state)
    agent.include_recall = True
    agent._recall_text = 'precise recalled evidence'
    messages = [UserMessage(content='compacted history')]

    async def exercise():
        async for _ in agent._provider_stream(messages):
            pass

    anyio.run(exercise)
    snapshot = provider.snapshots[0]
    assert any('retain objective' in str(m.content) for m in snapshot)
    assert any('precise recalled evidence' in str(m.content) for m in snapshot)
    _assert_message_invariants(snapshot)
    assert len(messages) == 1
