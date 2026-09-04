# Self-host on Baseten

Managed deployments with the fullest REST control plane of the bunch —
instance prices, build and runtime logs, autoscaling — on per-minute GPU
billing that scales to zero. Pricier cards than the marketplaces
(H100 $6.50/h, A100 $4.00/h, L4 ≈ $0.85/h) but the least babysitting.

`mantis-agent deploy up baseten <model> --gpu H100` uploads a tiny
[Truss](https://docs.baseten.co) config on the upstream `vllm/vllm-openai`
image with `weights: hf://…`, so Baseten mirrors the checkpoint once, then
polls the build to *ACTIVE* — 5–10 minutes the first time, ~10 s cold starts
after. See [Deploy — bring your own GPU](/docs/guides/deploy).

## Get a key for mantis

1. Sign up at [app.baseten.co](https://app.baseten.co/signup). New accounts
   include free credits to experiment; add a card under Billing when they
   run out.
2. [Organization settings → API keys](https://app.baseten.co/settings/api_keys)
   → **Create API key**. A *Personal* key is fine locally; a *Team* key needs
   **Full access** (deploy is more than *Inference only*).
3. Copy the key — `abcd1234.…`, an 8-character prefix, a dot, then the
   secret; shown once.

```bash
mantis-agent deploy creds baseten --set BASETEN_API_KEY=…
mantis-agent deploy gpus baseten            # accelerators with live prices
mantis-agent deploy up baseten Qwen/Qwen3-8B --gpu A100
```

Gated models: `mantis-agent deploy creds hf --set HF_TOKEN=hf_…` once; the
adapter stores it as the workspace secret `hf_access_token`.

## Connect

`up` connects for you. Manually, the endpoint is
`https://model-<id>.api.baseten.co/environments/production/sync/v1`, with the
API key as a Bearer token:

```
/connect https://model-<id>.api.baseten.co/environments/production/sync/v1 Qwen/Qwen3-8B
```

`model=` is whatever `--served-name` was (default: the HF id).

## Teardown

```bash
mantis-agent deploy down <id>
```

Or delete the deployment at [app.baseten.co/models](https://app.baseten.co/models).
Scaled-to-zero deployments cost nothing while idle.
