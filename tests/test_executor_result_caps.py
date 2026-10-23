"""An MCP tool's result is a whole record the model can't re-ask for "the
middle of". It gets Claude Code's MCP limit (25,000 tokens, the same
``MAX_MCP_OUTPUT_TOKENS`` variable), still bounded by the window; it used to
get the generic 30k-char cap, which elided a 43k-char CRM record's middle and
the do-not-contact line in it."""

from __future__ import annotations

from mantis_agent.streaming.executor import _result_cap, result_char_budget_for


def test_mcp_results_get_the_mcp_limit_within_the_window() -> None:
    big_window = result_char_budget_for(126_887)              # a 128k model
    assert _result_cap("mcp__crm__get_customer", big_window) == big_window   # the window binds, ~76k
    assert _result_cap("mcp__crm__get_customer", big_window) > 43_000
    assert _result_cap("grep", big_window) == 30_000            # other tools unchanged
    assert _result_cap("mcp__crm__get_customer", result_char_budget_for(6_000)) == 4_000   # a small model stays small


def test_max_mcp_output_tokens_sets_it() -> None:
    import os
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c", "from mantis_agent.streaming.executor import _result_cap; print(_result_cap('mcp__x__y', None))"],
        env={**os.environ, "MAX_MCP_OUTPUT_TOKENS": "5000"}, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "20000"
