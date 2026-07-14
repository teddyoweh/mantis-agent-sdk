# Self-hosting models

Run the full open weights yourself and point mantis at the URL. No vendor
key, no rate limits, your hardware (or your cloud). Wherever it runs, the
finish line is identical:

```
/connect <your-url>/v1 <model-id>        # in the mantis terminal
```

```python
options = MantisAgentOptions(model="<model-id>", backend="<your-url>/v1")   # SDK
```

Prefer clicking? Run `mantis serve`, open the **Models** tab, and use the
**Self-host / custom endpoint** form — paste the base URL, model id, and an
optional key, then hit Connect. Same result as `/connect`, no terminal.

## Where you can self-host — the full map

### On your own hardware — automated, no guide needed

Local runtimes (Ollama, llama.cpp, LM Studio) need no manual setup: **`mantis
setup` does it for you** — detects your machine, picks a fitting model, pulls
it, and verifies a real completion. This page is about hosting in a *cloud*.

### Serverless GPU clouds (pay per second, scale to zero)

| option | credentials | feel |
|---|---|---|
| **[Modal](https://modal.com)** ([guide](/docs/selfhost/modal)) | `modal setup` → tokens at Settings → API Tokens (`MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`) | deploy a Python file; **$30/mo free**; mantis auths `*.modal.run` natively — the deep-dive below |
| **[RunPod Serverless](https://www.runpod.io/serverless-gpu)** ([guide](/docs/selfhost/runpod)) | [Settings → API Keys](https://www.runpod.io/console/user/settings) (`RUNPOD_API_KEY`) | vLLM worker template; cheap spot pricing |
| **[Replicate](https://replicate.com)** | [account → API tokens](https://replicate.com/account/api-tokens) (`REPLICATE_API_TOKEN`) | push a model, get an endpoint; pay per second |
| **[Baseten](https://www.baseten.co)** | [API keys](https://app.baseten.co/settings/api_keys) (`BASETEN_API_KEY`) | Truss deploys, OpenAI-compat endpoints |
| **[Beam](https://www.beam.cloud)** | dashboard token (`BEAM_TOKEN`) | Modal-style Python deploys |

### GPU rentals (SSH in, run vLLM yourself)

| option | credentials | feel |
|---|---|---|
| **[RunPod Pods](https://www.runpod.io)** ([guide](/docs/selfhost/runpod)) | same key as above | on-demand or spot; web terminal; expose port 8000 via their proxy |
| **[Lambda](https://cloud.lambda.ai)** ([guide](/docs/selfhost/lambda)) | [API keys](https://cloud.lambda.ai/api-keys) (`LAMBDA_API_KEY`) | clean per-hour H100s/B200s |
| **[Vast.ai](https://vast.ai)** ([guide](/docs/selfhost/vastai)) | [account keys](https://cloud.vast.ai/manage-keys/) (`VAST_API_KEY`) | marketplace — cheapest GPUs anywhere, variable reliability |
| **[Paperspace](https://www.paperspace.com)** | console API keys | DigitalOcean-owned, simple |
| **[CoreWeave](https://coreweave.com)** | enterprise onboarding | serious scale, k8s-native |

### Managed dedicated endpoints (they run vLLM for you)

| option | credentials | feel |
|---|---|---|
| **[HF Inference Endpoints](https://huggingface.co/inference-endpoints)** ([guide](/docs/selfhost/hf-endpoints)) | [hf.co → Access Tokens](https://huggingface.co/settings/tokens) (`HF_TOKEN`) | one click on any HF model → OpenAI-compat URL |
| **[Together Dedicated](https://www.together.ai/products#dedicated)** / **[Fireworks On-Demand](https://fireworks.ai)** | their normal API keys | your own capacity behind their API shape |

### Hyperscalers (when compliance says so)

AWS (SageMaker/EC2 + vLLM), GCP (Vertex/GCE), Azure (AML/NC-series) — same
pattern: get a GPU VM, `pip install vllm`, serve, put it behind your VPN.
More IAM than engineering.

### Agent sandboxes (adjacent, not model hosting)

**[Daytona](https://app.daytona.io/dashboard/keys)** (`DAYTONA_API_KEY`),
**[E2B](https://e2b.dev/docs)** (`E2B_API_KEY`) — sandboxes for running *agent
code* safely, not for serving 100B weights. Pair them with any endpoint above.

---

## Deep dive — Modal + vLLM (serverless GPU)

The best "I don't own a GPU" option: per-second billing, scales to zero when
idle, and mantis has **native Modal auth** (it detects `*.modal.run` URLs and
sends your `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` as `Modal-Key`/`Modal-Secret`
headers automatically).

**1. Get your Modal credentials (once):**

```bash
pip install modal
modal setup          # opens the browser, authenticates, writes ~/.modal.toml
```

That's usually all you need. The manual routes:

- Sign up at [modal.com](https://modal.com) (free tier includes **$30/month
  of compute** — a lot of L4 hours).
- Tokens live at **modal.com → Settings → API Tokens** (or `modal token new`).
- Headless/CI: export `MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET` instead of the
  toml file. mantis reads the same two vars to authenticate your endpoint —
  private to you with zero extra key management.

**2. Deploy — save as `serve_glm.py`:**

```python
import modal

MODEL = "zai-org/GLM-4-9B-0414"          # swap for any HF model id
app = modal.App("mantis-glm")

image = (modal.Image.debian_slim(python_version="3.12")
         .pip_install("vllm>=0.8", "huggingface_hub"))

@app.function(
    image=image,
    gpu="L4",                             # see sizing table below
    timeout=60 * 20,
    scaledown_window=300,                 # idle 5 min → scale to zero → $0
    volumes={"/root/.cache/huggingface": modal.Volume.from_name(
        "hf-cache", create_if_missing=True)},   # cache weights across cold starts
)
@modal.concurrent(max_inputs=8)
@modal.web_server(8000, startup_timeout=600)
def serve():
    import subprocess
    subprocess.Popen([
        "vllm", "serve", MODEL,
        "--host", "0.0.0.0", "--port", "8000",
        "--enable-auto-tool-choice", "--tool-call-parser", "glm4",  # tools!
    ])
```

```bash
modal deploy serve_glm.py
# → https://<workspace>--mantis-glm-serve.modal.run
```

**3. Connect:**

```
/connect https://<workspace>--mantis-glm-serve.modal.run/v1 zai-org/GLM-4-9B-0414
```

First request after idle cold-starts (~1–2 min while weights load); mantis's
retry layer shows a spinner note and rides it out.

**Tool-call parsers** (`--tool-call-parser`): `glm4` for GLM, `hermes` for
Qwen, `llama3_json` for Llama, `deepseek_v3` for DeepSeek. Wrong/missing
parser → mantis falls back to text-parsed tool calls automatically.

## Deep dive — vLLM on any GPU box (rentals included)

Works identically on your basement server, a RunPod pod, Lambda, or Vast:

```bash
pip install vllm
vllm serve Qwen/Qwen3-32B \
  --host 0.0.0.0 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser hermes
```

```
/connect http://gpu-box:8000/v1 Qwen/Qwen3-32B
```

Off-LAN: `cloudflared tunnel --url http://localhost:8000`, Tailscale, or the
rental's port proxy (RunPod exposes `https://<pod-id>-8000.proxy.runpod.net`).
If you gate it (`--api-key sk-mine`), pass the key to mantis.

## Sizing cheat sheet

| model | VRAM (bf16 / 4-bit) | Modal GPU | good at |
|---|---|---|---|
| Qwen3-8B / GLM-4-9B | 18 GB / 6 GB | L4 (~$0.80/h) | daily driver, cheap |
| Qwen3-32B / GLM-Z1-32B | 66 GB / 20 GB | 1× H100 / A100-80 | strong agent work |
| gpt-oss-120b (MoE) | ~80 GB active | 1–2× H100 | flagship-ish, great tools |
| Llama-3.3-70B | 140 GB / 40 GB | 2× H100 or 1× H200 | general |
| GLM-4.7 / DeepSeek-V3 (355B+/671B MoE) | 400 GB+ | 8× H100 | usually cheaper via a [hosted provider](/docs/guides/providers) |

Rule of thumb: **params × 2 GB (bf16)** or **params × 0.6 GB (4-bit)**, plus
~20% for KV cache.

## Troubleshooting

- `/doctor` probes your backend live (HTTP status + auth) — start there.
- Pasted `.../v1/chat/completions` instead of `.../v1`? mantis normalizes it.
- Tool calls coming back as text? Add the right `--tool-call-parser`, or let
  mantis's text-parse fallback handle it (works, slightly slower).
- Slow first tokens on small local models: expected prefill; mantis slims the
  tool belt and keeps the prompt prefix stable so follow-up turns reuse the
  KV cache.

## Let your agent do all of this

There's a [skill](/skill) for that: give Claude Code (or mantis itself) the
**selfhost skill** and say *"host GLM-4-9B for me"* — it writes the Modal app,
deploys it, waits for the health check, and hands you the `/connect` line.
