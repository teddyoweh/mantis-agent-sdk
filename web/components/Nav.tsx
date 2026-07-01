"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Logo } from "./Logo";

const LINKS = [
  { href: "/docs", label: "Docs", always: true },
  { href: "/docs/guides/models-and-backends", label: "Models" },
  { href: "/docs/api", label: "API" },
  { href: "https://github.com/teddyoweh/mantis-agent-sdk", label: "GitHub", external: true, always: true },
];

export function Nav() {
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <header
      className="sticky top-0 z-50 transition-colors duration-300"
      style={{
        background: scrolled ? "rgba(255,255,255,0.85)" : "transparent",
        backdropFilter: scrolled ? "saturate(1.4) blur(12px)" : "none",
        WebkitBackdropFilter: scrolled ? "saturate(1.4) blur(12px)" : "none",
        boxShadow: scrolled ? "0 1px 0 rgba(0,0,0,0.05)" : "none",
      }}
    >
      <div className="wrap flex items-center justify-between h-14">
        <Logo />
        <nav className="flex items-center gap-1 sm:gap-2">
          {LINKS.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              target={l.external ? "_blank" : undefined}
              className={`px-2.5 py-1.5 text-[13.5px] text-ink-2 hover:text-ink transition-colors ${
                l.always ? "" : "hidden sm:inline"
              }`}
            >
              {l.label}
            </Link>
          ))}
          <Link
            href="/docs/getting-started/quickstart"
            className="hidden sm:inline-block ml-2 mono text-[12.5px] px-3 py-1.5 rounded-full bg-ink text-paper hover:bg-clay transition-colors"
          >
            pip install
          </Link>
        </nav>
      </div>
    </header>
  );
}
