"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { DOCS_NAV } from "@/lib/docsNav";

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();

  return (
    <nav className="text-[12.5px]">
      <Link
        href="/docs"
        onClick={onNavigate}
        className={`block mb-6 mono text-[12px] tracking-wide ${
          pathname === "/docs" ? "text-clay" : "text-ink-3 hover:text-ink"
        }`}
      >
        ← docs home
      </Link>
      {DOCS_NAV.map((section) => (
        <div key={section.title} className="mb-7">
          <div className="eyebrow mb-2.5">{section.label}</div>
          <ul className="space-y-0.5">
            {section.items.map((item) => {
              const href = `/docs/${item.slug}`;
              const active = pathname === href;
              return (
                <li key={item.slug}>
                  <Link
                    href={href}
                    onClick={onNavigate}
                    className={`block py-[5px] px-2.5 -mx-2.5 rounded-md transition-colors ${
                      active
                        ? "bg-paper-2 text-clay font-medium"
                        : "text-ink-2 hover:text-ink hover:bg-paper-2/60"
                    }`}
                  >
                    {item.title}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}
