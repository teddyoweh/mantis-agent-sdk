---
name: selfhost-runpod
description: >
  Deploy an open model on RunPod — Serverless vLLM worker (OpenAI-compatible,
  autoscaling) or a raw Pod — verify it answers, hand back the URL + connect
  command. Use when the user says "host <model> on RunPod" or wants cheap
  spot GPUs.
license: Apache-2.0
---

# selfhost-runpod

## 1. Credentials
Need `RUNPOD_API_KEY` (runpod.io → Settings → API Keys) with credit loaded.
Verify: `curl -s -H "Authorization: Bearer $RUNPOD_API_KEY" https://rest.runpod.io/v1/endpoints` returns 200.

## 2. Prefer Serverless (their vLLM worker)
Create via REST (or walk the user through Console → Serverless → vLLM if the
API shape fights you — do not guess undocumented fields):

- endpoint template: vLLM worker, env `MODEL_NAME=<model>`, workers min 0
- GPU tier: 24GB (8-9B models) / 80GB (32B)

The endpoint exposes OpenAI-compat at:
`https://api.runpod.ai/v2/<endpoint-id>/openai/v1`

## 3. Verify (mandatory)
```bash
curl -s https://api.runpod.ai/v2/<id>/openai/v1/models \
  -H "Authorization: Bearer $RUNPOD_API_KEY"        # poll until 200
# then one real chat completion the same way
```
Cold start on min-workers-0 can take minutes — poll, don't give up early.

## 4. Pod fallback (user asked for a raw box)
Deploy a Pod (PyTorch template, expose HTTP port 8000), then in its terminal:
`pip install vllm && vllm serve <MODEL> --host 0.0.0.0 --port 8000
--enable-auto-tool-choice --tool-call-parser <parser>`.
URL: `https://<pod-id>-8000.proxy.runpod.net/v1`. Remind: pods bill while
running — stop when done.

## 5. Hand back
```
✅ <MODEL> live on RunPod
URL:      https://api.runpod.ai/v2/<id>/openai/v1
Connect:  mantis --api-key $RUNPOD_API_KEY  →  /connect <URL> <MODEL>
Teardown: delete the endpoint (or stop the pod) in the console
```
