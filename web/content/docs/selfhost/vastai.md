# Self-host on Vast.ai

A GPU *marketplace* — the cheapest 4090s/A100s anywhere, rented from other
people's machines. Great for experiments; expect variable reliability (hosts
can be interruptible).

## Credentials

[cloud.vast.ai → Keys](https://cloud.vast.ai/manage-keys/) → `VAST_API_KEY`.

```bash
pip install vastai
vastai set api-key $VAST_API_KEY
```

## Get a key for mantis

1. Verify your email first — new accounts have a tiny spend limit until then.
2. [Keys](https://cloud.vast.ai/manage-keys/) (Account → API Keys) → **+New**;
   full permissions, or scoped to `user_read`, `instance_read`,
   `instance_write`. Copy it — shown once.
3. Billing → add credit: **$5 minimum deposit**, prepaid, no free tier. The
   instance bills every hour it exists; `mantis-agent deploy down` is the
   off switch.
4. Optional — `HF_TOKEN` for gated repos (passed to the container).

```bash
mantis-agent deploy creds vastai --set VAST_API_KEY=…
```

## Deploy

Find a machine and rent it with the vLLM-friendly PyTorch image:

```bash
vastai search offers 'gpu_name=RTX_4090 num_gpus=1 inet_down>200' -o 'dph+' | head
vastai create instance <offer-id> \
  --image pytorch/pytorch:latest --disk 60 --ssh --direct
vastai show instances        # grab ssh host/port when it's running
```

SSH in (note the custom port) and serve:

```bash
ssh -p <port> root@<host>
pip install vllm
vllm serve Qwen/Qwen3-8B --host 0.0.0.0 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser hermes
```

Reach it via an SSH tunnel (recommended on a marketplace box — don't expose
an unauthenticated server):

```bash
ssh -p <port> -N -L 8000:localhost:8000 root@<host>
```

```
/connect http://localhost:8000/v1 Qwen/Qwen3-8B
```

## Teardown

```bash
vastai destroy instance <instance-id>     # bills while it exists
```

## Let your agent do it

```bash
mkdir -p ~/.claude/skills/selfhost-vastai
curl -o ~/.claude/skills/selfhost-vastai/SKILL.md https://mantisagent.cc/skills/vastai.md
```
