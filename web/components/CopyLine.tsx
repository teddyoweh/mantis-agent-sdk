"use client";

import { useState } from "react";
import { Check, Copy } from "lucide-react";

export function CopyLine({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      /* clipboard unavailable — no-op */
    }
  };

  return (
    <button
      onClick={copy}
      className="group w-full flex items-center justify-between gap-3 rounded-lg pl-4 pr-3 py-3 text-left transition-colors"
      style={{ background: "var(--color-code)" }}
      aria-label={`Copy: ${text}`}
    >
      <span className="mono text-[13.5px] truncate" style={{ color: "#e8e2d4" }}>
        <span style={{ color: "var(--color-mantis-soft)" }}>$</span> {text}
      </span>
      <span
        className="shrink-0 grid place-items-center w-7 h-7 rounded-md transition-colors"
        style={{ color: copied ? "var(--color-mantis-soft)" : "#8b8577" }}
      >
        {copied ? <Check size={15} /> : <Copy size={15} />}
      </span>
    </button>
  );
}
