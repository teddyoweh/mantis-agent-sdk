# Guides

Topic-oriented walkthroughs. Pick the page that matches what you're trying
to do; each one is self-contained.

!!! info "One shape, everywhere"

    Every example in these guides uses `MantisAgentOptions` and the flat message
    shape:

    ```python
    from mantis_agent import MantisAgentOptions, query


    async def main() -> None:
        async for msg in query(
            prompt="hi", options=MantisAgentOptions(model="qwen2.5:7b")
        ):
            if msg.type == "assistant":
                for block in msg.content:          # flat: msg.content
                    print(getattr(block, "text", ""))
    ```

    `query()` also accepts a plain **dict**, which yields the nested wire shape
    (`msg.message.content`) for byte-level TS-SDK compatibility. It's a real,
    supported API — it is just not the one these pages teach, because mixing the
    two is the most common source of confusion here. The differences live in
    exactly two places: [the two option shapes](models-and-backends.md#the-two-option-shapes)
    and [How configuration works](how-it-works.md).

## New here?

Start with [Recipes](recipes.md) — complete, runnable scripts for the common
jobs — then dip into the reference pages below when you need detail.

## By task

- **See a complete working script** → [Recipes](recipes.md)
- **Understand precedence and silent keys** → [How configuration works](how-it-works.md)
- **Pick a model** → [Models and backends](models-and-backends.md)
- **Write a tool** → [Tools](tools.md)
- **Stream tokens + dispatch tools mid-response** → [Streaming](streaming.md)
- **Approve / deny / rewrite tool calls** → [Permissions](permissions.md)
- **Hook into the agent lifecycle** → [Hooks](hooks.md)
- **Fork or resume a conversation** → [Sessions and resume](sessions.md)
- **Surface reasoning blocks** → [Thinking blocks](thinking.md)
- **Cap spend and turn count** → [Budget and limits](budget.md)
- **Add an MCP server** → [MCP servers](mcp.md)
- **Compose multiple agents** → [Sub-agents](sub-agents.md)
- **Bundle a tool set as a plugin** → [Plugins](plugins.md)
- **Persist agent memory** → [Memory](memory.md)
