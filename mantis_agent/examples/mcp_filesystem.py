"""MCP example — filesystem server over stdio, driven by ``query()``.

Spawns the official ``@modelcontextprotocol/server-filesystem`` MCP
server as a subprocess, wires its tools into the agent registry, and
runs a single prompt that exercises one of those tools (``read_file``).

Prereqs::

    npm install -g @modelcontextprotocol/server-filesystem

Run::

    python -m mantis_agent.examples.mcp_filesystem
"""

from __future__ import annotations

import asyncio
import os

from mantis_agent import query


async def main() -> None:
    try:
        from mantis_agent.mcp import MCPClient
        from mantis_agent.mcp.types import StdioServerConfig
    except ImportError:
        print("MCP client module not present yet — skipping mcp_filesystem example.")
        return

    cwd = os.getcwd()
    server_cfg = StdioServerConfig(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", cwd],
    )

    async with MCPClient(server_cfg) as client:
        mcp_tools = await client.list_tools()
        tools = [mt.to_mantis_agent_tool(client) for mt in mcp_tools]

        async for msg in query(
            prompt=f"List the files in {cwd}.",
            options={
                "model": os.environ.get("MANTIS_AGENT_MODEL", "qwen2.5-7b-instruct"),
                "backend": os.environ.get(
                    "MANTIS_AGENT_BASE_URL", "http://localhost:11434"
                ),
                "tools": tools,
                "system": "You can read files via the filesystem tool. Be concise.",
                "max_tokens": 512,
                "max_turns": 3,
            },
        ):
            if msg.type == "assistant":
                for block in msg.message.content:
                    if hasattr(block, "text") and block.text:
                        print(f"[assistant] {block.text}")
            elif msg.type == "result":
                print(f"\n[result] {msg.subtype} · {msg.num_turns} turns")


if __name__ == "__main__":
    asyncio.run(main())
