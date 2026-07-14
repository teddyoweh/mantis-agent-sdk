#!/usr/bin/env python3
"""Example: Exa MCP via mantis_agent_sdk.

Requires an Exa API key. Either set EXA_API_KEY or pass a full MCP URL via
EXA_MCP_URL. The example enables Exa search, advanced search, and fetch tools.
"""

from __future__ import annotations

import asyncio
import os

import mantis_agent_sdk as sdk


def exa_mcp_url() -> str:
    explicit = os.environ.get("EXA_MCP_URL")
    if explicit:
        return explicit
    key = os.environ.get("EXA_API_KEY")
    if not key:
        raise SystemExit("Set EXA_API_KEY or EXA_MCP_URL before running this example")
    return (
        "https://mcp.exa.ai/mcp"
        f"?exaApiKey={key}"
        "&tools=web_search_exa,web_fetch_exa,web_search_advanced_exa"
    )


async def main() -> None:
    async for message in sdk.query(
        prompt=(
            "Use Exa MCP to search for the current Exa MCP server available "
            "tools, then fetch the Exa MCP README and summarize the correct "
            "tool argument shape for web_fetch_exa."
        ),
        options=sdk.MantisAgentOptions(
            model=os.environ.get("MANTIS_MODEL", "gpt-5.6-sol"),
            backend=os.environ.get("MANTIS_BACKEND", "https://api.openai.com/v1"),
            mcp_servers={
                "exa": {
                    "type": "http",
                    "url": exa_mcp_url(),
                }
            },
            max_turns=6,
            include_memory=False,
        ),
    ):
        if isinstance(message, sdk.AssistantMessage):
            print("".join(getattr(block, "text", "") for block in message.content))
        elif isinstance(message, sdk.ResultMessage):
            print("\n---")
            print(message.subtype, message.usage)


if __name__ == "__main__":
    asyncio.run(main())
