"""Named workflow definitions — parsing, precedence, validation, execution.

Every test runs against a FAKE agent runner and a temp MANTIS_AGENT_HOME: no
network, no model, no tokens, no writes outside tmp_path.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent.types import AssistantMessage, TextBlock, Usage
from mantis_agent.workflow import Workflow
from mantis_agent.workflow_defs import (
    MAX_DEFINITION_AGENTS,
    WorkflowDefinitionError,
    builtin_definitions,
    cache_key,
    discover_workflow_definitions,
    execute_definition,
    load_workflow_definition,
    parse_workflow_md,
    render_template,
    resolve_inputs,
    validate_definition_data,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    yield


def _counter():
    it = iter(range(0, 10_000_000, 100))
    return lambda: next(it)


def make_runner(prefix="out"):
    """Echo runner: one turn, deterministic text derived from the prompt."""

    async def runner(prompt, *, model, agent_type, schema=None):
        yield AssistantMessage(
            content=[TextBlock(text=f"{prefix}({agent_type}):{prompt.strip().splitlines()[-1]}")],
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    return runner


DEF_MD = """---
name: demo
description: A demo workflow
when_to_use: for tests
---

Shared briefing line.

```json
{
  "inputs": [
    {"name": "target", "required": true, "description": "what to look at"},
    {"name": "note", "default": "none"}
  ],
  "phases": [
    {"title": "Scan", "mode": "parallel", "detail": "two readers", "agents": [
      {"label": "a", "agent_type": "explore", "prompt": "scan {target}"},
      {"label": "b", "agent_type": "explore", "prompt": "scan again {target} {note}"}
    ]},
    {"title": "Sum", "mode": "sequential", "agents": [
      {"label": "s", "prompt": "summarize {phase:Scan}"}
    ]}
  ]
}
```
"""


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_parse_markdown_definition():
    d = parse_workflow_md(DEF_MD, "fallback")
    assert d.name == "demo"
    assert d.description == "A demo workflow"
    assert d.when_to_use == "for tests"
    assert d.briefing == "Shared briefing line."
    assert [p.title for p in d.phases] == ["Scan", "Sum"]
    assert d.phases[0].mode == "parallel"
    assert d.phases[0].detail == "two readers"
    assert [a.label for a in d.phases[0].agents] == ["a", "b"]
    assert d.phases[1].mode == "sequential"
    assert [i.name for i in d.inputs] == ["target", "note"]
    assert d.required_input_names() == ["target"]
    assert d.min_agents == 3


def test_parse_falls_back_to_filename_and_bare_json_body():
    text = '{"phases": [{"title": "T", "agents": [{"prompt": "go"}]}]}'
    d = parse_workflow_md(text, "from-file")
    assert d.name == "from-file"
    assert d.phases[0].title == "T"


def test_parse_without_graph_is_an_error():
    with pytest.raises(WorkflowDefinitionError) as e:
        parse_workflow_md("---\nname: x\n---\n\njust prose\n", "x")
    assert "json" in str(e.value).lower()


def test_parse_reports_every_validation_error_at_once():
    bad = """---
name: bad
---

```json
{"phases": [
  {"mode": "nonsense", "agents": []},
  {"title": "P", "mode": "pipeline", "over": "phase:Nope", "stages": [{}]}
]}
```
"""
    with pytest.raises(WorkflowDefinitionError) as e:
        parse_workflow_md(bad, "bad")
    joined = " ".join(e.value.errors)
    assert "missing 'title'" in joined
    assert "unknown mode" in joined
    assert "not an EARLIER phase" in joined
    assert "missing 'prompt'" in joined


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_validate_requires_phases():
    assert "'phases' must be a non-empty list" in validate_definition_data("x", {})
    assert "'phases' must be a non-empty list" in validate_definition_data("x", {"phases": []})


def test_validate_rejects_duplicate_phase_titles():
    data = {"phases": [
        {"title": "A", "agents": [{"prompt": "p"}]},
        {"title": "A", "agents": [{"prompt": "p"}]},
    ]}
    assert any("duplicates phase title" in e for e in validate_definition_data("x", data))


def test_validate_pipeline_over_must_reference_earlier_phase_or_input():
    ok = {"inputs": [{"name": "files"}], "phases": [
        {"title": "A", "agents": [{"prompt": "p"}]},
        {"title": "B", "mode": "pipeline", "over": "phase:A", "stages": [{"prompt": "p"}]},
        {"title": "C", "mode": "pipeline", "over": "input:files", "stages": [{"prompt": "p"}]},
    ]}
    assert validate_definition_data("x", ok) == []

    bad = {"phases": [
        {"title": "A", "mode": "pipeline", "over": "input:ghost", "stages": [{"prompt": "p"}]},
    ]}
    # No inputs declared at all → nothing to check against, so only the shape matters.
    assert validate_definition_data("x", bad) == []

    bad2 = {"inputs": [{"name": "real"}], "phases": [
        {"title": "A", "mode": "pipeline", "over": "input:ghost", "stages": [{"prompt": "p"}]},
    ]}
    assert any("undeclared input" in e for e in validate_definition_data("x", bad2))


def test_validate_caps_total_agents():
    data = {"phases": [{"title": "A", "agents": [
        {"prompt": f"p{i}"} for i in range(MAX_DEFINITION_AGENTS + 1)
    ]}]}
    assert any("over the" in e for e in validate_definition_data("x", data))


# ---------------------------------------------------------------------------
# built-ins + discovery/precedence
# ---------------------------------------------------------------------------


def test_builtins_all_parse_and_cover_the_four_patterns():
    names = {d.name for d in builtin_definitions()}
    assert {"understand", "design", "review", "research", "implement"} <= names
    review = next(d for d in builtin_definitions() if d.name == "review")
    modes = {p.mode for p in review.phases}
    assert {"parallel", "pipeline", "sequential"} == modes
    assert all(d.description for d in builtin_definitions())
    assert all(d.source == "builtin" for d in builtin_definitions())


def test_discovery_precedence_project_over_user_over_builtin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "workflows").mkdir(parents=True)
    (home / "workflows" / "review.md").write_text(
        DEF_MD.replace("name: demo", "name: review"), encoding="utf-8")
    (home / "workflows" / "userone.md").write_text(
        DEF_MD.replace("name: demo", "name: userone"), encoding="utf-8")

    proj = tmp_path / "proj"
    (proj / ".mantis" / "workflows").mkdir(parents=True)
    (proj / ".mantis" / "workflows" / "review.md").write_text(
        DEF_MD.replace("name: demo", "name: review")
              .replace("A demo workflow", "project override"), encoding="utf-8")

    found = {d.name: d for d in discover_workflow_definitions(proj)}
    assert found["review"].source == "project"
    assert found["review"].description == "project override"
    assert found["userone"].source == "user"
    assert found["understand"].source == "builtin"
    assert found["review"].path.endswith("review.md")


def test_discovery_skips_broken_files_and_reports_them(tmp_path):
    proj = tmp_path / "p"
    d = proj / ".mantis" / "workflows"
    d.mkdir(parents=True)
    (d / "broken.md").write_text("---\nname: broken\n---\nno graph here\n", encoding="utf-8")
    (d / "good.md").write_text(DEF_MD, encoding="utf-8")
    errors: list[str] = []
    names = {x.name for x in discover_workflow_definitions(proj, errors=errors)}
    assert "demo" in names and "broken" not in names
    assert len(errors) == 1 and "broken.md" in errors[0]


def test_load_by_name(tmp_path):
    assert load_workflow_definition("review").name == "review"
    assert load_workflow_definition("nope-not-here") is None
    assert load_workflow_definition("") is None


# ---------------------------------------------------------------------------
# templating + inputs
# ---------------------------------------------------------------------------


def test_render_template_leaves_unknown_placeholders_intact():
    out = render_template("hi {name}, json {\"a\": 1} and {missing}", {"name": "x"})
    assert out == 'hi x, json {"a": 1} and {missing}'


def test_render_template_supports_phase_references():
    assert render_template("see {phase:Scan}", {"phase:Scan": "R"}) == "see R"


def test_resolve_inputs_applies_defaults_and_keeps_extras():
    d = parse_workflow_md(DEF_MD, "demo")
    got = resolve_inputs(d, {"target": "t", "extra": "e"})
    assert got == {"target": "t", "note": "none", "extra": "e"}


def test_resolve_inputs_missing_required_lists_every_one():
    d = parse_workflow_md(DEF_MD, "demo")
    with pytest.raises(WorkflowDefinitionError) as e:
        resolve_inputs(d, {})
    assert "target" in str(e.value)
    assert "what to look at" in str(e.value)


def test_resolve_inputs_accepts_a_bare_string_as_objective():
    d = parse_workflow_md(DEF_MD.replace('"name": "target", "required": true',
                                         '"name": "objective"'), "demo")
    assert resolve_inputs(d, "do the thing")["objective"] == "do the thing"


def test_cache_key_changes_with_the_prompt():
    a = cache_key("P", "l", "prompt one")
    assert a == cache_key("P", "l", "prompt one")
    assert a != cache_key("P", "l", "prompt two")
    assert a != cache_key("Q", "l", "prompt one")


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


def _run_defn(defn, inputs, runner=None, **kw):
    wf = Workflow("t", agent_runner=runner or make_runner(), clock=_counter(), model="m")
    result = anyio.run(lambda: execute_definition(defn, workflow=wf, inputs=inputs, **kw))
    return wf, result


def test_execute_walks_phases_and_substitutes_inputs():
    defn = parse_workflow_md(DEF_MD, "demo")
    wf, result = _run_defn(defn, resolve_inputs(defn, {"target": "TGT"}))

    assert [p.title for p in wf.run.phases] == ["Scan", "Sum"]
    assert [a.label for a in wf.run.phases[0].agents] == ["a", "b"]
    assert wf.run.status == "done"
    assert result["definition"] == "demo"
    # the briefing rides along with every child prompt
    assert all(a.prompt.startswith("Shared briefing line.") for a in wf.run.all_agents())
    assert "scan TGT" in wf.run.phases[0].agents[0].prompt
    # the summarizer saw BOTH scan results through {phase:Scan}
    summarizer = wf.run.phases[1].agents[0]
    assert "scan TGT" in summarizer.prompt and "scan again TGT none" in summarizer.prompt


def test_execute_pipeline_fans_out_one_chain_per_item():
    md = """---
name: pipe
description: d
---

```json
{"inputs": [{"name": "files"}], "phases": [
  {"title": "Each", "mode": "pipeline", "over": "input:files", "stages": [
    {"label": "read", "prompt": "read {item}"},
    {"label": "check", "prompt": "check {prev}"}
  ]}
]}
```
"""
    defn = parse_workflow_md(md, "pipe")
    wf, result = _run_defn(defn, {"files": "one\ntwo\nthree"})
    by_label = {a.label: a for a in wf.run.all_agents()}
    # One chain per item, two stages each — registration order follows start
    # time (all stage-0s launch together), so compare as a set.
    assert set(by_label) == {"read·1", "check·1", "read·2", "check·2",
                             "read·3", "check·3"}
    assert "read one" in by_label["read·1"].prompt
    assert "read three" in by_label["read·3"].prompt
    # each stage-2 sees ITS OWN item's stage-1 output, not another item's
    assert "read one" in by_label["check·1"].prompt
    assert "read three" in by_label["check·3"].prompt
    assert len(result["phases"][0]["results"]) == 3


def test_execute_sequential_threads_prev_between_agents():
    md = """---
name: seq
description: d
---

```json
{"phases": [{"title": "S", "mode": "sequential", "agents": [
  {"label": "one", "prompt": "first"},
  {"label": "two", "prompt": "after: {prev}"}
]}]}
```
"""
    wf, _ = _run_defn(parse_workflow_md(md, "seq"), {})
    assert "after: out(general-purpose):first" in wf.run.all_agents()[1].prompt


def test_execute_replays_cached_agents_without_calling_the_runner():
    calls: list[str] = []

    async def counting(prompt, *, model, agent_type, schema=None):
        calls.append(prompt)
        yield AssistantMessage(content=[TextBlock(text="fresh")],
                               usage=Usage(input_tokens=1, output_tokens=1))

    defn = parse_workflow_md(DEF_MD, "demo")
    inputs = resolve_inputs(defn, {"target": "T"})

    wf1, _ = _run_defn(defn, inputs)
    from mantis_agent.workflow_store import replay_cache
    cache = replay_cache(wf1.run.to_dict())

    wf2 = Workflow("t2", agent_runner=counting, clock=_counter(), model="m")
    result = anyio.run(lambda: execute_definition(
        defn, workflow=wf2, inputs=inputs, cache=cache))

    assert calls == []                      # nothing hit the runner
    assert result["replayed"] == 3
    assert all(a.replayed for a in wf2.run.all_agents())
    assert all(a.status == "done" for a in wf2.run.all_agents())


def test_execute_coerces_unknown_agent_type_and_logs_it():
    md = """---
name: t
description: d
---

```json
{"phases": [{"title": "P", "agents": [
  {"label": "x", "agent_type": "no-such-persona", "prompt": "go"}
]}]}
```
"""
    wf, _ = _run_defn(parse_workflow_md(md, "t"), {},
                      valid_agent_types=["explore", "general-purpose"])
    assert wf.run.all_agents()[0].agent_type == "general-purpose"
    assert any("no-such-persona" in line for line in wf.run.log_lines)


def test_execute_skips_a_pipeline_with_no_items():
    md = """---
name: t
description: d
---

```json
{"inputs": [{"name": "files"}], "phases": [
  {"title": "P", "mode": "pipeline", "over": "input:files",
   "stages": [{"prompt": "go {item}"}]}
]}
```
"""
    wf, result = _run_defn(parse_workflow_md(md, "t"), {"files": ""})
    assert wf.run.all_agents() == []
    assert any("nothing to pipeline over" in line for line in wf.run.log_lines)
    assert result["status"] == "done"


def test_execute_refuses_a_runtime_fanout_over_the_cap():
    md = """---
name: t
description: d
---

```json
{"inputs": [{"name": "files"}], "phases": [
  {"title": "P", "mode": "pipeline", "over": "input:files",
   "stages": [{"prompt": "go {item}"}]}
]}
```
"""
    defn = parse_workflow_md(md, "t")
    items = "\n".join(f"f{i}" for i in range(MAX_DEFINITION_AGENTS + 5))
    with pytest.raises(WorkflowDefinitionError) as e:
        _run_defn(defn, {"files": items})
    assert "over the" in str(e.value)
