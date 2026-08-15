# Thinking blocks

Models that reason out loud — DeepSeek-R1, QwQ, Marco-o1, R1-distills, and
the OpenAI reasoning family — emit "thinking" content separately from the
final answer. `mantis-agent-sdk` normalises both forms into a single
`ThinkingBlock` you can render or hide.

## Two formats, one block

| Source | Wire format | Surface |
|---|---|---|
| DeepSeek-R1 / QwQ / Marco-o1 | Inline `<think>...</think>` in the text stream. | `ThinkingBlock` |
| DeepSeek API (out-of-band) | `thinking` field on the message, separate from `content`. | `ThinkingBlock` |
| OpenAI o1 / o3 / o4 | API doesn't expose tokens — only counts. | Not surfaced. |

The runtime parses `<think>` tags out of the text stream so by the time
you see content, thinking is its own block.

## Streaming

```python
async for event in agent.run_iter("..."):
    if event.type == "thinking_delta":
        print(f"\033[2m{event.text}\033[0m", end="", flush=True)
    elif event.type == "text_delta":
        print(event.text, end="", flush=True)
```

`thinking_delta` events stream as the thinking text arrives; `text_delta`
events are the final answer.

## Flat-shape messages

In `query()` output, `ThinkingBlock` appears in `message.content`:

```python
from mantis_agent import MantisAgentOptions, query

async for msg in query(
    prompt="...", options=MantisAgentOptions(model="deepseek-r1:1.5b")
):
    if msg.type == "assistant":
        for block in msg.content:
            # Blocks are objects, not dicts — check the attribute you want.
            if getattr(block, "thinking", None):
                ...      # hide from end users; show in a "details" element
            elif getattr(block, "text", None):
                print(block.text)
```

## Enabling / disabling

Some models gate thinking behind a request flag. Setting
`include_thinking=False` strips it from the request body where supported,
and removes any inline `<think>` blocks the model emits anyway:

There is no `include_thinking` option. What exists:

```python
from mantis_agent import MantisAgentOptions

# Anthropic-style thinking block, passed through to providers that take one.
options = MantisAgentOptions(
    model="claude-opus-5",
    backend="anthropic",
    thinking={"type": "enabled", "budget_tokens": 4096},
)

# Cap the reasoning budget without naming a provider shape.
capped = MantisAgentOptions(model="deepseek-r1:1.5b", max_thinking_tokens=2048)

# Or steer effort: "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | "none"
effortful = MantisAgentOptions(model="gpt-5.6", effort="high")

# reasoning_mode picks the provider's reasoning family where it has one:
# "standard" or "pro".
pro = MantisAgentOptions(model="gpt-5.6", reasoning_mode="pro")
```

These are *control* keys: they never reach a provider's wire verbatim. Each
adapter translates the ones it supports (Anthropic's `thinking` block, OpenAI's
`reasoning_effort`, Ollama's `think`) and drops the rest, so setting one can't
400 a provider that has never heard of it.

Inline `<think>` blocks a model emits anyway are parsed out of the text stream
regardless — see the capability check below.

## Capability check

```python
from mantis_agent import lookup_model

cap = lookup_model("deepseek-r1:1.5b")
print(cap.emits_thinking_blocks)    # True — reasoning arrives as its own blocks
print(cap.emits_inline_thinking)    # True — and/or inline in the text stream
print(cap.inline_thinking_tags)     # the tag pairs stripped from that stream
```

(`supports_thinking` and `thinking_format` never existed on `ModelCapability`;
these three are the real fields.)

## When you want to *use* thinking

Thinking blocks are useful for:

- **Debugging.** When a tool call goes wrong, the thinking trail shows
  why.
- **Self-consistency.** Re-running with a different seed and comparing
  thinking can surface confidence.
- **UX.** Render thinking in a collapsed disclosure so users can audit
  reasoning without it dominating the chat.

They are *not* useful as final output. Always render the regular text
blocks as the user-facing answer.
