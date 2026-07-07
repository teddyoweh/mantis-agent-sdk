# Qwen (DashScope)

Alibaba's international (Singapore) endpoint for the Qwen family — qwen-max, qwen3, and the coder line, served by the people who train them.

| | |
|---|---|
| endpoint | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| env var | `DASHSCOPE_API_KEY` (aliases: `QWEN_API_KEY`) |
| get a key | [Alibaba Model Studio](https://modelstudio.console.alibabacloud.com/?tab=playground#/api-key) |

## Enable

```bash
export DASHSCOPE_API_KEY=...        # shell profile — survives forever
```

or in the terminal — validates the key live before saving:

```
/enable qwen
```

or pick any locked 🔒 Qwen (DashScope) model in `/models` and paste the key inline.

## Models

Starter menu (once enabled, `/models` fetches the provider's full live list):

- `qwen-max`
- `qwen-plus`
- `qwen3-235b-a22b`
- `qwen3-coder-plus`

Switch anytime — `/model qwen-max` fuzzy-matches; context carries over.

## SDK

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(
    model="qwen-max",
    backend="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",     # key read from $DASHSCOPE_API_KEY
)
```

## Notes

- `QWEN_API_KEY` works as an alias.
- This is the **international** endpoint; mainland DashScope keys belong to a different console.
