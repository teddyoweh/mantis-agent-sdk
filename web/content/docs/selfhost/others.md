# Other platforms

Same pattern everywhere — *credentials → machine → `vllm serve` → `/connect`* —
these just have console-driven flows we won't reproduce click-by-click:

- **[Replicate](https://replicate.com)** ([API tokens](https://replicate.com/account/api-tokens),
  `REPLICATE_API_TOKEN`) — strongest for *their* hosted model catalog; custom
  serving means packaging with [cog](https://github.com/replicate/cog).
- **[Baseten](https://www.baseten.co)** ([API keys](https://app.baseten.co/settings/api_keys),
  `BASETEN_API_KEY`) — deploy via their model library or
  [Truss](https://docs.baseten.co); dedicated deployments expose
  OpenAI-compatible endpoints.
- **[Beam](https://www.beam.cloud)** (dashboard token) — Modal-style Python
  function deploys; adapt the [Modal recipe](/docs/selfhost/modal).
- **[Paperspace](https://www.paperspace.com)** (console API keys) — rent a
  machine, follow the [Lambda recipe](/docs/selfhost/lambda) verbatim.
- **AWS / GCP / Azure** — a GPU VM + vLLM behind your VPN; the engineering is
  identical to the [Lambda recipe](/docs/selfhost/lambda), the IAM is yours.
- **Agent sandboxes** — [Daytona](https://app.daytona.io/dashboard/keys)
  (`DAYTONA_API_KEY`), [E2B](https://e2b.dev/docs) (`E2B_API_KEY`): for running
  agent *code* safely, not serving weights; pair with any endpoint above.

Local runtimes (Ollama, llama.cpp, LM Studio) intentionally have **no pages
here** — `mantis setup` automates them end-to-end.
