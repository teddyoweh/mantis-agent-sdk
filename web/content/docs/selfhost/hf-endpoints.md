# Self-host on Hugging Face Inference Endpoints

The zero-ops option: pick any model on the Hub, click deploy, get a dedicated
OpenAI-compatible URL. HF runs vLLM/TGI for you on AWS/GCP/Azure capacity.

## Credentials

[hf.co → Settings → Access Tokens](https://huggingface.co/settings/tokens) →
a **fine-grained token** with *Inference Endpoints* scope → `HF_TOKEN`.
Billing: add a card at Settings → Billing (endpoints bill per minute).

## Deploy

1. Open the model page (e.g. `Qwen/Qwen3-8B`) → **Deploy → Inference
   Endpoints** — or go to [endpoints.huggingface.co](https://endpoints.huggingface.co)
2. Pick cloud/region/GPU (it suggests a fitting instance), set **scale-to-zero**
   if you want idle=free, create
3. When it's *Running*, copy the endpoint URL

## Connect

The endpoint speaks the OpenAI shape under `/v1`:

```
/connect https://<endpoint-id>.<region>.aws.endpoints.huggingface.cloud/v1 <model-id>
```

Auth is your `HF_TOKEN` as a bearer key: `mantis --api-key $HF_TOKEN` (or the
setup wizard's self-host flow).

## Teardown

Pause or delete the endpoint in the console. Scale-to-zero endpoints cold-start
like Modal's (~1 min).

## Let your agent do it

```bash
mkdir -p ~/.claude/skills/selfhost-hf
curl -o ~/.claude/skills/selfhost-hf/SKILL.md https://mantisagent.cc/skills/hf-endpoints.md
```
