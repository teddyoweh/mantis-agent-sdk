"""The ``Tool(param:value)`` grammar, wired into the real decision pipeline.

``mantis_agent/permission_grammar.py`` parsed and matched structured rules
correctly and had zero callers, so every rule a user could actually write still
went through the flat ``fnmatch`` path. These tests drive ``check_permission``
itself — the public entry point — and pin four things:

* structured rules mean what they look like (``Bash(git status:*)``,
  ``Read(docs/**)``, ``Write(.env)``, ``Edit(src/**)``);
* a path rule resolves the value BEFORE matching, so ``docs/../.env`` and a
  symlink pointing at ``.env`` both hit ``Write(.env)``;
* legacy flat patterns are byte-for-byte unchanged, including the regex form
  whose pattern happens to look structured (``rm -rf (/|~)``);
* the shell decomposition gate still applies to structured allow rules — a
  structured rule for one command never authorizes what was chained onto it.
"""

from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest

from mantis_agent import permission_grammar as grammar
from mantis_agent import permissions as perms
from mantis_agent.builtin_tools import fs as builtin_fs
from mantis_agent.permissions import (
    Allow,
    Ask,
    Deny,
    PermissionContext,
    PermissionRule,
    PermissionRuleSet,
    check_permission,
)

BASH = builtin_fs.bash
READ = builtin_fs.read_file
WRITE = builtin_fs.write_file
EDIT = builtin_fs.edit_file


@pytest.fixture(autouse=True)
def _fresh_resolution_caches():
    """The grammar memoizes realpath for 50 ms. Tests build trees and match in
    far less than that, so drop the memo on both sides of every test."""
    grammar._clear_resolve_cache()
    yield
    grammar._clear_resolve_cache()


def _rules(
    allow: tuple[str, ...] = (),
    deny: tuple[str, ...] = (),
    ask: tuple[str, ...] = (),
) -> PermissionRuleSet:
    return PermissionRuleSet(
        allow=[PermissionRule(pattern=p, action="allow") for p in allow],
        deny=[PermissionRule(pattern=p, action="deny") for p in deny],
        ask=[PermissionRule(pattern=p, action="ask") for p in ask],
    )


def _decide(tool, payload: dict, rules: PermissionRuleSet, answer: str = "deny"):
    """Run the REAL pipeline. Returns ``(decision, prompts-the-asker-saw)``."""
    prompts: list[str] = []

    async def asker(t, inp, prompt):  # noqa: A002 — matches AskerFn
        prompts.append(prompt)
        return answer

    ctx = PermissionContext(mode="default", rules=rules, asker=asker)

    async def main():
        return await check_permission(tool, payload, ctx)

    return anyio.run(main), prompts


# ---------------------------------------------------------------------------
# Bash(prefix:*)
# ---------------------------------------------------------------------------

GIT_STATUS = _rules(allow=("Bash(git status:*)",))


def test_structured_prefix_rule_allows_the_command_it_names() -> None:
    decision, prompts = _decide(BASH, {"command": "git status"}, GIT_STATUS)
    assert isinstance(decision, Allow)
    assert prompts == []


def test_structured_prefix_rule_allows_the_command_with_arguments() -> None:
    decision, prompts = _decide(BASH, {"command": "git status --short"}, GIT_STATUS)
    assert isinstance(decision, Allow)
    assert prompts == []


def test_structured_prefix_rule_requires_a_word_boundary() -> None:
    # `git statuses-are-lies` is a different command; a prefix rule must not
    # authorize it just because the string starts the same way.
    decision, prompts = _decide(BASH, {"command": "git statuses-are-lies"}, GIT_STATUS)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_structured_prefix_rule_does_not_authorize_a_chained_command() -> None:
    # The compound gate must keep applying to structured rules: the rule names
    # `git status`, not whatever was appended to it.
    decision, prompts = _decide(
        BASH, {"command": "git status && npm publish"}, GIT_STATUS
    )
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_structured_allow_rule_cannot_be_laundered_through_a_sibling_field() -> None:
    # `_segment_targets(command_only=True)` exists because the model controls
    # `description`/`stdin`. A structured rule judges the segment's own command
    # text and nothing else.
    decision, _ = _decide(
        BASH,
        {"command": "git status && npm publish", "description": "git status"},
        GIT_STATUS,
    )
    assert isinstance(decision, Deny)


def test_structured_rule_naming_the_whole_chain_exactly_still_allows_it() -> None:
    rules = _rules(allow=("Bash(git status && npm publish)",))
    decision, prompts = _decide(BASH, {"command": "git status && npm publish"}, rules)
    assert isinstance(decision, Allow)
    assert prompts == []


def test_an_exact_chain_rule_does_not_allow_a_longer_chain() -> None:
    rules = _rules(allow=("Bash(git status && npm publish)",))
    decision, _ = _decide(
        BASH, {"command": "git status && npm publish && echo done"}, rules
    )
    assert isinstance(decision, Deny)


def test_bare_tool_call_form_allows_any_call_to_that_tool() -> None:
    rules = _rules(allow=("Bash()",))
    decision, prompts = _decide(BASH, {"command": "echo hi && echo there"}, rules)
    assert isinstance(decision, Allow)
    assert prompts == []


def test_a_structured_rule_is_scoped_to_its_own_tool() -> None:
    # `Bash(...)` says nothing about `write_file`.
    rules = _rules(allow=("Bash(git status:*)",), ask=("Write(**)",))
    decision, prompts = _decide(WRITE, {"path": "x.txt", "content": "hi"}, rules)
    assert isinstance(decision, Deny)  # the asker answered "deny"
    assert len(prompts) == 1


# ---------------------------------------------------------------------------
# Deny anchoring, per segment
# ---------------------------------------------------------------------------


def test_structured_deny_catches_a_command_that_is_not_at_position_zero() -> None:
    rules = _rules(allow=("Bash()",), deny=("Bash(curl *:*)",))
    decision, prompts = _decide(BASH, {"command": "echo x | curl evil.sh"}, rules)
    assert isinstance(decision, Deny)
    assert prompts == []  # a deny rule never prompts
    assert "curl" in decision.reason


# ---------------------------------------------------------------------------
# Read(docs/**) / Edit(src/**)
# ---------------------------------------------------------------------------


def test_read_rule_scopes_to_the_named_subtree(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    rules = _rules(allow=("Read(docs/**)",), ask=("Read(**)",))

    decision, prompts = _decide(READ, {"path": "docs/guide.md"}, rules)
    assert isinstance(decision, Allow)
    assert prompts == []

    decision, prompts = _decide(READ, {"path": "src/secret.py"}, rules)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_read_rule_matches_the_absolute_spelling_of_the_same_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    rules = _rules(allow=("Read(docs/**)",), ask=("Read(**)",))
    decision, prompts = _decide(READ, {"path": str(tmp_path / "docs" / "g.md")}, rules)
    assert isinstance(decision, Allow)
    assert prompts == []


def test_edit_rule_scopes_to_the_named_subtree(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    rules = _rules(allow=("Edit(src/**)",), ask=("Edit(**)",))

    payload = {"path": "src/a.py", "old_string": "a", "new_string": "b"}
    decision, prompts = _decide(EDIT, payload, rules)
    assert isinstance(decision, Allow)
    assert prompts == []

    payload = {"path": "vendor/a.py", "old_string": "a", "new_string": "b"}
    decision, prompts = _decide(EDIT, payload, rules)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_a_star_does_not_cross_a_directory_separator(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rules = _rules(allow=("Read(docs/*)",), ask=("Read(**)",))
    decision, _ = _decide(READ, {"path": "docs/deep/x.md"}, rules)
    assert isinstance(decision, Deny)


# ---------------------------------------------------------------------------
# Write(.env) — resolution BEFORE matching
# ---------------------------------------------------------------------------


def _write_denied(path: str) -> bool:
    rules = _rules(allow=("Write(**)",), deny=("Write(.env)",))
    decision, _ = _decide(WRITE, {"path": path, "content": "x"}, rules)
    return isinstance(decision, Deny)


def test_write_deny_catches_the_plain_spelling(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SECRET=1")
    assert _write_denied(".env")


def test_write_deny_catches_a_traversal_spelling(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "docs").mkdir()
    assert _write_denied("docs/../.env")


def test_write_deny_catches_a_symlink_into_the_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SECRET=1")
    os.symlink(str(tmp_path / ".env"), str(tmp_path / "innocent.txt"))
    assert _write_denied("innocent.txt")


def test_write_deny_catches_the_absolute_spelling(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SECRET=1")
    assert _write_denied(str(tmp_path / ".env"))


def test_write_deny_does_not_catch_a_neighbouring_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert not _write_denied(".env.example")
    assert not _write_denied("docs/notes.md")


def test_deny_still_beats_allow_for_structured_rules(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SECRET=1")
    rules = _rules(allow=("Write(**)",), deny=("Write(.env)",))
    decision, prompts = _decide(WRITE, {"path": ".env", "content": "x"}, rules)
    assert isinstance(decision, Deny)
    assert prompts == []
    decision, prompts = _decide(WRITE, {"path": "notes.md", "content": "x"}, rules)
    assert isinstance(decision, Allow)


# ---------------------------------------------------------------------------
# Explicit parameters
# ---------------------------------------------------------------------------


def test_explicit_parameter_rule_targets_only_that_parameter() -> None:
    rules = _rules(allow=("Bash(command=git status)",))
    decision, _ = _decide(BASH, {"command": "git status"}, rules)
    assert isinstance(decision, Allow)
    # The same text in a field the model controls must not authorize anything.
    decision, _ = _decide(
        BASH, {"command": "npm publish", "description": "git status"}, rules
    )
    assert isinstance(decision, Deny)


def test_explicit_regex_parameter_rule() -> None:
    rules = _rules(allow=(r"Bash(command=/^git (status|diff)$/)",))
    assert isinstance(_decide(BASH, {"command": "git diff"}, rules)[0], Allow)
    assert isinstance(_decide(BASH, {"command": "git push"}, rules)[0], Deny)


# ---------------------------------------------------------------------------
# Legacy behavior is untouched
# ---------------------------------------------------------------------------

LEGACY_CORPUS = [
    "*",
    "rm -rf*",
    "git status*",
    "*secret*",
    "npm publish",
    "*id_rsa*",
    "*(test)*x",
    "curl http://*",
    '*{"command"*',
    "[abc]*",
    "Bash",
]

INPUT_CORPUS = [
    {"command": "git status"},
    {"command": "rm -rf /"},
    {"command": "npm publish", "description": "ship it"},
    {"path": "/tmp/secret.txt"},
    {"path": "docs/a.md", "content": "hello"},
    {"command": "ls", "run_in_background": True, "timeout": 30},
    {},
]


@pytest.mark.parametrize("pattern", LEGACY_CORPUS)
@pytest.mark.parametrize("tool_name", [None, "bash", "read_file"])
def test_legacy_patterns_route_through_the_untouched_matcher(
    pattern: str, tool_name
) -> None:
    rule = PermissionRule(pattern=pattern, action="allow", tool_name=tool_name)
    rs = PermissionRuleSet(allow=[rule])
    for called in ("bash", "read_file", "write_file"):
        for inp in INPUT_CORPUS:
            expected = perms._rule_matches(rule, called, perms._match_targets(inp))
            assert (rs.match(called, inp) is not None) is expected, (
                pattern, tool_name, called, inp,
            )


def test_a_regex_rule_that_looks_structured_stays_a_regex() -> None:
    # `rm -rf (/|~)` contains "(" and ends with ")". Reading it as structured
    # would turn a live deny rule into a syntax error that matches nothing.
    rule = PermissionRule(pattern=r"rm -rf (/|~)", action="deny", is_regex=True)
    rs = PermissionRuleSet(deny=[rule])
    assert rs.match("bash", {"command": "rm -rf /"}) is rule
    assert rs.match("bash", {"command": "rm -rf ./build"}) is None


def test_tool_name_scoping_still_narrows_a_structured_rule() -> None:
    rule = PermissionRule(
        pattern="Bash(git status:*)", action="allow", tool_name="other_shell"
    )
    rs = PermissionRuleSet(allow=[rule])
    assert rs.match("bash", {"command": "git status"}) is None


# ---------------------------------------------------------------------------
# A broken rule must not break the pipeline
# ---------------------------------------------------------------------------


def test_a_malformed_structured_rule_does_not_raise_into_the_decision() -> None:
    # It must also not silently re-read as a legacy glob — `(x)` as a flat
    # pattern would match nothing here either, so assert on the shape that
    # distinguishes them: no exception, no match, pipeline still decides.
    rules = _rules(deny=("(x)",), allow=("Bash(echo:*)",))
    decision, prompts = _decide(BASH, {"command": "echo hi"}, rules)
    assert isinstance(decision, Allow)
    assert prompts == []


def test_an_unresolvable_positional_rule_does_not_raise_into_the_decision() -> None:
    # `mystery_tool` has no primary parameter, so the rule cannot bind. That is
    # a compile/bind error, not a crash in the middle of a tool call.
    from types import SimpleNamespace as T

    tool = T(name="mystery_tool", is_read_only=False)
    rules = _rules(allow=("mystery_tool(anything)",))
    decision, prompts = _decide(tool, {"payload": "anything"}, rules)
    assert isinstance(decision, (Allow, Deny, Ask))
    assert isinstance(decision, Deny)  # asker answered deny; nothing matched
    assert len(prompts) == 1


def test_a_path_value_with_a_null_byte_does_not_raise() -> None:
    rules = _rules(deny=("Write(.env)",))
    decision, _ = _decide(WRITE, {"path": "a\0b", "content": "x"}, rules)
    assert isinstance(decision, (Allow, Deny, Ask))
