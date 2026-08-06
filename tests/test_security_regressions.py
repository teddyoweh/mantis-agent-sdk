"""Regressions for bypasses that survived a first round of fixing.

Each test here corresponds to a hole that was *proven* reachable against the
real public entry points after an earlier fix was already in place — the class
of bug where the reported syntax is closed and an equivalent one is left open.
They are grouped separately from the per-feature suites so the pattern stays
visible: a permission gate is only as good as its least-tested spelling.
"""

from __future__ import annotations

import anyio

from mantis_agent.builtin_tools import fs as builtin_fs
from mantis_agent.permissions import (
    PermissionContext,
    PermissionRule,
    PermissionRuleSet,
    check_permission,
)

# The shape the product actually generates: ``tui._load_permission_rules`` wraps
# every settings.json entry as ``*<entry>*``, so a substring glob is the realistic
# rule, not the anchored ``git status*`` a hand-written test would reach for.
_SUBSTRING_RULE = PermissionRuleSet(
    allow=[PermissionRule(pattern="*git status*", action="allow", tool_name="bash")]
)


async def _asker_deny(tool, input, prompt):  # noqa: A002 — matches AskerFn
    return "deny"


def _decide(payload: dict, *, asker) -> str:
    """Run the REAL bash tool through the REAL permission pipeline."""

    async def go() -> str:
        ctx = PermissionContext(mode="default", rules=_SUBSTRING_RULE, asker=asker)
        decision = await check_permission(builtin_fs.bash, payload, ctx)
        return type(decision).__name__

    return anyio.run(go)


# --------------------------------------------------------------------------
# A reserved word in front of a group used to hide the whole group
# --------------------------------------------------------------------------
#
# ``_analyze`` only recursed into a grouping construct when the segment *started
# with* ``(`` or ``{``. A leading ``!``, ``coproc`` or ``time`` therefore collapsed
# the entire line into one opaque, "confident" segment, which the whole-string
# glob then allowed — while the bare ``{ …; }`` form was correctly gated.

_PREFIXED_GROUPS = [
    "! { git status; cat ~/.ssh/id_rsa; }",
    "! ( git status; npm publish )",
    "coproc { git status; npm publish; }",
    "time { git status; npm publish; }",
    "!  {  git status ;  npm publish ; }",
]


def test_reserved_word_prefix_cannot_hide_a_group_interactive() -> None:
    for command in _PREFIXED_GROUPS:
        assert _decide({"command": command}, asker=_asker_deny) != "Allow", command


def test_reserved_word_prefix_cannot_hide_a_group_headless() -> None:
    # No asker at all — the library/headless default. Must fail CLOSED, because
    # a compound command that could not be fully authorized is an explicit ask.
    for command in _PREFIXED_GROUPS:
        assert _decide({"command": command}, asker=None) != "Allow", command


def test_bare_group_still_gated_and_plain_command_still_allowed() -> None:
    assert _decide({"command": "{ git status; npm publish; }"}, asker=_asker_deny) != "Allow"
    # ...and the fast path for a single command is untouched.
    assert _decide({"command": "git status"}, asker=_asker_deny) == "Allow"
    assert _decide({"command": "git status"}, asker=None) == "Allow"


# --------------------------------------------------------------------------
# A sibling field must never authorize the command
# --------------------------------------------------------------------------
#
# The per-segment allow check matched against the whole-input JSON projection in
# addition to the segment text. That projection still carries every sibling field,
# and the real ``bash`` tool declares a free-text, model-chosen ``stdin`` parameter
# — so putting the allowed phrase in ``stdin`` satisfied a substring rule for every
# segment and authorized the whole chain.


def test_stdin_cannot_launder_authorization_for_the_command() -> None:
    assert "stdin" in builtin_fs.bash.input_schema.get("properties", {}), (
        "this regression depends on bash declaring a model-controlled stdin field"
    )
    payloads = [
        {"command": "git status && npm publish", "stdin": "git status"},
        {"command": "{ git status; cat ~/.ssh/id_rsa; }", "stdin": "git status"},
        {
            "command": "git status && npm publish",
            "stdin": "please run git status",
            "run_in_background": True,
        },
    ]
    for payload in payloads:
        assert _decide(payload, asker=_asker_deny) != "Allow", payload
        assert _decide(payload, asker=None) != "Allow", payload


def test_sibling_field_does_not_break_a_legitimate_single_command() -> None:
    # Carrying an unrelated sibling field must not *cost* an otherwise-fine call.
    assert _decide({"command": "git status", "stdin": "anything"}, asker=_asker_deny) == "Allow"


# --------------------------------------------------------------------------
# A rule authorizes a command, never an arbitrary file write
# --------------------------------------------------------------------------
#
# The decomposer captured `ShellSegment.redirects` from the start, but nothing
# ever read them: `git status` is read-only, while
# `git status > ~/.ssh/authorized_keys` writes a file of the model's choosing,
# and an allow rule for the former authorized the latter.


def test_output_redirect_is_not_authorized_by_a_command_rule() -> None:
    for command in (
        "git status > out.txt",
        "git status >> ~/.bashrc",
        "git status 2> err.log",
        "git status &> all.log",
        "git status &>> all.log",
        "git status >& all.log",          # bash synonym for &>file, writes a file
        "git status > ~/.ssh/authorized_keys",
    ):
        assert _decide({"command": command}, asker=_asker_deny) != "Allow", command


def test_descriptor_manipulation_and_input_redirects_are_untouched() -> None:
    # These create no file: `2>&1` and `>&2` duplicate a descriptor, `>&-` closes
    # one, and `<` reads something the command could have read anyway. Gating them
    # would be noise with no security benefit.
    for command in (
        "git status",
        "git status < in.txt",
        "git status 0< in.txt",
        "git status 2>&1",
        "git status >&2",
        "git status >&-",
    ):
        assert _decide({"command": command}, asker=_asker_deny) == "Allow", command
