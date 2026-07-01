import fs from "node:fs";
import path from "node:path";
import matter from "gray-matter";

const DOCS_DIR = path.join(process.cwd(), "content", "docs");

/** Resolve a docs slug (e.g. "guides/mcp" or "api") to an on-disk .md file. */
function resolveFile(slug: string): string | null {
  const direct = path.join(DOCS_DIR, `${slug}.md`);
  if (fs.existsSync(direct)) return direct;
  const asIndex = path.join(DOCS_DIR, slug, "index.md");
  if (fs.existsSync(asIndex)) return asIndex;
  return null;
}

export type Doc = {
  slug: string;
  title: string;
  content: string;
};

export function getDoc(slug: string): Doc | null {
  const file = resolveFile(slug);
  if (!file) return null;
  const raw = fs.readFileSync(file, "utf8");
  const { content, data } = matter(raw);

  // Title: frontmatter → first H1 → slug tail.
  let title = (data.title as string) || "";
  if (!title) {
    const m = content.match(/^#\s+(.+)$/m);
    title = m ? m[1].trim() : slug.split("/").pop()!;
  }

  // Strip a leading H1 that duplicates the title we render in the header.
  const body = content.replace(/^#\s+.+\n+/, "");

  return { slug, title, content: body };
}

/** All slugs that map to a real file — for generateStaticParams. */
export function allDocSlugs(): string[] {
  const out: string[] = [];
  const walk = (dir: string, prefix: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.isDirectory()) {
        walk(path.join(dir, entry.name), `${prefix}${entry.name}/`);
      } else if (entry.name.endsWith(".md")) {
        if (entry.name === "index.md") {
          const s = prefix.replace(/\/$/, "");
          if (s) out.push(s);
        } else {
          out.push(`${prefix}${entry.name.replace(/\.md$/, "")}`);
        }
      }
    }
  };
  walk(DOCS_DIR, "");
  return out;
}

/** Rewrite a relative markdown link (…/x.md#h) into an absolute /docs route. */
export function rewriteHref(href: string, currentSlug: string): string {
  if (!href) return href;
  if (/^(https?:)?\/\//.test(href) || href.startsWith("#") || href.startsWith("mailto:")) return href;

  const [pathPart, hash] = href.split("#");
  const currentDir = currentSlug.includes("/")
    ? currentSlug.slice(0, currentSlug.lastIndexOf("/"))
    : "";

  // resolve against the current doc's directory
  const resolved = path
    .normalize(path.join("/", currentDir, pathPart))
    .replace(/\\/g, "/");

  let clean = resolved
    .replace(/\.md$/, "")
    .replace(/\/index$/, "")
    .replace(/\.\.\//g, ""); // any leftover

  // links that escaped the docs tree (e.g. ../../AGENTS.md) → GitHub
  if (clean.includes("AGENTS") || clean.startsWith("/..")) {
    return "https://github.com/teddyoweh/mantis-agent-sdk";
  }

  clean = `/docs${clean.startsWith("/") ? clean : `/${clean}`}`.replace(/\/+/g, "/");
  return hash ? `${clean}#${hash}` : clean;
}
