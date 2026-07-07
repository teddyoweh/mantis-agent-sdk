# DeepSeek

The official host for DeepSeek's own models — V3 for chat/agents, R1 for deep reasoning — at some of the lowest prices anywhere.

| | |
|---|---|
| endpoint | `https://api.deepseek.com/v1` |
| env var | `DEEPSEEK_API_KEY` |
| get a key | [platform.deepseek.com](https://platform.deepseek.com/api_keys) |

## Enable

```bash
export DEEPSEEK_API_KEY=...        # shell profile — survives forever
```

or in the terminal — validates the key live before saving:

```
/enable deepseek
```

or pick any locked 🔒 DeepSeek model in `/models` and paste the key inline.

## Models

Starter menu (once enabled, `/models` fetches the provider's full live list):

- `deepseek-chat`
- `deepseek-reasoner`

Switch anytime — `/model deepseek-cha` fuzzy-matches; context carries over.

## SDK

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(
    model="deepseek-chat",
    backend="https://api.deepseek.com/v1",     # key read from $DEEPSEEK_API_KEY
)
```

## Notes

- `deepseek-reasoner` (R1) returns thinking tokens; mantis renders them as thinking blocks.
- Prices are per-token among the lowest of any frontier-adjacent model.
