# OpenRouter

One key, essentially every model on the market — plus `:free` variants of many open models. The best single-key starting point.

| | |
|---|---|
| endpoint | `https://openrouter.ai/api/v1` |
| env var | `OPENROUTER_API_KEY` |
| get a key | [openrouter.ai](https://openrouter.ai/settings/keys) |

## Enable

```bash
export OPENROUTER_API_KEY=...        # shell profile — survives forever
```

or in the terminal — validates the key live before saving:

```
/enable openrouter
```

or pick any locked 🔒 OpenRouter model in `/models` and paste the key inline.

## Models

Starter menu (once enabled, `/models` fetches the provider's full live list):

- `openai/gpt-oss-120b`
- `z-ai/glm-4.7`
- `moonshotai/kimi-k2`
- `deepseek/deepseek-chat`
- `qwen/qwen3-235b-a22b`

Switch anytime — `/model gpt-oss-120b` fuzzy-matches; context carries over.

## SDK

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(
    model="openai/gpt-oss-120b",
    backend="https://openrouter.ai/api/v1",     # key read from $OPENROUTER_API_KEY
)
```

## Notes

- Try `:free` model variants (e.g. `meta-llama/llama-3.3-70b-instruct:free`) before spending anything.
- Model ids are namespaced (`z-ai/glm-4.7`, `moonshotai/kimi-k2`).
