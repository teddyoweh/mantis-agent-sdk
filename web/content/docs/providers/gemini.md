# Gemini

Google's Gemini via the OpenAI-compatible surface of the Generative Language API. AI Studio keys come with a free quota.

| | |
|---|---|
| endpoint | `https://generativelanguage.googleapis.com/v1beta/openai` |
| env var | `GEMINI_API_KEY` (aliases: `GOOGLE_API_KEY`) |
| get a key | [Google AI Studio](https://aistudio.google.com/apikey) |

## Enable

```bash
export GEMINI_API_KEY=...        # shell profile — survives forever
```

or in the terminal — validates the key live before saving:

```
/enable gemini
```

or pick any locked 🔒 Gemini model in `/models` and paste the key inline.

## Models

Starter menu (once enabled, `/models` fetches the provider's full live list):

- `gemini-2.5-pro`
- `gemini-2.5-flash`
- `gemini-2.0-flash`

Switch anytime — `/model gemini-2.5` fuzzy-matches; context carries over.

## SDK

```python
from mantis_agent import MantisAgentOptions
options = MantisAgentOptions(
    model="gemini-2.5-pro",
    backend="https://generativelanguage.googleapis.com/v1beta/openai",     # key read from $GEMINI_API_KEY
)
```

## Notes

- `GOOGLE_API_KEY` works too (alias).
- AI Studio keys have a free quota on flash models — a zero-cost way to try mantis on a hosted model.
