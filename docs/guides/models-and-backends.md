# Models and backends

Two values decide where a request goes:

- **`model`** — *what* answers. A model id, exactly as the thing serving it
  spells it (`qwen2.5:7b`, `Qwen/Qwen2.5-72B-Instruct`, `claude-opus-5`).
- **`backend`** — *where* it runs. A URL, or one of two sentinels
  (`"anthropic"`, `"mock"`). `base_url` is an accepted alias for the same
  field.

Five provider families are first-class — **OpenAI**, **Anthropic Claude**,
**Google Gemini**, **xAI Grok**, and **open-source** models (Ollama, vLLM,
llama.cpp, TGI, Together, Fireworks, Groq, OpenRouter, …). For the four
vendor APIs a bare model name is enough: `Agent(model="claude-opus-5")`,
`Agent(model="gpt-5.4")`, `Agent(model="gemini-2.5-pro")`,
`Agent(model="grok-4")` each pick their vendor's endpoint and read the
vendor's own key from the environment.

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

=== "Anthropic (Claude)"

    Claude speaks `/v1/messages`, not `/chat/completions`. A bare `claude-*`
    name selects that adapter on its own; `ANTHROPIC_API_KEY` (or a
    subscription OAuth token in `ANTHROPIC_AUTH_TOKEN`) is all it needs.

    ```python
    from mantis_agent import Agent

    agent = Agent(model="claude-opus-5")
    ```

    The literal sentinel `backend="anthropic"` says the same thing
    explicitly, and an `api.anthropic.com` URL or a `/anthropic/v1` gateway
    path (Bedrock Access Gateway, Azure Foundry, LiteLLM) points the same
    adapter elsewhere.

=== "OpenAI / Gemini / Grok"

    The three OpenAI-compatible vendor APIs. A bare first-party name implies
    the vendor endpoint, and each reads its own key: `OPENAI_API_KEY`,
    `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), `XAI_API_KEY` (or `GROK_API_KEY`).

    ```python
    from mantis_agent import Agent

    openai = Agent(model="gpt-5.4")
    gemini = Agent(model="gemini-2.5-pro")
    grok = Agent(model="grok-4")
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
| `anthropic_passthrough` | `"anthropic"`, `api.anthropic.com`, a `/anthropic/` gateway path, **or a bare `claude-*` model name** | `https://api.anthropic.com/v1` |
| `modal` | `modal:workspace/app`, or a `modal.run` host | — |
| `ollama` | `:11434` or `ollama` in the URL | `http://localhost:11434` |
| `llamacpp` | `llamacpp` or `llama.cpp` in the URL | `http://localhost:8080` |
| `tgi` | `tgi` or `text-generation-inference` in the URL | `http://localhost:3000/v1` |
| `openai_compat` | any other `http(s)://` URL (`api.openai.com`, `generativelanguage.googleapis.com`, `api.x.ai`, Together, …) — **and every other bare model name** | `$MANTIS_AGENT_BASE_URL`; else the vendor endpoint for a bare `gpt-*` / o-series / `gemini-*` / `grok-*` name; else `http://localhost:8000/v1` |

That last row is the one to know. Detection is model-aware only for the
first-party vendor names:

```python
from mantis_agent.providers.base import detect_provider

detect_provider("http://localhost:11434")   # 'ollama'
detect_provider("anthropic")                # 'anthropic_passthrough'
detect_provider("claude-opus-5")            # 'anthropic_passthrough'  ← Claude is first-class
detect_provider("qwen2.5:7b")               # 'openai_compat'  ← a model name, not a URL
```

So `Agent(model="qwen2.5:7b")` with no `backend` points at
`http://localhost:8000/v1` (vLLM's default), not at your Ollama. Pass a
`backend` — or use `MantisAgentOptions`, which infers one. A bare `gpt-5.4`,
`gemini-2.5-pro` or `grok-4` does go to its vendor.

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
| `gpt-*`, `o1*`, `o3*`, `o4*` | `gpt-5.4` | `https://api.openai.com/v1` (`OPENAI_API_KEY`) |
| `gemini-*` | `gemini-2.5-pro` | `https://generativelanguage.googleapis.com/v1beta/openai` (`GEMINI_API_KEY`) |
| `grok-*` | `grok-4` | `https://api.x.ai/v1` (`XAI_API_KEY`) |
| `claude-*` | `claude-opus-5` | the `"anthropic"` sentinel → native Anthropic adapter (`ANTHROPIC_API_KEY`) |
| `gpt-oss*` | `gpt-oss:20b` | `http://localhost:11434` (open weights — not served by OpenAI) |
| anything else | `mistral` | `http://localhost:11434` |

Check any name without running it:

```python
from mantis_agent.routing import infer_backend, resolve_backend

infer_backend("qwen2.5:7b")                  # 'http://localhost:11434'
infer_backend("Qwen/Qwen2.5-72B-Instruct")   # 'https://api.together.xyz/v1'
infer_backend("grok-4")                      # 'https://api.x.ai/v1'
infer_backend("claude-opus-5")               # 'anthropic'
resolve_backend("qwen2.5:7b", "http://gpu-box:11434")   # explicit wins
```

Both return the **backend value** — a URL for every family except Claude,
which returns the `"anthropic"` sentinel (there is no OpenAI-compat URL to
point at; Claude speaks `/v1/messages`). Neither returns an adapter name.

!!! note "Claude behind a gateway"

    The sentinel means api.anthropic.com. To reach Claude through Bedrock
    Access Gateway, Azure Foundry, Vertex or LiteLLM, name the destination:
    `backend="https://gateway.example/anthropic/v1"` — any `/anthropic/v1`
    path selects the same native adapter.

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
3. the vendor's own variable when the URL names the vendor —
   `$OPENAI_API_KEY` for `api.openai.com`, `$XAI_API_KEY` then
   `$GROK_API_KEY` for `api.x.ai`, `$GEMINI_API_KEY` then `$GOOGLE_API_KEY`
   for Google, `$GROQ_API_KEY` for Groq, and so on
4. the generic chain: `$OPENAI_API_KEY`, `$XAI_API_KEY`, `$GROK_API_KEY`,
   `$GEMINI_API_KEY`, `$GOOGLE_API_KEY`, `$TOGETHER_API_KEY`,
   `$FIREWORKS_API_KEY`, `$GROQ_API_KEY`, `$OPENROUTER_API_KEY`,
   `$DEEPSEEK_API_KEY`, `$DEEPINFRA_API_KEY`, `$CEREBRAS_API_KEY`,
   `$ANYSCALE_API_KEY`, `$MOONSHOT_API_KEY` — in that order

The third tier is why exporting the provider's own variable just works, with
no `MANTIS_`-prefixed setup at all, even with several keys in the shell. The
fourth is for self-hosted or unrecognised URLs, and is why a stale
`OPENAI_API_KEY` can reach a proxy you meant to leave unauthenticated — pass
`api_key=` (or `api_key=""`) explicitly there.

Anthropic resolves separately, matching Claude Code: `$ANTHROPIC_API_KEY`
becomes an `x-api-key` header; `$ANTHROPIC_AUTH_TOKEN` becomes
`Authorization: Bearer` (that is what OAuth logins and gateways use).

## Auth methods

Each provider family can be reached more than one way, and every way is a
first-class *method*. Claude in particular: a console API key, a Claude
subscription login, Google Vertex AI, Amazon Bedrock, or an Azure AI Foundry
deployment — all of them serve `model="claude-opus-5"` from the same code.

```bash
mantis-agent auth list                 # every family, every method, what is configured
mantis-agent auth list claude          # one family
mantis-agent auth use claude vertex --set GOOGLE_CLOUD_PROJECT=my-project
mantis-agent auth login claude         # browser login for a Claude subscription
mantis-agent auth check claude         # probe the active method over the network
mantis-agent auth clear claude api_key # forget what mantis saved
```

Every command takes `--json` for scripting, and the same surface is available
in Python:

```python
from mantis_agent.auth_methods import auth_methods, configured_method, set_method

for method in auth_methods("anthropic"):
    print(method.id, method.label, [f.env for f in method.fields])

set_method("anthropic", "bedrock", {"AWS_REGION": "us-east-1"})
print(configured_method("anthropic"))   # 'bedrock'
```

| Family | Method | Fields (env vars) | Backend it selects |
|---|---|---|---|
| `anthropic` | `api_key` | `ANTHROPIC_API_KEY` | `anthropic` |
| | `oauth` | *(browser login → `ANTHROPIC_AUTH_TOKEN`)* | `anthropic` |
| | `vertex` | `GOOGLE_CLOUD_PROJECT`, `CLOUD_ML_REGION`, `GOOGLE_APPLICATION_CREDENTIALS` | `vertex:anthropic` |
| | `bedrock` | `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_PROFILE` | `bedrock:anthropic` |
| | `azure` | `AZURE_ANTHROPIC_ENDPOINT`, `AZURE_ANTHROPIC_API_KEY` | *endpoint* + `/anthropic/v1` |
| `openai` | `api_key` | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
| | `azure_openai` | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION` | *endpoint* + `/openai/v1` |
| `gemini` | `api_key` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | Google's OpenAI-compat endpoint |
| | `vertex` | `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_REGION`, `GOOGLE_APPLICATION_CREDENTIALS` | `vertex:gemini` |
| `xai` | `api_key` | `XAI_API_KEY` (or `GROK_API_KEY`) | `https://api.x.ai/v1` |
| `oss` | `ollama` | *(none — a local daemon)* | `http://localhost:11434` |
| | `selfhost` | `MANTIS_AGENT_BASE_URL`, `MANTIS_AGENT_API_KEY` | your URL |
| | one per hosted provider | that provider's key variable | that provider's base URL |

A field names the variable the SDK already reads, so a value you exported in
your shell shows as configured without re-entering it, and a value entered
here is written to the same name (user settings + this process's environment;
API keys also land in the key store the model picker reads).

### Which method a bare model name uses

Precedence, highest first:

1. an explicit `backend=` / `base_url=`
2. `$MANTIS_AGENT_BASE_URL`
3. the family's **active method** — what you chose with `auth use` (or the
   dashboard), remembered in `~/.mantis-agent/models.json`
4. model-name inference (the table above)

If you never choose, the active method is the first *configured* one in the
order listed — API key, then subscription, then the clouds. Vertex and Bedrock
are the exception: they never activate on detection alone, because a working
`gcloud` login or `~/.aws` profile usually exists for unrelated reasons, and
silently billing Claude through a cloud you did not pick is worse than saying
"no API key". One `auth use claude bedrock` makes it active.

### How each cloud route is built

* **Claude on Vertex** — `POST https://{region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{region}/publishers/anthropic/models/{model}:streamRawPredict`,
  bearer ADC token, body carrying `anthropic_version: "vertex-2023-10-16"` and
  **no** `model` field (it is in the URL). Model ids take Vertex's `@`-dated
  spelling where it differs. Credentials: `GOOGLE_OAUTH_ACCESS_TOKEN`, else a
  service-account key at `GOOGLE_APPLICATION_CREDENTIALS` (exchanged with the
  RFC 7523 JWT-bearer grant), else `gcloud auth print-access-token`.
* **Claude on Bedrock** — `POST https://bedrock-runtime.{region}.amazonaws.com/model/{modelId}/invoke-with-response-stream`,
  signed with SigV4, body carrying `anthropic_version: "bedrock-2023-05-31"`.
  Model ids become cross-region inference profiles (`us.anthropic.…`). The
  response is an AWS event stream, decoded frame by frame. Credentials come
  from the environment, `~/.aws` (honouring `AWS_PROFILE`), or boto3 when it
  happens to be installed.
* **Claude on Azure AI Foundry** — the `/anthropic/v1` gateway path, which the
  native Messages adapter already handles; the key rides as `x-api-key`.
* **Azure OpenAI** — `api-key` header instead of `Authorization`, and either
  the `/openai/v1` surface or the classic
  `/openai/deployments/{deployment}/chat/completions?api-version=…` route,
  where the deployment name is what you pass as `model`.
* **Gemini on Vertex** — Vertex's OpenAI-compatible endpoint
  (`…/endpoints/openapi`) with an ADC bearer token, re-read per request so an
  hourly token refresh never 401s a long session.

### Extra request headers

Some endpoints authenticate with headers of their own rather than a key —
a Modal deployment behind proxy auth wants `Modal-Key` / `Modal-Secret`, a
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
variable is read as a JSON object — `mantis deploy … connect` exports it so
the terminal reaches a freshly deployed endpoint with no code change:

```bash
export MANTIS_AGENT_EXTRA_HEADERS='{"Modal-Key": "wk-…", "Modal-Secret": "ws-…"}'
```

Malformed JSON raises a `ValueError` naming the variable at `Agent(...)`
time rather than surfacing later as an opaque `401`. Values are kept out of
`repr` — they are usually secrets.

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
adapter handles cold starts and per-request keepalives. Proxy auth reads the
**proxy auth token** pair first — `MODAL_PROXY_TOKEN_ID` /
`MODAL_PROXY_TOKEN_SECRET` (`wk-…` / `ws-…`, what a web endpoint or Modal
Server checks) — and falls back to the **API token** pair `MODAL_TOKEN_ID` /
`MODAL_TOKEN_SECRET` (`ak-…` / `as-…`, what the `modal` CLI deploys with).
Both are sent as `Modal-Key` / `Modal-Secret`. A single `api_key` of the
joined form `wk-<id>.ws-<secret>` is sent as `Authorization: Bearer` instead,
and [`extra_headers`](#extra-request-headers) can carry the pair directly.

### OpenAI

`gpt-*` and the o-series, over `api.openai.com`. Reasoning models (gpt-5.x,
o1/o3/o4) get `max_completion_tokens` instead of `max_tokens`, no
`temperature` (they reject one), and the universal thinking config as
`reasoning_effort`; `effort="xhigh"` passes through, `"max"`/`"ultra"` clamp
to `high`. Streamed `reasoning` deltas surface as thinking blocks.

### Gemini

`gemini-*` over Google's OpenAI-compatible endpoint
(`generativelanguage.googleapis.com/v1beta/openai`). Effort words become
`reasoning_effort` (`low`/`medium`/`high`/`none`, which Google maps to
1k/8k/24k thinking tokens); an explicit `budget_tokens` is sent exactly as
`extra_body.google.thinking_config.thinking_budget`; `adaptive` with no
budget is Gemini's own dynamic default, so nothing is sent. Thought summaries
are opt-in — `extra={"extra_body": {"google": {"thinking_config":
{"include_thoughts": True}}}}` — and stream as thinking blocks.

### xAI Grok

`grok-*` over `api.x.ai/v1`, key in `XAI_API_KEY` (`GROK_API_KEY` is an
accepted alias). Only `grok-3-mini` takes `reasoning_effort` (`low`/`high`);
`grok-4`, `grok-3` and `grok-code-fast` reason at a fixed level and are never
sent the field. `reasoning_content` deltas surface as thinking blocks.

### Anthropic (Claude)

Real Claude over `/v1/messages`. Selected by a bare `claude-*` model name,
`backend="anthropic"`, an `api.anthropic.com` URL, or a gateway path ending
in `/anthropic` — which is how Bedrock Access Gateway, Azure Foundry, and
LiteLLM's Anthropic passthrough are reached. The thinking config follows the
model generation: Haiku 4.5 and ≤4.5 get `{"type": "enabled", "budget_tokens":
N}`; Opus 4.7/4.8/5 and Sonnet 5 get `{"type": "adaptive"}` plus
`output_config.effort` derived from the budget (they reject `budget_tokens`);
Fable/Mythos get effort only. Prompt caching is on by default.

### Mock

`backend="mock"` (or `MANTIS_AGENT_MOCK=1`) swaps in a scripted provider. Same
agent loop, no network — the way to test tool dispatch in CI.

## When it doesn't work

| Symptom | Cause | Fix |
|---|---|---|
| `Connection refused` on `localhost:8000` | a bare model name with no `backend` — detection defaulted to vLLM's port | pass `backend=`, or use `MantisAgentOptions` |
| `401` / `invalid api key` from the wrong provider | an unrelated `*_API_KEY` in the environment was picked up by the discovery chain | pass `api_key=` explicitly |
| `404 model not found` | the id isn't spelled the way that backend spells it | check the provider's model list; ids are not portable |
| `AuthError: ... needs credentials` on a `claude-*` model | no `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` in the environment | export one, or pass `api_key=` |
| `401` from `api.x.ai` | `XAI_API_KEY` (or `GROK_API_KEY`) not set, so the generic chain sent another vendor's key | export `XAI_API_KEY` |
| `AttributeError: 'SDKAssistantMessage' object has no attribute 'content'` | option shape and message shape mixed | dict → `msg.message.content`; typed → `msg.content` |
| an option seems to do nothing | unknown dict keys fall through to `Agent.extra` silently | check the key name against [MantisAgentOptions](../api/options.md) |
| `temperature` rejected as deprecated | some newer models refuse an explicit temperature | leave it unset; the default is suppressed per-provider |
