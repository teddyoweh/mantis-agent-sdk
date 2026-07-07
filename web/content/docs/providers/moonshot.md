# Kimi (Moonshot)

Moonshot's Kimi models — kimi-k2 is one of the strongest tool-calling open models in existence. This is its official home.

| | |
|---|---|
| endpoint | `https://api.moonshot.ai/v1` |
| env var | `MOONSHOT_API_KEY` |
| get a key | [platform.moonshot.ai](https://platform.moonshot.ai/console/api-keys) |

## Enable

```bash
export MOONSHOT_API_KEY=...        # shell profile — survives forever
```

or in the terminal — validates the key live before saving:

```
/enable moonshot
```

or pick any locked 🔒 Kimi (Moonshot) model in `/models` and paste the key inline.

## Models

Starter menu (once enabled, `/models` fetches the provider's full live list):

- `kimi-latest`
- `kimi-k2-0905-preview`
- `moonshot-v1-128k`
- `moonshot-v1-32k`

Switch anytime — `/model kimi-latest` fuzzy-matches; context carries over.

## SDK

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(
    model="kimi-latest",
    backend="https://api.moonshot.ai/v1",     # key read from $MOONSHOT_API_KEY
)
```

## Notes

- kimi-k2 is a top pick for agentic/tool-heavy work.
- `kimi-latest` tracks their newest snapshot.
