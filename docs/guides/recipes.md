# Recipes

Complete scripts for the things people actually build. Every one here was run
against a local `qwen2.5-coder:7b` on Ollama — no keys, no cloud. The outputs are
real, but a model's exact wording and turn count vary run to run, so treat them
as representative rather than literal. Swap the model and `base_url` for a hosted
provider and nothing else changes.

## A coding agent that edits your files

The built-in tools ship as one bundle. Give it a directory and a job.

```python
import asyncio
import pathlib
import tempfile

from mantis_agent import MantisAgentOptions, query
from mantis_agent.builtin_tools import bash, read_file, write_file

WORK = pathlib.Path(tempfile.mkdtemp())


async def main() -> None:
    async for msg in query(
        prompt=(
            "Use write_file to create fizzbuzz.py with a function fizzbuzz(n). "
            "Then use write_file to create test_fizzbuzz.py with asserts for "
            "3, 5, 15 and 7. Then use bash to run 'python test_fizzbuzz.py'."
        ),
        options=MantisAgentOptions(
            model="qwen2.5-coder:7b",
            tools=[write_file, read_file, bash],
            cwd=str(WORK),            # every relative path lands in here
            max_turns=14,
            permission_mode="bypass",  # unattended; see the guardrail recipe
            skills=[],                 # don't inherit ~/.mantis-agent/skills
        ),
    ):
        if msg.type == "assistant":
            for block in msg.content:
                if getattr(block, "name", None):
                    print("[tool]", block.name)
        elif msg.type == "result":
            print(f"[{msg.subtype}] {msg.num_turns} turns {msg.errors}")

    print(sorted(p.name for p in WORK.iterdir()))


asyncio.run(main())
```

```text
[tool] write_file
[tool] write_file
[tool] bash
[success] 5 turns []
['fizzbuzz.py', 'test_fizzbuzz.py']
```

Three tools on purpose. The full bundle is one import —
`from mantis_agent.builtin_tools import CODING_TOOLS`, which is `bash`,
`bash_output`, `bash_kill`, `monitor`, `read_file`, `write_file`, `edit_file`,
`multi_edit`, `notebook_edit`, `ls`, `glob`, `grep`, `sleep` — and it's the right
call for a frontier model. A 7B given thirteen tools wanders: in testing it
opened with `sleep` before doing any work. Narrow the belt to the job and small
models get sharply more reliable.

!!! tip "`cwd` is load-bearing"

    It scopes the file tools *and* is what the model is told its working
    directory is. Leave it unset and relative paths resolve against your
    process's directory, which is rarely what you want for a server.

!!! warning "Skills load themselves unless you say otherwise"

    With `skills` unset, any skill in `~/.mantis-agent/skills/` (or the
    project's) whose description matches the prompt is injected into the
    conversation, and the model will happily call it. That's the terminal's
    behavior inherited by the library — so an agent's behavior can depend on
    files in whoever's home directory ran it. `skills=[]` turns it off;
    `skills=["name"]` preloads exactly what you choose. Worth being explicit
    about in anything you ship.

## Extract structured JSON from prose

Hand over the type you already have; get an instance back.

```python
import asyncio
from dataclasses import dataclass

from mantis_agent import MantisAgentOptions, query


@dataclass
class Invoice:
    vendor: str
    total_usd: float
    due_date: str


TEXT = (
    "Invoice from Northwind Traders, amount due $1,240.50, "
    "payable by 2026-09-01. Reference NW-7781."
)


async def main() -> None:
    async for msg in query(
        prompt=f"Extract the invoice fields:\n\n{TEXT}",
        options=MantisAgentOptions(
            model="qwen2.5-coder:7b",
            response_model=Invoice,
            max_turns=2,
        ),
    ):
        if msg.type == "result":
            invoice = msg.parsed
            print(invoice, "| plus tax:", invoice.total_usd * 1.2)


asyncio.run(main())
```

```text
Invoice(vendor='Northwind Traders', total_usd=1240.5, due_date='2026-09-01') | plus tax: 1488.6
```

`response_model` takes a dataclass, a `msgspec.Struct`, a `TypedDict`, or a
pydantic model. It derives the JSON schema, sets the provider's
`response_format`, and decodes the reply onto `result.parsed` — so field names
live in one place instead of two.

A few behaviors worth knowing:

- **Fenced replies are handled.** Small models answer JSON in a ```` ```json ````
  block constantly; the fence is stripped before parsing.
- **A parse failure is a run failure.** You asked for an `Invoice`; getting
  `parsed=None` with a success flag would be a silent failure. The reason lands
  in `result.errors` (with the head of what the model actually said), and
  `raise_on_error=True` turns it into an exception.
- **An explicit `response_format` wins.** If you hand-wrote the envelope, it's
  kept as-is.

Prefer the raw envelope? `response_format` still works exactly as before — see
[Models and backends](models-and-backends.md).

## Stop the model from doing something dangerous

```python
import asyncio

from mantis_agent import (
    MantisAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    query,
    tool,
)


@tool
async def delete_account(user_id: str) -> str:
    """Permanently delete a user account."""
    return f"deleted {user_id}"


@tool
async def get_user(user_id: str) -> str:
    """Look up a user's status."""
    return f"{user_id}: active since 2024"


async def can_use_tool(tool_name, tool_input, ctx):
    # tool_name is a string; tool_input is the arguments the model produced.
    if tool_name == "delete_account":
        return PermissionResultDeny(message="Deleting accounts requires a human.")
    return PermissionResultAllow()


async def main() -> None:
    async for msg in query(
        prompt="Delete account u-42, then tell me its status.",
        options=MantisAgentOptions(
            model="qwen2.5-coder:7b",
            tools=[delete_account, get_user],
            can_use_tool=can_use_tool,
            max_turns=6,
        ),
    ):
        if msg.type == "result":
            print(f"[{msg.subtype}] blocked {len(msg.permission_denials)} call(s)")


asyncio.run(main())
```

```text
[success] blocked 2 call(s)
```

The denial goes back to the model as a tool error, so it adapts instead of
dying — here it gave up on deleting and reported the status instead. Every
blocked call is listed in `result.permission_denials` for auditing.

For rule-based gating instead of a callback, see
[Permissions](permissions.md); to confine shell commands at the OS level, see
`MANTIS_SANDBOX` in [Configuration](../getting-started/configuration.md).

## Fail loudly instead of quietly

`query()` reports provider failures on the final message and never raises. That
is right for a streaming API — you can render the partial transcript, then the
error — but it makes the silent version the easy one to write:

```python
import asyncio

from mantis_agent import AgentError, MantisAgentOptions, query


async def main() -> None:
    options = MantisAgentOptions(
        model="qwen2.5-coder:7b",
        max_turns=2,
        raise_on_error=True,      # the result is still yielded first
    )
    try:
        async for msg in query(prompt="hello", options=options):
            if msg.type == "result" and msg.is_error:
                print("errors:", msg.errors)
    except AgentError as e:
        print("raised:", e)


asyncio.run(main())
```

Without `raise_on_error`, check `msg.is_error` and read `msg.errors` yourself —
`["ProviderError: Not Found (404 from http://localhost:8000/v1/chat/completions)
— port 8000 is the vLLM default …"]`. Errors name the URL they tried, so a
wrong port or an unpulled model diagnoses itself.

## Hold a conversation

`ClaudeSDKClient` keeps history across calls.

```python
import asyncio

from mantis_agent import ClaudeSDKClient, MantisAgentOptions, tool


@tool
async def get_stock(symbol: str) -> str:
    """Current share price for a ticker symbol."""
    return {"ACME": "$41.20", "GLOB": "$7.05"}.get(symbol.upper(), "unknown")


async def main() -> None:
    options = MantisAgentOptions(
        model="qwen2.5-coder:7b", tools=[get_stock], max_turns=4
    )
    async with ClaudeSDKClient(options=options) as client:
        for prompt in ("What is ACME trading at?", "And what did I just ask about?"):
            await client.query(prompt)
            async for msg in client.receive_response():
                if msg.type == "assistant":
                    for block in msg.content:
                        if getattr(block, "text", None):
                            print(">", block.text.strip())


asyncio.run(main())
```

```text
> ACME is trading at $41.20.
> You just asked about the current share price of ACME.
```

## Delegate to a sub-agent

Wrap a whole agent as a tool the parent can call. Useful for giving a narrow job
a narrow tool belt, a cheaper model, or its own turn cap.

```python
import asyncio

from mantis_agent import MantisAgentOptions, SubAgentSpec, as_subagent_tool, query, tool


@tool
async def get_stock(symbol: str) -> str:
    """Current share price for a ticker symbol."""
    return {"ACME": "$41.20", "GLOB": "$7.05"}.get(symbol.upper(), "unknown")


analyst = as_subagent_tool(
    SubAgentSpec(
        name="stock_analyst",
        description="Looks up share prices and reports them.",
        system_prompt="Look up the price with get_stock and state it plainly.",
        model="qwen2.5-coder:7b",
        backend="http://localhost:11434",   # or share the parent's provider
        tools=[get_stock],
        max_turns=4,
    )
)


async def main() -> None:
    async for msg in query(
        prompt="What is GLOB trading at? Use your delegate.",
        options=MantisAgentOptions(
            model="qwen2.5-coder:7b",
            tools=[analyst],
            system_prompt="Delegate price questions to the stock_analyst tool.",
            max_turns=4,
        ),
    ):
        if msg.type == "assistant":
            for block in msg.content:
                if getattr(block, "name", None):
                    print("[delegates to]", block.name)
                elif getattr(block, "text", None):
                    print(">", block.text.strip())


asyncio.run(main())
```

```text
[delegates to] stock_analyst
> GLOB is trading at $7.05.
```

A spec needs a **destination**, not just a model name: pass `backend=` as above,
or `as_subagent_tool(spec, parent_provider=parent.provider)` to reuse the
parent's HTTP pool (cheaper, and the recommended form when both run on the same
backend).

## Cap what a run can cost

```python
from mantis_agent import BudgetExceededError, MantisAgentOptions, query


async def main() -> None:
    options = MantisAgentOptions(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        max_budget_usd=0.50,
        fallback_model="deepseek-chat",   # retried once if a turn dies pre-output
        max_turns=20,
    )
    try:
        async for msg in query(prompt="big job", options=options):
            pass
    except BudgetExceededError as e:
        print(f"stopped at ${e.used:.4f} of ${e.limit:.2f}")
```

A cap only bites where the model has a price — local models cost `$0.0000`, so
`max_turns` is your limit there. See [Budget and limits](budget.md).

## Test without a model

`backend="mock"` swaps in a scripted provider. Same agent loop, no network, so
tool dispatch and message shapes are exercised in CI with no keys.

```python
from mantis_agent import Agent

agent = Agent(model="mock", backend="mock")
print(type(agent.provider).__name__)
```

```text
MockProvider
```

For a scripted conversation, hand it a provider that yields your own stream
events — `tests/test_can_use_tool_contract.py` in the repo is a compact example.

## See what the agent is doing

`agent.stream()` gives you the raw event stream for one turn — tokens as they
arrive, tool calls as they form.

```python
import asyncio
import sys

from mantis_agent import Agent, UserMessage
from mantis_agent.events import ContentBlockDelta, TextDelta


async def main() -> None:
    agent = Agent(model="qwen2.5-coder:7b", backend="http://localhost:11434")
    try:
        async for event in agent.stream([UserMessage(content="Name one moon of Mars.")]):
            if isinstance(event, ContentBlockDelta) and isinstance(event.delta, TextDelta):
                sys.stdout.write(event.delta.text)
                sys.stdout.flush()
    finally:
        await agent.aclose()


asyncio.run(main())
```

`stream()` covers one assistant turn and does not run the loop — append the
result and call it again to continue. Full event taxonomy in
[Streaming](streaming.md).
