# Models and backends

Two values decide where a request lands: **`model`** (what answers) and
**`backend`** (where it runs — a URL, or the sentinel `"anthropic"` /
`"mock"`). `base_url` is an accepted alias for `backend`. Credentials are
separate and resolved last.

## The three ways to run a model

Only those two values change. Tools, prompts, budgets, sessions are identical
across all three.

```python
import os

from mantis_agent import Agent

# 1. Local — free, no key. `ollama pull qwen2.5:7b` first.
local = Agent(model="qwen2.5-7b-instruct", backend="http://localhost:11434")

# 2. Self-hosted — your GPU, your weights, no vendor key.
selfhost = Agent(model="Qwen/Qwen2.5-72B-Instruct", backend="http://gpu-box:8000/v1")

# 3. Hosted API — someone else's compute, your key.
hosted = Agent(
    model="accounts/fireworks/models/deepseek-v3",
    backend="https://api.fireworks.ai/inference/v1",
    api_key=os.environ["FIREWORKS_API_KEY"],
)
```

Claude needs one extra note: it speaks `/v1/messages`, not
`/chat/completions`, so name the wire format with the `"anthropic"` sentinel.

```python
import os

from mantis_agent import Agent

claude = Agent(
    model="claude-opus-5",
    backend="anthropic",
    api_key=os.environ["ANTHROPIC_API_KEY"],
)
```

## Auto-routing, and exactly when you get it

Name-shape inference exists, but **only on the typed-options path**
(`MantisAgentOptions`, or `query()` with no options). It maps a model name to a
URL:

| You write | Inferred backend |
|---|---|
| `qwen2.5:7b`, `llama3.2:3b` | `http://localhost:11434` (Ollama tag form) |
| `gpt-4o-mini`, `o3-mini` | `https://api.openai.com/v1` |
| `gemini-2.0-flash` | Google's OpenAI-compat endpoint |
| `accounts/fireworks/models/…` | `https://api.fireworks.ai/inference/v1` |
| `Qwen/Qwen2.5-72B-Instruct` | `https://api.together.xyz/v1` (`org/repo` shape) |
| `gpt-oss:20b` | `http://localhost:11434` — open weights, *not* served by OpenAI |
| `claude-opus-5` | refuses: raises `BackendRoutingError`, name your backend |

```python
from mantis_agent import MantisAgentOptions

# No backend needed: tag form resolves to local Ollama.
options = MantisAgentOptions(model="qwen2.5:7b")
```

Ask before you run:

```python
from mantis_agent.routing import infer_backend, resolve_backend

infer_backend("qwen2.5:7b")                             # 'http://localhost:11434'
infer_backend("gpt-4o-mini")                            # 'https://api.openai.com/v1'
resolve_backend("qwen2.5:7b", "http://gpu-box:11434")   # explicit wins
```

These return **URLs**, not adapter names. Precedence: explicit `backend=` →
`$MANTIS_AGENT_BASE_URL` → inferred → Ollama.

> **A plain `dict` of options does not auto-route.** It goes through `Agent`,
> which picks an adapter from the URL and defaults a bare model name to
> `http://localhost:8000/v1` (vLLM's port). If you pass a dict, pass a
> `backend`.

## Hosted providers — copy-paste setup

Every provider below speaks OpenAI-compatible HTTP. Same two values, different
URL:

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
# model="accounts/fireworks/models/deepseek-v3"
```

**Groq**

```bash
export MANTIS_AGENT_BASE_URL=https://api.groq.com/openai/v1
export MANTIS_AGENT_API_KEY=$GROQ_API_KEY
# model="llama-3.3-70b-versatile"
```

**OpenRouter**

```bash
export MANTIS_AGENT_BASE_URL=https://openrouter.ai/api/v1
export MANTIS_AGENT_API_KEY=$OPENROUTER_API_KEY
```

**Cerebras**

```bash
export MANTIS_AGENT_BASE_URL=https://api.cerebras.ai/v1
export MANTIS_AGENT_API_KEY=$CEREBRAS_API_KEY
```

Prefer it in code? Same thing, per agent — and unlike the env vars, this
works when one process talks to several providers:

```python
import os

from mantis_agent import MantisAgentOptions

options = MantisAgentOptions(
    model="llama-3.3-70b-versatile",
    base_url="https://api.groq.com/openai/v1",
    api_key=os.environ["GROQ_API_KEY"],
)
```

You can also skip `MANTIS_AGENT_API_KEY` entirely: the OpenAI-compat adapter
falls back to the provider's own variable — `OPENAI_API_KEY`,
`TOGETHER_API_KEY`, `FIREWORKS_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`,
`DEEPSEEK_API_KEY`, `DEEPINFRA_API_KEY`, `CEREBRAS_API_KEY`,
`ANYSCALE_API_KEY`, `MOONSHOT_API_KEY`, in that order. Handy, but it also means
a stale `OPENAI_API_KEY` in your shell can end up as the Bearer for a different
provider. Pass `api_key=` when several keys are in play.

`api_key=""` means *send no auth at all*, for backends that authenticate with
their own headers. `api_key=None` (the default) means "go look in the
environment".

## Self-hosted

**Ollama** — found automatically on `localhost:11434`. Remote box? Point at it:
`backend="http://gpu-box:11434"`.

**vLLM** — `vllm serve <model>`, then use the URL including `/v1`:

```bash
export MANTIS_AGENT_BASE_URL=http://localhost:8000/v1
```

**llama.cpp** — run `llama-server` with `--jinja` for native tool use
(`mantis-agent setup-local-llamacpp` does it for you):

```bash
export MANTIS_AGENT_BASE_URL=http://localhost:8080/v1
```

**TGI** — Hugging Face text-generation-inference; a URL containing `tgi`
selects the adapter, default `http://localhost:3000/v1`.

**Modal** — deploy on Modal's serverless GPUs and use `modal:workspace/app` or
the `modal.run` URL. The adapter absorbs cold starts.

## Closed models

The same harness drives closed models:

```python
from mantis_agent import MantisAgentOptions

openai = MantisAgentOptions(model="gpt-4o-mini")        # $OPENAI_API_KEY
gemini = MantisAgentOptions(model="gemini-2.0-flash")   # $GEMINI_API_KEY or $GOOGLE_API_KEY
```

Claude is first-class too — an API key, an OAuth/subscription token
(`ANTHROPIC_AUTH_TOKEN`), or a gateway on a `/anthropic/v1` path all work. Your
tools, sessions, permissions and budgets behave identically, so moving between
an open and a closed model stays a one-line change.

## How tool use adapts per model

Not every model learned function calling. mantis keeps a capability table (38
rows, plus family fallbacks) and picks a strategy from the **model and the
backend together** — both have to support a path for it to be usable:

| Path | Strategy | Chosen when |
|---|---|---|
| `A` | native `tools[]` in the request | model and backend both support native tools |
| `C` | server-enforced JSON grammar — a malformed call is impossible | both support grammars (llama.cpp, vLLM) |
| `B` | `<tool_call>` XML in the prompt, parsed from the stream | the universal fallback (Llama 2, Mistral 7B, older Qwens) |

Peek at what a model can do:

```python
from mantis_agent import lookup_model
from mantis_agent.capabilities import resolve_tool_use_path
from mantis_agent.providers.openai_compat import hosted_profile_from_url

cap = lookup_model("deepseek-r1:1.5b")
print(cap.supports_native_tools, cap.supports_grammar, cap.context_window)

print(resolve_tool_use_path(cap, hosted_profile_from_url("http://localhost:11434")))
```

There is no `tool_use_path` option. To force a path, take the capability away:

```python
from dataclasses import replace

from mantis_agent import Agent, lookup_model

cap = lookup_model("qwen2.5:0.5b")
agent = Agent(
    model="qwen2.5:0.5b",
    backend="http://localhost:11434",
    model_capability=replace(cap, supports_native_tools=False),
)
```

## Good to know

- **Retries are built in** — transient errors back off exponentially and honor
  `Retry-After`; context overflow triggers an emergency compact and retry; and
  `fallback_model="…"` retries a pre-output failure on a second model.
- **Small models get extra tolerance** — hallucinated tool args are dropped,
  string-typed ints/bools are coerced to the schema, near-miss tool names
  resolve, and `<function=NAME>` formats are salvaged.
- **`max_tokens` defaults to the model's own output budget**, not a flat 1024,
  so long answers stopped truncating by default.
- **Unknown option keys are silent** — a dict key mantis doesn't recognize
  lands in `Agent.extra` rather than raising. If an option seems to do nothing,
  check the spelling first.
- **Groq and Cerebras** serve tighter context windows than the model cards
  suggest; the capability table accounts for it.
