"""The docs must describe the code that exists.

Both doc trees shipped snippets that ran, did nothing, and said nothing about
it: `api_key` and `base_url` as options no code path read, `permissions`
sub-keys nothing consumed, hook events silently dropped, `ModelCapability`
fields that never existed. Nothing in the suite could tell a working example
from a decorative one, so the drift accumulated release over release.

``scripts/check_doc_snippets.py`` asks the live package about every claim a
snippet makes — see its module docstring for the rules. This wires it into the
suite so a wrong example fails here instead of shipping.

Failure means one of two things, and both are fixable in this repo:

* the doc is wrong — correct the snippet; or
* the code changed and the doc followed it late — correct the snippet, or the
  code if the old behavior was the intended one.

Run it directly for the full list, or scoped to what you're editing::

    python scripts/check_doc_snippets.py
    python scripts/check_doc_snippets.py docs/guides/models-and-backends.md
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

check_doc_snippets = pytest.importorskip(
    "check_doc_snippets",
    reason="scripts/check_doc_snippets.py is missing",
)


def _format(findings: list) -> str:
    by_rule: dict[str, int] = {}
    for f in findings:
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_rule.items()))
    lines = "\n".join(f"  {f}" for f in findings)
    return (
        f"{len(findings)} doc snippet(s) disagree with the code ({summary}):\n"
        f"{lines}\n\n"
        f"Reproduce with: python scripts/check_doc_snippets.py"
    )


@pytest.mark.parametrize("root", check_doc_snippets.DOC_ROOTS)
def test_doc_snippets_match_the_code(root: str) -> None:
    """Every python/bash/json snippet in a doc tree must match the real API.

    Parametrized per tree so a failure names which site is wrong — the mkdocs
    tree that ships with the repo, or the one mantisagent.cc serves.
    """

    if not (REPO / root).exists():
        pytest.skip(f"{root} not present in this checkout")
    findings = check_doc_snippets.run([root])
    assert not findings, _format(findings)


def test_checker_catches_a_planted_error() -> None:
    """The checker itself has to be able to fail.

    A drift check that silently stops working leaves the docs unguarded while
    still reporting green — worse than no check. Plant one instance of each
    rule's failure and require it to be caught.
    """

    block = check_doc_snippets.Block(
        "python",
        "\n".join(
            [
                "from mantis_agent import Agent, lookup_model",
                "from mantis_agent import NotARealExport",
                "cap = lookup_model('qwen2.5:7b')",
                "print(cap.tool_use_path)",
                "agent = Agent(model='m', system_prompt='x')",
                "options = {'model': 'm', 'definitely_not_an_option': 1}",
            ]
        ),
        1,
    )
    rules = {f.rule for f in check_doc_snippets.check_python(block, "planted.md")}
    assert {"import", "attr", "kwarg", "option"} <= rules, rules


def test_checker_probes_are_behavioral() -> None:
    """The probes must ask the code, not a hardcoded list.

    These four are the load-bearing ones; if any starts answering statically,
    the check quietly rots into a list that needs manual upkeep — the exact
    failure mode the docs already went through.
    """

    # Real option, honored → True. Invented key → False.
    assert check_doc_snippets._wire_key_is_honored("api_key")
    assert not check_doc_snippets._wire_key_is_honored("definitely_not_an_option")

    # Real settings key → True. Option-only name in a settings file → False.
    assert check_doc_snippets._settings_key_is_honored("max_budget_usd")
    assert not check_doc_snippets._settings_key_is_honored("max_usd")

    # Nested: permissions.allow is read, permissions.default_mode never was.
    assert check_doc_snippets._nested_settings_key_is_honored("permissions", "allow")
    assert not check_doc_snippets._nested_settings_key_is_honored(
        "permissions", "default_mode"
    )

    # Hook events the dict form maps, versus one it drops on the floor.
    assert check_doc_snippets._hook_event_is_honored("PreToolUse")
    assert not check_doc_snippets._hook_event_is_honored("PreModelCall")


# ---------------------------------------------------------------------------
# Coverage — the other half: what exists and is written down nowhere
# ---------------------------------------------------------------------------

check_doc_coverage = pytest.importorskip(
    "check_doc_coverage",
    reason="scripts/check_doc_coverage.py is missing",
)


def test_every_public_surface_is_documented() -> None:
    """A name you can only discover by reading the source is a doc bug.

    Covers `__all__`, `MantisAgentOptions` fields, the environment variables the
    package really reads, behavior-changing settings keys, and mapped hook
    events. "Documented" means mentioned anywhere in either tree — a floor, not
    a quality bar. Deliberate omissions live in
    ``check_doc_coverage.ALLOWLIST`` with a stated reason, so the decision is on
    the record instead of in someone's head.
    """

    gaps = check_doc_coverage.run()
    assert not gaps, (
        f"{len(gaps)} public name(s) appear in no doc:\n"
        + "\n".join(f"  {g}" for g in gaps)
        + "\n\nDocument them, or add to ALLOWLIST in "
        "scripts/check_doc_coverage.py with a reason."
    )


def test_env_var_extraction_ignores_fstring_fragments() -> None:
    """The env surface must be extracted by AST, not regex.

    A regex over the source matches the ``MANTIS_`` half of
    ``f"MANTIS_SUBAGENT_{name}"`` and every mention inside a docstring, which
    invents variables that don't exist. Phantom entries make the report
    untrustworthy, and an untrustworthy report gets ignored.
    """

    names = check_doc_coverage.env_vars()
    assert names, "expected the package to read at least one MANTIS_* variable"
    assert all(name == name.strip() and " " not in name for name in names)
    # Fragments a regex would have produced, that AST extraction must not.
    for fragment in ("MANTIS_SUBAGENT_", "MANTIS_TERM_", "MANTIS_HOOKS_"):
        assert fragment not in names
    # And a couple of real ones must survive.
    assert "MANTIS_AGENT_MODEL" in names
    assert "MANTIS_HOOKS_FAIL_CLOSED" in names
