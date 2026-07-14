# OpenAI

gpt-5.x. mantis handles this API's quirks for you — `max_completion_tokens` vs `max_tokens` and locked temperature are auto-negotiated.

| | |
|---|---|
| endpoint | `https://api.openai.com/v1` |
| env var | `OPENAI_API_KEY` |
| get a key | [platform.openai.com](https://platform.openai.com/api-keys) |

## Get an API key

1. Sign in at platform.openai.com
2. Open the API keys page
3. Click 'Create new secret key'
4. Name it and copy the key (shown once)
5. Add funds under Settings → Billing

[Create a key ↗](https://platform.openai.com/api-keys) · [Pricing ↗](https://openai.com/api/pricing)

> No free tier; pay-as-you-go needs a prepaid funded balance.

## Enable

```bash
export OPENAI_API_KEY=...        # shell profile — survives forever
```

or in the terminal — validates the key live before saving:

```
/enable openai
```

or pick any locked 🔒 OpenAI model in `/models` and paste the key inline.

## Models

Starter menu (once enabled, `/models` fetches the provider's full live list):

- `gpt-5.4`
- `gpt-5.4-mini`
- `gpt-5.4-nano`
- `gpt-5.4-pro`

Switch anytime — `/model gpt-5.4` fuzzy-matches; context carries over.

## SDK

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(
    model="gpt-5.4",
    backend="https://api.openai.com/v1",     # key read from $OPENAI_API_KEY
)
```

## Notes

- gpt-5.x rejects `max_tokens` and non-default temperature; mantis retries with the right parameters automatically.
- Realtime/audio/codex models are filtered out of `/models` — only chat models show.
