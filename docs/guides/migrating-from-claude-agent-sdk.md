# Migrating from claude-agent-sdk

mantis speaks the `claude-agent-sdk` API: `query()`, `ClaudeSDKClient`,
`@tool` and `create_sdk_mcp_server`, hooks, permissions, the message classes,
and Claude's built-in tool names. Moving a project over changes two names —
and it doesn't have to happen all at once. You can switch one module at a
time, keep Claude where it earns its price, and switch back with one
environment variable.

## The diff

```python
# Before
from claude_agent_sdk import query, ClaudeAgentOptions, tool

# After
from mantis_agent import query, MantisAgentOptions, tool
```

The options class is `MantisAgentOptions`; it takes the same fields, plus
`model`, `backend` and `api_key` to say which model runs and where.

## Move a module at a time — and back

Put the choice in one file and import from it everywhere else. Flipping
`AGENT_SDK` moves every agent; leaving it unset keeps everything on
`claude-agent-sdk`, which is also how you remove mantis again.

```python
# agent_sdk.py — the rest of the project imports from here
import os

if os.environ.get("AGENT_SDK") == "mantis":
    from mantis_agent import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock, query, tool
    from mantis_agent import MantisAgentOptions as AgentOptions
else:
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock, query, tool
    from claude_agent_sdk import ClaudeAgentOptions as AgentOptions
```

A sensible order:

1. **Switch the SDK, keep the model.** With `AGENT_SDK=mantis` and
   `model="claude-opus-5"` (an `ANTHROPIC_API_KEY`, or the Claude login the
   `claude` CLI uses), nothing should change but the import. This is the step
   that tells you whether anything in your code leans on a difference below.
2. **Move one agent to an open model.** Give that agent's options a `model`
   and `backend` — for example GLM 5.3 Flash on Fireworks, billed per token:

    ```python
    import os

    from mantis_agent import MantisAgentOptions

    options = MantisAgentOptions(
        model="accounts/fireworks/models/glm-5p3-flash",
        backend="https://api.fireworks.ai/inference/v1",
        api_key=os.environ["FIREWORKS_API_KEY"],
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=40,
        cwd=".",
    )
    ```

3. **Compare on your own tasks**, then move the next one.

## What carries over unchanged

- `query()` and `ClaudeSDKClient`, with the same flat message shapes:
  `AssistantMessage.content`, `ResultMessage.result`, `num_turns`,
  `total_cost_usd`, `is_error`.
- Custom tools with `@tool` and `create_sdk_mcp_server`, and external MCP
  servers in `mcp_servers`.
- Hooks (`HookMatcher`, matched on Claude's tool names) and permissions
  (`permission_mode`, `can_use_tool`, `PermissionResultAllow(updated_input=...)`).
  Claude's mode names work as they do there: `default`, `acceptEdits`,
  `plan` (read-only tools run, nothing else) and `bypassPermissions`.
- **Claude's built-in tool names.** `Read`, `Write`, `Edit`, `MultiEdit`,
  `Bash`, `Glob`, `Grep`, `LS`, `NotebookEdit`, `WebSearch` and `WebFetch` in
  `allowed_tools`, `tools`, or `disallowed_tools` map to mantis's own
  implementations, and `tools={"type": "preset", "preset": "claude_code"}`
  gives an agent all of them.
- **Bundled skills.** Claude Code gives an agent its bundled skills unasked.
  mantis ships its own `pdf`, `xlsx` and `dataviz` skills, and an agent that
  can write or run something gets them by default. `Skill` in a tool list
  means mantis's `load_skill`, and `disallowed_tools=["Skill"]` turns them off.
- **Reading PDFs.** `Read` on a PDF returns its text page by page, and
  `pages="1-5"` picks a range.

## What is different

- **An agent gets only the built-in tools you name.** Claude Code starts an
  agent with its whole toolset; mantis gives it the ones in `allowed_tools`
  or `tools` (or the `claude_code` preset). An agent configured with neither
  has no file or shell tools — a chat agent never gains `bash` by accident.
- **Web search runs through a search API you choose.** Claude uses
  Anthropic's server-side search. mantis uses Exa, Brave or Tavily when
  `EXA_API_KEY`, `BRAVE_API_KEY` or `TAVILY_API_KEY` is set, and falls back
  to DuckDuckGo with no key, which is slower and returns fewer results.
- **A model that can't see images still finishes the turn.** If the provider
  refuses an image (a tool read a PNG, say), mantis sends the request again
  with each image replaced by a one-line note, and keeps doing that for the
  rest of the run.
- **A few Claude Code tools have no built-in here**: `TodoWrite`, `Task` and
  `ExitPlanMode` are skipped if named. Sub-agents are `agents=` instead.
- **The options class is `MantisAgentOptions`**, and `model` / `backend` /
  `api_key` say which model runs where.

## How the two compare

The numbers below come from a local eval. The same script ran every task
through `claude-agent-sdk` (Claude Opus 5.5, on a Claude login) and through
mantis on GLM 5.3 Flash (Fireworks, billed per token). Each run was graded
automatically against the files it produced.

The everyday tier has twelve tasks:
- two web lookups and a question answered from a CSV;
- open action items found across 30 meeting notes;
- a precise edit across three files;
- a README for a CLI and a summary written to a spec;
- a PDF report with a table and chart, and an Excel workbook built on formulas;
- three code tasks: fix a failing suite, build a feature from a spec, and
  debug a crash and add a regression test.

| | Passed | Time (summed) | Cost | Turns |
|---|---|---|---|---|
| Claude Opus 5.5 · claude-agent-sdk | 12 / 12 | 557 s | $2.62 | 83 |
| GLM 5.3 Flash · mantis | 12 / 12 | 583 s | $0.17 | 116 |

Both finished every task, so on work of this size the difference is cost.
GLM 5.3 Flash did the same twelve tasks in about the same time, for about 7%
of the price. Web search ran on the DuckDuckGo fallback: the Exa key on the
test machine was rejected with a 401, so each search fell back.

The harder tier holds the same data to a bank's standard: "a Goldman Sachs
deliverable, ready for a client or an MD".
- **A PDF research note:** an executive summary backed by numbers, region and
  category tables with shares, the monthly trend, what drove the Q2-to-Q3
  change, three charts, a methodology note and page numbers.
- **An analyst-style Excel model:** live SUMIFS formulas throughout, a proper
  data table, share-of-total columns, totals that tie out, charts, and a
  notes block.

| | PDF note | Excel model | Cost, both |
|---|---|---|---|
| Claude Opus 5.5 · claude-agent-sdk | 1.00 · ~14 min | 1.00 · ~15 min | not reported¹ |
| GLM 5.3 Flash · mantis | 0.95 · 6 min | 1.00 · 10 min² | $0.18 |

¹ Both Claude runs ended in a claude-agent-sdk reader error (`JSON message
exceeded maximum buffer size of 1048576 bytes`) once Claude looked at its own
page images, after the files were written. Set `max_buffer_size` higher if you
do the same.

² The workbook was complete, and every check passed, when the run reached its
40-turn cap. The model was still polishing, so the result is marked
`error_max_turns`.

What closed the gap on the harder tier was the harness, not the model:
- **Room to finish a turn.** GLM spent a 4,096-token output cap planning
  before its first tool call. mantis now raises the cap and continues instead
  of ending the run.
- **The bundled `pdf`, `xlsx` and `dataviz` skills.** Exhibits with titles
  that state the finding, live formulas with check cells, and above all the
  check loop: render the pages and look at them, then recompute every number
  independently.

Claude's output still goes deeper: a driver breakdown of the biggest change,
more exhibits, a more finished workbook. GLM 5.3 Flash's is clean and correct,
for 18 cents.
