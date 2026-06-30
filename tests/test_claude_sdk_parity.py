"""Verify the public SDK surface matches Claude Agent SDK field-for-field.

These are the tests that catch divergence drift. Each one asserts a
canonical shape from the upstream zip (``entrypoints/sdk/coreSchemas.ts``).
If we add/rename a field, this test must be updated AND the upstream
schema must be re-checked.
"""

from __future__ import annotations

import msgspec

from mantis_agent import (
    APIAssistantMessage,
    APIUserMessage,
    ModelUsage,
    SDKAssistantMessage,
    SDKCompactBoundaryMessage,
    SDKPermissionDenial,
    SDKResultMessage,
    SDKStatusMessage,
    SDKSystemMessage,
    SDKUserMessage,
    Usage,
)


# ---------------------------------------------------------------------------
# Field-set equivalence with upstream schemas
# ---------------------------------------------------------------------------


def _fields(struct_cls) -> set[str]:
    return set(struct_cls.__struct_fields__)


def test_model_usage_fields_camelcase() -> None:
    """ModelUsage on the wire uses camelCase (matches JS SDK consumers)."""

    expected = {
        "inputTokens",
        "outputTokens",
        "cacheReadInputTokens",
        "cacheCreationInputTokens",
        "webSearchRequests",
        "costUSD",
        "contextWindow",
        "maxOutputTokens",
    }
    assert _fields(ModelUsage) == expected


def test_usage_fields_snake_case() -> None:
    """Internal Usage uses snake_case (Python idiom)."""

    expected = {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }
    assert _fields(Usage) == expected


def test_sdk_result_message_fields() -> None:
    """SDKResultMessage matches Claude SDK's union of Success + Error
    subtypes — same fields, default subtype='success'."""

    expected = {
        "subtype",
        "duration_ms",
        "duration_api_ms",
        "is_error",
        "num_turns",
        "result",
        "stop_reason",
        "total_cost_usd",
        "usage",
        "modelUsage",
        "permission_denials",
        "errors",
        "uuid",
        "session_id",
    }
    assert _fields(SDKResultMessage) == expected


def test_sdk_assistant_message_nests_message() -> None:
    """Assistant turn nests the API message under ``message``."""

    expected = {"message", "parent_tool_use_id", "uuid", "session_id", "error"}
    assert _fields(SDKAssistantMessage) == expected


def test_sdk_user_message_nests_message() -> None:
    """User turn nests the API message under ``message``."""

    expected = {
        "message",
        "parent_tool_use_id",
        "uuid",
        "session_id",
        "isSynthetic",
        "tool_use_result",
    }
    assert _fields(SDKUserMessage) == expected


def test_sdk_system_message_init_subtype() -> None:
    """System message defaults to subtype='init' (session-start banner)."""

    expected = {
        "subtype",
        "apiKeySource",
        "cwd",
        "tools",
        "mcp_servers",
        "model",
        "permissionMode",
        "slug",
        "output_style",
        "agents",
        "uuid",
        "session_id",
    }
    assert _fields(SDKSystemMessage) == expected
    msg = SDKSystemMessage()
    assert msg.subtype == "init"


def test_sdk_compact_boundary_subtype() -> None:
    msg = SDKCompactBoundaryMessage()
    assert msg.subtype == "compact_boundary"
    assert _fields(SDKCompactBoundaryMessage) == {
        "subtype",
        "compact_metadata",
        "uuid",
        "session_id",
    }


def test_sdk_status_subtype() -> None:
    msg = SDKStatusMessage()
    assert msg.subtype == "status"


def test_permission_denial_shape() -> None:
    pd = SDKPermissionDenial(tool_name="x", tool_use_id="y", tool_input={"a": 1})
    assert pd.tool_name == "x"
    assert pd.tool_use_id == "y"
    assert pd.tool_input == {"a": 1}


# ---------------------------------------------------------------------------
# Wire-format roundtrip
# ---------------------------------------------------------------------------


def test_sdk_result_message_encodes_with_subtype_tag() -> None:
    """Encoded SDKResultMessage carries type='result' and subtype='success'."""

    enc = msgspec.json.Encoder()
    out = enc.encode(
        SDKResultMessage(
            duration_ms=42,
            num_turns=2,
            result="hello",
            total_cost_usd=0.001,
        )
    )
    decoded = msgspec.json.decode(out)
    assert decoded["type"] == "result"
    assert decoded["subtype"] == "success"
    assert decoded["result"] == "hello"
    assert decoded["total_cost_usd"] == 0.001
    assert decoded["num_turns"] == 2


def test_sdk_assistant_message_wire_shape() -> None:
    """Encoded SDKAssistantMessage has type='assistant' + a nested ``message``."""

    enc = msgspec.json.Encoder()
    api = APIAssistantMessage(id="msg_1", model="qwen2.5-72b-instruct")
    out = enc.encode(
        SDKAssistantMessage(message=api, uuid="u1", session_id="s1")
    )
    decoded = msgspec.json.decode(out)
    assert decoded["type"] == "assistant"
    assert decoded["message"]["id"] == "msg_1"
    assert decoded["message"]["role"] == "assistant"
    assert decoded["message"]["model"] == "qwen2.5-72b-instruct"
    assert decoded["session_id"] == "s1"


def test_sdk_user_message_wire_shape() -> None:
    enc = msgspec.json.Encoder()
    api = APIUserMessage(content="hi")
    out = enc.encode(SDKUserMessage(message=api, session_id="s1"))
    decoded = msgspec.json.decode(out)
    assert decoded["type"] == "user"
    assert decoded["message"]["role"] == "user"
    assert decoded["message"]["content"] == "hi"
