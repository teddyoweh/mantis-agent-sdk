"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Logo } from "./Logo";
import { NavInstall } from "./NavInstall";

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
        background: scrolled ? "rgba(10,10,10,0.8)" : "transparent",
        backdropFilter: scrolled ? "saturate(1.4) blur(12px)" : "none",
        WebkitBackdropFilter: scrolled ? "saturate(1.4) blur(12px)" : "none",
        boxShadow: scrolled ? "0 1px 0 rgba(255,255,255,0.05)" : "none",
      }}
    >
      <div className="wrap flex items-center justify-between h-12">
        <Logo />
        <nav className="flex items-center gap-1 sm:gap-2">
          {LINKS.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              target={l.external ? "_blank" : undefined}
              className={`px-2 py-1 text-[13px] text-ink-2 hover:text-ink transition-colors ${
                l.always ? "" : "hidden sm:inline"
              }`}
            >
              {l.label}
            </Link>
          ))}
          <div className="hidden sm:block ml-2">
            <NavInstall />
          </div>
        </nav>
      </div>
    </header>
  );
}
