---
name: selfhost-hf
description: >
  Stand up a Hugging Face Inference Endpoint (dedicated, OpenAI-compatible)
  for any Hub model, verify it, and hand back the URL + connect command. Use
  when the user says "host <model> on Hugging Face" or wants zero-ops
  dedicated serving.
license: Apache-2.0
---

# selfhost-hf

## 1. Credentials
`HF_TOKEN` — fine-grained token with *Inference Endpoints* scope
(hf.co → Settings → Access Tokens), billing enabled on the account.
Verify: `curl -s -H "Authorization: Bearer $HF_TOKEN" https://huggingface.co/api/whoami-v2`.

## 2. Create (API; console fallback if fields drift)
```bash
curl -s https://api.endpoints.huggingface.cloud/v2/endpoint/<username> \
  -X POST -H "Authorization: Bearer $HF_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"mantis-<model-short>","type":"protected",
       "model":{"repository":"Qwen/Qwen3-8B","framework":"pytorch","task":"text-generation"},
       "compute":{"accelerator":"gpu","instanceType":"nvidia-l4","instanceSize":"x1",
                  "scaling":{"minReplica":0,"maxReplica":1}},
       "provider":{"vendor":"aws","region":"us-east-1"}}'
```
If the API rejects fields, walk the user through the console flow instead
(model page → Deploy → Inference Endpoints) — do not invent parameters.

## 3. Verify (mandatory)
Poll the endpoint status until `running`, then
`curl -sf <URL>/v1/models -H "Authorization: Bearer $HF_TOKEN"` and one real
completion. Scale-to-zero cold-starts ~1 min.

## 4. Hand back
```
✅ <MODEL> live on HF Endpoints
URL:      <endpoint-url>/v1
Connect:  mantis --api-key $HF_TOKEN  →  /connect <endpoint-url>/v1 <MODEL>
Teardown: pause or delete the endpoint (Settings tab) — pausing stops billing
```
