import Link from "next/link";
import type { Metadata } from "next";
import { Shiki } from "@/components/Shiki";
import { CopyLine } from "@/components/CopyLine";

export const metadata: Metadata = {
  title: "Documentation",
  description:
    "The Claude Agent SDK surface, on any model you can serve. Install, quickstart, guides, and full API reference for mantis-agent-sdk.",
};

const HELLO = `import asyncio
from mantis_agent import query, tool

@tool
async def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"{city}: 67°F, partly cloudy"

async def main():
    async for msg in query(
        prompt="What's the weather in Lagos?",
        options={"model": "qwen2.5:7b", "tools": [get_weather]},
    ):
        if msg.type == "assistant":
            for block in msg.message["content"]:
                if block["type"] == "text":
                    print(block["text"])

asyncio.run(main())`;

const SECTIONS: {
  label: string;
  title: string;
  blurb: string;
  links: { title: string; href: string; desc: string }[];
}[] = [
  {
    label: "start",
    title: "Getting started",
    blurb: "Install, pull a local model, and run your first tool-calling loop.",
    links: [
      { title: "Installation", href: "/docs/getting-started/installation", desc: "pip install, extras, hosted vs local" },
      { title: "Quickstart", href: "/docs/getting-started/quickstart", desc: "Up and running in five minutes" },
      { title: "Local setup", href: "/docs/getting-started/local-setup", desc: "Ollama / llama.cpp, CPU-friendly models" },
      { title: "Configuration", href: "/docs/getting-started/configuration", desc: "Env vars, backends, precedence" },
    ],
  },
  {
    label: "guides",
    title: "Guides",
    blurb: "How each part of the surface works, with runnable examples.",
    links: [
      { title: "Models & backends", href: "/docs/guides/models-and-backends", desc: "Auto-routing from the model name" },
      { title: "Tools", href: "/docs/guides/tools", desc: "Native, prompted, grammar-constrained" },
      { title: "Streaming", href: "/docs/guides/streaming", desc: "Mid-stream tool dispatch" },
      { title: "MCP servers", href: "/docs/guides/mcp", desc: "In-process, stdio, sse, http" },
      { title: "Sessions & resume", href: "/docs/guides/sessions", desc: "Persist, fork, resume, compact" },
      { title: "Sub-agents", href: "/docs/guides/sub-agents", desc: "Compose agents as tools" },
    ],
  },
  {
    label: "reference",
    title: "API reference",
    blurb: "Every exported symbol, its signature, and what it returns.",
    links: [
      { title: "query / ClaudeSDKClient", href: "/docs/api/client", desc: "The two entry points" },
      { title: "MantisAgentOptions", href: "/docs/api/options", desc: "Every option, typed" },
      { title: "Message types", href: "/docs/api/messages", desc: "Flat-shape message + block types" },
      { title: "Errors", href: "/docs/api/errors", desc: "The exception hierarchy" },
    ],
  },
];

export default function DocsHome() {
  return (
    <div className="max-w-[820px] pb-8">
      <div className="eyebrow mb-3">documentation</div>
      <h1 className="font-display text-[clamp(2.4rem,5vw,3.6rem)] leading-[1.0]">
        The Claude Agent SDK surface, on any model you can serve.
      </h1>
      <p className="mt-6 text-[16px] text-ink-2 leading-relaxed max-w-[620px]">
        If you have working Claude SDK code, you almost always change one import. The yielded message
        shapes match; <span className="mono text-ink">MantisAgentOptions</span>,{" "}
        <span className="mono text-ink">Plugin</span>, <span className="mono text-ink">HookMatcher</span>,{" "}
        <span className="mono text-ink">create_sdk_mcp_server</span> — all of it works.
      </p>

      <div className="mt-8 grid grid-cols-1 sm:grid-cols-[minmax(0,1fr)_auto] gap-4 items-start">
        <div className="min-w-0">
          <Shiki code={HELLO} lang="python" title="hello.py" />
        </div>
        <div className="flex flex-col gap-3 sm:w-[240px]">
          <CopyLine text="pip install mantis-agent-sdk" />
          <CopyLine text="mantis-agent setup-local" />
          <p className="text-[12.5px] text-ink-3 leading-relaxed">
            <span className="mono text-mantis">setup-local</span> installs Ollama, pulls a CPU-friendly
            model, and smoke-tests the install.
          </p>
        </div>
      </div>

      <div className="mt-16 space-y-14">
        {SECTIONS.map((sec) => (
          <section key={sec.title}>
            <div className="flex items-baseline gap-3 mb-1">
              <div className="eyebrow">{sec.label}</div>
            </div>
            <h2 className="font-display text-[1.7rem] leading-tight">{sec.title}</h2>
            <p className="text-[14px] text-ink-3 mt-1 mb-5">{sec.blurb}</p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-px">
              {sec.links.map((l) => (
                <Link
                  key={l.href}
                  href={l.href}
                  className="group flex items-baseline justify-between gap-4 py-3 rule-t hover:pl-2 transition-all"
                >
                  <span>
                    <span className="text-[14.5px] font-medium text-ink group-hover:text-clay transition-colors">
                      {l.title}
                    </span>
                    <span className="block text-[12.5px] text-ink-3">{l.desc}</span>
                  </span>
                  <span className="mono text-ink-3 group-hover:text-clay transition-colors">→</span>
                </Link>
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
