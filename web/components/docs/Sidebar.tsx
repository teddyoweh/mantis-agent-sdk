"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { DOCS_NAV } from "@/lib/docsNav";

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();

  return (
    <nav className="text-[13.5px]">
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
                    className="block py-1 -ml-3 pl-3 rounded-md transition-colors"
                    style={
                      active
                        ? { background: "var(--color-clay-wash)", color: "var(--color-clay)", fontWeight: 500 }
                        : undefined
                    }
                  >
                    <span className={active ? "" : "text-ink-2 hover:text-ink transition-colors"}>
                      {item.title}
                    </span>
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
