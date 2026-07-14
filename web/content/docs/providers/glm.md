# GLM (Zhipu)

Zhipu's official international endpoint for GLM — glm-4.7 is their flagship agentic model; glm-4-flash is the fast/cheap tier.

| | |
|---|---|
| endpoint | `https://api.z.ai/api/paas/v4` |
| env var | `ZHIPUAI_API_KEY` (aliases: `ZAI_API_KEY`, `ZHIPU_API_KEY`) |
| get a key | [z.ai](https://z.ai/model-api) |

## Get an API key

1. Sign up at z.ai (mainland users: open.bigmodel.cn)
2. Open the API Keys page from your account menu
3. Click 'Create a new API key'
4. Copy the key now — it's shown once
5. Set ZHIPUAI_API_KEY (base URL api.z.ai/api/paas/v4)

[Create a key ↗](https://z.ai/manage-apikey/apikey-list) · [Pricing ↗](https://docs.z.ai/guides/overview/pricing)

> Free Flash-tier models; full GLM API is pay-per-token (trial credits for new users).

## Enable

```bash
export ZHIPUAI_API_KEY=...        # shell profile — survives forever
```

or in the terminal — validates the key live before saving:

```
/enable glm
```

or pick any locked 🔒 GLM (Zhipu) model in `/models` and paste the key inline.

## Models

Starter menu (once enabled, `/models` fetches the provider's full live list):

- `glm-4.7`
- `glm-4.6`
- `glm-4-plus`
- `glm-4-flash`

Switch anytime — `/model glm-4.7` fuzzy-matches; context carries over.

## SDK

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(
    model="glm-4.7",
    backend="https://api.z.ai/api/paas/v4",     # key read from $ZHIPUAI_API_KEY
)
```

## Notes

- `ZAI_API_KEY` and `ZHIPU_API_KEY` work as aliases.
- glm-4.7 full-size is the model that's impractical to self-host — this endpoint is the sane way to run it.
