# Self-host on Lambda

Clean per-hour GPU instances (A10 → H100 → B200) with simple pricing and
fast provisioning. You get a raw Ubuntu box: SSH in, run vLLM.

## Credentials

[cloud.lambda.ai → API keys](https://cloud.lambda.ai/api-keys) →
`LAMBDA_API_KEY` (for their instance API) — the console alone is enough for
manual use. Add an SSH key under **SSH keys**.

## Deploy

1. Console → **Instances → Launch** — pick a GPU (1× A10 runs 8-9B; 1× H100
   runs 32B), your SSH key, launch
2. SSH in and serve:

```bash
ssh ubuntu@<instance-ip>
pip install vllm
vllm serve Qwen/Qwen3-32B --host 0.0.0.0 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser hermes
```

3. Reach it — pick one:
   - **SSH tunnel** (simplest, secure): `ssh -N -L 8000:localhost:8000 ubuntu@<ip>`
     then `/connect http://localhost:8000/v1 Qwen/Qwen3-32B`
   - Open the port in the instance firewall and connect directly (put
     `--api-key sk-something` on vLLM if you do)

## Teardown

Terminate the instance in the console — billing is per-hour while it exists,
idle or not. Weights re-download next time (or park them on a persistent
filesystem).

## Let your agent do it

```bash
mkdir -p ~/.claude/skills/selfhost-lambda
curl -o ~/.claude/skills/selfhost-lambda/SKILL.md https://mantisagent.cc/skills/lambda.md
```
