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
        options={
            "model": "gpt-4o",
            "max_usd": 0.50,    # 50 cents, hard cap
        },
    ):
        ...
except BudgetExceededError as e:
    print(f"Stopped at ${e.spent:.4f} of ${e.cap:.2f}")
```

The error preserves the partial transcript on disk so you can fork from
a checkpoint and continue with a fresh cap.

## `max_turns`

Cap the number of model calls regardless of cost:

```python
options = {
    "model": "qwen2.5:7b",
    "max_turns": 10,
}
```

After the 10th `assistant` message, the runtime stops, even if the model
emitted another `tool_use`. The final `ResultMessage` will have
`stop_reason='max_turns'`.

## Reading cost on the result

```python
async for msg in query(...):
    if msg.type == "result":
        print(f"Cost: ${msg.total_cost_usd:.4f}")
        for model_id, usage in msg.modelUsage.items():
            print(f"  {model_id}: {usage.input_tokens}in / "
                  f"{usage.output_tokens}out / ${usage.cost_usd:.4f}")
```

`modelUsage` is per-model — useful when sub-agents on different models
contributed to the same session.

## Pricing table

The pricing table lives in `mantis_agent/budget.py` as a `dict[str,
tuple[float, float]]` of `(input_per_million, output_per_million)`. The
runtime looks up the model by name and applies the rates.

Open-source models running locally have a price of `(0, 0)` — they
contribute to token counts but not dollar cost. That keeps `max_usd`
meaningful in mixed sessions (some sub-agents on hosted models, some on
local ones).

### Adding / overriding a model

```python
from mantis_agent.budget import register_pricing

register_pricing("my-org/my-finetune", input=1.50, output=3.00)
```

Or pass a one-off override on the options:

```python
options = {
    "model": "my-org/my-finetune",
    "pricing_override": {"input": 1.50, "output": 3.00},
}
```

## Cost from outside the session

```python
from mantis_agent.budget import estimate_cost

cost = estimate_cost(
    model="gpt-4o-mini",
    input_tokens=1_200,
    output_tokens=400,
)
print(f"${cost:.6f}")
```

## Budgeting in a multi-step pipeline

If you compose multiple `query()` calls, set per-call budgets *and* a
session-level budget:

```python
async with ClaudeSDKClient(ClaudeAgentOptions(
    model="gpt-4o-mini",
    max_usd=1.00,          # cap for the whole client lifetime
)) as client:
    async for msg in client.query("...", max_usd=0.20):  # cap this call
        ...
    async for msg in client.query("...", max_usd=0.20):
        ...
```

`max_usd` on `client.query` is the call-level cap; the session-level cap
on `ClaudeAgentOptions` is the absolute ceiling. Both are enforced.
