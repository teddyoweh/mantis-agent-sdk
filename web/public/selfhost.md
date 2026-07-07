---
name: selfhost-model
description: >
  Deploy any open-weight model (GLM, Qwen, DeepSeek, Llama, gpt-oss) as an
  OpenAI-compatible endpoint the user can point an agent at — Modal serverless
  vLLM by default, or vLLM/llama.cpp/Ollama on a box they own. Use when the
  user says "host <model> for me", "self-host", "put GLM on my Modal",
  "give me an endpoint for <model>", or wants off hosted APIs. Ends by
  handing back the URL + the exact connect command.
license: Apache-2.0
---

# selfhost-model

You are deploying an open model and handing back a working URL. Do the work —
write the file, deploy it, verify it answers — don't describe steps.

## 0. Choose the path (ask only if genuinely unclear)

- User has Modal (check: `modal --version` and `~/.modal.toml`) → **Modal + vLLM** (default).
- User names a GPU box/server → **vLLM on that box**.
- CPU-only / tiny model → **llama.cpp** or **Ollama**.

## 1. Pick model + GPU (don't ask for permission on obvious picks)

| ask | model id | Modal GPU | vLLM tool parser |
|---|---|---|---|
| "GLM" (affordable) | `zai-org/GLM-4-9B-0414` | `L4` | `glm4` |
| "Qwen" small | `Qwen/Qwen3-8B` | `L4` | `hermes` |
| "Qwen" strong | `Qwen/Qwen3-32B` | `H100` | `hermes` |
| "gpt-oss" | `openai/gpt-oss-120b` | `H100` | *(none — omit flags)* |
| "Llama" | `meta-llama/Llama-3.3-70B-Instruct` | `H100:2` | `llama3_json` |
| "DeepSeek"/"GLM-4.7" full | too big for casual hosting | — | recommend a hosted provider instead, offer 9B/32B alternative |

VRAM rule: params × 2 GB bf16 (× 0.6 four-bit) + 20% KV.

## 2. Modal path (default)

Write `serve_model.py` (substitute MODEL / GPU / PARSER from the table):

```python
import modal

MODEL = "zai-org/GLM-4-9B-0414"
app = modal.App("mantis-selfhost")

image = (modal.Image.debian_slim(python_version="3.12")
         .pip_install("vllm>=0.8", "huggingface_hub"))

@app.function(
    image=image,
    gpu="L4",
    timeout=60 * 20,
    scaledown_window=300,
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

Notes:
- Gated HF models (Llama): needs `modal secret create huggingface HF_TOKEN=...`
  and `secrets=[modal.Secret.from_name("huggingface")]` on the function.
- Drop the two tool flags entirely for models without a parser.

Deploy and capture the URL:

```bash
modal deploy serve_model.py     # prints https://<ws>--mantis-selfhost-serve.modal.run
```

## 3. Verify before declaring success (mandatory)

Cold start loads weights — poll up to ~5 min:

```bash
curl -sf --max-time 10 <URL>/v1/models   # retry until 200
curl -s <URL>/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"<MODEL>","messages":[{"role":"user","content":"say ok"}],"max_tokens":5}'
```

A real completion back = done. 4xx/5xx = read `modal app logs mantis-selfhost`
and fix; do not hand over a dead URL.

## 4. Hand it back (exact format)

```
✅ <MODEL> is live
URL:      https://<ws>--mantis-selfhost-serve.modal.run/v1
Connect:  /connect https://<ws>--mantis-selfhost-serve.modal.run/v1 <MODEL>
SDK:      MantisAgentOptions(model="<MODEL>", backend="https://.../v1")
Cost:     ~$<GPU $/h> while warm · scales to zero after 5 min idle
          (first request after idle cold-starts ~1-2 min)
Teardown: modal app stop mantis-selfhost
```

mantis authenticates `*.modal.run` URLs automatically via
`MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`. For other agents/clients, no key is
set unless you added one.

## 5. Own-box path (when not Modal)

```bash
pip install vllm
nohup vllm serve <MODEL> --host 0.0.0.0 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser <PARSER> > vllm.log 2>&1 &
# wait for "Application startup complete" in vllm.log, then verify as above
```
URL is `http://<box>:8000/v1`. Suggest `cloudflared tunnel --url http://localhost:8000`
or Tailscale if they need it off-LAN. CPU-only → `llama-server -hf <gguf-repo> --jinja --port 8080`.

## Failure modes you must handle

- **OOM on start** → smaller GPU picked: bump GPU (`L4`→`A100-80GB`→`H100`) or
  add `--max-model-len 16384`; MoE 100B+ on one GPU never fits — say so.
- **401 from HF** → gated model, get token + secret (step 2 note).
- **Timeout on first call** → cold start; keep polling, tell the user why.
- **Tool calls broken** → wrong parser; drop the flags (text-parse fallback
  still works in mantis) or fix per the table.
