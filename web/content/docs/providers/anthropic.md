# Claude (Anthropic)

Claude is a first-class provider: a bare `claude-*` model name routes to the real Messages API (`/v1/messages`, not a translation layer), authenticated by an API key **or a Claude subscription login**. Gateways — Bedrock Access Gateway, Azure Foundry, LiteLLM — work on any `/anthropic/v1` path.

| | |
|---|---|
| endpoint | `https://api.anthropic.com/v1` |
| env var | `ANTHROPIC_API_KEY` (API key) · `ANTHROPIC_AUTH_TOKEN` (subscription OAuth token or gateway Bearer) |
| get a key | [console.anthropic.com](https://console.anthropic.com/settings/keys) |

## Get an API key

1. Sign in at console.anthropic.com
2. Open Settings → API Keys
3. Click 'Create Key' and name it
4. Copy the `sk-ant-` key (shown once)
5. Add credits under Settings → Billing

[Create a key ↗](https://console.anthropic.com/settings/keys) · [Pricing ↗](https://www.anthropic.com/pricing#api)

> No ongoing free tier; buy prepaid credits (a small trial credit may apply).

## Or use your Claude subscription

If you already pay for Claude, you don't need an API key. Paste the OAuth token from your Claude login (`sk-ant-oat…`) into `/enable anthropic` — mantis recognises the shape, stores it as `ANTHROPIC_AUTH_TOKEN`, sends it as `Authorization: Bearer` with the Claude Code identity Anthropic expects, and Opus, Sonnet and Haiku all answer on your subscription's usage window. The dashboard and the terminal footer show the source as **OAuth token**.

## Enable

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # shell profile — survives forever
# or, for a subscription / gateway token:
export ANTHROPIC_AUTH_TOKEN=sk-ant-oat...
```

or in the terminal — detects key vs. token by shape and validates before saving:

```
/enable anthropic
```

or pick any locked 🔒 Claude (Anthropic) model in `/models` and paste either credential inline.

## Models

Starter menu (once enabled, `/models` fetches the provider's full live list):

- `claude-opus-5`
- `claude-sonnet-5`
- `claude-haiku-4-5`
- `claude-fable-5-1`

Switch anytime — `/model claude` jumps to the flagship, `/model opus` / `/model sonnet` / `/model haiku` resolve a tier; context carries over.

## SDK

No `backend` needed — the name selects the native adapter:

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(model="claude-opus-5")     # $ANTHROPIC_API_KEY or $ANTHROPIC_AUTH_TOKEN
```

The explicit forms say the same thing, or point the adapter at a gateway:

```python
from mantis_agent import MantisAgentOptions
direct = MantisAgentOptions(model="claude-opus-5", backend="anthropic")
gateway = MantisAgentOptions(model="claude-opus-5", backend="https://gateway.example/anthropic/v1")
```

## Notes

- **Thinking follows the model generation.** The universal [`thinking`](/docs/guides/thinking) config becomes `budget_tokens` on Haiku 4.5 and older, `adaptive` plus `output_config.effort` on Opus 4.7+/5 and Sonnet 5 (they reject `budget_tokens`), and effort only on Fable. An unknown id is retried once with the other form, and `temperature` is dropped where sampling was removed.
- **Prompt caching is on by default**, with the cache breakpoint on the last block so the whole prefix is reused.
- Tool definitions are converted to Anthropic's `input_schema` shape automatically; structured output (`response_model` / `response_format`) uses the native envelope.
- Bedrock and Vertex credentials are understood too — paste an AWS key or a GCP token and the request is signed for the regional host.
- A `429` with an OAuth token is a spent usage window on your subscription, not a missing entitlement; wait it out or switch to an API key.
