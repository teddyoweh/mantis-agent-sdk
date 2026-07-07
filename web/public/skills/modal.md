---
name: selfhost-modal
description: >
  Deploy an open-weight model (GLM, Qwen, Llama, gpt-oss) as an OpenAI-compatible
  vLLM endpoint on Modal serverless GPUs, verify it answers, and hand back the
  URL + connect command. Use when the user says "host <model> on Modal",
  "deploy <model> serverless", or wants a scale-to-zero endpoint.
license: Apache-2.0
---

# selfhost-modal

Do the work — write the file, deploy, verify, hand back the URL.

## 1. Credentials
`modal --version` missing → `pip install modal`. No `~/.modal.toml` and no
`MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` in env → run `modal setup` (browser) or
have the user mint a token at modal.com → Settings → API Tokens and export
both vars. No account → modal.com signup includes $30/mo free compute.

## 2. Pick model + GPU
| ask | model id | gpu | parser |
|---|---|---|---|
| GLM | `zai-org/GLM-4-9B-0414` | `L4` | `glm4` |
| Qwen small / strong | `Qwen/Qwen3-8B` / `Qwen/Qwen3-32B` | `L4` / `H100` | `hermes` |
| Llama | `meta-llama/Llama-3.3-70B-Instruct` | `H100:2` | `llama3_json` |
| gpt-oss | `openai/gpt-oss-120b` | `H100` | *(omit tool flags)* |
| DeepSeek-V3 / GLM-4.7 full | don't — recommend a hosted provider | — | — |

## 3. Deploy
Write `serve_model.py` exactly as below (substitute MODEL/GPU/PARSER), then
`modal deploy serve_model.py` and capture the printed URL.

```python
import modal

MODEL = "zai-org/GLM-4-9B-0414"
app = modal.App("mantis-selfhost")
image = (modal.Image.debian_slim(python_version="3.12")
         .pip_install("vllm>=0.8", "huggingface_hub"))

@app.function(image=image, gpu="L4", timeout=1200, scaledown_window=300,
              volumes={"/root/.cache/huggingface": modal.Volume.from_name(
                  "hf-cache", create_if_missing=True)})
@modal.concurrent(max_inputs=8)
@modal.web_server(8000, startup_timeout=600)
def serve():
    import subprocess
    subprocess.Popen(["vllm", "serve", MODEL, "--host", "0.0.0.0",
                      "--port", "8000", "--enable-auto-tool-choice",
                      "--tool-call-parser", "glm4"])
```

Gated model → `modal secret create huggingface HF_TOKEN=...` +
`secrets=[modal.Secret.from_name("huggingface")]`.

## 4. Verify (mandatory — cold start ≈ 1-5 min)
Poll `curl -sf <URL>/v1/models` until 200, then one real completion. Failure →
`modal app logs mantis-selfhost`; OOM → bigger GPU or `--max-model-len 16384`.
Never hand over a dead URL.

## 5. Hand back
```
✅ <MODEL> live
URL:      <URL>/v1
Connect:  /connect <URL>/v1 <MODEL>
Cost:     ~$<gpu-rate>/h warm · scales to zero after 5 min idle
Teardown: modal app stop mantis-selfhost
```
