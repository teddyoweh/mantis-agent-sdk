# Models and backends

`mantis-agent-sdk` ships seven backends. You almost never pick one by name —
the SDK auto-routes from the model string.

## Backends at a glance

| Backend | Use when | Routes from |
|---|---|---|
| `ollama` | You're running Ollama locally (or remote). | Model names with a tag form: `llama3.2:3b`, `qwen2.5:7b`, `deepseek-r1:1.5b`. |
| `openai_compat` | Hosted OpenAI-compatible endpoints: vLLM, Together, Fireworks, Groq, OpenRouter, Cerebras. | `MANTIS_AGENT_BASE_URL` set; or org-prefixed names like `Qwen/Qwen2.5-72B-Instruct`. |
| `openai` | OpenAI proper. | `gpt-4*`, `gpt-3.5*`, `o1*`, `o3*`, `o4*`. |
| `gemini` | Google Gemini via the OpenAI-compat endpoint. | `gemini-*`. |
| `llamacpp` | Local llama.cpp `llama-server`. | `--backend llamacpp` or `base_url=http://localhost:8080/v1`. |
| `tgi` | HuggingFace text-generation-inference. | `--backend tgi`. |
| `modal` | Modal serverless GPUs. | `--backend modal` with a `MODAL_*` env. |
| `anthropic_passthrough` | Parity testing against real Claude. | `claude-*` model names (only when `ANTHROPIC_API_KEY` is set). |
| `mock` | Tests / smoke runs. | `MANTIS_AGENT_MOCK=1` env. |

## Auto-routing rules

The `routing` module maps model names to backends in priority order:

1. Explicit `backend=` or `MANTIS_AGENT_BACKEND` env wins.
2. `MANTIS_AGENT_MOCK=1` → `mock`.
3. Ollama tag form (`name:tag`) → `ollama`.
4. `gpt-*` / `o[134]*` → `openai`.
5. `gemini-*` → `gemini`.
6. `claude-*` with `ANTHROPIC_API_KEY` → `anthropic_passthrough`.
7. Org-prefixed (`Qwen/...`, `meta-llama/...`) → `openai_compat`
   (needs `MANTIS_AGENT_BASE_URL`).
8. Otherwise → error with a hint about which env vars to set.

If you're unsure what a name will route to:

```python
from mantis_agent.routing import resolve_backend
print(resolve_backend("qwen2.5:7b"))         # → 'ollama'
print(resolve_backend("gpt-4o-mini"))        # → 'openai'
print(resolve_backend("Qwen/Qwen2.5-72B"))   # → 'openai_compat'
```

## Forcing a backend

```python
options = {
    "model": "Qwen/Qwen2.5-72B-Instruct",
    "backend": "openai_compat",
    "base_url": "https://api.together.xyz/v1",
    "api_key": os.environ["TOGETHER_API_KEY"],
}
```

Or set `MANTIS_AGENT_BACKEND=openai_compat`, `MANTIS_AGENT_BASE_URL=…`,
`MANTIS_AGENT_API_KEY=…` and skip the explicit fields.

## Capabilities

Every model also carries a `ModelCapability` row that tells the runtime
*how* to drive tool use. The capability table currently covers 30+ models:

```python
from mantis_agent import lookup_model, resolve_tool_use_path

cap = lookup_model("deepseek-r1:1.5b")
print(cap.tool_use_path)   # ToolUsePath.XML_PROMPT_ENGINEERED
print(cap.supports_thinking)  # True
print(cap.context_window)  # 128_000
```

`resolve_tool_use_path()` chooses between three strategies:

- `NATIVE_TOOLS` — pass `tools[]` in the request body. Modern OpenAI, Claude,
  most Qwens, llama 3.1+.
- `XML_PROMPT_ENGINEERED` — inject `<tool_call>` XML into the system prompt
  and parse it back out of completions. Llama 2, Mistral 7B, older Qwens.
- `GRAMMAR_CONSTRAINED_JSON` — use a JSON-schema grammar (llama.cpp,
  vLLM) to force valid tool-call JSON.

You don't normally pick this manually — the routing module handles it. But
you can override if a specific model needs a different path:

```python
options = {
    "model": "qwen2.5:0.5b",
    "tool_use_path": "xml_prompt_engineered",
}
```

## Per-backend notes

### Ollama

- Auto-discovered on `http://localhost:11434` unless overridden by
  `MANTIS_AGENT_BASE_URL`.
- Native tool use supported for Llama 3.1+ and Qwen 2.5+.
- `setup-local` writes a startup-on-first-run launcher for the Ollama
  daemon — see [Local setup](../getting-started/local-setup.md).

### OpenAI-compat (vLLM, Together, Fireworks, Groq, OpenRouter, Cerebras)

- Set `MANTIS_AGENT_BASE_URL` and `MANTIS_AGENT_API_KEY`.
- Tool use goes via native `tools[]`.
- Some providers (Cerebras, Groq) have stricter context windows; the
  capability table tracks these.

### llama.cpp

- Use `--jinja` to enable native tool-use templates. `mantis-agent-sdk` does
  this for you when starting via `setup-local-llamacpp`.
- Without `--jinja`, falls back to `GRAMMAR_CONSTRAINED_JSON`.

### Modal serverless

- For multi-hour or GPU runs: launch a model on Modal, point the SDK at
  the Modal URL. The Modal adapter handles cold-start delays and
  per-request keepalives.

### Anthropic passthrough

- **Only for parity testing**. Pins to the real Anthropic API so you can
  compare mantis-agent-sdk's behaviour against the source.
- Not part of the 1.0 public surface — don't build production code on it.
