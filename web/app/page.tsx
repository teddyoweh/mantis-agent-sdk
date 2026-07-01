import Link from "next/link";
import { Nav } from "@/components/Nav";
import { Footer } from "@/components/Footer";
import { DiffHero } from "@/components/landing/DiffHero";
import { Terminal } from "@/components/landing/Terminal";
import { Shiki } from "@/components/Shiki";
import { CopyLine } from "@/components/CopyLine";

const BACKENDS = [
  "Ollama", "vLLM", "llama.cpp", "TGI", "Together", "Fireworks",
  "Groq", "OpenRouter", "Cerebras", "OpenAI", "Gemini", "Modal",
];

const QUICKSTART = `import asyncio
from mantis_agent import query, ClaudeAgentOptions, tool, AssistantMessage

@tool
async def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"{city}: 67°F"

async def main():
    async for msg in query(
        prompt="What's the weather in SF?",
        options=ClaudeAgentOptions(
            model="qwen2.5:1.5b",   # routes to local Ollama automatically
            tools=[get_weather],
            max_turns=5,
        ),
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if hasattr(block, "text"):
                    print(block.text)

asyncio.run(main())`;

const SWAP = `# same script, three backends — change one line
options = ClaudeAgentOptions(model="qwen2.5:7b")                       # → local Ollama
options = ClaudeAgentOptions(model="Qwen/Qwen2.5-72B-Instruct-Turbo")  # → Together
options = ClaudeAgentOptions(model="llama-3.3-70b-versatile",
                            backend="https://api.groq.com/openai/v1")  # → Groq`;

const TRACING = `from mantis_agent import Agent, InMemoryTracer

tracer = InMemoryTracer()
agent  = Agent(model="qwen2.5:7b", tools=[...], tracer=tracer)
await agent.run(...)

tracer.summary()            # turns / tokens / cost_usd on the root span
tracer.write_jsonl("t.jsonl")

# ship the same spans to Datadog / Honeycomb / Tempo — zero extra code
from mantis_agent import OTelTracer
agent = Agent(model="qwen2.5:7b", tracer=OTelTracer(service_name="my-agent"))`;

const FEATURES = [
  {
    k: "one api, many backends",
    t: "Route from the model name",
    d: "qwen3:8b → Ollama. Qwen/… → Together. gpt-4o-mini → OpenAI. The URL is inferred from the model name shape; pass backend= to override.",
  },
  {
    k: "real tool use",
    t: "Native, prompted, or grammar-constrained",
    d: "Native tools[] where supported, prompt-engineered <tool_call> XML where not, grammar-constrained JSON where the server enforces it. Chosen per model, automatically.",
  },
  {
    k: "full mcp",
    t: "Four transports, both directions",
    d: "In-process via create_sdk_mcp_server, plus stdio / sse / http. Elicitation lets servers prompt the user; sampling lets them call back into the model.",
  },
  {
    k: "sessions",
    t: "Survive restarts, fork, resume",
    d: "JSONL transcript persistence, fork from any checkpoint, resume from an arbitrary one, auto-compaction at a token threshold.",
  },
  {
    k: "sub-agents + plugins",
    t: "Compose agents as tools",
    d: "Plugin(tools=, system_prompt_addition=, hooks=) merges at session start. Rewrite tool args before dispatch with PermissionResultAllow(updated_input=…).",
  },
  {
    k: "budget",
    t: "A ceiling on every run",
    d: "Per-model pricing table, max_usd and max_turns ceilings, BudgetExceededError, total_cost_usd on every ResultMessage.",
  },
];

const PATHS = [
  {
    n: "A",
    t: "Native",
    d: "OpenAI-compat tools[]. The fast path for anything that speaks function-calling — Qwen 2.5+, Llama 3.1+, gpt-oss.",
  },
  {
    n: "B",
    t: "Prompted",
    d: "Prompt-engineered <tool_call> XML, parsed back out. Brings tool use to Llama 2, Mistral 7B, and older Qwens that never learned the schema.",
  },
  {
    n: "C",
    t: "Constrained",
    d: "Grammar-constrained JSON when the server can enforce it. The model physically cannot emit an invalid call.",
  },
];

const MODELS = [
  ["Kimi K2.6", "cloud", "moonshotai/Kimi-K2.6-Instruct", "#1 open-weights GPQA"],
  ["Qwen3 235B-A22B", "cloud · 64 GB+", "Qwen/Qwen3-235B-A22B-Instruct-Turbo", "Apache 2.0, broad leader"],
  ["GLM-5", "cloud", "zai-org/GLM-5", "Best open Arena Elo"],
  ["MiniMax M2.5", "cloud", "minimaxai/MiniMax-M2.5", "80.2% SWE-bench"],
  ["DeepSeek-V3.2", "cloud · 80 GB+", "deepseek-ai/DeepSeek-V3.2", "Top general-purpose OSS"],
  ["gpt-oss-120b", "cloud · 80 GB", "gpt-oss:120b", "OpenAI open, ~o4-mini class"],
  ["Qwen2.5-Coder 7B", "8 GB local", "qwen2.5-coder:7b", "Strongest small coder"],
  ["qwen2.5:1.5b", "4 GB local", "qwen2.5:1.5b", "CPU default, tool-capable"],
];

function SectionLabel({ children }: { children: React.ReactNode }) {
  return <div className="eyebrow mb-4">{children}</div>;
}

export default function Home() {
  return (
    <>
      <Nav />
      <main>
        {/* ============ HERO ============ */}
        <section className="wrap pt-16 sm:pt-24 pb-20">
          <div className="max-w-[760px]">
            <h1
              className="rise font-display text-[clamp(1.7rem,3.6vw,2.8rem)]"
              style={{ animationDelay: "0.05s" }}
            >
              Claude Code, for open source.
              <br />
              Any model, any provider.
            </h1>
            <p
              className="rise mt-6 text-[17px] sm:text-[18px] text-ink-2 leading-relaxed max-w-[620px]"
              style={{ animationDelay: "0.1s" }}
            >
              A Claude-Code-style coding agent in your terminal, and Anthropic&apos;s{" "}
              <span className="mono text-ink">claude-agent-sdk</span> surface as a library — both
              running on Llama, Qwen, DeepSeek, GLM, or anything behind Ollama, vLLM, Groq, or your
              own GPU box. The migration is one import.
            </p>
          </div>

          <div className="mt-12 grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px] gap-8 items-start">
            <DiffHero />
            <div className="rise flex flex-col gap-3" style={{ animationDelay: "0.25s" }}>
              <CopyLine text="pip install mantis-agent-sdk" />
              <div className="flex flex-wrap gap-2.5">
                <Link
                  href="/docs/getting-started/quickstart"
                  className="mono text-[13px] px-4 py-2.5 rounded-lg bg-ink text-paper hover:bg-clay transition-colors"
                >
                  Quickstart →
                </Link>
                <Link
                  href="/docs"
                  className="mono text-[13px] px-4 py-2.5 rounded-lg bg-paper-2 hover:bg-paper-3 text-ink transition-colors"
                >
                  Read the docs
                </Link>
              </div>
              <p className="mt-1 text-[13px] text-ink-3 leading-relaxed">
                The <span className="mono text-ink-2">mantis</span> terminal ships in the same install
                — a Claude-Code-style coding agent driving the open model you choose.
              </p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {["831 tests", "Python 3.11–3.13", "Apache-2.0", "v1.21.0"].map((t) => (
                  <span key={t} className="pill">
                    {t}
                  </span>
                ))}
              </div>
            </div>
          </div>
        </section>

        {/* ============ BACKENDS ============ */}
        <section className="band py-10">
          <div className="wrap">
            <div className="flex flex-col md:flex-row md:items-center gap-6 md:gap-10">
              <p className="mono text-[12px] text-ink-3 shrink-0 md:w-[160px] uppercase tracking-wider">
                One kwarg between
              </p>
              <div className="flex flex-wrap gap-2">
                {BACKENDS.map((b) => (
                  <span key={b} className="pill">
                    {b}
                  </span>
                ))}
              </div>
            </div>
          </div>
        </section>

        {/* ============ TWO WAYS IN ============ */}
        <section className="wrap py-24">
          <SectionLabel>two ways in · one pip install</SectionLabel>
          <h2 className="font-display text-[clamp(2rem,4.5vw,3.2rem)] max-w-[720px]">
            A terminal to code in, and a library to build with.
          </h2>
          <div className="mt-14 grid grid-cols-1 lg:grid-cols-2 gap-12 items-start">
            <div className="min-w-0">
              <div className="flex items-baseline gap-3 mb-4">
                <span className="mono text-[13px] text-clay">01</span>
                <h3 className="text-[18px] font-medium">The mantis terminal</h3>
              </div>
              <p className="text-[14.5px] text-ink-2 leading-relaxed mb-6">
                Point it at any directory. It reads, writes, edits, greps, and runs shell commands —
                Claude Code&apos;s feel, driving your local Ollama, your vLLM box, or a hosted endpoint.
                The input stays pinned to the bottom; replies render as Markdown; file edits come back
                as real, line-numbered diffs.
              </p>
              <Terminal />
              <div className="mt-4">
                <CopyLine text="mantis setup && mantis" />
              </div>
            </div>
            <div className="min-w-0">
              <div className="flex items-baseline gap-3 mb-4">
                <span className="mono text-[13px] text-mantis">02</span>
                <h3 className="text-[18px] font-medium">The Python library</h3>
              </div>
              <p className="text-[14.5px] text-ink-2 leading-relaxed mb-6">
                The same engine, as an SDK. A tool-calling loop is a few lines away — and the exact same
                script runs against Together, Fireworks, vLLM, or Groq by changing one string.
              </p>
              <Shiki code={QUICKSTART} lang="python" title="quickstart.py" />
              <div className="mt-4">
                <Shiki code={SWAP} lang="python" />
              </div>
            </div>
          </div>
        </section>

        {/* ============ FEATURES ============ */}
        <section className="band">
          <div className="wrap py-24">
            <SectionLabel>the whole surface</SectionLabel>
            <h2 className="font-display text-[clamp(2rem,4.5vw,3.2rem)] max-w-[760px]">
              Streaming dispatch, hooks, permissions, MCP, sub-agents, sessions. None of the OSS
              alternatives ship the whole set.
            </h2>
            <div className="mt-14 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-10 gap-y-12">
              {FEATURES.map((f) => (
                <div key={f.k} className="group">
                  <div className="eyebrow text-clay mb-2.5">{f.k}</div>
                  <h3 className="text-[17px] font-medium mb-2 leading-snug">{f.t}</h3>
                  <p className="text-[13.5px] text-ink-2 leading-relaxed">{f.d}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* ============ TOOL-USE PATHS ============ */}
        <section className="wrap py-24">
          <SectionLabel>universal tool use</SectionLabel>
          <h2 className="font-display text-[clamp(2rem,4.5vw,3.2rem)] max-w-[680px]">
            Every model gets tool use — through whichever path it can actually take.
          </h2>
          <p className="mt-5 text-[15px] text-ink-2 max-w-[560px] leading-relaxed">
            A capability table (30+ models) picks the path per model, automatically. You write one
            <span className="prose-inline-code mx-1">@tool</span>; the library figures out how the model
            in front of it can call it.
          </p>
          <div className="mt-14 grid grid-cols-1 md:grid-cols-3 gap-px" style={{ background: "var(--color-hair)" }}>
            {PATHS.map((p) => (
              <div key={p.n} className="bg-paper p-7">
                <div className="flex items-center gap-3 mb-4">
                  <span
                    className="mono text-[13px] w-8 h-8 grid place-items-center rounded-full text-paper"
                    style={{ background: "var(--color-ink)" }}
                  >
                    {p.n}
                  </span>
                  <span className="text-[17px] font-medium">{p.t}</span>
                </div>
                <p className="text-[13.5px] text-ink-2 leading-relaxed">{p.d}</p>
              </div>
            ))}
          </div>
        </section>

        {/* ============ MODELS ============ */}
        <section className="band">
          <div className="wrap py-24">
            <SectionLabel>ranked · picked by where they run</SectionLabel>
            <h2 className="font-display text-[clamp(2rem,4.5vw,3.2rem)] max-w-[640px]">
              Pick the highest-ranked model that fits your hardware.
            </h2>
            <div className="mt-12 overflow-x-auto">
              <table className="w-full text-left border-collapse min-w-[640px]">
                <thead>
                  <tr className="eyebrow">
                    <th className="pb-3 pr-4 font-normal">Model</th>
                    <th className="pb-3 pr-4 font-normal">Runs</th>
                    <th className="pb-3 pr-4 font-normal">model=</th>
                    <th className="pb-3 font-normal">Notable</th>
                  </tr>
                </thead>
                <tbody className="mono text-[13px]">
                  {MODELS.map((m, i) => (
                    <tr
                      key={m[2]}
                      className="transition-colors hover:bg-paper-3/60"
                      style={{ background: i % 2 ? "transparent" : "rgba(0,0,0,0.012)" }}
                    >
                      <td className="py-2.5 pr-4 text-ink font-medium whitespace-nowrap">{m[0]}</td>
                      <td className="py-2.5 pr-4 text-ink-3 whitespace-nowrap">{m[1]}</td>
                      <td className="py-2.5 pr-4 text-mantis whitespace-nowrap">{m[2]}</td>
                      <td className="py-2.5 text-ink-2" style={{ fontFamily: "var(--font-sans)" }}>
                        {m[3]}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-6 text-[13px] text-ink-3">
              Full ranked catalog — 20 hosted + 10 CPU-friendly tiers — in{" "}
              <Link href="/docs/guides/models-and-backends" className="ul text-clay">
                Models &amp; backends
              </Link>
              .
            </p>
          </div>
        </section>

        {/* ============ OBSERVABILITY ============ */}
        <section className="wrap py-24">
          <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)] gap-12 items-center">
            <div>
              <SectionLabel>observability, shipped</SectionLabel>
              <h2 className="font-display text-[clamp(1.9rem,4vw,2.9rem)] leading-[1.05]">
                A full span tree of every run — tokens and cost on the root.
              </h2>
              <p className="mt-5 text-[14.5px] text-ink-2 leading-relaxed max-w-[460px]">
                <span className="mono text-ink">agent.run → agent.turn → llm.call + tool.call</span>,
                with per-model usage on the root span. Swap{" "}
                <span className="mono text-ink">InMemoryTracer</span> for{" "}
                <span className="mono text-ink">OTelTracer</span> to ship the same spans to your
                pipeline. Tool spans record input <em>keys</em>, never values — the safe choice is the
                only choice.
              </p>
            </div>
            <Shiki code={TRACING} lang="python" title="tracing.py" />
          </div>
        </section>

        {/* ============ PROOF / CTA ============ */}
        <section className="band-3">
          <div className="wrap py-24 text-center">
            <SectionLabel>does it actually work?</SectionLabel>
            <h2 className="font-display text-[clamp(2.2rem,5vw,3.6rem)] max-w-[720px] mx-auto">
              On a fresh machine, no GPU. Works on the first try.
            </h2>
            <div className="mt-10 max-w-[560px] mx-auto text-left">
              <Shiki
                code={`pip install mantis-agent-sdk
mantis-agent setup-local     # pulls a CPU-friendly model, smoke-tests
python my_agent.py           # two tools, a 5-turn task — first try`}
                lang="bash"
              />
            </div>
            <p className="mt-8 text-[15px] text-ink-2 max-w-[540px] mx-auto leading-relaxed">
              Change one word — <span className="mono text-clay">model=</span> — and the same script runs
              against Together, Fireworks, vLLM, llama.cpp, or Groq.
            </p>
            <div className="mt-9 flex flex-wrap gap-3 justify-center">
              <Link
                href="/docs/getting-started/quickstart"
                className="mono text-[13.5px] px-5 py-3 rounded-lg bg-ink text-paper hover:bg-clay transition-colors"
              >
                Get started →
              </Link>
              <Link
                href="https://github.com/teddyoweh/mantis-agent-sdk"
                target="_blank"
                className="mono text-[13.5px] px-5 py-3 rounded-lg bg-paper hover:bg-paper-2 text-ink transition-colors"
              >
                Star on GitHub
              </Link>
            </div>
          </div>
        </section>
      </main>
      <Footer />
    </>
  );
}
