import Link from "next/link";
import { notFound } from "next/navigation";
import type { Metadata } from "next";
import { getDoc, allDocSlugs } from "@/lib/docs";
import { renderMarkdown } from "@/lib/markdown";
import { DOCS_NAV, neighbors } from "@/lib/docsNav";
import { Toc } from "@/components/docs/Toc";

export function generateStaticParams() {
  return allDocSlugs().map((slug) => ({ slug: slug.split("/") }));
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string[] }>;
}): Promise<Metadata> {
  const { slug } = await params;
  const doc = getDoc(slug.join("/"));
  if (!doc) return {};
  return { title: doc.title };
}

function sectionLabel(slug: string): string {
  const top = slug.split("/")[0];
  const s = DOCS_NAV.find((sec) => sec.items.some((i) => i.slug.startsWith(top)));
  return s?.label ?? "docs";
}

export default async function DocPage({
  params,
}: {
  params: Promise<{ slug: string[] }>;
}) {
  const { slug } = await params;
  const slugStr = slug.join("/");
  const doc = getDoc(slugStr);
  if (!doc) notFound();

  const { html, headings } = await renderMarkdown(doc.content, slugStr);
  const { prev, next } = neighbors(slugStr);

  return (
    <div className="xl:grid xl:grid-cols-[minmax(0,1fr)_180px] xl:gap-12">
      <article className="min-w-0 max-w-[760px] pb-10">
        <div className="eyebrow mb-3">{sectionLabel(slugStr)}</div>
        <h1 className="font-display text-[clamp(2.1rem,4.5vw,3rem)] mb-8 leading-[1.02]">
          {doc.title}
        </h1>
        <div className="prose" dangerouslySetInnerHTML={{ __html: html }} />

        {/* prev / next */}
        <div className="mt-16 pt-8 rule-t grid grid-cols-1 sm:grid-cols-2 gap-4">
          {prev ? (
            <Link
              href={`/docs/${prev.slug}`}
              className="group rounded-lg p-4 bg-paper-2 hover:bg-paper-3 transition-colors"
            >
              <div className="eyebrow mb-1.5">← previous</div>
              <div className="text-[14.5px] font-medium text-ink group-hover:text-clay transition-colors">
                {prev.title}
              </div>
            </Link>
          ) : (
            <span />
          )}
          {next && (
            <Link
              href={`/docs/${next.slug}`}
              className="group rounded-lg p-4 bg-paper-2 hover:bg-paper-3 transition-colors text-right"
            >
              <div className="eyebrow mb-1.5">next →</div>
              <div className="text-[14.5px] font-medium text-ink group-hover:text-clay transition-colors">
                {next.title}
              </div>
            </Link>
          )}
        </div>
      </article>

      <aside className="hidden xl:block">
        <Toc headings={headings} />
      </aside>
    </div>
  );
}
