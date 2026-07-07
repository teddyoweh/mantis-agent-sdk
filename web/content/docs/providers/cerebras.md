# Cerebras

Wafer-scale hardware; the fastest tokens/sec on the market, with a free tier. Small catalog, extreme speed.

| | |
|---|---|
| endpoint | `https://api.cerebras.ai/v1` |
| env var | `CEREBRAS_API_KEY` |
| get a key | [cloud.cerebras.ai](https://cloud.cerebras.ai/platform/) |

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
