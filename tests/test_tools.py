"""@tool decorator + ToolRegistry tests."""

from __future__ import annotations

import pytest

from mantis_agent import Tool, ToolRegistry, tool


# ---------------------------------------------------------------------------
# @tool decorator — schema derivation
# ---------------------------------------------------------------------------


class TestToolDecorator:
    def test_basic_async_function(self) -> None:
        @tool
        async def get_weather(city: str) -> str:
            """Return weather for a city."""

            return f"{city}: sunny"

        assert isinstance(get_weather, Tool)
        assert get_weather.name == "get_weather"
        assert "weather" in get_weather.description.lower()
        # Schema should be a JSON-schema-shaped dict.
        assert get_weather.input_schema["type"] == "object"
        assert get_weather.input_schema["properties"]["city"]["type"] == "string"
        assert get_weather.input_schema["required"] == ["city"]

    def test_integer_param(self) -> None:
        @tool
        async def double(n: int) -> int:
            """Return n * 2."""

            return n * 2

        assert double.input_schema["properties"]["n"]["type"] == "integer"

    def test_optional_param_not_required(self) -> None:
        @tool
        async def greet(name: str = "world") -> str:
            """Say hi."""

            return f"hi {name}"

        # Default value → not in required list.
        assert greet.input_schema.get("required", []) == []

    def test_list_and_dict_params(self) -> None:
        @tool
        async def aggregate(xs: list[int], meta: dict[str, str]) -> str:
            """Aggregate."""

            return str(xs) + str(meta)

        props = aggregate.input_schema["properties"]
        assert props["xs"]["type"] == "array"
        assert props["xs"]["items"]["type"] == "integer"
        assert props["meta"]["type"] == "object"

    def test_explicit_name_override(self) -> None:
        @tool(name="custom_name")
        async def something(x: str) -> str:
            """X."""

            return x

        assert something.name == "custom_name"

    def test_explicit_input_schema_wins(self) -> None:
        custom_schema = {"type": "object", "properties": {"q": {"type": "string"}}}

        @tool(input_schema=custom_schema)
        async def search(q: str) -> str:
            """Search."""

            return q

        assert search.input_schema is custom_schema

    def test_sync_function_rejected(self) -> None:
        # The decorator is strict — sync functions must be rejected so users
        # don't quietly get a tool that won't await.
        with pytest.raises(TypeError, match="async"):

            @tool
            def sync_tool(x: str) -> str:
                return x

    def test_to_wire_shape(self) -> None:
        @tool
        async def t(x: str) -> str:
            """Doc."""

            return x

        wire = t.to_wire()
        assert wire["name"] == "t"
        assert wire["description"] == "Doc."
        assert wire["input_schema"]["type"] == "object"


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class TestToolRegistry:
    def test_add_and_get(self) -> None:
        @tool
        async def t(x: str) -> str:
            """Doc."""

            return x

        r = ToolRegistry()
        r.add(t)
        got = r.get("t")
        assert got is t

    def test_get_missing_returns_none(self) -> None:
        r = ToolRegistry()
        assert r.get("nope") is None

    def test_duplicate_name_raises(self) -> None:
        @tool
        async def t(x: str) -> str:
            """Doc."""

            return x

        r = ToolRegistry()
        r.add(t)
        # Adding another tool with the same name should error — silent override
        # would be a foot-gun in long-lived sessions.
        with pytest.raises(ValueError, match="duplicate"):
            r.add(t)

    def test_bool_and_len(self) -> None:
        r = ToolRegistry()
        assert not r
        assert len(r) == 0

        @tool
        async def t(x: str) -> str:
            """Doc."""

            return x

        r.add(t)
        assert r
        assert len(r) == 1

    def test_iter(self) -> None:
        @tool
        async def a(x: str) -> str:
            """A."""

            return x

        @tool
        async def b(x: str) -> str:
            """B."""

            return x

        r = ToolRegistry()
        r.add(a, b)
        names = {t.name for t in r}
        assert names == {"a", "b"}

    def test_to_wire_lists_all(self) -> None:
        @tool
        async def a(x: str) -> str:
            """A."""

            return x

        r = ToolRegistry()
        r.add(a)
        wire = r.to_wire()
        assert len(wire) == 1
        assert wire[0]["name"] == "a"
