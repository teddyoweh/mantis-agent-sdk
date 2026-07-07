import fs from "node:fs";
import path from "node:path";
import type { Metadata } from "next";
import matter from "gray-matter";
import { Nav } from "@/components/Nav";
import { Footer } from "@/components/Footer";
import { CopyLine } from "@/components/CopyLine";
import { renderMarkdown } from "@/lib/markdown";

export const metadata: Metadata = {
  title: "Agent Skill",
  description:
    "SKILL.md for mantis-agent-sdk — teach Claude Code or any agent to build with the SDK. Raw file at /skill.md.",
};

export default async function SkillPage() {
  const raw = fs.readFileSync(path.join(process.cwd(), "public", "skill.md"), "utf8");
  const { content, data } = matter(raw);
  const { html } = await renderMarkdown(content.trim().replace(/^#\s+.+\n+/, ""), "skill");

  return (
    <>
      <Nav />
      <div className="wrap wrap-tight pt-12 pb-8">
        <div className="eyebrow mb-3">for agents · SKILL.md</div>
        <h1 className="font-display text-[clamp(1.9rem,4vw,2.6rem)] leading-[1.05]">
          Teach your agent to build with mantis.
        </h1>
        <p className="mt-4 text-[15px] text-ink-2 leading-relaxed max-w-[560px]">
          This page is an{" "}
          <a
            className="ul text-clay"
            href="https://code.claude.com/docs/en/skills"
            target="_blank"
            rel="noopener noreferrer"
          >
            Agent Skill
          </a>{" "}
          — drop it into Claude Code (or any agent that reads{" "}
          <span className="mono text-ink">SKILL.md</span>) and it knows how to install, route,
          and build with the SDK. The raw file lives at{" "}
          <a className="ul text-clay" href="/skill.md">
            /skill.md
          </a>
          .
        </p>
        <div className="mt-6 max-w-[560px] flex flex-col gap-2.5">
          <CopyLine text="mkdir -p ~/.claude/skills/mantis-agent-sdk" />
          <CopyLine text="curl -o ~/.claude/skills/mantis-agent-sdk/SKILL.md https://mantis-agent-sdk.vercel.app/skill.md" />
        </div>

        <hr className="rule my-10" />

        <div className="mono text-[12px] text-ink-3 mb-6">
          name: {String(data.name)} · license: {String(data.license)}
        </div>
        <article className="prose" dangerouslySetInnerHTML={{ __html: html }} />
      </div>
      <Footer />
    </>
  );
}
