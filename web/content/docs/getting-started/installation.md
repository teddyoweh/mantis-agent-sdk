# Installation

`mantis-agent-sdk` is a normal Python package. No build tools, no native
extensions, no required system dependencies.

## Requirements

- **Python ≥ 3.11**
- **One of:** a hosted backend you can reach over HTTPS (OpenAI, Together,
  Fireworks, Groq, OpenRouter, …), a local Ollama install, or a local
  llama.cpp install.

## Install

```bash
pip install mantis-agent-sdk
```

That covers the hosted-backends path: `query`, `ClaudeSDKClient`, tools,
MCP, sessions, sub-agents, hooks, budget — all of it.

## Optional extras

The package has no required optional groups, but a few extras pull in
provider-specific clients for convenience:

| Extra | What it adds |
|---|---|
| `mantis-agent-sdk[bedrock]` | `boto3` for the AWS Bedrock runtime adapter |
| `mantis-agent-sdk[all]` | Every backend's optional client (currently just `bedrock`) |
| `mantis-agent-sdk[dev]` | `pytest`, `pytest-anyio`, `ruff`, `respx` for tests |

For Anthropic (`anthropic_passthrough`), OpenAI, Gemini, vLLM, Ollama,
Together, Fireworks, Groq, OpenRouter, Cerebras, llama.cpp, and TGI — no
extras are required. The provider uses `httpx` directly.

## Verify

```python
import mantis_agent
print(mantis_agent.__version__)
```

You should see something like `0.1.0`.

## CLI

The package installs a single console script:

```bash
mantis-agent --help
```

Subcommands:

- `mantis-agent setup-local` — install Ollama, pull a CPU-friendly model, run a
  smoke test. Supports Linux, macOS, and Windows.
- `mantis-agent setup-local-llamacpp` — same idea, but builds llama.cpp from
  source and pulls a GGUF model.

See [Local setup](local-setup.md) for details.
