export type NavItem = { title: string; slug: string };
export type NavSection = { title: string; label: string; items: NavItem[] };

export const DOCS_NAV: NavSection[] = [
  {
    title: "Getting started",
    label: "start",
    items: [
      { title: "Overview", slug: "getting-started" },
      { title: "Installation", slug: "getting-started/installation" },
      { title: "Quickstart", slug: "getting-started/quickstart" },
      { title: "Examples", slug: "examples" },
      { title: "Local setup", slug: "getting-started/local-setup" },
      { title: "Configuration", slug: "getting-started/configuration" },
    ],
  },
  {
    title: "Self-host",
    label: "selfhost",
    items: [
      { title: "Overview — the full map", slug: "guides/self-hosting" },
      { title: "Modal", slug: "selfhost/modal" },
      { title: "RunPod", slug: "selfhost/runpod" },
      { title: "Lambda", slug: "selfhost/lambda" },
      { title: "Vast.ai", slug: "selfhost/vastai" },
      { title: "HF Endpoints", slug: "selfhost/hf-endpoints" },
      { title: "Other platforms", slug: "selfhost/others" },
    ],
  },
  {
    title: "Guides",
    label: "guides",
    items: [
      { title: "Overview", slug: "guides" },
      { title: "Models & backends", slug: "guides/models-and-backends" },
      { title: "Provider access", slug: "guides/providers" },
      { title: "The mantis terminal", slug: "guides/terminal" },
      { title: "Headless & CI", slug: "guides/headless" },
      { title: "Tools", slug: "guides/tools" },
      { title: "Streaming", slug: "guides/streaming" },
      { title: "Permissions", slug: "guides/permissions" },
      { title: "Hooks", slug: "guides/hooks" },
      { title: "Sessions & resume", slug: "guides/sessions" },
      { title: "Thinking blocks", slug: "guides/thinking" },
      { title: "Budget & limits", slug: "guides/budget" },
      { title: "MCP servers", slug: "guides/mcp" },
      { title: "Sub-agents", slug: "guides/sub-agents" },
      { title: "Plugins", slug: "guides/plugins" },
      { title: "Memory", slug: "guides/memory" },
    ],
  },
  {
    title: "API reference",
    label: "reference",
    items: [
      { title: "Overview", slug: "api" },
      { title: "query / ClaudeSDKClient", slug: "api/client" },
      { title: "MantisAgentOptions", slug: "api/options" },
      { title: "Message types", slug: "api/messages" },
      { title: "Tools", slug: "api/tools" },
      { title: "Sessions", slug: "api/sessions" },
      { title: "Errors", slug: "api/errors" },
    ],
  },
  {
    title: "More",
    label: "more",
    items: [
      { title: "Upstream comparison", slug: "development/upstream-comparison" },
      { title: "Parity roadmap", slug: "development/parity-roadmap" },
    ],
  },
];

export const ALL_ITEMS: NavItem[] = DOCS_NAV.flatMap((s) => s.items);

export function flatSlugs(): string[] {
  return ALL_ITEMS.map((i) => i.slug).filter((s) => s !== "getting-started" && s !== "guides" && s !== "api");
  // section-index slugs are still valid files; include the ones that map to real index.md
}

export function neighbors(slug: string): { prev?: NavItem; next?: NavItem } {
  const idx = ALL_ITEMS.findIndex((i) => i.slug === slug);
  if (idx === -1) return {};
  return { prev: ALL_ITEMS[idx - 1], next: ALL_ITEMS[idx + 1] };
}
