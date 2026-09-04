# Grok (xAI)

xAI's Grok models over their OpenAI-compatible API. A bare `grok-*` model name is all mantis needs — it routes to `api.x.ai` on its own and maps the reasoning knob per model.

| | |
|---|---|
| endpoint | `https://api.x.ai/v1` |
| env var | `XAI_API_KEY` (alias: `GROK_API_KEY`) |
| get a key | [console.x.ai](https://console.x.ai) |

## Get an API key

1. Sign in at console.x.ai with your X or xAI account
2. Open the API Keys page and click 'Create API key'
3. Name it and copy the `xai-…` key (shown once)
4. Add credits under Billing

[Create a key ↗](https://console.x.ai) · [Pricing ↗](https://docs.x.ai/docs/models)

> Pay-as-you-go; new accounts usually get a small trial credit.

## Enable

```bash
export XAI_API_KEY=xai-...        # shell profile — survives forever
```

or in the terminal — validates the key live before saving:

```
/enable xai
```

or pick any locked 🔒 Grok (xAI) model in `/models` and paste the key inline.

## Models

Starter menu (once enabled, `/models` fetches the provider's full live list):

- `grok-4` — 256k window, reasoning always on
- `grok-4-fast` — 2M window
- `grok-3`
- `grok-3-mini` — the one that takes `reasoning_effort` (`low` / `high`)

Switch anytime — `/model grok` jumps to the flagship, `/model grok-3` fuzzy-matches; context carries over.

## SDK

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(model="grok-4")      # routes to api.x.ai; key read from $XAI_API_KEY
```

The explicit form is the same as any other hosted provider:

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(
    model="grok-4",
    backend="https://api.x.ai/v1",
)
```

## Notes

- `GROK_API_KEY` works too (alias). The vendor key wins by host, so a stale `OPENAI_API_KEY` in the same shell is never sent to xAI.
- `reasoning_effort` is sent only to the models that accept it — `grok-3-mini` — and the universal [`thinking`](/docs/guides/thinking) config maps onto it. `grok-4`, `grok-3` and `grok-code-fast` reason at a fixed level and are never sent the field.
- Streamed `reasoning_content` deltas surface as thinking blocks, collapsed to one line in the terminal (`/thinking show` expands them).
- Native tool calling on every model; xAI bills cached prompt tokens at a discount, and mantis keeps the prompt prefix stable to make use of it.
