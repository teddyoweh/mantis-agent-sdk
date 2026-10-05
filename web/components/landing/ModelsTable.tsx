"use client";

import { useState } from "react";
import { Check, Copy } from "lucide-react";

/* Models grouped by where they run. Ids come from mantis_agent/catalog.py
   (verified against the providers' model lists, 2026-09-23). */

type Row = { name: string; id: string; maker: string; where: string };

const GROUPS: { key: string; label: string; note: string; rows: Row[] }[] = [
  {
    key: "open",
    label: "Open frontier",
    note: "Open weights. Use a hosted API, or deploy them to your own GPUs from mantis.",
    rows: [
      { name: "GLM-5.3", id: "z-ai/glm-5.3", maker: "Zhipu", where: "OpenRouter · z.ai · your GPU" },
      { name: "Kimi K3", id: "moonshotai/kimi-k3", maker: "Moonshot", where: "OpenRouter · your GPU" },
      { name: "DeepSeek-V4 Pro", id: "deepseek/deepseek-v4-pro", maker: "DeepSeek", where: "OpenRouter · your GPU" },
      { name: "MiniMax M3", id: "minimax/minimax-m3", maker: "MiniMax", where: "OpenRouter · your GPU" },
      { name: "Qwen3.8 27B", id: "qwen/qwen3.8-27b", maker: "Alibaba", where: "OpenRouter · Cerebras · your GPU" },
      { name: "gpt-oss-120b", id: "openai/gpt-oss-120b", maker: "OpenAI", where: "Groq · Together · Fireworks · Cerebras" },
    ],
  },
  {
    key: "closed",
    label: "Closed frontier",
    note: "Native APIs, first-class. Bring the key you already have.",
    rows: [
      { name: "Claude Opus 5", id: "claude-opus-5", maker: "Anthropic", where: "Anthropic API" },
      { name: "GPT-5.6", id: "gpt-5.6", maker: "OpenAI", where: "OpenAI API" },
      { name: "Gemini 2.5 Pro", id: "gemini-2.5-pro", maker: "Google", where: "Gemini API" },
      { name: "Grok 4", id: "grok-4", maker: "xAI", where: "xAI API" },
    ],
  },
  {
    key: "local",
    label: "On your laptop",
    note: "Free and private through Ollama. No key, no account.",
    rows: [
      { name: "gpt-oss-20b", id: "gpt-oss:20b", maker: "OpenAI", where: "Ollama · 16 GB RAM" },
      { name: "Qwen2.5-Coder 7B", id: "qwen2.5-coder:7b", maker: "Alibaba", where: "Ollama · 8 GB RAM" },
      { name: "Qwen2.5 1.5B", id: "qwen2.5:1.5b", maker: "Alibaba", where: "Ollama · 4 GB RAM, CPU only" },
    ],
  },
];

function CopyId({ id }: { id: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(id);
          setDone(true);
          setTimeout(() => setDone(false), 1200);
        } catch {
          /* clipboard unavailable — no-op */
        }
      }}
      aria-label={`Copy ${id}`}
      className="group inline-flex items-center gap-2 mono text-[13px] text-accent hover:text-ink transition-colors"
    >
      {id}
      <span className="opacity-0 group-hover:opacity-100 transition-opacity text-ink-3">
        {done ? <Check size={13} /> : <Copy size={13} />}
      </span>
    </button>
  );
}

export function ModelsTable() {
  const [tab, setTab] = useState(GROUPS[0].key);
  const g = GROUPS.find((x) => x.key === tab) ?? GROUPS[0];

  return (
    <div>
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div role="tablist" aria-label="Model groups" className="inline-flex p-1 rounded-full bg-paper-2 self-start">
          {GROUPS.map((x) => (
            <button
              key={x.key}
              role="tab"
              id={`models-tab-${x.key}`}
              aria-selected={x.key === tab}
              onClick={() => setTab(x.key)}
              className={`px-4 py-1.5 rounded-full text-[13.5px] transition-colors ${
                x.key === tab ? "bg-paper-3 text-ink" : "text-ink-3 hover:text-ink"
              }`}
            >
              {x.label}
            </button>
          ))}
        </div>
        <p className="text-[13.5px] text-ink-3">{g.note}</p>
      </div>

      <div className="mt-6 rounded-xl bg-paper-2 overflow-x-auto">
        <table className="w-full text-left min-w-[640px]">
          <thead>
            <tr className="eyebrow">
              <th className="font-normal px-5 pt-4 pb-3">Model</th>
              <th className="font-normal px-5 pt-4 pb-3">model=</th>
              <th className="font-normal px-5 pt-4 pb-3">Runs on</th>
            </tr>
          </thead>
          <tbody>
            {g.rows.map((r) => (
              <tr key={r.id} className="border-t border-hair">
                <td className="px-5 py-3.5 whitespace-nowrap">
                  <span className="text-[14.5px] text-ink">{r.name}</span>
                  <span className="ml-2 text-[12.5px] text-ink-3">{r.maker}</span>
                </td>
                <td className="px-5 py-3.5 whitespace-nowrap">
                  <CopyId id={r.id} />
                </td>
                <td className="px-5 py-3.5 text-[13.5px] text-ink-2">{r.where}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
