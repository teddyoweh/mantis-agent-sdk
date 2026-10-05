"use client";

import { useState } from "react";
import { BrowserFrame } from "./BrowserFrame";

/* A product screenshot with a tab row: each tab swaps the shot and the one
   line of copy beside it. Shots are real captures of `mantis serve`. */

export type Screen = {
  key: string;
  label: string;
  title: string;
  desc: string;
  src: string;
  url: string;
  alt: string;
};

export function ScreenTabs({ screens }: { screens: Screen[] }) {
  const [active, setActive] = useState(screens[0].key);
  const cur = screens.find((s) => s.key === active) ?? screens[0];

  return (
    <div>
      <div role="tablist" aria-label="Dashboard pages" className="flex flex-wrap gap-1.5">
        {screens.map((s) => (
          <button
            key={s.key}
            role="tab"
            id={`screen-tab-${s.key}`}
            aria-selected={s.key === active}
            onClick={() => setActive(s.key)}
            className={`px-3.5 py-1.5 rounded-full text-[13.5px] transition-colors ${
              s.key === active ? "bg-ink text-paper" : "bg-paper-2 text-ink-2 hover:text-ink"
            }`}
          >
            {s.label}
          </button>
        ))}
      </div>

      <div className="mt-6 grid grid-cols-1 lg:grid-cols-[260px_minmax(0,1fr)] gap-6 lg:gap-10 items-start">
        <div className="lg:pt-2">
          <h3 className="text-[18px] font-medium leading-snug">{cur.title}</h3>
          <p className="mt-2 text-[14.5px] text-ink-3 leading-relaxed">{cur.desc}</p>
        </div>
        <BrowserFrame url={cur.url}>
          {screens.map((s) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              key={s.key}
              src={s.src}
              alt={s.alt}
              hidden={s.key !== active}
              loading={s.key === screens[0].key ? "eager" : "lazy"}
              className="block w-full h-auto"
            />
          ))}
        </BrowserFrame>
      </div>
    </div>
  );
}
