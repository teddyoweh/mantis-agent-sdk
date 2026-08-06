"""Model-authored workflow scripts.

Two things need pinning: the restricted subset (what a script may and may not
do), and that the hooks map onto the real engine — phases, the concurrency cap,
personas, and the return value that comes back as the tool result.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent.types import AssistantMessage, TextBlock, Usage
from mantis_agent.workflow import Workflow
from mantis_agent.workflow_script import (
    ScriptError,
    extract_meta,
    run_workflow_script,
    validate_script,
)


def _stub(record: list | None = None):
    async def runner(prompt, *, model="", agent_type="", schema=None):
        async def gen():
            if record is not None:
                record.append((prompt, agent_type, model))
            await anyio.sleep(0.001)
            yield AssistantMessage(
                content=[TextBlock(text=f"<{prompt[:40]}>")],
                usage=Usage(input_tokens=1, output_tokens=1),
            )
        return gen()
    return runner


def _run(script: str, *, args=None, record=None, concurrency=4, runner=None):
    wf = Workflow("placeholder", agent_runner=runner or _stub(record),
                  concurrency=concurrency)
    out = anyio.run(lambda: run_workflow_script(script, wf, args=args))
    return wf, out


# -- the restricted subset ---------------------------------------------------


@pytest.mark.parametrize(
    ("label", "src"),
    [
        ("import", "import os\nreturn 1"),
        ("from-import", "from os import system\nreturn 1"),
        ("dunder escape", "return ().__class__.__bases__[0].__subclasses__()"),
        ("private attribute", "return args._secret"),
        ("eval", "return eval('1+1')"),
        ("open", "return open('/etc/passwd')"),
        ("__import__", "return __import__('os')"),
        ("globals", "return globals()"),
        ("getattr", "return getattr(args, 'x')"),
        ("class def", "class X:\n    pass\nreturn 1"),
        ("dunder string", "return args['__class__']"),
        ("generator", "yield 1"),
    ],
)
def test_escapes_are_refused(label: str, src: str) -> None:
    """A workflow script orchestrates agents and computes over their results.
    Anything reaching outside that is an agent's job, where permissions apply."""
    with pytest.raises(ScriptError):
        validate_script(src)


@pytest.mark.parametrize(
    ("label", "src"),
    [
        ("comprehension + f-string", 'return [f"{i}" for i in range(3)]'),
        ("loops + conditionals", "t = 0\nfor i in range(5):\n    if i % 2:\n"
                                 "        t += i\nreturn t"),
        ("while + break", "n = 0\nwhile True:\n    n += 1\n    if n > 3:\n"
                          "        break\nreturn n"),
        ("try/except", "try:\n    x = 1 / 0\nexcept Exception:\n    x = 7\nreturn x"),
        ("nested def", "def double(x):\n    return x * 2\nreturn double(21)"),
        ("lambda thunks", "return [(lambda i=i: i) for i in range(3)]"),
        ("dict/sorted/sum", "return sum(sorted({'b': 2, 'a': 1}.values()))"),
    ],
)
def test_real_control_flow_is_available(label: str, src: str) -> None:
    """The point of a script over a declarative definition is control flow —
    refusing loops or comprehensions would defeat the feature."""
    validate_script(src)


def test_a_syntax_error_names_its_line() -> None:
    with pytest.raises(ScriptError) as e:
        validate_script("x = 1\ny = (\n")
    assert e.value.line == 2


def test_a_runtime_error_names_the_scripts_own_line() -> None:
    """The AST is wrapped in an async function rather than re-indented into a
    string, so line numbers still point at what the model wrote."""
    with pytest.raises(ScriptError) as e:
        _run("a = 1\nb = 2\nreturn a / 0\n")
    assert e.value.line == 3


# -- meta --------------------------------------------------------------------


def test_meta_is_read_before_the_script_runs() -> None:
    """The run has to be named and its phases drawn from the moment it starts —
    including when the body later fails."""
    tree = validate_script(
        'meta = {"name": "n", "phases": [{"title": "A"}]}\nreturn 1')
    meta, body = extract_meta(tree)
    assert meta["name"] == "n"
    assert len(body) == 1                     # meta removed from the body


def test_meta_must_be_a_literal() -> None:
    with pytest.raises(ScriptError, match="literal"):
        extract_meta(validate_script('n = "x"\nmeta = {"name": n}\nreturn 1'))


def test_declared_phases_appear_in_order_before_any_agent_runs() -> None:
    wf, _ = _run('meta = {"name": "w", "phases": [{"title": "A"}, {"title": "B"}]}\n'
                 "return 1")
    assert [p.title for p in wf.run.phases] == ["A", "B"]
    assert wf.run.name == "w"


# -- the hooks map onto the engine -------------------------------------------


def test_phase_routes_agents_and_persona_is_honoured() -> None:
    record: list = []
    wf, out = _run(
        'phase("Review")\n'
        'a = await agent("look", label="looker")\n'
        'phase("Verify")\n'
        'b = await agent("check", agent_type="verify")\n'
        "return [a, b]",
        record=record,
    )
    assert [p.title for p in wf.run.phases] == ["Review", "Verify"]
    by_phase = {a.phase: a for a in wf.run.all_agents()}
    assert by_phase["Review"].label == "looker"
    assert by_phase["Verify"].agent_type == "verify"
    assert len(out) == 2


def test_parallel_and_pipeline_are_the_engines_own() -> None:
    wf, out = _run(
        'rs = await parallel([(lambda i=i: agent(f"find {i}", label=f"f{i}"))\n'
        "                     for i in range(3)])\n"
        'ch = await pipeline(rs, lambda r: agent(f"verify {r}"))\n'
        "return {'found': len(rs), 'checked': len(ch)}"
    )
    assert out == {"found": 3, "checked": 3}
    assert len(wf.run.all_agents()) == 6


def test_the_concurrency_cap_still_applies_to_a_script() -> None:
    """A script cannot talk its way past the cap by fanning out harder."""
    state = {"live": 0, "peak": 0}

    async def runner(prompt, *, model="", agent_type="", schema=None):
        async def gen():
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
            await anyio.sleep(0.01)
            state["live"] -= 1
            yield AssistantMessage(content=[TextBlock(text="ok")])
        return gen()

    _run('return await parallel([(lambda i=i: agent(f"a{i}")) for i in range(12)])',
         concurrency=2, runner=runner)
    assert state["peak"] == 2


def test_args_reach_the_script() -> None:
    _, out = _run("return args['target']", args={"target": "auth.py"})
    assert out == "auth.py"


def test_log_lands_on_the_run() -> None:
    wf, _ = _run('log("halfway")\nreturn 1')
    assert any("halfway" in str(line) for line in wf.run.log_lines)


def test_print_is_routed_to_log_not_stdout(capsys) -> None:
    """A script that prints means to say something to the user watching the
    run — sending it to stdout would scribble over the TUI frame."""
    wf, _ = _run('print("progress")\nreturn 1')
    assert capsys.readouterr().out == ""
    assert any("progress" in str(line) for line in wf.run.log_lines)


# -- the shapes a declarative definition cannot express ----------------------


def test_loop_until_dry() -> None:
    """Unknown-size discovery: keep hunting until two consecutive rounds turn up
    nothing new. This is the shape that motivated scripts — a phase list cannot
    express 'repeat until'."""
    calls = {"n": 0}

    async def runner(prompt, *, model="", agent_type="", schema=None):
        async def gen():
            calls["n"] += 1
            text = f"FOUND {calls['n']}" if calls["n"] <= 4 else "nothing new"
            yield AssistantMessage(content=[TextBlock(text=text)])
        return gen()

    wf, out = _run(
        "seen = []\n"
        "dry = 0\n"
        "rounds = 0\n"
        "while dry < 2 and rounds < 10:\n"
        "    rounds += 1\n"
        '    phase(f"round {rounds}")\n'
        '    got = await parallel([(lambda i=i: agent(f"hunt {i}")) for i in range(2)])\n'
        '    fresh = [g for g in got if "FOUND" in g and g not in seen]\n'
        "    if fresh:\n"
        "        dry = 0\n"
        "        seen = seen + fresh\n"
        "    else:\n"
        "        dry += 1\n"
        "return {'rounds': rounds, 'found': len(seen)}",
        runner=runner,
    )
    assert out["found"] == 4
    assert out["rounds"] < 10                       # it converged, not capped out
    assert [p.title for p in wf.run.phases][0] == "round 1"


def test_budget_view_is_readable_from_a_script() -> None:
    """`while budget.remaining() > x` is the other shape a definition can't do."""
    _, out = _run("return [budget.total, budget.spent(), budget.remaining()]")
    total, spent, remaining = out
    assert total is None                    # no budget configured
    assert spent == 0.0
    assert remaining == float("inf")        # unbounded, so a loop guard must check total


# -- through the workflow tool ----------------------------------------------


def _tool(**kw):
    from mantis_agent.workflow_tool import make_workflow_tool

    return make_workflow_tool(model="m", agent_runner=_stub(), definitions=[], **kw)


def test_the_tool_runs_a_script_and_reports_what_ran() -> None:
    out = anyio.run(lambda: _tool().fn(
        script='meta = {"name": "audit"}\n'
               'phase("Find")\n'
               'r = await parallel([(lambda m=m: agent(f"audit {m}", label=m))\n'
               '                    for m in args["modules"]])\n'
               "return {'n': len(r)}",
        args={"modules": ["auth", "billing"]},
        run_in_background=False,
    ))
    assert "# Workflow — audit" in out
    assert "## Find" in out
    assert "- auth" in out and "- billing" in out
    assert "2/2 agents completed" in out
    assert '"n": 2' in out


def test_a_rejected_script_costs_nothing() -> None:
    """Validation happens before any agent is built, so a bad script comes back
    as a fixable message rather than a half-finished run."""
    out = anyio.run(lambda: _tool().fn(script="import os\nreturn 1",
                                       run_in_background=False))
    assert "rejected" in out and "imports are not available" in out


def test_a_failing_script_reports_the_line() -> None:
    out = anyio.run(lambda: _tool().fn(script="a = 1\nreturn a / 0",
                                       run_in_background=False))
    assert "failed" in out and "line 2" in out


def test_script_and_name_are_mutually_exclusive() -> None:
    out = anyio.run(lambda: _tool().fn(script="return 1", name="x",
                                       run_in_background=False))
    assert "EITHER" in out


def test_neither_script_nor_name_explains_the_choice() -> None:
    out = anyio.run(lambda: _tool().fn(run_in_background=False))
    assert "script" in out and "name" in out


def test_a_script_returning_nothing_says_so() -> None:
    """Silently reporting an empty result would read as 'the work produced
    nothing' rather than 'you forgot to return it'."""
    out = anyio.run(lambda: _tool().fn(script='await agent("do a thing")',
                                       run_in_background=False))
    assert "returned nothing" in out and "`return`" in out


def test_the_script_parameter_teaches_the_api() -> None:
    """The description IS the interface — a model that can't see the hook
    signatures or the thunk-binding rule will write scripts that don't run."""
    schema = _tool().input_schema
    help_text = schema["properties"]["script"]["description"]
    for needed in ("await agent(", "await parallel(", "await pipeline(",
                   "phase(", "lambda d=d:", "meta", "return"):
        assert needed in help_text, needed
    # and the guidance that actually changes what it writes
    assert "DEFAULT TO pipeline" in help_text
    assert "NOT available" in help_text
