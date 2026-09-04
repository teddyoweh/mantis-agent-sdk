# Models and backends

Two values decide where a request lands: **`model`** (what answers) and
**`backend`** (where it runs — a URL, or the sentinel `"anthropic"` /
`"mock"`). `base_url` is an accepted alias for `backend`. Credentials are
separate and resolved last.

Five provider families are first-class: **OpenAI**, **Claude (Anthropic)**,
**Gemini (Google)**, **Grok (xAI)**, and **open-source** models — Ollama,
vLLM, llama.cpp, TGI, Together, Fireworks, Groq, OpenRouter, and friends.
For the four vendor APIs a bare model name is the whole configuration:
`Agent(model="claude-opus-5")`, `Agent(model="gpt-5.4")`,
`Agent(model="gemini-2.5-pro")`, `Agent(model="grok-4")` each pick their
vendor's endpoint and read the vendor's own key from the environment.

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

The vendor APIs are the third case with the URL and the key filled in for
you. Claude gets one extra note: it speaks `/v1/messages`, not
`/chat/completions`, so a bare `claude-*` name selects the native Anthropic
adapter rather than an OpenAI-compatible URL. `backend="anthropic"` says the
same thing explicitly.

```python
from mantis_agent import Agent

claude = Agent(model="claude-opus-5")       # $ANTHROPIC_API_KEY, or a subscription token
openai = Agent(model="gpt-5.4")             # $OPENAI_API_KEY
gemini = Agent(model="gemini-2.5-pro")      # $GEMINI_API_KEY or $GOOGLE_API_KEY
grok = Agent(model="grok-4")                # $XAI_API_KEY or $GROK_API_KEY
```

To reach Claude through a gateway — Bedrock Access Gateway, Azure Foundry,
LiteLLM — name the destination: `backend="https://gateway.example/anthropic/v1"`.
Any `/anthropic/v1` path (or an `api.anthropic.com` URL) selects the same
native adapter.

## Auto-routing, and exactly when you get it

Name-shape inference exists, but **only on the typed-options path**
(`MantisAgentOptions`, or `query()` with no options). It maps a model name to a
backend:

| You write | Inferred backend |
|---|---|
| `qwen2.5:7b`, `llama3.2:3b` | `http://localhost:11434` (Ollama tag form) |
| `gpt-5.4`, `o3`, `o4-mini` | `https://api.openai.com/v1` (`OPENAI_API_KEY`) |
| `gemini-2.5-pro` | Google's OpenAI-compat endpoint (`GEMINI_API_KEY` / `GOOGLE_API_KEY`) |
| `grok-4` | `https://api.x.ai/v1` (`XAI_API_KEY` / `GROK_API_KEY`) |
| `claude-opus-5` | the `"anthropic"` sentinel → native Messages API (`ANTHROPIC_API_KEY` or a Claude subscription login) |
| `accounts/fireworks/models/…` | `https://api.fireworks.ai/inference/v1` |
| `Qwen/Qwen2.5-72B-Instruct` | `https://api.together.xyz/v1` (`org/repo` shape) |
| `gpt-oss:20b` | `http://localhost:11434` — open weights, *not* served by OpenAI |
| anything else | `http://localhost:11434` |

```python
from mantis_agent import MantisAgentOptions

# No backend needed: tag form resolves to local Ollama.
options = MantisAgentOptions(model="qwen2.5:7b")
```

Ask before you run:

```python
from mantis_agent.routing import infer_backend, resolve_backend

infer_backend("qwen2.5:7b")                             # 'http://localhost:11434'
infer_backend("gpt-5.4")                                # 'https://api.openai.com/v1'
infer_backend("grok-4")                                 # 'https://api.x.ai/v1'
infer_backend("claude-opus-5")                          # 'anthropic'
resolve_backend("qwen2.5:7b", "http://gpu-box:11434")   # explicit wins
```

These return the **backend value** — a URL for every family except Claude,
which returns the `"anthropic"` sentinel — never an adapter name. Precedence:
explicit `backend=` → `$MANTIS_AGENT_BASE_URL` → inferred → Ollama.

> **A plain `dict` of options does not auto-route.** It goes through `Agent`,
> which picks an adapter from the URL. The four vendor names are the exception
> — a bare `gpt-*`, o-series, `gemini-*`, `grok-*` or `claude-*` still goes to
> its vendor — but any *other* bare model name defaults to
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

## Authentication

`api_key` is a real option on `Agent`, `MantisAgentOptions`, and the dict
form. Three values, three meanings: a non-empty string is used exactly;
`None` (the default) means "go look in the environment"; `""` means *send no
auth at all*, for backends that authenticate with their own headers.

Discovery for OpenAI-compatible backends, first hit wins:

1. `api_key=` on the options or the `Agent`
2. `$MANTIS_AGENT_API_KEY`
3. **the vendor's own variable when the URL names the vendor** —
   `$OPENAI_API_KEY` for `api.openai.com`, `$XAI_API_KEY` then
   `$GROK_API_KEY` for `api.x.ai`, `$GEMINI_API_KEY` then `$GOOGLE_API_KEY`
   for Google, `$GROQ_API_KEY` for Groq, and so on
4. the generic chain: `OPENAI_API_KEY`, `XAI_API_KEY`, `GROK_API_KEY`,
   `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `TOGETHER_API_KEY`,
   `FIREWORKS_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`,
   `DEEPSEEK_API_KEY`, `DEEPINFRA_API_KEY`, `CEREBRAS_API_KEY`,
   `ANYSCALE_API_KEY`, `MOONSHOT_API_KEY` — in that order

Tier three is why exporting the provider's own variable just works with no
`MANTIS_`-prefixed setup, even with several keys in the shell — a stale
`OPENAI_API_KEY` no longer outranks `XAI_API_KEY` for Grok. Tier four is for
self-hosted or unrecognised URLs, and is why a stale `OPENAI_API_KEY` can end
up as the Bearer for a proxy you meant to leave unauthenticated. Pass
`api_key=` (or `api_key=""`) when that matters.

Claude resolves separately, matching Claude Code: `$ANTHROPIC_API_KEY`
becomes an `x-api-key` header; `$ANTHROPIC_AUTH_TOKEN` becomes
`Authorization: Bearer` — that is what a Claude subscription's OAuth token
(`sk-ant-oat…`) and gateways use. Paste either into `/enable anthropic` and
mantis works out which it is from the shape.

There is deliberately no `api_key` in `settings.json` — it is designed to be
committed. Use the environment or pass `api_key=`.

### Extra request headers

Some endpoints authenticate with headers of their own rather than a key — a
Modal deployment behind proxy auth wants `Modal-Key` / `Modal-Secret`, a
gateway may want a tenant header. `extra_headers` (an option on `Agent`,
`MantisAgentOptions`, and the dict form) is sent on every provider request,
merged **after** the adapter's own auth header, so an explicit header wins:

```python
from mantis_agent import Agent

agent = Agent(
    model="Qwen/Qwen3-8B",
    backend="https://alice--llm-serve.modal.run/v1",
    extra_headers={"Modal-Key": "wk-…", "Modal-Secret": "ws-…"},
)
```

When `extra_headers` is unset, the `MANTIS_AGENT_EXTRA_HEADERS` environment
variable is read as a JSON object — `mantis-agent deploy … connect` exports it
so the terminal reaches a freshly [deployed endpoint](deploy.md) with no code
change:

```bash
export MANTIS_AGENT_EXTRA_HEADERS='{"Modal-Key": "wk-…", "Modal-Secret": "ws-…"}'
```

Malformed JSON raises a `ValueError` naming the variable at `Agent(...)` time
rather than surfacing later as an opaque `401`. Values are kept out of `repr`
— they are usually secrets.

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
the `modal.run` URL. The adapter absorbs cold starts and sends your Modal
tokens as `Modal-Key` / `Modal-Secret` — the proxy-auth pair
`MODAL_PROXY_TOKEN_ID` / `MODAL_PROXY_TOKEN_SECRET` first, then the API-token
pair `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`.

**Don't have a box?** [Deploy one](deploy.md): `mantis-agent deploy up runpod
Qwen/Qwen3-32B --gpu <id>` turns a GPU-cloud account into an OpenAI-compatible
endpoint and connects it.

## The four vendor APIs

The same harness drives the closed models, and each one's reasoning knob is
mapped from the universal [`thinking` config](thinking.md):

```python
from mantis_agent import MantisAgentOptions

claude = MantisAgentOptions(model="claude-opus-5")      # $ANTHROPIC_API_KEY / $ANTHROPIC_AUTH_TOKEN
openai = MantisAgentOptions(model="gpt-5.4")            # $OPENAI_API_KEY
gemini = MantisAgentOptions(model="gemini-2.5-pro")     # $GEMINI_API_KEY or $GOOGLE_API_KEY
grok = MantisAgentOptions(model="grok-4")               # $XAI_API_KEY or $GROK_API_KEY
```

**Claude (Anthropic)** — real Claude over `/v1/messages`, selected by a bare
`claude-*` name, `backend="anthropic"`, an `api.anthropic.com` URL, or a
gateway path ending in `/anthropic`. An API key or a subscription login both
work. The thinking config follows the model generation: Haiku 4.5 and older
get `{"type": "enabled", "budget_tokens": N}`; Opus 4.7/4.8/5 and Sonnet 5
get `{"type": "adaptive"}` plus `output_config.effort` derived from the
budget (they reject `budget_tokens`); Fable/Mythos get effort only.
`temperature` is dropped where sampling was removed, an unknown id is
retried once with the other thinking form, and prompt caching is on by
default.

**OpenAI** — `gpt-*` and the o-series over `api.openai.com`. Reasoning
models (gpt-5.x, o1/o3/o4) get `max_completion_tokens` instead of
`max_tokens`, no `temperature` (they reject one), and the thinking config as
`reasoning_effort`; `effort="xhigh"` passes through, `"max"`/`"ultra"` clamp
to `high`. Streamed `reasoning` deltas surface as thinking blocks.

**Gemini** — `gemini-*` over Google's OpenAI-compatible endpoint. Effort
words become `reasoning_effort` (`low`/`medium`/`high`/`none`); an explicit
`budget_tokens` is sent exactly as
`extra_body.google.thinking_config.thinking_budget`; `adaptive` with no
budget is Gemini's own dynamic default, so nothing is sent. Thought
summaries are opt-in — `extra={"extra_body": {"google": {"thinking_config":
{"include_thoughts": True}}}}` — and stream as thinking blocks.

**Grok (xAI)** — `grok-*` over `api.x.ai/v1`, key in `XAI_API_KEY`
(`GROK_API_KEY` is an accepted alias). Only `grok-3-mini` takes
`reasoning_effort` (`low`/`high`); `grok-4`, `grok-3` and `grok-code-fast`
reason at a fixed level and are never sent the field. `reasoning_content`
deltas surface as thinking blocks.

Your tools, sessions, permissions and budgets behave identically across all
five families, so moving between an open and a closed model stays a one-line
change.

## How tool use adapts per model

Not every model learned function calling. mantis keeps a capability table
(current models across all five families, plus family fallbacks) and picks a
strategy from the **model and the backend together** — both have to support
a path for it to be usable:

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
- **Errors name where they went** — a 404 reads `Not Found (404 from
  http://localhost:8000/v1/chat/completions) — port 8000 is the vLLM default …`,
  so a bare model name that fell through to the wrong port is a one-line fix.
- **`401` from `api.x.ai`** means `XAI_API_KEY` (or `GROK_API_KEY`) is unset
  and the generic chain sent another vendor's key; `AuthError: … needs
  credentials` on a `claude-*` model means neither `ANTHROPIC_API_KEY` nor
  `ANTHROPIC_AUTH_TOKEN` is set.
- **Groq and Cerebras** serve tighter context windows than the model cards
  suggest; the capability table accounts for it.
