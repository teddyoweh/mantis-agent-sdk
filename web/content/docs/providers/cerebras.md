# Cerebras

Wafer-scale hardware; the fastest tokens/sec on the market, with a free tier. Small catalog, extreme speed.

| | |
|---|---|
| endpoint | `https://api.cerebras.ai/v1` |
| env var | `CEREBRAS_API_KEY` |
| get a key | [cloud.cerebras.ai](https://cloud.cerebras.ai/platform/) |

## Get an API key

1. Sign up or log in at cloud.cerebras.ai
2. Click 'API Keys' in the left nav
3. Click 'Create API Key' and name it
4. Copy the key and store it securely
5. Export it as CEREBRAS_API_KEY

[Create a key ↗](https://cloud.cerebras.ai/platform/apikeys) · [Pricing ↗](https://www.cerebras.ai/pricing)

> Free dev tier: 1M tokens/day, no card, ~30 req/min (8k context cap).

## Enable

```bash
export CEREBRAS_API_KEY=...        # shell profile — survives forever
```

or in the terminal — validates the key live before saving:

```
/enable cerebras
```

or pick any locked 🔒 Cerebras model in `/models` and paste the key inline.

## Models

Starter menu (once enabled, `/models` fetches the provider's full live list):

- `gpt-oss-120b`
- `zai-glm-4.7`
- `llama-3.3-70b`
- `gemma-4-31b`

Switch anytime — `/model gpt-oss-120b` fuzzy-matches; context carries over.

## SDK

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(
    model="gpt-oss-120b",
    backend="https://api.cerebras.ai/v1",     # key read from $CEREBRAS_API_KEY
)
```

## Notes

- Free tier available.
- Catalog is small but every model streams at 1000+ tok/s.
