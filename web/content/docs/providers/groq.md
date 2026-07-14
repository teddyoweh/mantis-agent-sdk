# Groq

Custom LPU silicon serving open models at absurd token speeds, with a generous free tier — the fastest way to *feel* gpt-oss or Kimi.

| | |
|---|---|
| endpoint | `https://api.groq.com/openai/v1` |
| env var | `GROQ_API_KEY` |
| get a key | [console.groq.com](https://console.groq.com/keys) |

## Get an API key

1. Sign up at console.groq.com and verify your email
2. Open 'API Keys', click 'Create API Key'
3. Name the key and submit
4. Copy it immediately, then set GROQ_API_KEY

[Create a key ↗](https://console.groq.com/keys) · [Pricing ↗](https://groq.com/pricing)

> Free tier, no credit card — all models with per-minute/daily limits.

## Enable

```bash
export GROQ_API_KEY=...        # shell profile — survives forever
```

or in the terminal — validates the key live before saving:

```
/enable groq
```

or pick any locked 🔒 Groq model in `/models` and paste the key inline.

## Models

Starter menu (once enabled, `/models` fetches the provider's full live list):

- `openai/gpt-oss-120b`
- `openai/gpt-oss-20b`
- `moonshotai/kimi-k2-instruct-0905`
- `qwen/qwen3-32b`
- `llama-3.3-70b-versatile`

Switch anytime — `/model gpt-oss-120b` fuzzy-matches; context carries over.

## SDK

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(
    model="openai/gpt-oss-120b",
    backend="https://api.groq.com/openai/v1",     # key read from $GROQ_API_KEY
)
```

## Notes

- Free tier is rate-limited but real — great for evaluation.
- Ids are namespaced (`openai/gpt-oss-120b`, `qwen/qwen3-32b`).
