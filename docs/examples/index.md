# Examples

All examples live under `mantis_agent/examples/` in the repo. Each one
is runnable directly:

```bash
python -m mantis_agent.examples.quickstart
```

## Basics

| Example | What it shows |
|---|---|
| [`quickstart.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/quickstart.py) | `query()` with one tool, byte-for-byte equivalent to the Claude Agent SDK pattern. |
| [`ollama_local.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/ollama_local.py) | Pointing at a local Ollama daemon. |
| [`with_thinking.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/with_thinking.py) | Rendering thinking blocks separately from final answer. |
| [`tools_option.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/tools_option.py) | Multiple tools, parallel dispatch. |
| [`system_prompt.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/system_prompt.py) | Setting `system_prompt`. |

## MCP

| Example | What it shows |
|---|---|
| [`mcp_calculator.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/mcp_calculator.py) | In-process MCP server via `create_sdk_mcp_server`. |
| [`mcp_filesystem.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/mcp_filesystem.py) | External stdio MCP server (`uvx mcp-server-fetch`). |

## Streaming

| Example | What it shows |
|---|---|
| [`streaming_render.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/streaming_render.py) | Token-by-token rendering with `Agent.run_iter`. |
| [`streaming_mode_ipython.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/streaming_mode_ipython.py) | Streaming in an IPython notebook. |
| [`stderr_callback_example.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/stderr_callback_example.py) | Using the `stderr` callback for debug logging. |

## Sub-agents

| Example | What it shows |
|---|---|
| [`multi_agent_research.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/multi_agent_research.py) | Parent agent fanning out to researcher + drafter + reviewer sub-agents. Runs in real-backend mode or `MANTIS_AGENT_MOCK=1` smoke mode. |
| [`research_agent.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/research_agent.py) | A single research agent with web tools. |

## Budget

| Example | What it shows |
|---|---|
| [`max_budget_usd.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/max_budget_usd.py) | Hitting `BudgetExceededError` deliberately, recovering from a checkpoint. |

## Hosted backends

| Example | What it shows |
|---|---|
| [`fireworks_hosted.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/fireworks_hosted.py) | Running against live Fireworks (or `MANTIS_AGENT_MOCK=1`). |
| [`vllm_self_hosted.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/vllm_self_hosted.py) | Running against a self-hosted vLLM (or `MANTIS_AGENT_MOCK=1`). |

## Running offline

Most examples auto-detect `MANTIS_AGENT_MOCK=1` and run against the mock
provider so CI works without API keys:

```bash
MANTIS_AGENT_MOCK=1 python -m mantis_agent.examples.quickstart
```

In mock mode the assistant emits canned but well-shaped responses;
useful for verifying integration plumbing.
