"use client";

import { useEffect, useState } from "react";

/* The signature object: the entire migration, rendered as a diff.
   The `mantis_agent` token types itself in over the greyed-out
   claude import — the whole thesis of the library in one gesture. */

const TARGET = "mantis_agent";

export function DiffHero() {
  const [typed, setTyped] = useState("");
  const [done, setDone] = useState(false);

  useEffect(() => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) {
      setTyped(TARGET);
      setDone(true);
      return;
    }
    let i = 0;
    const start = setTimeout(function tick() {
      i += 1;
      setTyped(TARGET.slice(0, i));
      if (i < TARGET.length) {
        setTimeout(tick, 55);
      } else {
        setDone(true);
      }
    }, 650);
    return () => clearTimeout(start);
  }, []);

  return (
    <div className="rise min-w-0" style={{ animationDelay: "0.15s" }}>
      <div
        className="mono text-[13px] sm:text-[14.5px] leading-[2.05] rounded-xl overflow-hidden"
        style={{ background: "var(--color-code)" }}
      >
        <div className="flex items-center gap-2 px-4 py-2.5" style={{ background: "var(--color-code-2)" }}>
          <span className="w-2.5 h-2.5 rounded-full" style={{ background: "#25312a" }} />
          <span className="w-2.5 h-2.5 rounded-full" style={{ background: "#25312a" }} />
          <span className="w-2.5 h-2.5 rounded-full" style={{ background: "#25312a" }} />
          <span className="ml-2 text-[11.5px]" style={{ color: "#7a887f" }}>
            agent.py
          </span>
        </div>
        <div className="px-4 sm:px-5 py-4">
          {/* removed */}
          <div
            className="flex items-start gap-3 transition-opacity duration-700"
            style={{ opacity: done ? 0.42 : 0.7 }}
          >
            <span style={{ color: "#e67e80" }}>-</span>
            <span style={{ color: "#9aa79f" }}>
              <span style={{ color: "#d699b6" }}>from</span> claude_agent_sdk{" "}
              <span style={{ color: "#d699b6" }}>import</span> query, ClaudeAgentOptions, tool
            </span>
          </div>
          {/* added */}
          <div className="flex items-start gap-3">
            <span style={{ color: "var(--color-mantis-soft)" }}>+</span>
            <span style={{ color: "#e2eae4" }}>
              <span style={{ color: "#d699b6" }}>from</span>{" "}
              <span
                style={{
                  color: "var(--color-mantis-soft)",
                  background: done ? "transparent" : "rgba(103,165,107,0.16)",
                  transition: "background 0.5s",
                  borderRadius: 3,
                  padding: "0 1px",
                }}
              >
                {typed}
              </span>
              {!done && <span className="caret" style={{ marginLeft: 1 }} />}
              {typed.length === TARGET.length && (
                <>
                  {" "}
                  <span style={{ color: "#d699b6" }}>import</span> query, MantisAgentOptions, tool
                </>
              )}
            </span>
          </div>
        </div>
      </div>
      <p className="mt-3 text-[13px] text-ink-3">
        <span className="mono text-mantis">↑</span> Already on <span className="mono">claude-agent-sdk</span>?
        That&apos;s the whole migration. Your code keeps its shape; the model underneath becomes
        your choice.
      </p>
    </div>
  );
}
