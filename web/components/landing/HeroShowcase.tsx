"use client";

import { useState } from "react";
import { TrafficLights } from "./BrowserFrame";

/* The hero's product shot: one app window, two tabs — a real capture of the
   mantis CLI, and the SDK quickstart (rendered on the server, handed in). */

const TABS = [
  { key: "cli", label: "CLI", path: "~/code/todo-api — mantis" },
  { key: "sdk", label: "SDK", path: "quickstart.py" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

export function HeroShowcase({ cli, sdk }: { cli: React.ReactNode; sdk: React.ReactNode }) {
  const [tab, setTab] = useState<TabKey>("cli");
  const active = TABS.find((t) => t.key === tab)!;

  return (
    <div className="hero-window rise rounded-2xl overflow-hidden" style={{ animationDelay: "0.2s" }}>
      <div className="flex items-center gap-3 h-11 px-4 bg-paper border-b border-hair">
        <TrafficLights />
        <div role="tablist" aria-label="Product" className="flex gap-1 ml-2">
          {TABS.map((t) => (
            <button
              key={t.key}
              role="tab"
              id={`hero-tab-${t.key}`}
              aria-selected={tab === t.key}
              onClick={() => setTab(t.key)}
              className={`px-3 py-1 rounded-md text-[13px] transition-colors ${
                tab === t.key ? "bg-paper-2 text-ink font-medium" : "text-ink-3 hover:text-ink"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <span className="mono text-[12px] text-ink-3 ml-auto truncate hidden sm:block">{active.path}</span>
      </div>
      <div style={{ background: "var(--color-code)" }}>
        <div hidden={tab !== "cli"}>{cli}</div>
        <div hidden={tab !== "sdk"}>{sdk}</div>
      </div>
    </div>
  );
}
