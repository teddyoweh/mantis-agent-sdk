"use client";

import { useState } from "react";
import { Check, Copy } from "lucide-react";

const CMD = "pip install mantis-agent-sdk";

export function NavInstall() {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(CMD);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      /* clipboard unavailable — no-op */
    }
  };
  return (
    <button
      onClick={copy}
      aria-label={`Copy: ${CMD}`}
      className="mono inline-flex items-center gap-2 text-[12px] pl-2.5 pr-2 py-1 rounded-full bg-ink text-paper hover:bg-accent transition-colors"
    >
      {CMD}
      {copied ? <Check size={13} /> : <Copy size={13} className="opacity-60" />}
    </button>
  );
}
