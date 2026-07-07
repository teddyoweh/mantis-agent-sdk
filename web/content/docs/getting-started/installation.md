# Installation

One `pip install`. No build tools, no native extensions, no system
dependencies.

```bash
pip install mantis-agent-sdk
```

That's everything: the full SDK (`query`, `ClaudeSDKClient`, tools, MCP,
sessions, sub-agents, hooks, budgets) **and** the `mantis` terminal — a
Claude-Code-style coding agent you can run in any directory.

## Requirements

- **Python 3.11 or newer**
- **Somewhere to run a model.** Any one of these: a local
  [Ollama](local-setup.md) install (free, works on a laptop), a hosted
  provider key (Together, Fireworks, Groq, OpenRouter, …), or an OpenAI /
  Gemini key.

Don't have any of those yet? `mantis-agent setup-local` gets you a working
local model in one command — see [Local setup](local-setup.md).

## What gets installed

Two commands land on your PATH:

- **`mantis`** — the terminal coding agent. Run it in a project directory
  and start typing.
- **`mantis-agent`** — the utility CLI: `setup-local`, `probe`,
  `list-models`, `run`, `chat`.

## Optional extras

You almost certainly don't need these. Every backend — Ollama, vLLM,
Together, Fireworks, Groq, OpenRouter, Cerebras, llama.cpp, TGI, OpenAI,
Gemini — works with the base install.

| Extra | When you'd want it |
|---|---|
| `mantis-agent-sdk[bedrock]` | Running models through AWS Bedrock (adds `boto3`) |
| `mantis-agent-sdk[dev]` | Contributing — test and lint tooling |

## Verify it worked

```bash
python -c "import mantis_agent; print(mantis_agent.__version__)"
```

Prints the installed version (e.g. `1.21.0`). Then head to the
[Quickstart](quickstart.md).

> Want `mantis` available everywhere, isolated from your projects?
> `uv tool install mantis-agent-sdk` or `pipx install mantis-agent-sdk`.
