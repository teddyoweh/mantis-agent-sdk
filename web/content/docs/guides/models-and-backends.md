# Models and backends

You name a model; mantis works out where it runs and how to talk to it.
That's the whole mental model. This page covers what the name resolves to,
how to point at any provider, and what to do when you want to override the
guess.

## How routing works

mantis reads the *shape* of the model name:

| You write | It runs on | Why |
|---|---|---|
| `qwen2.5:7b`, `llama3.2:3b` | Local Ollama | `name:tag` is Ollama's naming form |
| `gpt-4o-mini`, `o3-mini` | OpenAI | OpenAI's prefixes |
| `gemini-2.0-flash` | Google Gemini | `gemini-` prefix |
| `Qwen/Qwen2.5-72B-Instruct` | Your OpenAI-compat provider | `org/model` form + your `MANTIS_AGENT_BASE_URL` |
| `claude-*` | Anthropic (parity testing only) | Requires `ANTHROPIC_API_KEY` |

Two overrides always win over the guess:

1. `backend=` in options (or `MANTIS_AGENT_BACKEND` in the env)
2. `MANTIS_AGENT_MOCK=1` — forces the mock provider for tests and CI

Not sure where a name will land? Ask:

```python
from mantis_agent.routing import resolve_backend
resolve_backend("qwen2.5:7b")        # 'ollama'
resolve_backend("gpt-4o-mini")       # 'openai'
resolve_backend("Qwen/Qwen2.5-72B")  # 'openai_compat'
```

## Hosted providers — copy-paste setup

Every hosted provider below speaks the same OpenAI-compatible protocol.
Setup is always the same two env vars — URL and key — then you use the
provider's model names.

**Together**

```bash
export MANTIS_AGENT_BASE_URL=https://api.together.xyz/v1
export MANTIS_AGENT_API_KEY=$TOGETHER_API_KEY
# model="Qwen/Qwen2.5-72B-Instruct-Turbo"
```

**Fireworks**

```bash
export MANTIS_AGENT_BASE_URL=https://api.fireworks.ai/inference/v1
export MANTIS_AGENT_API_KEY=$FIREWORKS_API_KEY
# model="accounts/fireworks/models/llama-v3p1-70b-instruct"
```

**Groq**

```bash
export MANTIS_AGENT_BASE_URL=https://api.groq.com/openai/v1
export MANTIS_AGENT_API_KEY=$GROQ_API_KEY
# model="llama-3.3-70b-versatile"
```

**OpenRouter** (200+ models behind one key)

```bash
export MANTIS_AGENT_BASE_URL=https://openrouter.ai/api/v1
export MANTIS_AGENT_API_KEY=$OPENROUTER_API_KEY
# model="deepseek/deepseek-chat"
```

**Cerebras**

```bash
export MANTIS_AGENT_BASE_URL=https://api.cerebras.ai/v1
export MANTIS_AGENT_API_KEY=$CEREBRAS_API_KEY
# model="llama-3.3-70b"
```

Prefer keeping it in code instead of the env? Same thing, per-agent:

```python
options = MantisAgentOptions(
    model="llama-3.3-70b-versatile",
    backend="https://api.groq.com/openai/v1",
    api_key=os.environ["GROQ_API_KEY"],
)
```

## Self-hosted

**Ollama** — found automatically on `localhost:11434`. Remote box? Set
`MANTIS_AGENT_BASE_URL=http://gpu-box:11434`.

**vLLM** — start `vllm serve`, then point at it like any provider:

```bash
export MANTIS_AGENT_BASE_URL=http://localhost:8000/v1
```

**llama.cpp** — run `llama-server` with `--jinja` for native tool use
(`mantis-agent setup-local-llamacpp` does all of this for you):

```bash
export MANTIS_AGENT_BASE_URL=http://localhost:8080/v1
```

**TGI** (Hugging Face text-generation-inference) — pass `backend="tgi"`.

**Modal** — deploy a model on Modal's serverless GPUs and point mantis at
the Modal URL. The adapter absorbs cold-start delays for you.

## Closed models

The same harness drives closed models when you want them:

```python
options = MantisAgentOptions(model="gpt-4o-mini")        # OpenAI
options = MantisAgentOptions(model="gemini-2.0-flash")   # Google
```

Just set `OPENAI_API_KEY` / `GEMINI_API_KEY`. Your tools, sessions,
permissions, and budgets work identically — switching between an open and
a closed model is still a one-line change.

## How tool use adapts per model

Not every model learned function calling. mantis keeps a capability table
(30+ models) and picks the right strategy for each:

- **Native** — the model supports `tools[]`; use it directly. Qwen 2.5+,
  Llama 3.1+, gpt-oss, all closed models.
- **Prompted** — teach the schema in the prompt and parse the reply.
  Rescues Llama 2, Mistral 7B, and older models.
- **Grammar-constrained** — where the server can enforce a JSON grammar
  (llama.cpp, vLLM), the model *cannot* emit a malformed call.

You never pick this by hand — but you can peek, or override:

```python
from mantis_agent import lookup_model

cap = lookup_model("deepseek-r1:1.5b")
cap.tool_use_path       # ToolUsePath.XML_PROMPT_ENGINEERED
cap.supports_thinking   # True
cap.context_window      # 128_000
```

```python
# force a specific path for one model
options = {"model": "qwen2.5:0.5b", "tool_use_path": "xml_prompt_engineered"}
```

## Good to know

- **Retries are built in** — transient errors back off exponentially and
  honor `Retry-After`; context overflow triggers an emergency compact and
  retry; and `fallback_model="..."` in options retries a pre-output
  failure on a second model.
- **Small models get extra tolerance** — hallucinated extra tool args are
  dropped, string-typed ints/bools are coerced to the schema, near-miss
  tool names resolve, and `<function=NAME>` call formats are salvaged. A
  1.5B model misbehaving rarely breaks the loop.
- **`max_tokens` defaults to the model's output budget**, not a fixed
  1024 — long answers stopped getting truncated by default.
- **Ollama** supports native tool use for Llama 3.1+ and Qwen 2.5+; older
  models fall back to the prompted path automatically.
- **Groq and Cerebras** have tighter context windows than the model cards
  suggest; the capability table accounts for it.
- **Anthropic passthrough** (`claude-*` names) exists so we can test parity
  against the real thing. Don't build on it — if you want Claude in
  production, use Anthropic's own SDK.
