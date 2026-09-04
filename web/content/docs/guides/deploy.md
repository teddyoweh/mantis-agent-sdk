# Deploy — bring your own GPU provider

Add a GPU cloud credential once, then deploy **any open-weight model** as an
OpenAI-compatible endpoint — from the [dashboard](dashboard.md#deploy), the
`mantis-agent deploy` CLI, or `/deploy` inside the terminal — and use it
immediately. Six providers ship in the box:

| provider | id | credential | scale to zero | endpoint auth | engines |
|---|---|---|---|---|---|
| **RunPod Serverless** | `runpod` | `RUNPOD_API_KEY` ([console](https://console.runpod.io/serverless)) | yes | your RunPod key as Bearer | vLLM |
| **Hugging Face Inference Endpoints** | `hf` | `HF_TOKEN` with write scope ([console](https://ui.endpoints.huggingface.co)) | yes | your HF token (endpoints are created *protected*) | vLLM, SGLang, TGI, llama.cpp |
| **Modal** | `modal` | `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`, optional proxy pair `MODAL_PROXY_TOKEN_ID` / `MODAL_PROXY_TOKEN_SECRET` ([console](https://modal.com/apps)) | yes | proxy token as `Modal-Key` / `Modal-Secret`, else a generated key | vLLM, SGLang |
| **DeepInfra** | `deepinfra` | `DEEPINFRA_API_KEY` ([console](https://deepinfra.com/dash/deployments)) | yes | your DeepInfra key; `model="deploy_id:<id>"` | vLLM |
| **Baseten** | `baseten` | `BASETEN_API_KEY` ([console](https://app.baseten.co/models)) | yes | your Baseten key | vLLM, SGLang |
| **Vast.ai** | `vastai` | `VAST_API_KEY`, optional `HF_TOKEN` ([console](https://cloud.vast.ai/manage-keys/)) | **no** — bills every hour it exists | a generated key, **plain HTTP** on a public IP | vLLM, SGLang |

Every one of them ends the same way: an endpoint URL ending in `/v1`, the
model name the endpoint answers to, and the env var that authenticates it.
Once a deployment is `running` the rest of mantis treats it like any other
backend. If you'd rather run the cloud by hand, the
[self-hosting pages](self-hosting.md) walk each provider's console.

## The three-minute path

```bash
mantis-agent deploy providers                                 # what's registered, what's configured
mantis-agent deploy creds runpod --set RUNPOD_API_KEY=...      # once — validated over the network
mantis-agent deploy models qwen3                              # search the HF Hub: params · dtype · VRAM · vLLM-ok
mantis-agent deploy inspect Qwen/Qwen3-32B                    # pre-flight one model
mantis-agent deploy gpus runpod --min-vram 48                 # the catalogue, cheapest first
mantis-agent deploy up runpod Qwen/Qwen3-32B --gpu AMPERE_80  # pre-flight, deploy, wait, print the endpoint
mantis-agent deploy connect <id>                              # (done for you by `up` unless --no-connect)
mantis                                                        # the terminal now runs on it
```

Nothing runs through a mantis server: your key talks to the provider, the
weights go straight from the Hub to the provider, and the endpoint is yours.

Or open `mantis serve` → **Deploy**: pick a model, see which GPUs fit and
what they cost per hour, click deploy, watch it come up, click **Use this
model**. Or, without leaving a session, `/deploy` in the terminal — the same
grammar as the CLI, with `up` running as a background job and a **Use it
now?** prompt when the endpoint is ready (see
[the terminal guide](terminal.md#deploy--bring-your-own-gpu)).

## The CLI

`mantis-agent deploy <action>`; every action takes `--json` for scripts.

| Action | What it does |
|---|---|
| `providers` | Every adapter, whether it's configured, its engines, console URL, scale-to-zero and public-endpoint flags |
| `creds <provider> [--set ENV=value …]` | Show a provider's credential fields, or save them (repeatable `--set`), validate over the network, print the account (balance / credits where the provider reports one) |
| `gpus <provider> [--min-vram GB]` | The GPU catalogue with prices, cheapest first |
| `models [query] [--sort trending\|downloads\|likes] [--limit N]` | Search the Hub for text-generation models; a curated list of good first deploys when the query is empty |
| `inspect <model>` | Pre-flight one model: architectures, params, dtype, licence, gated, vLLM servability, VRAM estimate. `model` is an HF id or `ollama:<tag>` |
| `up <provider> <model> --gpu <id>` | Deploy. `--engine vllm\|sglang\|tgi\|llamacpp`, `--max-model-len N`, `--tp N`, `--min 0`, `--max 1`, `--idle 300`, `--name X`, `--served-name X`, `--quantization fp8\|awq\|…`, `--trust-remote-code`, `--hf-token …`, `--no-wait`, `--force`, `--no-connect` |
| `ls [--refresh] [--provider ID]` | Stored deployments; `--refresh` re-queries every provider and **adopts** endpoints made elsewhere (the console, an earlier machine) |
| `status <id> [--no-refresh]` | One deployment, refreshed from the provider |
| `logs <id> [--tail N]` | Recent log lines (providers without a logs API say so) |
| `connect <id>` | Verify `GET /v1/models` answers (riding out cold-start 503s), make it the current model for the SDK and the terminal, print the shell and Python lines |
| `down <id> [--yes]` | Tear it down on the provider (asks first, names the hourly cost it stops) |

`up` waits until the endpoint is `running` and then connects it unless you
say `--no-wait` / `--no-connect`; the wait tolerates the 503s that Modal, HF
Endpoints and friends return while a cold replica boots.

### GPU ids per provider

`--gpu` takes the provider's own id, exactly as `deploy gpus` prints it:

- **RunPod** — GPU *pools*: `AMPERE_80` (A100 80 GB), `ADA_80_PRO` (H100),
  `ADA_24` (L4 / 4090), `HOPPER_141` (H200), `BLACKWELL_180` (B200). Prices
  are the live serverless flex rate; the endpoint is
  `https://api.runpod.ai/v2/<id>/openai/v1`, authenticated with the same key.
- **HF Inference Endpoints** — `vendor/region/type/size`, e.g.
  `aws/us-east-1/nvidia-a100/x1`. The endpoint is created *protected* (your
  token is the auth) and the repo is mounted server-side. Set
  `HF_ENDPOINTS_NAMESPACE` to deploy into an org.
- **Modal** — `L4`, `A100-40`, `A100-80`, `H100`. Endpoint auth is the
  proxy-token pair when you saved one, else a generated key.
- **DeepInfra** — the five strings DeepInfra accepts (`A100-80GB`,
  `H100-80GB`, `H200-141GB`, `B200-180GB`, `B300-288GB`); `H100-80GB:2` for
  multi-GPU, four max. Inference goes to the shared
  `https://api.deepinfra.com/v1/openai` with `model="deploy_id:<id>"`; there
  is no logs API.
- **Baseten** — accelerators (`A10G`, `A100`, `H100`, `H100:2`, `H200`,
  `B200`). The first build takes 5–10 minutes (a Truss config with the
  upstream vLLM image and `weights: hf://…`); later cold starts are ~10 s
  plus engine load.
- **Vast.ai** — an *offer* id from the marketplace search; you rent that
  host until you `down` it.

## Pre-flight, and what "fits" means

Before anything is created, `inspect` (and `up`) reads the model from the
Hub: architectures, parameter count, dominant safetensors dtype, licence,
gated flag, context length — plus a **vLLM servability** verdict from the
architecture list (`?` for an architecture vLLM hasn't listed, which usually
still runs through its Transformers backend). Ollama tags work too:
`inspect ollama:qwen3:8b`.

VRAM is estimated as weights plus KV cache: `params × bytes-per-param` (2 for
bf16, 1 for fp8/int8, ~0.55 for 4-bit — a checkpoint that is already FP8
sizes itself correctly, and an MoE counts every expert) plus one sequence of
KV cache at the chosen context. vLLM keeps 90% of the card and needs ~1.5 GB
for activations, so `need ≤ 0.9 × VRAM` is the bar; under 15% headroom is
**tight** (reduce `--max-model-len`), over is **fits**, and anything else is
**no** with the reason. `up` refuses a **no** unless you pass `--force`.

## What a deployment remembers

Every deployment lands in `~/.mantis-agent/deployments.json` with its
provider, model, GPU, status, and the three things the SDK needs:

- **`endpoint_url`** — the OpenAI base URL *including* `/v1`.
- **`served_model_name`** — what goes in `model=`. This is the one thing that
  differs per provider: RunPod and HF want the HF id, DeepInfra wants
  `deploy_id:<id>`, and you can override it with `--served-name`. Callers
  never guess.
- **`auth_env`** / **`auth_headers`** — the *name* of the env var whose value
  is sent as `Authorization: Bearer`, or the header pair (Modal's
  `Modal-Key` / `Modal-Secret` as `${MODAL_PROXY_TOKEN_ID}`-style env refs).
  The file never holds a secret.

So the SDK side is two fields:

```python
from mantis_agent import MantisAgentOptions
from mantis_agent.deploy import list_deployments


async def options_for_latest() -> MantisAgentOptions:
    dep = (await list_deployments())[-1]
    return MantisAgentOptions(model=dep.served_model_name, backend=dep.endpoint_url)
```

`connect` exports the same facts for the terminal: it records the endpoint
as the current model (`~/.mantis-agent/models.json`, so a bare `mantis`
opens on it), saves the Bearer key under `MANTIS_AGENT_API_KEY`, and — for
header-authenticated endpoints such as Modal — writes
`MANTIS_AGENT_EXTRA_HEADERS` so the session reaches the endpoint with no code
change (see [extra request headers](models-and-backends.md#extra-request-headers)).
The printed one-liner is ready to paste:

```bash
MANTIS_AGENT_MODEL=Qwen/Qwen3-32B MANTIS_AGENT_BASE_URL=https://api.runpod.ai/v2/<id>/openai/v1 MANTIS_AGENT_API_KEY=$RUNPOD_API_KEY mantis
```

## From Python

The CLI, the dashboard and `/deploy` all call `mantis_agent.deploy`, and so
can you:

```python
import asyncio

from mantis_agent.deploy import DeployOpts, connect, deploy, teardown


async def main() -> None:
    dep = await deploy(
        "runpod",
        "Qwen/Qwen3-8B",
        gpu="AMPERE_80",                    # an id from `deploy gpus runpod`
        engine="vllm",
        opts=DeployOpts(max_model_len=16384, min_replicas=0, idle_timeout_s=300),
        progress=print,                     # "pre-flight: 8.2B · BF16 · ~20 GB", "cold start… 503", …
    )
    wiring = await connect(dep.id)          # {"model", "backend", "api_key_env", "headers"}
    print(wiring["model"], wiring["backend"])
    ...
    await teardown(dep.id)


asyncio.run(main())
```

`providers()`, `gpus()`, `search_models()`, `inspect_model()`, `fit()`,
`status()`, `logs()`, `list_deployments()` and `cost()` round out the
surface; every call raises `DeployError` (with a user-facing `hint`) on
failure and `NotSupported` where a provider simply has no API for the
operation.

## Cost, idle time and scale-to-zero

The confirmation — in the CLI, the dashboard and `/deploy` — names the cost
before anything is billed: *$X/h while running · $Y/h idle*. `--min 0` (the
default) means the provider scales the endpoint to zero after `--idle`
seconds, and the idle cost is $0 on RunPod, HF Endpoints, Modal, DeepInfra
and Baseten. The first request after that pays a cold start — seconds on
RunPod (FlashBoot plus the model cache), tens of seconds on Modal and
Baseten, a few minutes on HF — and mantis's retry layer rides out the 503s.
`--min 1` keeps a warm replica when latency matters more than the idle bill.
Non-streaming proxies time out at 100–120 s on RunPod pods and HF; the
terminal streams by default so long generations are unaffected.

**Vast.ai is different**: it rents a whole host by the hour, there is no
scale-to-zero, and the instance bills (including storage while stopped)
until you `down` it. `ls --refresh` shows accrued cost where the provider's
billing API reports it, and `/dash` in the terminal shows the combined $/h
of everything that's live.

Tear things down when you're done. `down` asks first and names the money.

## Gated models and `HF_TOKEN`

Llama, Gemma and friends need the licence accepted on the Hub *and* a token
the provider can use to download. `deploy inspect` still sizes a gated model
without one (the Hub serves the metadata; when `config.json` isn't readable
the estimate falls back to a 20% headroom rule), but `deploy up` refuses with
the licence link until `HF_TOKEN` is set — `mantis-agent deploy creds hf
--set HF_TOKEN=hf_...` saves it for every provider, or pass `--hf-token`.
Each adapter plumbs it into the provider's own secret mechanism: an HF
Endpoints token authenticates the control plane, the endpoint *and* the gated
repo in one go; Modal gets a `modal.Secret`, Baseten a workspace secret
`hf_access_token`, DeepInfra `hf.token`, RunPod and Vast.ai an environment
variable on the worker.

Reproducibility: `DeployOpts.engine_version` (from Python) pins the
vLLM/SGLang image tag, and `MANTIS_RUNPOD_VLLM_IMAGE` pins the RunPod vLLM
worker image (`runpod/worker-v1-vllm:<tag>`) when the `stable` tag moving
under you would matter.

## Security

- **Credentials live in your user settings env.** `creds --set`, the
  dashboard's **Add key** form and `/deploy creds` all write the value into
  the `env` block of the *user* `settings.json` under `~/.mantis-agent` —
  the same `0600` file `/enable` uses for provider keys — and export it into
  the running process. `deployments.json` records env var *names* only, and
  provider objects pass through a secret masker before they are written.
- **Generated inference keys** (Modal without a proxy token, Vast.ai) live
  in `MANTIS_DEPLOY_<SLUG>_KEY` in the environment; again only the name is
  persisted.
- **Vast.ai endpoints are plain HTTP on a shared public IP.** The generated
  Bearer key is the only thing between the internet and your GPU, and it
  travels in the clear. Fine for an experiment; not for anything you'd mind
  a network neighbour reading. The provider card and the confirm dialog say
  so in warning colours, and RunPod's are reachable by anyone *with your
  API key* — keep it out of chat logs.
- **Deploy actions are writes.** In the dashboard they need the per-launch
  token, and under `--lan` anyone holding the URL can start and stop GPU
  spend on your accounts — treat it as the credential it is.
- **Nothing phones home.** The only outbound requests are the ones you asked
  for: Hub search, credential validation, deploy, logs, teardown.

## When it doesn't work

- **`HTTP 401` with a hint naming the env var** — the key was rejected.
  Re-save it with `deploy creds <provider> --set ENV=...`; the hint links the
  console.
- **`HTTP 402` / quota** — the provider wants credit or a GPU-quota bump; the
  hint links the billing page. HF also rejects a deploy when the namespace's
  accelerator quota is used up (scaled-to-zero endpoints still hold quota).
- **`does not fit`** — weights plus KV cache exceed 90% of the card. `deploy
  gpus <provider> --min-vram N` lists cards that fit; `--max-model-len`
  shrinks the KV budget; `--force` deploys anyway.
- **`not servable by vllm`** — the architecture isn't in vLLM's registry.
  Try `--trust-remote-code`, another engine (`--engine sglang|tgi|llamacpp`),
  or `--force` to let vLLM's Transformers fallback try.
- **`not ready after 120s`** on `connect` — a cold replica is still booting.
  Check `deploy status <id>` / `deploy logs <id>` and retry; on RunPod the
  first request itself boots the worker.
- **The store and the console disagree** — `deploy ls --refresh` re-queries
  every configured provider, marks vanished deployments deleted and adopts
  endpoints created elsewhere.

## Where next

- [The dashboard](dashboard.md#deploy) — the Deploy page, button by button.
- [The terminal](terminal.md#deploy--bring-your-own-gpu) — `/deploy` and the
  background job it runs.
- [Self-hosting](self-hosting.md) — the manual route through each provider's
  console, and the sizing cheat sheet.
- [Models & backends](models-and-backends.md) — what happens once the
  endpoint is current.
