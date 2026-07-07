import Link from "next/link";
import { MantisMark } from "./Logo";

const COLS: { title: string; links: { label: string; href: string; external?: boolean }[] }[] = [
  {
    title: "Start",
    links: [
      { label: "Installation", href: "/docs/getting-started/installation" },
      { label: "Quickstart", href: "/docs/getting-started/quickstart" },
      { label: "Local setup", href: "/docs/getting-started/local-setup" },
      { label: "Configuration", href: "/docs/getting-started/configuration" },
    ],
  },
  {
    title: "Guides",
    links: [
      { label: "Models & backends", href: "/docs/guides/models-and-backends" },
      { label: "Tools", href: "/docs/guides/tools" },
      { label: "Streaming", href: "/docs/guides/streaming" },
      { label: "MCP servers", href: "/docs/guides/mcp" },
      { label: "Sessions", href: "/docs/guides/sessions" },
    ],
  },
  {
    title: "Reference",
    links: [
      { label: "query / client", href: "/docs/api/client" },
      { label: "MantisAgentOptions", href: "/docs/api/options" },
      { label: "Message types", href: "/docs/api/messages" },
      { label: "Errors", href: "/docs/api/errors" },
    ],
  },
  {
    title: "Project",
    links: [
      { label: "SKILL.md — for agents", href: "/skill" },
      { label: "GitHub", href: "https://github.com/teddyoweh/mantis-agent-sdk", external: true },
      { label: "PyPI", href: "https://pypi.org/project/mantis-agent-sdk/", external: true },
      { label: "Changelog", href: "https://github.com/teddyoweh/mantis-agent-sdk/blob/main/CHANGELOG.md", external: true },
      { label: "License — Apache 2.0", href: "https://github.com/teddyoweh/mantis-agent-sdk/blob/main/LICENSE", external: true },
    ],
  },
];

export function Footer() {
  return (
    <footer className="band mt-24">
      <div className="wrap py-16">
        <div className="grid grid-cols-2 md:grid-cols-6 gap-x-6 gap-y-10">
          <div className="col-span-2">
            <div className="flex items-center gap-2">
              <MantisMark size={20} className="text-ink" />
              <span className="mono text-[14px]">mantis-agent-sdk</span>
            </div>
            <p className="mt-3 text-[13px] text-ink-3 leading-relaxed max-w-[220px]">
              The Claude Agent SDK, reimplemented for open models. One import.
            </p>
            <div className="mt-4 flex items-center gap-2">
              <span className="inline-flex items-center gap-1.5 mono text-[11.5px] text-mantis">
                <span className="w-1.5 h-1.5 rounded-full bg-mantis" /> 831 tests · Apache-2.0
              </span>
            </div>
          </div>
          {COLS.map((col) => (
            <div key={col.title}>
              <div className="eyebrow mb-3">{col.title}</div>
              <ul className="space-y-2">
                {col.links.map((l) => (
                  <li key={l.label}>
                    <Link
                      href={l.href}
                      target={l.external ? "_blank" : undefined}
                      className="text-[13px] text-ink-2 hover:text-clay transition-colors"
                    >
                      {l.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
        <div className="mt-14 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <p className="mono text-[11.5px] text-ink-3">
            © 2026 mantis-agent-sdk · not affiliated with Anthropic
          </p>
          <p className="mono text-[11.5px] text-ink-3">
            written to the <span className="text-ink-2">claude-agent-sdk</span> surface
          </p>
        </div>
      </div>
    </footer>
  );
}
