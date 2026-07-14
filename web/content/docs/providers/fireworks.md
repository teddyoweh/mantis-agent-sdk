# Fireworks

Fast open-model serving with fine-grained model versions — note the long `accounts/fireworks/models/...` ids are normal.

| | |
|---|---|
| endpoint | `https://api.fireworks.ai/inference/v1` |
| env var | `FIREWORKS_API_KEY` |
| get a key | [app.fireworks.ai](https://app.fireworks.ai/settings/users/api-keys) |

## Get an API key

1. Sign up or log in at app.fireworks.ai
2. Open Settings → API Keys
3. Click 'Create API Key'
4. Copy the key and store it securely
5. Export it as FIREWORKS_API_KEY

[Create a key ↗](https://app.fireworks.ai/settings/users/api-keys) · [Pricing ↗](https://fireworks.ai/pricing)

> New accounts get $1 free credit (10 req/min without a payment method).

## Enable

```bash
export FIREWORKS_API_KEY=...        # shell profile — survives forever
```

or in the terminal — validates the key live before saving:

```
/enable fireworks
```

or pick any locked 🔒 Fireworks model in `/models` and paste the key inline.

## Models

Starter menu (once enabled, `/models` fetches the provider's full live list):

- `accounts/fireworks/models/gpt-oss-120b`
- `accounts/fireworks/models/deepseek-v3`
- `accounts/fireworks/models/qwen3-235b-a22b`

Switch anytime — `/model gpt-oss-120b` fuzzy-matches; context carries over.

## SDK

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(
    model="accounts/fireworks/models/gpt-oss-120b",
    backend="https://api.fireworks.ai/inference/v1",     # key read from $FIREWORKS_API_KEY
)
```

## Notes

- Ids look like `accounts/fireworks/models/gpt-oss-120b` — paste them whole; `/model oss` fuzzy-matches fine.
