"""Execution evidence describes actual invocations, never synthetic results."""

from copy import deepcopy

import anyio
import pytest

from mantis_agent.streaming.executor import StreamingToolExecutor
from mantis_agent.tools import Tool, ToolRegistry
from mantis_agent.types import ToolUseBlock


def _registry(tools):
    registry = ToolRegistry()
    registry.add(*tools)
    return registry


def test_canonical_coerced_filtered_inputs_and_detached_snapshot():
    async def run():
        received = []

        async def read_file(count, enabled, payload):
            received.append(deepcopy(dict(count=count, enabled=enabled, payload=payload)))
            # Evidence must already exist and survive mutations by tool code.
            assert executor.executed_calls[0].input == received[0]
            payload["items"].append("tool mutation")
            return "ok"

        schema = {"properties": {
            "count": {"type": "integer"},
            "enabled": {"type": "boolean"},
            "payload": {"type": "object"},
        }}
        registry = _registry([Tool("read_file", "", schema, read_file)])
        original = ToolUseBlock(id="one", name="ReadFile", input={
            "count": "12", "enabled": "true", "payload": {"items": [1]},
            "hallucinated": "drop me",
        })
        async with StreamingToolExecutor(registry) as executor:
            assert executor.executed_calls == []
            executor.add_tool_call(original)
            results = await executor.wait_all()
        assert not results[0].is_error
        expected = ToolUseBlock(id="one", name="read_file", input={
            "count": 12, "enabled": True, "payload": {"items": [1]},
        })
        assert received == [expected.input]
        assert executor.executed_calls == [expected]
        snapshot = executor.executed_calls
        snapshot[0].input["payload"]["items"].append("consumer mutation")
        snapshot.clear()
        original.input["count"] = "99"
        assert executor.executed_calls == [expected]

    anyio.run(run)


@pytest.mark.parametrize("mode", ["unknown", "denied", "permission_error", "pre_cancelled"])
def test_non_invocations_are_excluded(mode):
    async def run():
        async def body():
            pytest.fail("tool must not be invoked")

        async def permission(tool, inputs, context):
            if mode == "permission_error":
                raise RuntimeError("permission failed")
            return False, "not allowed"

        signal = anyio.Event()
        if mode == "pre_cancelled":
            signal.set()
        registry = _registry([Tool("body", "", {}, body)])
        async with StreamingToolExecutor(
            registry, cancellation_signal=signal,
            can_use_tool=permission if mode in ("denied", "permission_error") else None,
        ) as executor:
            executor.add_tool_call(ToolUseBlock(
                id="one", name="missing" if mode == "unknown" else "body", input={},
            ))
            results = await executor.wait_all()
        assert len(results) == 1 and results[0].is_error
        assert executor.executed_calls == []

    anyio.run(run)


@pytest.mark.parametrize("safe", [True, False])
def test_waiting_cancelled_calls_are_excluded(safe):
    async def run():
        started = anyio.Event()
        waiting = anyio.Event()
        signal = anyio.Event()

        async def body(label):
            assert label == "running"
            started.set()
            await anyio.sleep_forever()

        def concurrency(inputs):
            if inputs["label"] == "waiting":
                waiting.set()
            return safe

        registry = _registry([Tool("body", "", {}, body, is_concurrency_safe=concurrency)])
        with anyio.fail_after(5):
            async with StreamingToolExecutor(
                registry, max_concurrency=1, cancellation_signal=signal,
            ) as executor:
                first = ToolUseBlock(id="one", name="body", input={"label": "running"})
                executor.add_tool_call(first)
                await started.wait()
                executor.add_tool_call(ToolUseBlock(
                    id="two", name="body", input={"label": "waiting"},
                ))
                await waiting.wait()
                signal.set()
                results = await executor.wait_all()
        assert len(results) == 2 and all(result.is_error for result in results)
        assert executor.executed_calls == [first]

    anyio.run(run)


@pytest.mark.parametrize("mode", ["error", "timeout", "timeout_setup_error"])
def test_errors_and_timeouts_require_actual_invocation(mode, monkeypatch):
    async def run():
        entered = []

        async def body():
            entered.append(True)
            if mode == "error":
                raise RuntimeError("real tool error")
            await anyio.sleep_forever()

        if mode == "timeout_setup_error":
            def broken_timeout(*args, **kwargs):
                raise RuntimeError("timeout setup failed")

            monkeypatch.setattr(anyio, "fail_after", broken_timeout)
        registry = _registry([Tool(
            "body", "", {}, body, timeout_s=None if mode == "error" else 0.01,
        )])
        async with StreamingToolExecutor(registry) as executor:
            call = ToolUseBlock(id="one", name="body", input={})
            executor.add_tool_call(call)
            results = await executor.wait_all()
        assert results[0].is_error
        if mode == "timeout_setup_error":
            assert entered == []
            assert executor.executed_calls == []
        else:
            assert entered == [True]
            assert executor.executed_calls == [call]

    anyio.run(run)
