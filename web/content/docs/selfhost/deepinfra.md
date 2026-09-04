# Self-host on DeepInfra

Dedicated vLLM deployments behind DeepInfra's shared OpenAI-compatible API:
one API call, no image to build, scale-to-zero, and the cheapest managed
H100 in this list ($2.20/h; A100-80 $0.89/h, H200 $2.69/h, B200 $3.69/h).
Trade-offs: five GPU strings, no logs API, no quantised checkpoints.

`mantis-agent deploy up deepinfra <model> --gpu H100-80GB` creates the
deployment and waits for it; `--gpu H100-80GB:2` for multi-GPU (four max).
See [Deploy — bring your own GPU](/docs/guides/deploy).

## Get a key for mantis

1. Sign in at [deepinfra.com](https://deepinfra.com/login) — GitHub, Google
   or email.
2. [Dashboard → API Keys](https://deepinfra.com/dash/api_keys) → **New API
   key**; name it, copy it (shown once).
3. Add a payment method under Billing — dedicated GPUs need one on file.
   Usage-based, invoiced as you cross spend thresholds; no advertised free
   credit for dedicated GPUs.

```bash
mantis-agent deploy creds deepinfra --set DEEPINFRA_API_KEY=…
mantis-agent deploy up deepinfra Qwen/Qwen3-8B --gpu A100-80GB
```

Gated models: `mantis-agent deploy creds hf --set HF_TOKEN=hf_…`; the
adapter passes it as the deployment's `hf.token`.

## Connect

`up` connects for you. Manually: the base URL is the shared
`https://api.deepinfra.com/v1/openai` and the model name is
`deploy_id:<id>` — that is what `served_model_name` holds.

```
/connect https://api.deepinfra.com/v1/openai deploy_id:<id>
```

Auth is the same API key as a Bearer token.

## Teardown

```bash
mantis-agent deploy down <id>
```

Or from [deepinfra.com/dash/deployments](https://deepinfra.com/dash/deployments).
`deploy logs` has nothing to show here — read them in the console.
