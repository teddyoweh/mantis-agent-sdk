# Deploy on your own GPU cloud (`mantis-agent deploy`)

You have an account on a GPU cloud. mantis turns that into "any open-weight
model, served as an OpenAI-compatible endpoint, wired in as the current
model" — from the CLI, the dashboard, or the terminal's `/deploy`. Nothing
runs through a mantis server: your key talks to the provider, the weights go
straight from the Hugging Face Hub to the provider, and the endpoint is yours.

Providers in this release:

| id | Provider | Engines | Scale to zero | Cheapest H100 | Notes |
|---|---|---|---|---|---|
| `runpod` | RunPod Serverless | vLLM | yes (`idleTimeout`) | ~$4.20/h flex | Live GPU catalogue, worker logs, FlashBoot cold starts |
| `hf` | Hugging Face Inference Endpoints | vLLM, SGLang, TGI, llama.cpp | yes | $10/h (GCP only — prefer A100 $2.50, L4 $0.80) | One `hf_` token for the control plane, the endpoint *and* gated repos |
| `modal` | Modal | vLLM, SGLang | yes | $3.95/h | $30/month free credit; endpoint auth via proxy token |
| `deepinfra` | DeepInfra | vLLM | yes | $2.20/h | One API call; no logs API; no quantised checkpoints |
| `baseten` | Baseten | vLLM, SGLang | yes | $6.50/h | Fullest REST control plane (prices, logs, autoscaling); pricier GPUs |
| `vastai` | Vast.ai | vLLM, SGLang | **no** | ~$1.5–2.3/h market | Cheapest; plain-HTTP marketplace VM, you pay until you `down` it |

## The five-minute path

Every provider follows the same four steps: get a key → save it → deploy →
connect. The credential is stored in your user `settings.json` `env` block
(mode `0600`, same place `mantis setup` keeps keys) and exported into the
process environment; `deployments.json` next to it never holds a secret.

```bash
mantis-agent deploy providers                      # what's registered, what's configured
mantis-agent deploy creds runpod --set RUNPOD_API_KEY=rp_...   # saves + validates over the network
mantis-agent deploy gpus runpod --min-vram 40      # catalogue with prices, cheapest first
mantis-agent deploy inspect Qwen/Qwen3-8B          # params, dtype, gated?, vLLM?, VRAM estimate
mantis-agent deploy up runpod Qwen/Qwen3-8B --gpu AMPERE_80 --max-model-len 16384
mantis-agent deploy connect <id>                   # (done for you by `up` unless --no-connect)
```

`up` pre-flights the model (architecture, size, gated status), checks it fits
the GPU you picked (refuses on "no", warns on "tight", `--force` overrides),
creates the deployment, waits for the endpoint to answer `/v1/models`
(cold-start `503`s are expected and retried), and prints the exact line to
launch the terminal on it:

```text
MANTIS_AGENT_MODEL=Qwen/Qwen3-8B MANTIS_AGENT_BASE_URL=https://api.runpod.ai/v2/abc123/openai/v1 MANTIS_AGENT_API_KEY=$RUNPOD_API_KEY mantis
```

`connect` also records the endpoint as the current model
(`~/.mantis-agent/models.json`), so a bare `mantis` opens on it, and saves
the auth key under `MANTIS_AGENT_API_KEY` for the OpenAI-compatible adapter.
Non-bearer auth (Modal's `Modal-Key` / `Modal-Secret`) is exported as
`MANTIS_AGENT_EXTRA_HEADERS`.

### Per provider

**RunPod** — key from *console.runpod.io → Settings → API Keys*. GPU ids are
*pools* (`AMPERE_80` = A100 80 GB, `ADA_80_PRO` = H100, `ADA_24` = L4/4090,
`HOPPER_141` = H200); prices are the live serverless flex rate. The endpoint is
`https://api.runpod.ai/v2/<id>/openai/v1`, authenticated with the same key.
`--min 0` (the default) scales to zero; idle workers bill until `--idle`
seconds pass. The vLLM worker image is
`runpod/worker-v1-vllm:stable-cuda12.1.0`; pin another tag with
`DeployOpts.engine_version` from Python or the `MANTIS_RUNPOD_VLLM_IMAGE`
environment variable.

**Hugging Face Inference Endpoints** — a token with *write* scope from
*huggingface.co/settings/tokens*. GPU ids are `vendor/region/type/size`, e.g.
`aws/us-east-1/nvidia-a100/x1`. The endpoint is created `protected` (your
token is the auth), the model repo is mounted server-side (no download on
your machine), and the same token unlocks gated repos. Scaled-to-zero
endpoints answer `503` while they wake; mantis sends `X-Scale-Up-Timeout`
and keeps polling. Set `HF_ENDPOINTS_NAMESPACE` to deploy into an org.

**DeepInfra** — key from *deepinfra.com/dash/api_keys*. GPU ids are the five
strings DeepInfra accepts (`A100-80GB`, `H100-80GB`, `H200-141GB`,
`B200-180GB`, `B300-288GB`; `--gpu H100-80GB:2` for multi-GPU, four max). The
endpoint is the shared `https://api.deepinfra.com/v1/openai` and the model
name is `deploy_id:<id>` — that is what `served_model_name` holds. No logs
API (`deploy logs` says so and points at the console).

**Baseten** — key from *app.baseten.co → Settings → API keys*. GPU ids are
accelerators (`A10G`, `A100`, `H100`, `H100:2`, `H200`, `B200`); prices come
from `/v1/instance_type_prices`. Deploy uploads a tiny Truss config (upstream
`vllm/vllm-openai` image, `weights: hf://…` so Baseten mirrors the checkpoint
once) and polls the build to `ACTIVE` — the first build takes 5–10 minutes,
later cold starts ~10 s plus engine load. Endpoint:
`https://model-<id>.api.baseten.co/environments/production/sync/v1`.

**Modal** and **Vast.ai** are documented in their adapters
(`mantis_agent/deploy/providers/modal_deploy.py`, `vastai.py`); Modal needs
`MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET`, Vast.ai `VAST_API_KEY`.

## Getting a key

The same steps the dashboard's *Add key* panel shows
(`mantis_agent.provider_guides.deploy_guide(<id>)`). Every key is saved with
`mantis-agent deploy creds <id> --set VAR=…`, which also validates it over
the network.

### RunPod — `RUNPOD_API_KEY`

1. Sign in at [console.runpod.io](https://console.runpod.io/signup).
2. Open [Settings](https://console.runpod.io/user/settings), expand **API
   Keys**, click **Create API Key**.
3. Permission **All** (or **Restricted** with Serverless set to *Read/Write*
   — mantis creates endpoints).
4. Copy the key now; RunPod does not store it.
5. Add credit under Billing (prepaid, per-second; $10 is enough to start).
   No free credit. [Pricing](https://www.runpod.io/pricing): H100 ≈ $4.20/h
   flex, $0 idle at `--min 0`.

### Hugging Face Inference Endpoints — `HF_TOKEN`

1. [Settings → Access Tokens](https://huggingface.co/settings/tokens) →
   **Create new token** → *Fine-grained*.
2. Tick **Inference → Manage Inference Endpoints** and **Make calls to
   Inference Endpoints**, plus read access to the repos you will deploy. A
   classic *Write* token also works.
3. Copy the `hf_…` token (shown once). The same token unlocks gated repos.
4. Add a payment method under Settings → Billing — endpoints will not create
   without one. No free tier; billed per minute while running, $0 at
   scale-to-zero. [Pricing](https://huggingface.co/docs/inference-endpoints/pricing):
   L4 ≈ $0.80/h, A100 ≈ $2.50/h.

### Modal — `MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET`

1. Sign up at [modal.com](https://modal.com/signup) (GitHub login).
2. [Settings → API Tokens](https://modal.com/settings/tokens) → **New
   Token**; copy the id (`ak-…`) and secret (`as-…`). Or
   `pip install modal && modal token new`.
3. Optional — [Settings → Proxy Auth Tokens](https://modal.com/settings/proxy-auth-tokens)
   → **New Token** (`wk-…` / `ws-…`) as `MODAL_PROXY_TOKEN_ID` /
   `MODAL_PROXY_TOKEN_SECRET`. With them the endpoint only answers callers
   holding the token (mantis sends `Modal-Key` / `Modal-Secret`); without
   them it is deployed behind a generated vLLM `--api-key`.
4. Optional — `HF_TOKEN` for gated repos (stored as a `modal.Secret`).
5. $30/month of free compute on Starter ($100 on Team); a card is only
   needed beyond that. [Pricing](https://modal.com/pricing): H100 ≈ $3.95/h,
   L4 ≈ $0.80/h, per second.

### DeepInfra — `DEEPINFRA_API_KEY`

1. Sign in at [deepinfra.com](https://deepinfra.com/login) (GitHub, Google
   or email).
2. [Dashboard → API Keys](https://deepinfra.com/dash/api_keys) → **New API
   key**; name it, copy it (shown once).
3. Add a payment method under Billing — dedicated GPUs need one on file. No
   advertised free credit. [Pricing](https://deepinfra.com/pricing): A100-80
   $0.89/h, H100 $2.20/h, H200 $2.69/h, B200 $3.69/h per GPU, $0 scaled to
   zero.

### Baseten — `BASETEN_API_KEY`

1. Sign up at [app.baseten.co](https://app.baseten.co/signup).
2. [Organization settings → API keys](https://app.baseten.co/settings/api_keys)
   → **Create API key**. A *Personal* key is fine locally; a *Team* key needs
   **Full access** (deploy is more than *Inference only*).
3. Copy the key — `abcd1234.…`, an 8-character prefix, a dot, the secret.
4. New accounts start with free credits; add a card under Billing when they
   run out. [Pricing](https://www.baseten.co/pricing/): H100 $6.50/h, A100
   $4.00/h, per minute, $0 scaled to zero.

### Vast.ai — `VAST_API_KEY`

1. Sign up at [cloud.vast.ai](https://cloud.vast.ai/signup) and verify your
   email (lifts the new-account spend limit).
2. [Keys](https://cloud.vast.ai/manage-keys/) (Account → API Keys) → **+New**;
   keep full permissions or scope to `user_read`, `instance_read`,
   `instance_write`.
3. Copy the key now — shown once; sent as a Bearer token.
4. Billing → add credit: $5 minimum deposit, prepaid, no free tier.
   [Pricing](https://vast.ai/pricing) is a market: H100 ≈ $1.5–2.3/h, a 4090
   well under $0.50/h — charged every hour until `deploy down`.
5. Optional — `HF_TOKEN` for gated repos (passed to the container).

## From Python

```python
import anyio

from mantis_agent import query
from mantis_agent.deploy import DeployOpts, connect, deploy


async def main() -> None:
    dep = await deploy(
        "runpod", "Qwen/Qwen3-8B",
        gpu="AMPERE_80", engine="vllm",
        opts=DeployOpts(max_model_len=16384, min_replicas=0, idle_timeout_s=300),
        progress=print,
    )
    info = await connect(dep.id)  # {"model", "backend", "api_key_env", "headers"}
    async for msg in query(
        prompt="Say hello from the GPU cloud.",
        options={"model": info["model"], "backend": info["backend"]},
    ):
        if msg.type == "assistant":
            for block in msg.message.content:
                print(getattr(block, "text", ""))


anyio.run(main)
```

`inspect_model`, `search_models`, `fit`, `gpus`, `status`, `logs`, `teardown`
and `list_deployments` are the same operations the CLI exposes; every one is
async and raises `DeployError` (with a user-facing `hint`) on failure.
`NotSupported` means the provider has no API for that operation.

## The dashboard

`mantis serve` has a **Deploy** tab built on the same manager: pick a
provider, paste the key once, search the Hub, see which GPUs the model fits
(with prices), deploy, watch the progress log, connect, and tear down —
the deployments list and cost figures are the ones `deploy ls` and
`deploy status` print.

## Cost and idle behaviour

- `deploy status <id>` shows `$/h running` (list price × max replicas) and
  `$/h idle`. Idle is `$0` only where the provider scales to zero **and**
  `--min 0` — RunPod, HF Endpoints, Modal, DeepInfra, Baseten. Vast.ai bills
  every hour the instance exists; `deploy down` is the only off switch.
- A scaled-to-zero endpoint pays a cold start on the first request: seconds
  on RunPod (FlashBoot + model cache), tens of seconds on Modal/Baseten, a few
  minutes on HF. Set `--min 1` for a warm replica when latency matters more
  than the idle bill.
- Non-streaming proxies time out at 100–120 s on RunPod pods and HF; the
  terminal streams by default so long generations are unaffected.

## Gated models

Llama, Gemma and friends need the licence accepted on the Hub *and* a token
the provider can use to download. `deploy inspect` still sizes a gated model
without a token (the Hub serves the metadata); `deploy up` refuses with the
licence link until `HF_TOKEN` is set — `mantis-agent deploy creds hf --set
HF_TOKEN=hf_...` saves it for every provider. Each adapter plumbs the token
into the provider's own secret mechanism (RunPod env, HF `secrets`, Baseten
workspace secret `hf_access_token`, DeepInfra `hf.token`).

## What `served_model_name` means

The wire is always OpenAI-compatible, but what goes in `model=` differs per
provider: RunPod and HF answer to the HF id, DeepInfra wants
`deploy_id:<id>`, Baseten whatever `--served-name` was set to (default: the HF
id). `Deployment.served_model_name` is always the right value; `connect`
returns it as `model` and the launch line uses it — never guess.

## Environment variables

| Variable | Read by |
|---|---|
| `RUNPOD_API_KEY`, `HF_TOKEN`, `DEEPINFRA_API_KEY`, `BASETEN_API_KEY`, `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`, `VAST_API_KEY` | provider credentials (saved by `deploy creds`) |
| `HF_ENDPOINTS_NAMESPACE` | HF Endpoints: deploy into an org instead of your user namespace |
| `MANTIS_RUNPOD_VLLM_IMAGE` | RunPod: override the vLLM worker image tag |
| `MANTIS_AGENT_MODEL`, `MANTIS_AGENT_BASE_URL`, `MANTIS_AGENT_API_KEY`, `MANTIS_AGENT_EXTRA_HEADERS` | set by `deploy connect` so `mantis` / `Agent()` find the endpoint |

## Troubleshooting

- **`HTTP 401` with a hint naming the env var** — the key was rejected. Re-save
  it with `deploy creds <provider> --set ENV=...`; the hint links the console.
- **`HTTP 402` / quota** — the provider wants credit or a GPU-quota bump; the
  hint links the billing page. HF also rejects `deploy` when the namespace's
  accelerator quota is used up (scaled-to-zero endpoints still hold quota).
- **`does not fit`** — pre-flight says the weights + KV cache exceed 90 % of
  the card. `deploy gpus <provider> --min-vram N` lists cards that fit;
  `--max-model-len` shrinks the KV budget; `--force` deploys anyway.
- **`not servable by vllm`** — the architecture isn't in vLLM's registry.
  Try `--trust-remote-code`, another engine (`--engine sglang|tgi|llamacpp`),
  or `--force` to let vLLM's Transformers fallback try.
- **`not ready after 120s`** on `connect` — a cold replica is still booting.
  Check `deploy status <id>` / `deploy logs <id>` and retry; on RunPod the
  first request itself boots the worker.
- **The store and the console disagree** — `deploy ls --refresh` re-queries
  every configured provider, marks vanished deployments deleted and adopts
  endpoints created elsewhere.
- **Secrets on disk?** — never. `deployments.json` stores `auth_env` (the
  variable *name*); provider objects are redacted before they are written.
