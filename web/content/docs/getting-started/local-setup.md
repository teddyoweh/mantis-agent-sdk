# Local setup

If you don't have GPU access, `mantis-agent setup-local` is the fastest path to
a working agent. It runs CPU-friendly models locally so you can develop,
test, and run examples without an API key.

## `mantis-agent setup-local` (Ollama)

```bash
mantis-agent setup-local
```

What this does, in order:

1. **Detects your OS** — Linux, macOS, or Windows.
2. **Installs Ollama** if it isn't already on `PATH`:
   - Linux / macOS: runs the official `curl | sh` installer.
   - Windows: downloads `OllamaSetup.exe`, runs it with Inno Setup silent
     flags (`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-`), then
     prepends `%LOCALAPPDATA%\Programs\Ollama` to the running process's
     `PATH` so the rest of the command can find it.
3. **Starts `ollama serve`** if the daemon isn't already running.
4. **Pulls a CPU-friendly model** from a curated 12-entry catalog
   (135M → 8B params). Default is `qwen2.5:0.5b`. Override with
   `--model llama3.2:3b` or any entry from the catalog.
5. **Smoke-tests** the model with a one-shot `query()` call.

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
