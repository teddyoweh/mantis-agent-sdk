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
async for msg in query(prompt="...", options={"model": "deepseek-r1:1.5b"}):
    if msg.type == "assistant":
        for block in msg.message["content"]:
            if block["type"] == "thinking":
                # Hide from end users; show in a "details" UI element
                ...
            elif block["type"] == "text":
                print(block["text"])
```

## Enabling / disabling

Some models gate thinking behind a request flag. Setting
`include_thinking=False` strips it from the request body where supported,
and removes any inline `<think>` blocks the model emits anyway:

```python
options = {
    "model": "deepseek-r1:1.5b",
    "include_thinking": False,
}
```

The default is `True` (surface everything).

## Capability check

```python
from mantis_agent import lookup_model

cap = lookup_model("deepseek-r1:1.5b")
print(cap.supports_thinking)        # True
print(cap.thinking_format)          # 'inline' | 'out_of_band' | 'hidden'
```

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
