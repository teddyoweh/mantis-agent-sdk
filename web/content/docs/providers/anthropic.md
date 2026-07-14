# Claude (Anthropic)

Claude, natively — mantis speaks the real Messages API (x-api-key), not a translation layer. Gateways/OAuth work via Bearer tokens.

| | |
|---|---|
| endpoint | `https://api.anthropic.com/v1` |
| env var | `ANTHROPIC_API_KEY` |
| get a key | [console.anthropic.com](https://console.anthropic.com/settings/keys) |

## Get an API key

1. Sign in at console.anthropic.com
2. Open Settings → API Keys
3. Click 'Create Key' and name it
4. Copy the sk-ant- key (shown once)
5. Add credits under Settings → Billing

[Create a key ↗](https://console.anthropic.com/settings/keys) · [Pricing ↗](https://www.anthropic.com/pricing#api)

> No ongoing free tier; buy prepaid credits (small trial credit may apply).

## Enable

```bash
export ANTHROPIC_API_KEY=...        # shell profile — survives forever
```

or in the terminal — validates the key live before saving:

```
/enable anthropic
```

or pick any locked 🔒 Claude (Anthropic) model in `/models` and paste the key inline.

## Models

Starter menu (once enabled, `/models` fetches the provider's full live list):

- `claude-opus-4-8`
- `claude-sonnet-5`
- `claude-haiku-4-5-20251001`
- `claude-fable-5`

Switch anytime — `/model claude-opus-4` fuzzy-matches; context carries over.

## SDK

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(
    model="claude-opus-4-8",
    backend="https://api.anthropic.com/v1",     # key read from $ANTHROPIC_API_KEY
)
```

## Notes

- Behind a gateway or OAuth? Set `ANTHROPIC_AUTH_TOKEN` instead of the API key — mantis sends `Authorization: Bearer` and preserves your gateway URL.
- Tool definitions are converted to Anthropic's `input_schema` shape automatically.
