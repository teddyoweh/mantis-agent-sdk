"""`mantis run --json` — structured result output for scripting/CI."""

from __future__ import annotations

import json

from mantis_agent.cli import _build_parser, _result_to_json
from mantis_agent.query import SDKResultMessage
from mantis_agent.types import Usage


def _result(**kw) -> SDKResultMessage:
    base = dict(subtype="success", is_error=False, num_turns=2, result="",
                total_cost_usd=0.01, session_id="sess-1",
                usage=Usage(input_tokens=120, output_tokens=40))
    base.update(kw)
    return SDKResultMessage(**base)


def test_json_has_expected_fields() -> None:
    obj = _result_to_json(_result(), "final text")
    assert obj["result"] == "final text"          # fell back to accumulated text
    assert obj["is_error"] is False
    assert obj["num_turns"] == 2
    assert obj["total_cost_usd"] == 0.01
    assert obj["session_id"] == "sess-1"
    assert obj["usage"]["input_tokens"] == 120
    assert obj["usage"]["output_tokens"] == 40


def test_explicit_result_not_overwritten() -> None:
    obj = _result_to_json(_result(result="model said this"), "fallback")
    assert obj["result"] == "model said this"


def test_error_result_flagged() -> None:
    obj = _result_to_json(_result(is_error=True, result="boom"), "")
    assert obj["is_error"] is True and obj["result"] == "boom"


def test_output_is_valid_json() -> None:
    obj = _result_to_json(_result(), "hi")
    s = json.dumps(obj, default=str)
    assert json.loads(s)["session_id"] == "sess-1"    # round-trips


def test_flags_parse() -> None:
    p = _build_parser()
    assert p.parse_args(["run", "--model", "m", "--json", "go"]).output_format == "json"
    assert p.parse_args(["run", "--model", "m", "--output-format", "json", "go"]).output_format == "json"
    assert p.parse_args(["run", "--model", "m", "go"]).output_format == "text"
