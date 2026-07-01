import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import remarkRehype from "remark-rehype";
import rehypeSlug from "rehype-slug";
import rehypePrettyCode from "rehype-pretty-code";
import rehypeStringify from "rehype-stringify";
import { visit } from "unist-util-visit";
import type { Root, Element } from "hast";
import { rewriteHref } from "./docs";

/** Rewrite relative .md links to /docs routes, mark external links. */
function rehypeRewriteLinks(currentSlug: string) {
  return (tree: Root) => {
    visit(tree, "element", (node: Element) => {
      if (node.tagName !== "a") return;
      const href = node.properties?.href;
      if (typeof href !== "string") return;
      const next = rewriteHref(href, currentSlug);
      node.properties!.href = next;
      if (/^https?:\/\//.test(next)) {
        node.properties!.target = "_blank";
        node.properties!.rel = "noopener noreferrer";
      }
    });
  };
}

export type Heading = { depth: number; text: string; id: string };

/** Collect h2/h3 for the on-page table of contents. */
function rehypeCollectHeadings(sink: Heading[]) {
  return (tree: Root) => {
    visit(tree, "element", (node: Element) => {
      if (node.tagName !== "h2" && node.tagName !== "h3") return;
      const id = node.properties?.id;
      if (typeof id !== "string") return;
      const text = toText(node);
      sink.push({ depth: node.tagName === "h2" ? 2 : 3, text, id });
    });
  };
}

function toText(node: Element): string {
  let out = "";
  visit(node, "text", (t: { value: string }) => {
    out += t.value;
  });
  return out;
}

export async function renderMarkdown(
  content: string,
  slug: string
): Promise<{ html: string; headings: Heading[] }> {
  const headings: Heading[] = [];
  const file = await unified()
    .use(remarkParse)
    .use(remarkGfm)
    .use(remarkRehype)
    .use(rehypeSlug)
    .use(rehypeCollectHeadings, headings)
    .use(rehypeRewriteLinks, slug)
    .use(rehypePrettyCode, {
      theme: "vesper",
      keepBackground: true,
      defaultLang: { block: "text" },
    })
    .use(rehypeStringify)
    .process(content);

  return { html: String(file), headings };
}
