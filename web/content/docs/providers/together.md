# Together

A broad, reliable open-model menu (GLM, DeepSeek, Llama, gpt-oss) with dedicated-capacity options when you outgrow shared.

| | |
|---|---|
| endpoint | `https://api.together.xyz/v1` |
| env var | `TOGETHER_API_KEY` |
| get a key | [api.together.xyz](https://api.together.xyz/settings/api-keys) |

## Get an API key

1. Create an account at api.together.xyz
2. Open Settings → API Keys
3. Click 'Create key' and name it
4. Copy the key value (shown only once)
5. Set TOGETHER_API_KEY

[Create a key ↗](https://api.together.xyz/settings/api-keys) · [Pricing ↗](https://www.together.ai/pricing)

> Small starter credit for new accounts; ~$5 top-up for continued use.

## Enable

```bash
export TOGETHER_API_KEY=...        # shell profile — survives forever
```

or in the terminal — validates the key live before saving:

```
/enable together
```

or pick any locked 🔒 Together model in `/models` and paste the key inline.

## Models

Starter menu (once enabled, `/models` fetches the provider's full live list):

- `openai/gpt-oss-120b`
- `zai-org/GLM-4.7`
- `deepseek-ai/DeepSeek-V3`
- `meta-llama/Llama-3.3-70B-Instruct-Turbo`

Switch anytime — `/model gpt-oss-120b` fuzzy-matches; context carries over.

## SDK

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(
    model="openai/gpt-oss-120b",
    backend="https://api.together.xyz/v1",     # key read from $TOGETHER_API_KEY
)
```

## Notes

- Ids are HF-style (`zai-org/GLM-4.7`, `deepseek-ai/DeepSeek-V3`).
