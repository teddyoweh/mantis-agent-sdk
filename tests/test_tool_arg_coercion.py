"""Schema-driven arg coercion — typed args passed as strings are fixed up."""

from __future__ import annotations


from mantis_agent.streaming.executor import _coerce_to_schema, _coerce_value
from mantis_agent.builtin_tools.fs import grep

_SCHEMA = {"properties": {
    "n": {"type": "integer"}, "flag": {"type": "boolean"},
    "ratio": {"type": "number"}, "items": {"type": "array"}, "obj": {"type": "object"},
}}


def test_int_from_string() -> None:
    assert _coerce_to_schema({"n": "10"}, _SCHEMA)["n"] == 10
    assert _coerce_to_schema({"n": "10.0"}, _SCHEMA)["n"] == 10


def test_bool_from_string() -> None:
    for s, exp in [("true", True), ("false", False), ("yes", True), ("no", False),
                   ("1", True), ("0", False), ("on", True), ("off", False)]:
        assert _coerce_value(s, "boolean") is exp


def test_bool_from_number() -> None:
    assert _coerce_value(1, "boolean") is True
    assert _coerce_value(0, "boolean") is False


def test_number_and_array_and_object() -> None:
    out = _coerce_to_schema({"ratio": "0.5", "items": '["a","b"]', "obj": '{"k":1}'}, _SCHEMA)
    assert out["ratio"] == 0.5
    assert out["items"] == ["a", "b"]
    assert out["obj"] == {"k": 1}


def test_correct_types_unchanged() -> None:
    inp = {"n": 5, "flag": True}
    assert _coerce_to_schema(inp, _SCHEMA) is inp        # identity, no copy


def test_uncoercible_left_as_is() -> None:
    assert _coerce_value("not-a-number", "integer") == "not-a-number"
    assert _coerce_to_schema({"n": "abc"}, _SCHEMA)["n"] == "abc"


def test_no_schema_is_noop() -> None:
    d = {"x": "5"}
    assert _coerce_to_schema(d, None) is d
    assert _coerce_to_schema(d, {}) is d


def test_end_to_end_grep_with_string_ints() -> None:
    # grep's head_limit is an int; a model passing "3" (string) must still work
    coerced = _coerce_to_schema({"pattern": "x", "head_limit": "3"}, grep.input_schema)
    assert coerced["head_limit"] == 3 and isinstance(coerced["head_limit"], int)
