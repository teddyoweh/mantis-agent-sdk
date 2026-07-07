---
name: selfhost-vastai
description: >
  Rent the cheapest suitable GPU on the Vast.ai marketplace, run vLLM over
  SSH, tunnel the port, and hand back a working URL + connect command. Use
  when the user says "host <model> on Vast" or wants the cheapest GPUs.
license: Apache-2.0
---

# selfhost-vastai

## 1. Credentials
`pip install vastai && vastai set api-key $VAST_API_KEY`
(key from cloud.vast.ai → Keys). Verify: `vastai show user` succeeds.

## 2. Rent
```bash
vastai search offers 'gpu_name=RTX_4090 num_gpus=1 inet_down>200 reliability>0.98' -o 'dph+' | head -5
vastai create instance <offer-id> --image pytorch/pytorch:latest --disk 60 --ssh --direct
vastai show instances     # wait for running; note ssh host + port
```
Pick 4090/A5000 for 8-9B (4-bit), A100-80 for 32B. Prefer reliability>0.98
offers — marketplace boxes vary.

## 3. Serve + tunnel (never expose an unauthenticated port on a marketplace box)
```bash
ssh -p <port> root@<host> \
  'pip install -q vllm && nohup vllm serve <MODEL> --host 0.0.0.0 --port 8000 \
   --enable-auto-tool-choice --tool-call-parser <parser> > vllm.log 2>&1 &'
ssh -p <port> -f -N -L 8000:localhost:8000 root@<host>
```

## 4. Verify (mandatory)
`curl -sf http://localhost:8000/v1/models` then one real completion. Host
flaky/unreachable → destroy and rent the next offer; that's normal on Vast.

## 5. Hand back
```
✅ <MODEL> live on Vast.ai (~$<dph>/h)
URL:      http://localhost:8000/v1   (SSH tunnel)
Connect:  /connect http://localhost:8000/v1 <MODEL>
Teardown: vastai destroy instance <id>   — bills while it exists
```
