# Local setup

Run models on your own machine — free, offline, no API key. One command
sets up everything, and it works on a plain laptop with no GPU.

```bash
mantis-agent setup-local
```

## What that command does

1. **Installs Ollama** if you don't have it (official installer; Linux,
   macOS, and Windows all supported).
2. **Starts it** if it isn't already running.
3. **Pulls a model that fits your machine** from a curated catalog of
   CPU-friendly models (135M–8B parameters). Default is `qwen2.5:0.5b`;
   pick another with `--model llama3.2:3b`.
4. **Smoke-tests it** with a real `query()` call, so you know the whole
   path works before you write any code.

### Picking a model

```bash
mantis-agent setup-local --list
```

prints the catalog. Each entry shows the model tag, RAM footprint, and a
short note about strengths. The catalog covers:

- 135M / 360M models for tiny dev loops (`smollm2:135m`, `qwen2.5:0.5b`)
- 1–3B models for serious local work (`llama3.2:1b`, `qwen2.5:1.5b`, `qwen2.5:3b`)
- 7–8B models for full-quality CPU runs (`qwen2.5:7b`, `llama3.1:8b`)

### Verifying

```python
import asyncio
from mantis_agent import query

async def main():
    async for msg in query(
        prompt="say hi",
        options={"model": "qwen2.5:0.5b"},
    ):
        print(msg)

asyncio.run(main())
```

If that prints assistant + result messages, the install is working.

## `mantis-agent setup-local-llamacpp` (llama.cpp)

If you prefer GGUF + llama.cpp over Ollama:

```bash
mantis-agent setup-local-llamacpp
```

This:

1. Clones llama.cpp into `~/.mantis-agent/llama.cpp/`.
2. Builds it from source (`make` / `cmake`).
3. Downloads a default GGUF model into `~/.mantis-agent/models/`.
4. Starts `llama-server` on `localhost:8080`.
5. Smoke-tests via the OpenAI-compatible endpoint.

After that, `mantis-agent-sdk` auto-routes any `--backend llamacpp` or
`base_url=http://localhost:8080/v1` request through the
[OpenAI-compat provider](../guides/models-and-backends.md).

## Where state lives

`mantis-agent-sdk` writes nothing to your project. Everything goes under
`~/.mantis-agent/`:

```
~/.mantis-agent/
├── settings.json       merged settings (see Configuration)
├── memory/             persistent memory entries (see Memory guide)
├── sessions/           JSONL transcripts
├── models/             GGUF models pulled by setup-local-llamacpp
└── llama.cpp/          llama.cpp build tree (if you used it)
```

You can override the root with `MANTIS_AGENT_HOME=/path`.
