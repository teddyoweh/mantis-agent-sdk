import Link from "next/link";
import { Nav } from "@/components/Nav";
import { Footer } from "@/components/Footer";
import { BackendPills } from "@/components/landing/BackendPills";
import { HeroShowcase } from "@/components/landing/HeroShowcase";
import { ScreenTabs, type Screen } from "@/components/landing/ScreenTabs";
import { BrowserFrame } from "@/components/landing/BrowserFrame";
import { DEPLOY_LOGOS } from "@/components/landing/deployLogos";
import { ModelsTable } from "@/components/landing/ModelsTable";
import {
  ModelsArt,
  ToolsArt,
  McpArt,
  DeployArt,
  SessionsArt,
  BudgetArt,
} from "@/components/landing/FeatureArt";
import { Shiki } from "@/components/Shiki";
import { CopyLine } from "@/components/CopyLine";

const QUICKSTART = `import asyncio
from mantis_agent import (
    query, MantisAgentOptions, tool, AssistantMessage,
)

@tool
async def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"{city}: 67°F"

async def main():
    async for msg in query(
        prompt="What's the weather in SF?",
        options=MantisAgentOptions(
            model="qwen2.5:1.5b",  # local Ollama
            tools=[get_weather],
            max_turns=5,
        ),
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if hasattr(block, "text"):
                    print(block.text)

asyncio.run(main())`;

const ART = {
  models: ModelsArt,
  tools: ToolsArt,
  mcp: McpArt,
  deploy: DeployArt,
  sessions: SessionsArt,
  budget: BudgetArt,
};

const FEATURES: { k: keyof typeof ART; t: string; d: string }[] = [
  { k: "models", t: "Pick any model", d: "Write the model name. mantis works out where it lives and how to talk to it." },
  { k: "tools", t: "Tools that just work", d: "Any Python function becomes a tool, even for models never trained to call one." },
  { k: "mcp", t: "Every MCP server", d: "Connect filesystems, GitHub, databases and browsers, or publish your own tools." },
  { k: "deploy", t: "Your own GPUs", d: "Pick an open model and a GPU. mantis deploys it and points your agent at it." },
  { k: "sessions", t: "Sessions that keep going", d: "Resume yesterday's work or fork to try another approach. Long chats compact on their own." },
  { k: "budget", t: "A hard spending limit", d: "Cap a run in dollars. Every call is traced with its tokens and cost." },
];

const DEPLOY_PROVIDERS = ["RunPod", "Modal", "Hugging Face", "DeepInfra", "Baseten", "Vast.ai", "Fireworks"];

const DEPLOY_STEPS = [
  { t: "Pick a model", d: "Search any open model. mantis checks it will fit before you spend anything." },
  { t: "Pick a GPU", d: "See which GPUs fit, what they cost per hour, and choose one." },
  { t: "Code with it", d: "When it boots, it is a model in your CLI and SDK. Stop it from the same page." },
];

const SCREENS: Screen[] = [
  {
    key: "sessions",
    url: "localhost:8788/#sessions",
    label: "Sessions",
    title: "Every session, every project",
    desc: "Open any conversation with its model, context fill and cost. Resume it in the terminal.",
    src: "/shots/app-sessions.png",
    alt: "The mantis dashboard listing sessions across projects with model, context and cost",
  },
  {
    key: "usage",
    url: "localhost:8788/#home",
    label: "Usage",
    title: "Know what you spend",
    desc: "Messages, tool calls, tokens and dollars by day and by provider, with every connected model family.",
    src: "/shots/app-home.png",
    alt: "The mantis dashboard overview with spend, tokens and providers",
  },
  {
    key: "skills",
    url: "localhost:8788/#skills",
    label: "Skills",
    title: "Skills your agent can load",
    desc: "Write and edit skills in one place. Every model sees the same catalog.",
    src: "/shots/app-skills.png",
    alt: "The mantis dashboard skills page",
  },
  {
    key: "memory",
    url: "localhost:8788/#memory",
    label: "AGENTS.md",
    title: "Project memory, edited in place",
    desc: "Keep AGENTS.md and memory files current without leaving the dashboard.",
    src: "/shots/app-memory.png",
    alt: "The mantis dashboard editing an AGENTS.md file",
  },
  {
    key: "mcp",
    url: "localhost:8788/#mcp",
    label: "MCP",
    title: "All your MCP servers",
    desc: "Add, check and remove MCP servers for every agent on the machine.",
    src: "/shots/app-mcp.png",
    alt: "The mantis dashboard MCP servers page",
  },
];

const START = [
  {
    t: "Install",
    d: "One package gives you the CLI, the SDK and the dashboard.",
    cmd: "pip install mantis-agent-sdk",
  },
  {
    t: "Choose a model",
    d: "Paste a key, pick a local model, or deploy one to your GPUs.",
    cmd: "mantis setup",
  },
  {
    t: "Start",
    d: "Open the agent in any project, or the dashboard in your browser.",
    cmd: "mantis",
  },
];

function Heading({ eyebrow, title, sub }: { eyebrow: string; title: string; sub?: string }) {
  return (
    <div className="max-w-[640px]">
      <div className="eyebrow mb-3">{eyebrow}</div>
      <h2 className="font-display text-[clamp(1.5rem,2.6vw,2.1rem)] leading-[1.12] text-balance">{title}</h2>
      {sub && <p className="mt-3 text-[15.5px] text-ink-3 leading-relaxed">{sub}</p>}
    </div>
  );
}

export default function Home() {
  return (
    <>
      <Nav />
      <main>
        {/* ============ HERO ============ */}
        <section className="wrap pt-10 sm:pt-14 pb-14 sm:pb-20">
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-8 lg:gap-16 items-start">
            <div className="rise" style={{ animationDelay: "0.05s" }}>
              <h1 className="font-display text-[clamp(1.45rem,2.3vw,1.95rem)] leading-[1.15]">A coding agent for any model.</h1>
              <p className="mt-3 text-[15.5px] text-ink-3 leading-relaxed max-w-[460px]">
                The mantis CLI reads, edits and runs your code on the model you pick, local or
                hosted.
              </p>
            </div>
            <div className="rise" style={{ animationDelay: "0.1s" }}>
              <p className="font-display text-[clamp(1.45rem,2.3vw,1.95rem)] leading-[1.15]">An SDK to build your own.</p>
              <p className="mt-3 text-[15.5px] text-ink-3 leading-relaxed max-w-[460px]">
                Tools, MCP, sessions and sub-agents in a few lines of Python. The same code runs on
                every model.
              </p>
            </div>
          </div>

          <div className="mt-8 sm:mt-10">
            <HeroShowcase
              cli={
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src="/shots/cli.png"
                  width={2200}
                  height={1310}
                  alt="The mantis CLI adding a DELETE endpoint to a FastAPI app on qwen3-coder, with a syntax-highlighted diff"
                  className="block w-full h-auto"
                />
              }
              sdk={<Shiki code={QUICKSTART} lang="python" className="hero-code" />}
            />
          </div>
        </section>

        {/* ============ BACKENDS ============ */}
        <section className="band py-10">
          <div className="wrap">
            <div className="flex flex-col md:flex-row md:items-center gap-6 md:gap-10">
              <p className="mono text-[12px] text-ink-3 shrink-0 md:w-[100px] uppercase tracking-wider">
                Runs on
              </p>
              <BackendPills />
            </div>
          </div>
        </section>

        {/* ============ DEPLOY ============ */}
        <section className="wrap py-20">
          <Heading
            eyebrow="Deploy"
            title="Run any open model on your own GPUs."
            sub="Deploy GLM, Kimi, DeepSeek or Qwen to the GPU cloud you already use, and your agent is running on it in minutes."
          />
          <div className="mt-10 grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_300px] gap-8 lg:gap-12 items-start">
            <BrowserFrame url="localhost:8788/#deploy">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src="/shots/app-deploy.png"
                alt="The mantis dashboard Deploy page with a running open-model deployment"
                className="block w-full h-auto"
              />
            </BrowserFrame>
            <div>
              <ol className="flex flex-col gap-6">
                {DEPLOY_STEPS.map((s, i) => (
                  <li key={s.t} className="flex gap-4">
                    <span className="mono text-[12px] text-ink-3 w-5 shrink-0 pt-1">{i + 1}</span>
                    <div>
                      <div className="text-[15.5px] font-medium">{s.t}</div>
                      <p className="mt-1 text-[14px] text-ink-3 leading-relaxed">{s.d}</p>
                    </div>
                  </li>
                ))}
              </ol>
              <div className="mt-8 pt-6 border-t border-hair">
                <div className="eyebrow mb-3">Deploys to</div>
                <div className="flex flex-wrap gap-2">
                  {DEPLOY_PROVIDERS.map((p) => (
                    <span key={p} className="pill">
                      <span
                        aria-hidden="true"
                        className="provider-logo"
                        dangerouslySetInnerHTML={{ __html: DEPLOY_LOGOS[p] }}
                      />
                      {p}
                    </span>
                  ))}
                </div>
                <Link href="/docs/guides/deploy" className="inline-block mt-5 ul text-[14px] text-ink">
                  How deploys work →
                </Link>
              </div>
            </div>
          </div>
        </section>

        {/* ============ DASHBOARD ============ */}
        <section className="band">
          <div className="wrap py-20">
            <Heading
              eyebrow="Dashboard"
              title="One place to run all your agents."
              sub="mantis serve opens a local dashboard for everything on your machine."
            />
            <div className="mt-10">
              <ScreenTabs screens={SCREENS} />
            </div>
            <Link href="/docs/guides/dashboard" className="inline-block mt-8 ul text-[14px] text-ink">
              Open the dashboard guide →
            </Link>
          </div>
        </section>

        {/* ============ FEATURES ============ */}
        <section className="wrap py-20">
          <Heading eyebrow="Built in" title="What every model gets." />
          <div className="mt-10 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-8 gap-y-12">
            {FEATURES.map((f) => {
              const Art = ART[f.k];
              return (
                <div key={f.k}>
                  <Art />
                  <h3 className="mt-5 text-[16.5px] font-medium">{f.t}</h3>
                  <p className="mt-1.5 text-[14px] text-ink-3 leading-relaxed">{f.d}</p>
                </div>
              );
            })}
          </div>
        </section>

        {/* ============ MODELS ============ */}
        <section className="band">
          <div className="wrap py-20">
            <Heading eyebrow="Models" title="The best models, open and closed." />
            <div className="mt-10">
              <ModelsTable />
            </div>
            <p className="mt-5 text-[13.5px] text-ink-3">
              Any other model works too.{" "}
              <Link href="/docs/guides/models-and-backends" className="ul text-accent">
                See all models and backends
              </Link>
            </p>
          </div>
        </section>

        {/* ============ GET STARTED ============ */}
        <section className="wrap py-20">
          <Heading eyebrow="Get started" title="Up and running in a minute." />
          <div className="mt-10 grid grid-cols-1 md:grid-cols-3 gap-4">
            {START.map((s, i) => (
              <div key={s.t} className="rounded-xl bg-paper-2 p-6 flex flex-col">
                <span className="mono text-[12px] text-ink-3">{String(i + 1).padStart(2, "0")}</span>
                <div className="mt-3 text-[16.5px] font-medium">{s.t}</div>
                <p className="mt-1.5 text-[14px] text-ink-3 leading-relaxed flex-1">{s.d}</p>
                <div className="mt-5">
                  <CopyLine text={s.cmd} />
                </div>
              </div>
            ))}
          </div>
          <div className="mt-8 flex flex-wrap items-center gap-x-6 gap-y-3 text-[14px]">
            <Link href="/docs/getting-started/quickstart" className="ul text-ink">
              Read the quickstart →
            </Link>
            <Link href="/docs/guides/tools" className="ul text-ink-2">
              Build with the SDK
            </Link>
            <Link href="https://github.com/teddyoweh/mantis-agent-sdk" target="_blank" className="ul text-ink-2">
              GitHub
            </Link>
          </div>
        </section>
      </main>
      <Footer />
    </>
  );
}
