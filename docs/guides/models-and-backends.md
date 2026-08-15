# Models and backends

Two values decide where a request goes:

- **`model`** — *what* answers. A model id, exactly as the thing serving it
  spells it (`qwen2.5:7b`, `Qwen/Qwen2.5-72B-Instruct`, `claude-opus-5`).
- **`backend`** — *where* it runs. A URL, or one of two sentinels
  (`"anthropic"`, `"mock"`). `base_url` is an accepted alias for the same
  field.

Credentials are separate, and resolved last: see [Authentication](#authentication).

## The three ways to run a model

Only `model` and `backend` change between them. Everything else — tools,
system prompt, turn limits — is identical.

=== "Local (Ollama)"

    Free, no key, no account. `ollama pull qwen2.5:7b` first.

    ```python
    from mantis_agent import Agent

    agent = Agent(
        model="qwen2.5-7b-instruct",
        backend="http://localhost:11434",
    )
    ```

=== "Self-hosted (vLLM / llama.cpp / TGI)"

    Your GPU, your weights, no vendor key.

    ```python
    from mantis_agent import Agent

    agent = Agent(
        model="Qwen/Qwen2.5-72B-Instruct",
        backend="http://gpu-box.internal:8000/v1",
    )
    ```

=== "Hosted API"

    A provider runs it; you supply a key.

    ```python
    import os

    from mantis_agent import Agent

    agent = Agent(
        model="accounts/fireworks/models/deepseek-v3",
        backend="https://api.fireworks.ai/inference/v1",
        api_key=os.environ["FIREWORKS_API_KEY"],
    )
    ```

=== "Anthropic"

    Claude speaks `/v1/messages`, not `/chat/completions`. The literal
    sentinel `"anthropic"` selects that wire format.

    ```python
    import os

    from mantis_agent import Agent

    agent = Agent(
        model="claude-opus-5",
        backend="anthropic",
        api_key=os.environ["ANTHROPIC_API_KEY"],
    )
    ```

## The two option shapes

!!! important "Read this before anything else"

    `query()` has **two** option shapes. They take different key names, route
    differently, and yield differently-shaped messages. Mixing them produces
    an `AttributeError` a long way from its cause, and it is the single most
    common source of confusion in this SDK.

| | `MantisAgentOptions` (or no options) | plain `dict` |
|---|---|---|
| Message shape | flat, Claude-SDK-identical — `msg.content` | nested wire shape — `msg.message.content` |
| System prompt key | `system_prompt` | `system` |
| Budget key | `max_budget_usd` | `max_usd` |
| Backend resolution | `routing.resolve_backend` — **infers a URL from the model name** | `providers.base.detect_provider` — **no inference** |
| Entry point | `compat_query` | `query._agent_from_options` |

Typed options, which auto-route:

```python
import asyncio

from mantis_agent import MantisAgentOptions, query


async def main() -> None:
    # No backend: `qwen2.5:7b` is Ollama tag form, so this resolves to
    # http://localhost:11434 on its own.
    options = MantisAgentOptions(
        model="qwen2.5:7b",
        system_prompt="Reply in one sentence.",
    )
    async for msg in query(prompt="Weather in SF?", options=options):
        if msg.type == "assistant":
            for block in msg.content:          # flat shape
                print(getattr(block, "text", ""))


asyncio.run(main())
```

The same run with a dict, which does **not** infer — so the backend is
required:

```python
import asyncio

from mantis_agent import query


async def main() -> None:
    async for msg in query(
        prompt="Weather in SF?",
        options={
            "model": "qwen2.5-7b-instruct",
            "backend": "http://localhost:11434",   # required: no inference here
            "system": "Reply in one sentence.",     # note: "system", not "system_prompt"
        },
    ):
        if msg.type == "assistant":
            for block in msg.message.content:      # nested shape
                print(getattr(block, "text", ""))


asyncio.run(main())
```

Unrecognized keys in a dict are **not** an error — they flow into
`Agent.extra` for adapter-specific knobs. That is deliberate, and it means a
misspelled key fails silently. When something has no effect, suspect the key
name first.

## Backend detection

`Agent` picks an adapter by inspecting `backend or model` — string matching, in
this order. There are exactly seven adapters.

| Adapter | Selected by | Default URL when `backend` is unset |
|---|---|---|
| `mock` | the literal `"mock"`, or `MANTIS_AGENT_MOCK=1` | — |
| `anthropic_passthrough` | `"anthropic"`, `api.anthropic.com`, or a `/anthropic/` gateway path | `https://api.anthropic.com/v1` |
| `modal` | `modal:workspace/app`, or a `modal.run` host | — |
| `ollama` | `:11434` or `ollama` in the URL | `http://localhost:11434` |
| `llamacpp` | `llamacpp` or `llama.cpp` in the URL | `http://localhost:8080` |
| `tgi` | `tgi` or `text-generation-inference` in the URL | `http://localhost:3000/v1` |
| `openai_compat` | any other `http(s)://` URL — **and every bare model name** | `$MANTIS_AGENT_BASE_URL`, else `http://localhost:8000/v1` |

That last row is the one that bites. Detection is *not* model-aware:

```python
from mantis_agent.providers.base import detect_provider

detect_provider("http://localhost:11434")   # 'ollama'
detect_provider("anthropic")                # 'anthropic_passthrough'
detect_provider("qwen2.5:7b")               # 'openai_compat'  ← a model name, not a URL
detect_provider("claude-opus-5")            # 'openai_compat'  ← ditto
```

So `Agent(model="qwen2.5:7b")` with no `backend` points at
`http://localhost:8000/v1` (vLLM's default), not at your Ollama. Pass a
`backend` — or use `MantisAgentOptions`, which infers one.

To override detection entirely, pass a ready-made provider:

```python
from mantis_agent import Agent
from mantis_agent.providers.ollama import OllamaProvider

agent = Agent(
    model="qwen2.5:7b",
    provider=OllamaProvider(base_url="http://gpu-box:11434"),
)
```

## Model-name inference

This is what `MantisAgentOptions` uses when you give it no `backend`.
Precedence: explicit `backend` → `$MANTIS_AGENT_BASE_URL` → the shape of the
model name → Ollama.

| Model name shape | Example | Inferred backend |
|---|---|---|
| tag form (`:`, no `/`) | `qwen2.5:7b` | `http://localhost:11434` |
| `accounts/fireworks/models/…` | `accounts/fireworks/models/deepseek-v3` | `https://api.fireworks.ai/inference/v1` |
| `org/repo` (`/`, no `:`) | `Qwen/Qwen2.5-72B-Instruct` | `https://api.together.xyz/v1` |
| `gpt-*`, `o1*`, `o3*`, `o4*` | `gpt-4o-mini` | `https://api.openai.com/v1` |
| `gemini-*` | `gemini-2.5-pro` | Google's OpenAI-compat endpoint |
| `gpt-oss*` | `gpt-oss:20b` | `http://localhost:11434` (open weights — not served by OpenAI) |
| `claude-*` | `claude-opus-5` | **raises `BackendRoutingError`** |
| anything else | `mistral` | `http://localhost:11434` |

Check any name without running it:

```python
from mantis_agent.routing import infer_backend, resolve_backend

infer_backend("qwen2.5:7b")                  # 'http://localhost:11434'
infer_backend("Qwen/Qwen2.5-72B-Instruct")   # 'https://api.together.xyz/v1'
resolve_backend("qwen2.5:7b", "http://gpu-box:11434")   # explicit wins
```

Both return **URLs**, not adapter names.

!!! note "Why `claude-*` raises"

    Inference refuses to guess Anthropic, because a bare `claude-*` name is
    ambiguous between the real API, a gateway, and Bedrock/Vertex. Name the
    destination and it works: `backend="anthropic"`, a gateway URL, or any
    `/anthropic/v1` proxy path.

## Authentication

`api_key` is a real option on `Agent`, `MantisAgentOptions`, and the dict
form. Three values, three meanings:

| Value | Meaning |
|---|---|
| a non-empty string | use exactly this |
| `None` (the default) | discover a key from the environment |
| `""` | send no auth at all — for backends that authenticate with their own headers |

Discovery order for OpenAI-compatible backends, first hit wins:

1. `api_key=` on the options or the `Agent`
2. `$MANTIS_AGENT_API_KEY`
3. `$OPENAI_API_KEY`, `$TOGETHER_API_KEY`, `$FIREWORKS_API_KEY`,
   `$GROQ_API_KEY`, `$OPENROUTER_API_KEY`, `$DEEPSEEK_API_KEY`,
   `$DEEPINFRA_API_KEY`, `$CEREBRAS_API_KEY`, `$ANYSCALE_API_KEY`,
   `$MOONSHOT_API_KEY` — in that order

That third tier is why exporting the provider's own variable usually just
works, with no `MANTIS_`-prefixed setup at all. It is also why a stale
`OPENAI_API_KEY` in your shell can send the wrong Bearer to a different
provider — pass `api_key=` explicitly when several are floating around.

Anthropic resolves separately, matching Claude Code: `$ANTHROPIC_API_KEY`
becomes an `x-api-key` header; `$ANTHROPIC_AUTH_TOKEN` becomes
`Authorization: Bearer` (that is what OAuth logins and gateways use).

!!! warning "Keys don't come from `settings.json`"

    There is deliberately no `api_key` key in the settings file: it is
    designed to be committed. Use the environment or pass `api_key=`.

## Capabilities

Each model also resolves to a `ModelCapability` — how to drive tool calls,
how much context it has, what a sane temperature is.

```python
from mantis_agent import lookup_model

cap = lookup_model("deepseek-r1:1.5b")
print(cap.supports_native_tools)   # can it use tools[] in the request body?
print(cap.supports_grammar)        # can it honor a constrained-JSON grammar?
print(cap.emits_thinking_blocks)   # does it stream reasoning separately?
print(cap.context_window, cap.max_output_tokens)
```

Resolution: exact id → provider prefix stripped (`meta-llama/Llama-3.1-70B` →
`llama-3.1-70b`) → substring match → family default → generic ChatML. So an
unknown finetune inherits its base family's behavior instead of failing.

The tool-use strategy is not a field — it is **computed** from the model and
the backend together, because both have to support a path for it to work:

```python
from mantis_agent import lookup_model
from mantis_agent.capabilities import resolve_tool_use_path
from mantis_agent.providers.openai_compat import hosted_profile_from_url

model_cap = lookup_model("qwen2.5:7b")
backend_cap = hosted_profile_from_url("http://localhost:11434")
print(resolve_tool_use_path(model_cap, backend_cap))   # 'A', 'B', or 'C'
```

| Path | Strategy | Chosen when |
|---|---|---|
| `A` | native `tools[]` in the request body | model **and** backend support native tools |
| `C` | prompt-engineered + server-enforced JSON grammar | both support grammars (llama.cpp, vLLM) |
| `B` | `<tool_call>` XML injected into the prompt, parsed from the text stream | neither — the universal fallback |

To force a path, override the capability rather than looking for an option
(there is no `tool_use_path` option — a claim earlier versions of this page
made in error):

```python
from dataclasses import replace

from mantis_agent import Agent, lookup_model

cap = lookup_model("qwen2.5:0.5b")

# A tiny model that advertises native tools but is bad at them: take the
# capability away and the runtime drops to a prompt-engineered path.
agent = Agent(
    model="qwen2.5:0.5b",
    backend="http://localhost:11434",
    model_capability=replace(cap, supports_native_tools=False),
)
```

## Per-backend notes

### Ollama

Defaults to `http://localhost:11434`. Native tool use on Llama 3.1+ and Qwen
2.5+; older models fall back to the XML path automatically. `mantis
setup-local` installs and launches the daemon for you.

### OpenAI-compatible (vLLM, Together, Fireworks, Groq, OpenRouter, Cerebras, …)

The catch-all. Give it the base URL *including* the `/v1` suffix the provider
publishes. Context windows vary sharply between providers serving the same
weights — the capability table tracks the common cases, and
`backend_capability` overrides it.

### llama.cpp

Start `llama-server` with `--jinja` for native tool-use templates; without it,
tool calls fall back to grammar-constrained JSON.

### TGI

HuggingFace text-generation-inference, default `http://localhost:3000/v1`.

### Modal

Serverless GPUs, addressed as `modal:workspace/app` or a `modal.run` URL. The
adapter handles cold starts and per-request keepalives.

### Anthropic passthrough

Real Claude over `/v1/messages`. Selected by `backend="anthropic"`, an
`api.anthropic.com` URL, or a gateway path ending in `/anthropic` — which is
how Bedrock Access Gateway, Azure Foundry, and LiteLLM's Anthropic passthrough
are reached.

### Mock

`backend="mock"` (or `MANTIS_AGENT_MOCK=1`) swaps in a scripted provider. Same
agent loop, no network — the way to test tool dispatch in CI.

## When it doesn't work

| Symptom | Cause | Fix |
|---|---|---|
| `Connection refused` on `localhost:8000` | a bare model name with no `backend` — detection defaulted to vLLM's port | pass `backend=`, or use `MantisAgentOptions` |
| `401` / `invalid api key` from the wrong provider | an unrelated `*_API_KEY` in the environment was picked up by the discovery chain | pass `api_key=` explicitly |
| `404 model not found` | the id isn't spelled the way that backend spells it | check the provider's model list; ids are not portable |
| `BackendRoutingError` on a `claude-*` model | name-based inference refuses to guess Anthropic | `backend="anthropic"` |
| `AttributeError: 'SDKAssistantMessage' object has no attribute 'content'` | option shape and message shape mixed | dict → `msg.message.content`; typed → `msg.content` |
| an option seems to do nothing | unknown dict keys fall through to `Agent.extra` silently | check the key name against [MantisAgentOptions](../api/options.md) |
| `temperature` rejected as deprecated | some newer models refuse an explicit temperature | leave it unset; the default is suppressed per-provider |
