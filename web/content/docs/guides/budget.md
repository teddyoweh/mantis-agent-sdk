# Budget and limits

`mantis-agent-sdk` tracks token usage and dollar cost across every turn of
every session. You can cap either, and every `ResultMessage` carries the
final accounting.

## `max_usd`

Hard ceiling on session cost. If the next request would push spend over
the cap, the runtime raises `BudgetExceededError` *before* dispatching
it.

```python
from mantis_agent import BudgetExceededError, query

try:
    async for msg in query(
        prompt="big task",
        options=MantisAgentOptions(
            model="deepseek-chat",
            backend="https://api.deepseek.com/v1",
            max_budget_usd=0.50,    # 50 cents, hard cap
        ),
    ):
        ...
except BudgetExceededError as e:
    # kind is "usd" or "turns"; limit is the cap, used is the spend at the stop.
    print(f"Stopped at ${e.used:.4f} of ${e.limit:.2f} ({e.kind})")
```

The exception carries `kind`, `limit`, and `used`. The partial transcript stays
on disk, so you can fork from a checkpoint and continue under a fresh cap.

## `max_turns`

Cap the number of model calls regardless of cost:

```python
options = MantisAgentOptions(
    model="qwen2.5:7b",
    max_turns=10,
)
```

After the 10th `assistant` message, the runtime stops, even if the model
emitted another `tool_use`. The final `ResultMessage` will have
`stop_reason='max_turns'`.

## Reading cost on the result

```python
async for msg in query(prompt="...", options=MantisAgentOptions(
    model="deepseek-chat",
)):
    if msg.type == "result":
        print(f"Cost: ${msg.total_cost_usd:.4f}")
        for model_id, usage in msg.modelUsage.items():
            print(f"  {model_id}: {usage.inputTokens}in / "
                  f"{usage.outputTokens}out / ${usage.costUSD:.4f}")
```

`modelUsage` is per-model — useful when sub-agents on different models
contributed to one session. Its field names are camelCase for byte-level TS-SDK
parity: `inputTokens`, `outputTokens`, `cacheReadInputTokens`,
`cacheCreationInputTokens`, `webSearchRequests`, `costUSD`, `contextWindow`,
`maxOutputTokens`.

## The pricing table

`PRICING_TABLE` maps a **`(provider, model_id)` tuple** to a `Pricing` record:

```python
from mantis_agent.budget import PRICING_TABLE, lookup_pricing

# The backend hint is what identifies the provider half of the key.
print(lookup_pricing("deepseek-v3", "https://api.deepseek.com/v1"))
print(len(PRICING_TABLE), "priced (provider, model) pairs")
```

`Pricing` carries `prompt_per_million`, `completion_per_million`, and optional
`cache_read_per_million` / `cache_write_per_million`.

Priced providers today: `deepseek`, `fireworks`, `groq`, `openrouter`,
`together`, `modal`, plus the local runners (`ollama`, `llamacpp`, `vllm`,
`tgi`) at zero. A model with no row prices as `None` and contributes tokens but
no dollars — which is what keeps `max_usd` meaningful in a mixed session where
some sub-agents run locally.

> **A cap only bites where there is a price.** Without a matching row, spend
> stays at `$0.00` and the cap is never reached. Check with `lookup_pricing`
> before relying on `max_usd`.

### Adding or overriding a model

There is no `register_pricing` function — insert into the table:

```python
from mantis_agent.budget import PRICING_TABLE, Pricing

PRICING_TABLE[("together", "my-org/my-finetune")] = Pricing(
    prompt_per_million=1.50,
    completion_per_million=3.00,
)
```

The first element is the provider key the backend URL resolves to; the second
is the model id as that provider spells it. (There is no per-call pricing
option either — earlier versions of this page showed a `pricing_override` key,
which never existed.)

## Cost from outside the session

```python
from mantis_agent.budget import estimate_cost
from mantis_agent.types import Usage

usage = Usage(input_tokens=1_200, output_tokens=400)
cost = estimate_cost(usage, "deepseek-v3", "https://api.deepseek.com/v1")
print("no pricing row" if cost is None else f"${cost:.6f}")
```

`estimate_cost(usage, model_id, backend_hint=None)` takes a `Usage` struct,
not loose token counts, and returns `None` for an unpriced model.

## Budgeting across several calls

`ClaudeSDKClient` holds one budget for its lifetime; there is no per-call cap
argument. For per-step ceilings, use a fresh options object per step:

```python
from mantis_agent import ClaudeSDKClient, MantisAgentOptions


async def run_step(prompt: str, cap: float) -> None:
    options = MantisAgentOptions(model="deepseek-chat", max_budget_usd=cap)
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            print(msg.type)
```

Note the field name: `max_budget_usd` on `MantisAgentOptions`, `max_usd` in a
plain options dict. They are the same cap under two names, and the wrong one in
the wrong place is silently ignored.
