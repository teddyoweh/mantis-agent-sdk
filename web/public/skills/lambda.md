---
name: selfhost-lambda
description: >
  Rent a Lambda GPU instance, run vLLM on it over SSH, tunnel the port, and
  hand back a working OpenAI-compatible URL + connect command. Use when the
  user says "host <model> on Lambda" or has a Lambda account.
license: Apache-2.0
---

# selfhost-lambda

## 1. Credentials
`LAMBDA_API_KEY` (cloud.lambda.ai → API keys) + an SSH key registered.
Verify: `curl -su $LAMBDA_API_KEY: https://cloud.lambda.ai/api/v1/instance-types | head -c 200`.

## 2. Launch
Pick the smallest fitting GPU (A10 → 8-9B, 1× H100 → 32B, 2× H100 → 70B):

```bash
curl -su $LAMBDA_API_KEY: https://cloud.lambda.ai/api/v1/instance-operations/launch \
  -d '{"region_name":"<region>","instance_type_name":"gpu_1x_a10",
       "ssh_key_names":["<key>"],"quantity":1}' -H 'Content-Type: application/json'
```

Poll `/api/v1/instances` until status=active, grab the IP.

## 3. Serve + tunnel
```bash
ssh -o StrictHostKeyChecking=accept-new ubuntu@<ip> \
  'pip install -q vllm && nohup vllm serve <MODEL> --host 0.0.0.0 --port 8000 \
   --enable-auto-tool-choice --tool-call-parser <parser> > vllm.log 2>&1 &'
# wait for "Application startup complete" in vllm.log, then local tunnel:
ssh -f -N -L 8000:localhost:8000 ubuntu@<ip>
```

## 4. Verify (mandatory)
`curl -sf http://localhost:8000/v1/models` then one real completion.

## 5. Hand back
```
✅ <MODEL> live on Lambda (<instance-type>, ~$<rate>/h until terminated)
URL:      http://localhost:8000/v1   (via SSH tunnel to <ip>)
Connect:  /connect http://localhost:8000/v1 <MODEL>
Teardown: terminate via console or /api/v1/instance-operations/terminate
          — Lambda bills per hour while the instance EXISTS, idle or not.
```
