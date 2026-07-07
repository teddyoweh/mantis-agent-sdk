# Self-host on RunPod

Two modes: **Serverless** (their vLLM worker — autoscaling, per-second, easy)
and **Pods** (a raw GPU you SSH into). Spot pricing makes it one of the
cheapest ways to run big models.

## Credentials

[runpod.io → Settings → API Keys](https://www.runpod.io/console/user/settings)
→ `RUNPOD_API_KEY`. Add credit (pay-as-you-go).

## Mode A — Serverless vLLM worker (recommended)

1. Console → **Serverless → New Endpoint → vLLM** preset
2. Set `MODEL_NAME` (e.g. `Qwen/Qwen3-8B`), pick GPU tier, min workers 0
3. Deploy → you get an endpoint id

RunPod exposes an **OpenAI-compatible URL** per endpoint:

```
/connect https://api.runpod.ai/v2/<endpoint-id>/openai/v1 Qwen/Qwen3-8B
```

Auth is a Bearer key — launch mantis with it:
`mantis --api-key $RUNPOD_API_KEY` (or paste it in the setup wizard's
self-host flow). Scale-to-zero cold starts behave like Modal's.

## Mode B — a Pod you control

1. Console → **Pods → Deploy** (PyTorch template, pick GPU, add a network
   volume if you want weight caching)
2. Expose HTTP port **8000** in the pod config
3. In the pod's web terminal / SSH:

```bash
pip install vllm
vllm serve Qwen/Qwen3-8B --host 0.0.0.0 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser hermes
```

4. Connect through RunPod's proxy:

```
/connect https://<pod-id>-8000.proxy.runpod.net/v1 Qwen/Qwen3-8B
```

Stop the pod when done — pods bill while running, even idle.

## Let your agent do it

```bash
mkdir -p ~/.claude/skills/selfhost-runpod
curl -o ~/.claude/skills/selfhost-runpod/SKILL.md https://mantisagent.cc/skills/runpod.md
```
