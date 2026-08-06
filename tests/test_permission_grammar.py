"""The structured ``Tool(param:value)`` rule grammar.

Four contracts, in order of how much damage getting them wrong does:

1. **Legacy rules keep behaving EXACTLY as they do today.** Proven by running a
   corpus through both ``permission_grammar`` and the real, untouched
   ``permissions._rule_matches`` and asserting they agree call for call.
2. **Paths are resolved before matching.** ``docs/../.env`` and a symlink
   pointing at ``.env`` both hit ``Write(.env)``; a value that escapes the
   project root does not ride in on a ``**``.
3. **A positional rule never degrades to matching everything.** When the primary
   parameter cannot be resolved, the rule raises.
4. **Specificity is deterministic** so "which rule fired" always has an answer.
"""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace as T

import pytest

from mantis_agent import permission_grammar as grammar
from mantis_agent import permissions as perms
from mantis_agent.permission_grammar import (
    AmbiguousPrimaryParamError,
    CompiledRule,
    RuleSyntaxError,
    UnknownToolInRuleError,
    best_match,
    bind_rule,
    canonical_tool_name,
    compile_rule,
    fs_case_sensitive,
    is_structured_rule,
    path_matches,
    primary_param_for,
    resolve_path,
    rule_matches,
    specificity,
)

BASH = T(name="bash", is_read_only=False)
WRITE = T(name="write_file", is_read_only=False)
READ = T(name="read_file", is_read_only=True)


def fires(rule_text: str, tool_name: str, input: dict, **kw) -> bool:
    """Did ``rule_text`` fire on this call?"""
    return rule_matches(compile_rule(rule_text), tool_name, input, **kw) is not None


# ---------------------------------------------------------------------------
# Syntax: every §5 form
# ---------------------------------------------------------------------------


def test_bare_tool_form_matches_any_call_to_that_tool() -> None:
    r = compile_rule("Bash()")
    assert r.matcher == "tool" and r.tool == "bash"
    assert fires("Bash()", "bash", {"command": "anything at all"})
    assert not fires("Bash()", "read_file", {"path": "x"})


def test_bare_word_is_legacy_unless_opted_in() -> None:
    # No parens ⇒ legacy, always. Backward compatibility outranks convenience.
    assert compile_rule("Bash").matcher == "legacy"
    assert compile_rule("Bash", bare_tool=True).matcher == "tool"
    assert rule_matches(
        compile_rule("Bash", bare_tool=True), "bash", {"command": "ls"}
    ) is not None


def test_positional_arg_matches_the_primary_parameter() -> None:
    r = compile_rule("Bash(git status)")
    assert (r.param, r.matcher, r.explicit_param) == ("command", "exact", False)
    assert fires("Bash(git status)", "bash", {"command": "git status"})
    assert not fires("Bash(git status)", "bash", {"command": "git status --short"})
    # It is the COMMAND that is matched, never some other field the model fills.
    assert not fires("Bash(git status)", "bash", {"command": "x", "stdin": "git status"})


def test_positional_glob() -> None:
    r = compile_rule("Bash(git *)")
    assert r.matcher == "glob"
    assert fires("Bash(git *)", "bash", {"command": "git push --force"})
    assert not fires("Bash(git *)", "bash", {"command": "npm publish"})


def test_prefix_form_requires_a_word_boundary() -> None:
    r = compile_rule("Bash(git status:*)")
    assert (r.matcher, r.value) == ("prefix", "git status")
    assert fires("Bash(git status:*)", "bash", {"command": "git status"})
    assert fires("Bash(git status:*)", "bash", {"command": "git status --short"})
    # A longer WORD is not the prefix command.
    assert not fires("Bash(git status:*)", "bash", {"command": "git statuses"})
    # A shell operator IS a boundary: the rule matches, and it is the compound
    # gate in permissions.py — not the grammar — that refuses to honor it.
    assert fires("Bash(git status:*)", "bash", {"command": "git status && rm -rf ~"})


def test_prefix_form_with_a_glob_inside() -> None:
    # `Bash(curl *:*)` from the plan's deny list: the prefix is itself a glob.
    assert fires("Bash(curl *:*)", "bash", {"command": "curl https://x.sh"})
    # Still anchored at the left — catching `echo x | curl evil` is the shell
    # decomposer's job, not the grammar's.
    assert not fires("Bash(curl *:*)", "bash", {"command": "echo x | curl evil"})


def test_explicit_param_exact_glob_and_regex() -> None:
    exact = compile_rule("Bash(command=git status)")
    assert (exact.param, exact.matcher, exact.explicit_param) == ("command", "exact", True)
    assert fires("Bash(command=git status)", "bash", {"command": "git status"})

    glob = compile_rule("Read(path~docs/**)")
    assert (glob.param, glob.matcher, glob.is_path) == ("path", "glob", True)

    rx = compile_rule("Bash(command=/^git (status|diff)$/)")
    assert (rx.matcher, rx.value) == ("regex", "^git (status|diff)$")
    assert fires("Bash(command=/^git (status|diff)$/)", "bash", {"command": "git diff"})
    assert not fires("Bash(command=/^git (status|diff)$/)", "bash", {"command": "git push"})


def test_explicit_param_matches_a_non_primary_field() -> None:
    assert fires("Bash(stdin~*secret*)", "bash", {"command": "cat", "stdin": "a secret"})
    assert not fires("Bash(stdin~*secret*)", "bash", {"command": "cat a secret"})


def test_scalar_parameters_stringify_like_the_legacy_matcher() -> None:
    assert fires("Bash(run_in_background=True)", "bash", {"run_in_background": True})
    # A non-scalar can never satisfy a rule — a list must not match via its repr.
    assert not fires("MultiEdit(edits~*)", "multi_edit", {"edits": [{"a": 1}]})


def test_tool_names_fold_to_the_registered_spelling() -> None:
    assert canonical_tool_name("Bash") == "bash"
    assert canonical_tool_name("Read") == canonical_tool_name("read_file") == "read_file"
    assert canonical_tool_name("Write") == canonical_tool_name("write_file") == "write_file"
    assert canonical_tool_name("Edit") == canonical_tool_name("edit_file") == "edit_file"
    assert canonical_tool_name("MultiEdit") == "multi_edit"
    assert canonical_tool_name("WebFetch") == "web_fetch"
    # An unknown tool folds to a stable normalized form — equal on both sides.
    assert canonical_tool_name("My_Tool") == canonical_tool_name("mytool") == "mytool"
    assert fires("Read(a.py)", "read_file", {"path": "a.py"})
    assert fires("read_file(a.py)", "Read", {"path": "a.py"})


def test_compilation_is_cached() -> None:
    assert compile_rule("Bash(git status:*)") is compile_rule("Bash(git status:*)")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "(git status)",          # no tool name
        "1Bash(x)",              # not an identifier
        "Bash(:*)",              # empty prefix
        "Bash(command=)",        # empty value
        "Bash(command=/[unclosed/)",  # regex that does not compile
    ],
)
def test_malformed_structured_rules_raise(text: str) -> None:
    with pytest.raises(RuleSyntaxError):
        compile_rule(text)


def test_unknown_tool_is_rejected_when_a_registry_is_supplied() -> None:
    known = frozenset({"bash", "read_file"})
    assert compile_rule("Bash(ls)", known_tools=known).tool == "bash"
    with pytest.raises(UnknownToolInRuleError):
        compile_rule("Nope(ls)", known_tools=known)


def test_documented_ambiguities_resolve_deterministically() -> None:
    # `IDENT=` at the head is ALWAYS the explicit-parameter form. An env
    # assignment in command position must be written out explicitly.
    assert compile_rule("Bash(FOO=bar make)").param == "FOO"
    assert compile_rule("Bash(command=FOO=bar make)").value == "FOO=bar make"
    # `param=/.../` is the regex form, so an absolute directory needs `~`.
    assert compile_rule("Write(path=/etc/)").matcher == "regex"
    assert compile_rule("Write(path~/etc/)").matcher == "glob"
    # A space before the operator keeps the body positional.
    assert compile_rule("Bash(rm -rf ~)").explicit_param is False


# ---------------------------------------------------------------------------
# Legacy detection + equivalence with the untouched matcher
# ---------------------------------------------------------------------------

LEGACY_CORPUS = [
    "*",
    "rm -rf*",
    "ls*",
    "git*",
    "*secret*",
    "*.env*",
    "npm publish",
    "*id_rsa*",
    "*(test)*x",           # parens, but does not END with ')' ⇒ legacy
    "curl http://*",
    "*{\"command\"*",
    "[abc]*",
]

INPUT_CORPUS = [
    {"command": "git status"},
    {"command": "rm -rf /"},
    {"command": "npm publish", "description": "ship it"},
    {"path": "/tmp/secret.txt"},
    {"path": "docs/a.md", "content": "hello"},
    {"url": "http://example.com"},
    {"command": "ls", "run_in_background": True, "timeout": 30},
    {},
    {"edits": [{"old_string": "a"}]},
]


@pytest.mark.parametrize("pattern", LEGACY_CORPUS)
@pytest.mark.parametrize("tool_name", [None, "bash", "read_file"])
def test_legacy_rules_match_exactly_as_they_do_today(pattern: str, tool_name) -> None:
    assert not is_structured_rule(pattern), "corpus must stay on the legacy side"
    compiled = compile_rule(pattern, tool_name=tool_name)
    assert compiled.matcher == "legacy" and compiled.structured is False
    legacy = perms.PermissionRule(pattern=pattern, action="allow", tool_name=tool_name)
    for inp in INPUT_CORPUS:
        for called in ("bash", "read_file", "write_file"):
            mine = rule_matches(compiled, called, inp) is not None
            theirs = perms._rule_matches(legacy, called, perms._match_targets(inp))
            assert mine is theirs, (pattern, tool_name, called, inp)


@pytest.mark.parametrize("pattern", ["secret", "^git ", "rm -rf", "(?i)NPM"])
def test_legacy_regex_rules_match_exactly_as_they_do_today(pattern: str) -> None:
    compiled = compile_rule(pattern, is_regex=True)
    legacy = perms.PermissionRule(pattern=pattern, action="deny", is_regex=True)
    for inp in INPUT_CORPUS:
        mine = rule_matches(compiled, "bash", inp) is not None
        theirs = perms._rule_matches(legacy, "bash", perms._match_targets(inp))
        assert mine is theirs, (pattern, inp)


def test_match_target_projection_is_a_faithful_copy() -> None:
    # The copy exists only because `permissions` will import this module (an
    # import back the other way would be a cycle). Pin it to the original.
    from mantis_agent.permission_grammar import _match_targets as mine

    for inp in INPUT_CORPUS:
        assert mine(inp) == perms._match_targets(inp)


@pytest.mark.parametrize(
    "text,structured",
    [
        ("Bash(ls)", True),
        ("Write(.env)", True),
        ("Bash()", True),
        ("rm -rf*", False),
        ("Bash", False),
        ("*(test)*", False),   # contains '(' but does not end with ')'
        ("(x)", True),         # structured-shaped ⇒ structured (and malformed)
    ],
)
def test_structured_detection_is_unambiguous(text: str, structured: bool) -> None:
    assert is_structured_rule(text) is structured


def test_a_malformed_structured_rule_never_falls_back_to_legacy() -> None:
    # Silently re-reading a broken deny rule as a glob that matches nothing is
    # exactly how a deny list evaporates.
    with pytest.raises(RuleSyntaxError):
        compile_rule("(x)")


# ---------------------------------------------------------------------------
# Primary parameter resolution
# ---------------------------------------------------------------------------


def test_primary_param_resolution_order() -> None:
    # 1. explicit attribute on the tool wins over everything
    declared = T(name="bash", primary_param="script", input_schema={})
    assert primary_param_for(declared) == "script"
    # 2. built-in table
    assert primary_param_for("bash") == "command"
    assert primary_param_for("Read") == "path"
    assert primary_param_for("web_fetch") == "url"
    assert primary_param_for("Glob") == "pattern"
    # 3. first REQUIRED string property of the schema, in schema order
    custom = T(
        name="deploy",
        input_schema={
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean"},
                "target": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["dry_run", "target"],
        },
    )
    assert primary_param_for(custom) == "target"


def test_unresolvable_primary_param_raises_and_names_the_tool() -> None:
    with pytest.raises(AmbiguousPrimaryParamError) as ei:
        primary_param_for(T(name="mystery", input_schema={"properties": {"a": {}}}))
    assert "mystery" in str(ei.value)


def test_a_positional_rule_on_an_unknown_tool_refuses_to_match_everything() -> None:
    rule = compile_rule("Mystery(anything)")
    assert rule.needs_binding is True
    with pytest.raises(AmbiguousPrimaryParamError):
        rule_matches(rule, "mystery", {"whatever": "anything"})
    # Binding at load time against the real tool is the fix.
    tool = T(
        name="mystery",
        input_schema={
            "type": "object",
            "properties": {"payload": {"type": "string"}},
            "required": ["payload"],
        },
    )
    bound = bind_rule(rule, tool)
    assert bound.param == "payload"
    assert rule_matches(bound, "mystery", {"payload": "anything"}) is not None
    assert rule_matches(rule, "mystery", {"payload": "anything"}, tool=tool) is not None


def test_binding_is_a_noop_for_rules_that_already_know_their_target() -> None:
    for text in ("Bash(ls)", "Bash(command=ls)", "Bash()", "rm -rf*"):
        r = compile_rule(text)
        assert bind_rule(r, BASH) is r


# ---------------------------------------------------------------------------
# Path matching — gitignore semantics
# ---------------------------------------------------------------------------


@pytest.fixture()
def project(tmp_path):
    """A small tree plus an ``outside`` sibling the root must not reach into."""
    root = tmp_path / "proj"
    (root / "docs" / "sub").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "build").mkdir()
    (root / "docs" / "a.md").write_text("a")
    (root / "docs" / "sub" / "b.md").write_text("b")
    (root / "src" / "app.py").write_text("x")
    (root / ".env").write_text("SECRET=1")
    (root / ".env.local").write_text("SECRET=2")
    (root / "build" / "out.js").write_text("j")
    outside = tmp_path / "outside" / "docs"
    outside.mkdir(parents=True)
    (outside / "x.md").write_text("x")
    return root


def test_double_star_crosses_separators_and_single_star_does_not(project) -> None:
    root = str(project)
    assert path_matches("docs/**", "docs/a.md", root)
    assert path_matches("docs/**", "docs/sub/b.md", root)
    assert path_matches("docs/*", "docs/a.md", root)
    assert not path_matches("docs/*", "docs/sub/b.md", root)
    assert not path_matches("docs/**", "src/app.py", root)


def test_leading_slash_anchors_to_the_project_root(project) -> None:
    root = str(project)
    (project / "sub" / "docs").mkdir(parents=True)
    (project / "sub" / "docs" / "c.md").write_text("c")
    assert path_matches("/docs/**", "docs/a.md", root)
    assert not path_matches("/docs/**", "sub/docs/c.md", root)
    # Unanchored patterns match at any depth (inside the root).
    assert path_matches("docs/**", "sub/docs/c.md", root)


def test_a_slashless_pattern_matches_the_basename_at_any_depth(project) -> None:
    root = str(project)
    assert path_matches(".env", ".env", root)
    (project / "svc").mkdir()
    (project / "svc" / ".env").write_text("x")
    assert path_matches(".env", "svc/.env", root)
    assert path_matches("**/.env*", "svc/.env", root)
    assert path_matches("**/.env*", ".env.local", root)
    assert not path_matches(".env", "docs/a.md", root)


def test_trailing_slash_matches_a_directory_and_everything_under_it(project) -> None:
    root = str(project)
    assert path_matches("build/", "build", root)
    assert path_matches("build/", "build/out.js", root)
    assert not path_matches("build/", "src/app.py", root)
    # Without the trailing slash a bare name is just that name — a rule must not
    # silently widen to the whole subtree.
    assert not path_matches("build", "build/out.js", root)


def test_absolute_patterns_survive_a_symlinked_system_directory() -> None:
    # /etc is a symlink into /private/etc on macOS: the VALUE resolves, so the
    # RULE's literal head has to resolve too or `/etc/**` misses `/etc/hosts`.
    if not os.path.exists("/etc/hosts"):
        pytest.skip("no /etc/hosts on this platform")
    assert path_matches("/etc/**", "/etc/hosts")
    assert path_matches("/etc/**", "/etc/../etc/hosts")


# --- resolution before matching: the bypasses ------------------------------


def test_traversal_resolves_before_matching(project) -> None:
    root = str(project)
    assert path_matches(".env", "docs/../.env", root)
    assert path_matches(".env", "docs/sub/../../.env", root)
    assert path_matches("/.env", "docs/../.env", root)
    assert fires("Write(.env)", "write_file", {"path": "docs/../.env"},
                 project_root=root)


def test_a_symlink_to_a_protected_file_is_resolved(project) -> None:
    link = project / "docs" / "notes.md"
    os.symlink(project / ".env", link)
    root = str(project)
    assert path_matches(".env", "docs/notes.md", root)
    assert fires("Write(.env)", "write_file", {"path": "docs/notes.md"},
                 project_root=root)
    # ...and the innocent-looking name does NOT satisfy a docs allow rule that
    # was written for markdown, because the value is judged after resolution.
    assert not path_matches("docs/*.md", "docs/notes.md", root)


def test_a_symlinked_directory_is_resolved(project) -> None:
    os.symlink(project / "src", project / "code")
    root = str(project)
    assert path_matches("src/**", "code/app.py", root)
    # A rule written against the symlink resolves the same way, so it fires too.
    assert path_matches("code/**", "src/app.py", root)


def test_a_value_escaping_the_project_root_is_not_swallowed_by_a_star(project) -> None:
    root = str(project)
    escaped = "../outside/docs/x.md"
    assert resolve_path(escaped, root) == str(
        (project.parent / "outside" / "docs" / "x.md").resolve()
    )
    # `**/docs/**` must not be conjured out of `docs/**` for a path that is not
    # in the project at all.
    assert not path_matches("docs/**", escaped, root)
    assert not path_matches("docs/**", "/tmp/docs/x.md", root)


def test_absolute_values_are_matched_against_the_relative_rule(project) -> None:
    root = str(project)
    assert path_matches("docs/**", str(project / "docs" / "a.md"), root)
    assert path_matches(".env", str(project / ".env"), root)


def test_case_sensitivity_follows_a_filesystem_probe(tmp_path) -> None:
    d = tmp_path / "probe"
    d.mkdir()
    (d / "Aa.txt").write_text("x")
    # Ground truth for THIS filesystem, measured rather than assumed.
    truth = not os.path.exists(str(d / "aa.txt"))
    assert fs_case_sensitive(str(d)) is truth
    (d / ".env").write_text("x")
    assert path_matches(".ENV", ".env", str(d)) is (not truth)
    assert path_matches(".env", ".env", str(d))


def test_case_probe_falls_back_to_the_platform_default_on_an_empty_dir(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    expected = not (sys.platform == "darwin" or os.name == "nt")
    assert fs_case_sensitive(str(empty)) is expected


def test_resolution_memo_expires_so_a_new_symlink_is_still_seen(project) -> None:
    # The memo exists so 200 path rules cost one realpath, not 200. It must not
    # become a TOCTOU hole: write an innocent file, swap it for a symlink into
    # `.env`, and the very next window has to judge the resolved target.
    root = str(project)
    grammar._clear_resolve_cache()
    victim = project / "docs" / "swap.md"
    victim.write_text("innocent")
    assert not path_matches(".env", "docs/swap.md", root)
    victim.unlink()
    os.symlink(project / ".env", victim)
    time.sleep(grammar._RESOLVE_TTL_S * 1.5)
    assert path_matches(".env", "docs/swap.md", root)


def test_a_whole_rule_set_resolves_the_value_once(monkeypatch, project) -> None:
    # The memo's job: 200 rules asking about one path must not cost 200
    # realpath walks. Counted rather than timed, so it cannot flake.
    grammar._clear_resolve_cache()
    seen = []
    real = os.path.realpath

    def counting(p, *a, **kw):
        seen.append(str(p))
        return real(p, *a, **kw)

    monkeypatch.setattr(grammar.os.path, "realpath", counting)
    rules = _rules(*(f"Read(src/mod{i}/**)" for i in range(50)))
    best_match(rules, "read_file", {"path": "docs/a.md"}, project_root=str(project))
    assert len([p for p in seen if p.endswith("docs/a.md")]) == 1


def test_path_semantics_only_apply_to_path_shaped_parameters() -> None:
    # `glob(pattern=...)` is a query, not a location: it must stay a flat glob
    # so realpath never touches it.
    assert compile_rule("Glob(**/*.py)").is_path is False
    assert compile_rule("Read(docs/**)").is_path is True
    assert compile_rule("Bash(git status)").is_path is False


# ---------------------------------------------------------------------------
# Specificity
# ---------------------------------------------------------------------------


def _rules(*texts: str) -> list:
    return [compile_rule(t) for t in texts]


def test_specificity_orders_every_positional_form() -> None:
    positional = _rules("git*", "Bash()", "Bash(git *)", "Bash(git:*)", "Bash(git)")
    keys = [specificity(r) for r in positional]
    assert keys == sorted(keys), keys
    assert len(set(keys)) == len(keys), "every form must be distinguishable"


def test_an_explicit_parameter_outranks_every_positional_form() -> None:
    for text in ("git*", "Bash()", "Bash(git *)", "Bash(git:*)", "Bash(git)"):
        positional = specificity(compile_rule(text))
        assert specificity(compile_rule("Bash(command=/git/)")) > positional
        assert specificity(compile_rule("Bash(command=git)")) > positional
    # ...and the matcher ranking still applies among explicit rules.
    regex = specificity(compile_rule("Bash(command=/git/)"))
    assert specificity(compile_rule("Bash(command~git*)")) > regex
    assert specificity(compile_rule("Bash(command=git)")) > regex


def test_a_longer_literal_prefix_outranks_a_shorter_one() -> None:
    short, long = compile_rule("Read(src/**)"), compile_rule("Read(src/api/**)")
    assert specificity(long) > specificity(short)
    short_p, long_p = compile_rule("Bash(git:*)"), compile_rule("Bash(git push:*)")
    assert specificity(long_p) > specificity(short_p)


def test_best_match_returns_the_most_specific_rule_not_the_first() -> None:
    rules = _rules("Bash(git:*)", "Bash(git push:*)", "git*")
    hit = best_match(rules, "bash", {"command": "git push --force"})
    assert hit is not None and hit.rule.text == "Bash(git push:*)"
    assert (hit.param, hit.value) == ("command", "git push --force")


def test_best_match_prefers_an_explicit_parameter_over_a_positional_one() -> None:
    rules = _rules("Read(docs/**)", "Read(path~docs/**)")
    hit = best_match(rules, "read_file", {"path": "docs/a.md"})
    assert hit is not None and hit.rule.explicit_param is True


def test_best_match_is_none_when_nothing_fires() -> None:
    assert best_match(_rules("Bash(git:*)"), "bash", {"command": "npm publish"}) is None


def test_ties_keep_the_declared_order() -> None:
    rules = _rules("Bash(git push:*)", "Bash(git pull:*)")
    hit = best_match(rules, "bash", {"command": "git push"})
    assert hit is not None and hit.rule.text == "Bash(git push:*)"


# ---------------------------------------------------------------------------
# Worked examples from the plan
# ---------------------------------------------------------------------------


def test_the_plans_example_ruleset(project) -> None:
    root = str(project)
    deny = _rules("Write(.env)", "Write(**/.env*)", "Read(**/id_rsa)", "Bash(sudo:*)")
    allow = _rules("Read(docs/**)", "Bash(git status:*)", "Bash(pytest:*)")

    def hits(rules, tool, inp):
        return best_match(rules, tool, inp, project_root=root) is not None

    assert hits(deny, "write_file", {"path": ".env"})
    assert hits(deny, "write_file", {"path": "svc/.env.production"})
    assert hits(deny, "read_file", {"path": "home/.ssh/id_rsa"})
    assert hits(deny, "bash", {"command": "sudo rm -rf /"})
    assert not hits(deny, "write_file", {"path": "docs/a.md"})

    assert hits(allow, "read_file", {"path": "docs/sub/b.md"})
    assert hits(allow, "bash", {"command": "pytest -q tests/"})
    assert not hits(allow, "read_file", {"path": ".env"})
    assert not hits(allow, "bash", {"command": "npm publish"})


def test_compiled_rule_is_hashable_and_frozen() -> None:
    r = compile_rule("Bash(git status:*)")
    assert isinstance(r, CompiledRule)
    assert {r, compile_rule("Bash(git status:*)")} == {r}
    with pytest.raises(Exception):
        r.value = "x"  # type: ignore[misc]
