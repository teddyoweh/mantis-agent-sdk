# Self-host on Modal

Serverless GPUs: deploy a Python file, pay per second, scale to zero when
idle. The best option if you don't own a GPU — and mantis authenticates
`*.modal.run` endpoints **natively** (no key management at all).

**Cost feel:** L4 ≈ $0.80/h · A100-80 ≈ $2.50/h · H100 ≈ $4/h — only while
warm. Free tier includes **$30/month** of compute.

## Credentials

```bash
pip install modal
modal setup                    # browser auth → ~/.modal.toml
```

Manual: [modal.com](https://modal.com) → **Settings → API Tokens** (or
`modal token new`). Headless/CI: export `MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET`.
mantis reads those same two vars to sign requests to your endpoint
(`Modal-Key`/`Modal-Secret` headers) — the endpoint stays private to you.

## Deploy

```python
# serve_model.py — modal deploy serve_model.py
import modal

MODEL = "zai-org/GLM-4-9B-0414"
app = modal.App("mantis-glm")

image = (modal.Image.debian_slim(python_version="3.12")
         .pip_install("vllm>=0.8", "huggingface_hub"))

@app.function(
    image=image,
    gpu="L4",
    timeout=60 * 20,
    scaledown_window=300,          # idle 5 min → $0
    volumes={"/root/.cache/huggingface": modal.Volume.from_name(
        "hf-cache", create_if_missing=True)},
)
@modal.concurrent(max_inputs=8)
@modal.web_server(8000, startup_timeout=600)
def serve():
    import subprocess
    subprocess.Popen([
        "vllm", "serve", MODEL,
        "--host", "0.0.0.0", "--port", "8000",
        "--enable-auto-tool-choice", "--tool-call-parser", "glm4",
    ])
```

Gated models (Llama): `modal secret create huggingface HF_TOKEN=...` and add
`secrets=[modal.Secret.from_name("huggingface")]`.

## Connect

```
/connect https://<workspace>--mantis-glm-serve.modal.run/v1 zai-org/GLM-4-9B-0414
```

First request after idle cold-starts ~1–2 min (weights load); mantis's retry
note rides it out. Teardown: `modal app stop mantis-glm`.

## Let your agent do it

```bash
mkdir -p ~/.claude/skills/selfhost-modal
curl -o ~/.claude/skills/selfhost-modal/SKILL.md https://mantisagent.cc/skills/modal.md
```

Then: *"host GLM-4-9B on my Modal"* — it deploys, health-checks, and hands
back the `/connect` line.
