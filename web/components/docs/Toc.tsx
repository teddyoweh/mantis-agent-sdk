"use client";

import { useEffect, useState } from "react";
import type { Heading } from "@/lib/markdown";

export function Toc({ headings }: { headings: Heading[] }) {
  const [active, setActive] = useState<string>("");

  useEffect(() => {
    if (!headings.length) return;
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) setActive(e.target.id);
        }
      },
      { rootMargin: "-80px 0px -70% 0px", threshold: 0 }
    );
    for (const h of headings) {
      const el = document.getElementById(h.id);
      if (el) obs.observe(el);
    }
    return () => obs.disconnect();
  }, [headings]);

  if (headings.length < 2) return null;

  return (
    <div className="sticky top-14 pt-10">
      <div className="eyebrow mb-3">on this page</div>
      <ul className="space-y-1.5 text-[12.5px]">
        {headings.map((h) => (
          <li key={h.id} style={{ paddingLeft: h.depth === 3 ? 12 : 0 }}>
            <a
              href={`#${h.id}`}
              className="block leading-snug transition-colors"
              style={{ color: active === h.id ? "var(--color-clay)" : "var(--color-ink-3)" }}
            >
              {h.text}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}
