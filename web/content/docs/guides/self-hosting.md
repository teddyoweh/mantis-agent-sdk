# Self-hosting models

Run the full open weights yourself and point mantis at the URL. No vendor
key, no rate limits, your hardware (or your cloud). Every path below ends the
same way:

```
/connect <your-url>/v1 <model-id>        # in the mantis terminal
```

or in the SDK:

```python
options = MantisAgentOptions(model="<model-id>", backend="<your-url>/v1")
```

Pick your path:

| you have | path | cold start | cost shape |
|---|---|---|---|
| nothing (just a laptop) | **Ollama** | none | free |
| a Modal account | **Modal + vLLM** | ~1–2 min | per-second GPU, scales to zero |
| a GPU box / server | **vLLM** | none | your power bill |
| CPU-only server / edge | **llama.cpp** | none | free-ish |

---

## Path 1 — Modal + vLLM (serverless GPU)

The best "I don't own a GPU" option: per-second billing, scales to zero when
idle, and mantis has **native Modal auth** (it detects `*.modal.run` URLs and
sends your `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` as `Modal-Key`/`Modal-Secret`
headers automatically).

**1. Setup (once):**

```bash
pip install modal
modal setup          # opens browser, writes ~/.modal.toml
```

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

That's it — auth rides your Modal tokens from the environment. First request
after idle cold-starts (~1–2 min while weights load); mantis's retry layer
shows a spinner note and rides it out.

**Tool-call parsers** (`--tool-call-parser`): `glm4` for GLM, `hermes` for
Qwen, `llama3_json` for Llama, `deepseek_v3` for DeepSeek. Wrong/missing
parser → mantis falls back to text-parsed tool calls automatically, but the
native path is faster and stricter.

---

## Path 2 — vLLM on your own GPU box

```bash
pip install vllm
vllm serve Qwen/Qwen3-32B \
  --host 0.0.0.0 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser hermes
```

```
/connect http://gpu-box:8000/v1 Qwen/Qwen3-32B
```

Expose it beyond your LAN with a tunnel if needed
(`cloudflared tunnel --url http://localhost:8000` or Tailscale). If you put an
API key on it (`--api-key sk-mine`), pass it: `mantis --api-key sk-mine` or
the setup wizard's self-host flow asks for it.

---

## Path 3 — llama.cpp (CPU or small GPU, GGUF)

```bash
brew install llama.cpp          # or build from source
llama-server -hf unsloth/Qwen3-8B-GGUF:Q4_K_M --port 8080 --jinja
```

```
/connect http://localhost:8080/v1 qwen3-8b
```

`--jinja` enables the chat template with tool support. mantis's capability
layer knows llama.cpp's grammar-constrained tool mode and routes accordingly.
(`mantis setup` also has a guided llama.cpp flow: `mantis setup llamacpp`.)

---

## Path 4 — Ollama (local or a remote box)

Local is zero-config (mantis's default backend). For a **remote** Ollama box:

```bash
# on the GPU box
OLLAMA_HOST=0.0.0.0 ollama serve
ollama pull qwen3:8b
```

```bash
# on your laptop
export OLLAMA_HOST=gpu-box:11434     # mantis honors this
mantis
```

or explicitly: `/connect http://gpu-box:11434/v1 qwen3:8b`.

---

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
- Slow first tokens on small local models: expected prefill; mantis already
  slims the tool belt and keeps the prompt prefix stable so follow-up turns
  reuse the KV cache.

## Let your agent do all of this

There's a [skill](/skill) for that: give Claude Code (or mantis itself) the
**selfhost skill** and say *"host GLM-4-9B for me"* — it writes the Modal app,
deploys it, waits for the health check, and hands you the `/connect` line.
