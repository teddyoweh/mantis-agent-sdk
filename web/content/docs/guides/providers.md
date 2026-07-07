# Getting provider access

Every hosted provider mantis ships in the catalog: where to get the key, the
env var it reads, and the one-liner to turn it on. A provider is **enabled**
the moment mantis can find its key — env var or saved via `/enable`
(stored `chmod 600` in `~/.mantis-agent/models.json`).

Two ways to enable anything:

```bash
export DEEPSEEK_API_KEY=sk-...       # env — survives via your shell profile
```
```
/enable deepseek                     # in-app — prompts for the key, validates, saves
```

…or just pick a locked 🔒 model in `/models` and paste the key when asked.

## Open-model hosts

| provider | get a key | env var | notes |
|---|---|---|---|
| **[DeepSeek](/docs/providers/deepseek)** | [platform.deepseek.com](https://platform.deepseek.com/api_keys) | `DEEPSEEK_API_KEY` | dirt-cheap V3/R1, official host |
| **[Moonshot (Kimi)](/docs/providers/moonshot)** | [platform.moonshot.ai](https://platform.moonshot.ai/console/api-keys) | `MOONSHOT_API_KEY` | kimi-k2 — top-tier tool calling |
| **[Z.ai (GLM)](/docs/providers/glm)** | [z.ai/model-api](https://z.ai/model-api) | `ZHIPUAI_API_KEY` (or `ZAI_API_KEY`/`ZHIPU_API_KEY`) | glm-4.7 official host |
| **[Alibaba (Qwen)](/docs/providers/qwen)** | [Model Studio](https://modelstudio.console.alibabacloud.com/?tab=playground#/api-key) | `DASHSCOPE_API_KEY` (or `QWEN_API_KEY`) | qwen-max / qwen3 international endpoint |
| **[Groq](/docs/providers/groq)** | [console.groq.com/keys](https://console.groq.com/keys) | `GROQ_API_KEY` | free tier; absurdly fast gpt-oss + kimi |
| **[OpenRouter](/docs/providers/openrouter)** | [openrouter.ai/keys](https://openrouter.ai/settings/keys) | `OPENROUTER_API_KEY` | one key, ~every model; `:free` variants |
| **[Together](/docs/providers/together)** | [api.together.xyz](https://api.together.xyz/settings/api-keys) | `TOGETHER_API_KEY` | broad OSS menu |
| **[Fireworks](/docs/providers/fireworks)** | [fireworks.ai](https://app.fireworks.ai/settings/users/api-keys) | `FIREWORKS_API_KEY` | fast OSS serving |
| **[Cerebras](/docs/providers/cerebras)** | [cloud.cerebras.ai](https://cloud.cerebras.ai/platform/) | `CEREBRAS_API_KEY` | free tier; fastest tokens/s anywhere |

## Closed models

| provider | get a key | env var | notes |
|---|---|---|---|
| **[OpenAI](/docs/providers/openai)** | [platform.openai.com](https://platform.openai.com/api-keys) | `OPENAI_API_KEY` | gpt-5.x — mantis handles the `max_completion_tokens`/temperature quirks |
| **[Anthropic](/docs/providers/anthropic)** | [console.anthropic.com](https://console.anthropic.com/settings/keys) | `ANTHROPIC_API_KEY` | Claude via the native Messages API |
| **Anthropic (gateway/OAuth)** | your gateway | `ANTHROPIC_AUTH_TOKEN` | `Authorization: Bearer` instead of x-api-key — LiteLLM/proxy setups |
| **[Google (Gemini)](/docs/providers/gemini)** | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | free tier via AI Studio |

## Free ways to start (no card)

1. **Ollama** — local, free forever: `mantis setup` pulls a model for you.
2. **Groq / Cerebras free tiers** — hosted OSS with generous limits.
3. **OpenRouter `:free` models** — e.g. `meta-llama/llama-3.3-70b-instruct:free`.
4. **Gemini via AI Studio** — free quota on 2.5-flash.

## How enablement actually works

- Env vars win over saved keys; alias vars (`GOOGLE_API_KEY`,
  `ZAI_API_KEY`, `QWEN_API_KEY`) are honored.
- Keys/URLs are whitespace-stripped and validated on save — `/enable` does a
  live `/models` probe and refuses to store a bad key.
- `/models` groups by provider: enabled ones first, locked 🔒 ones below so
  you can see the whole menu and enable inline.
- `/disable <provider>` forgets a saved key.
- Switching models mid-session (`/model kimi`) re-wires the backend + key
  automatically; your session context carries over.

Self-hosting instead? See [Self-hosting models](/docs/guides/self-hosting).
