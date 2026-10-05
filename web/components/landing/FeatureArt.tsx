"use client";

import { useEffect, useState, type ComponentType, type ReactNode } from "react";

/* Feature-card illustrations for the landing page. Each one is a 480x300
   (16:10) SVG scene that scales with its card. Motion is a small state loop
   or a CSS keyframe from the "feature art" block in globals.css; with
   prefers-reduced-motion the loop never starts and every scene rests on a
   meaningful static frame (the one it renders on the server). */

const INK = "var(--color-ink)";
const INK2 = "var(--color-ink-2)";
const INK3 = "var(--color-ink-3)";
const HAIR = "var(--color-hair)";
const P3 = "var(--color-paper-3)";
const CODE = "var(--color-code)";
const ACC = "var(--color-accent)";

/* Cycle 0..n-1 every `ms`. Starts (and, under reduced motion, stays) at `rest`
   so server and client render the same first frame. */
function useCycle(n: number, ms: number, rest: number) {
  const [i, setI] = useState(rest);
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let k = rest;
    const id = window.setInterval(() => {
      k = (k + 1) % n;
      setI(k);
    }, ms);
    return () => window.clearInterval(id);
  }, [n, ms, rest]);
  return i;
}

function Frame({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="relative aspect-[16/10] w-full overflow-hidden rounded-xl bg-paper-2">
      <svg
        viewBox="0 0 480 300"
        className="absolute inset-0 h-full w-full font-mono"
        role="img"
        aria-label={label}
      >
        {children}
      </svg>
    </div>
  );
}

type Seg = [string, string];

function Code({ lines, x, y, lh, size }: { lines: Seg[][]; x: number; y: number; lh: number; size: number }) {
  return (
    <text x={x} y={y} fontSize={size}>
      {lines.map((segs, li) => (
        <tspan key={li} x={x} y={y + li * lh}>
          {segs.map(([t, c], si) => (
            <tspan key={si} fill={c}>
              {t.replace(/ /g, "\u00a0")}
            </tspan>
          ))}
        </tspan>
      ))}
    </text>
  );
}

/* ------------------------------------------------------------------ models */

const ROUTES = [
  { model: "qwen3-coder:30b", provider: "Ollama · local", wire: "ollama /api/chat" },
  { model: "claude-opus-5", provider: "Anthropic", wire: "messages" },
  { model: "gpt-5.4", provider: "OpenAI", wire: "responses" },
  { model: "grok-4", provider: "xAI", wire: "chat.completions" },
  { model: "gemini-3-pro", provider: "Google", wire: "generateContent" },
  { model: "moonshotai/Kimi-K2.6", provider: "Together", wire: "chat.completions" },
];

export function ModelsArt() {
  const a = useCycle(ROUTES.length, 1900, 0);
  const r = ROUTES[a];
  const rowY = (i: number) => 92 + i * 32;
  const hub = { x: 196, y: 189 };
  return (
    <Frame label="One model string routed to its provider">
      {/* config line */}
      <rect x={20} y={22} width={440} height={50} rx={9} fill={CODE} stroke={HAIR} />
      <text x={36} y={52} fontSize={11} fill={INK3}>
        1
      </text>
      <text x={56} y={52} fontSize={13}>
        <tspan fill={INK2}>agent</tspan>
        <tspan fill={INK3}> = </tspan>
        <tspan fill={INK}>Agent</tspan>
        <tspan fill={INK3}>(</tspan>
        <tspan fill={INK2}>model</tspan>
        <tspan fill={INK3}>=&quot;</tspan>
        <tspan key={a} className="fa-in" fill={ACC}>
          {r.model}
        </tspan>
        <tspan fill={INK3}>&quot;)</tspan>
      </text>

      {/* stem into the router */}
      <line x1={116} y1={72} x2={116} y2={162} stroke={HAIR} strokeDasharray="3 4" />

      {/* router */}
      <rect x={36} y={162} width={160} height={54} rx={9} fill={P3} stroke={HAIR} />
      <text x={50} y={184} fontSize={11.5} fill={INK}>
        route(model)
      </text>
      <text x={50} y={203} fontSize={10} fill={INK3}>
        wire{" "}
        <tspan key={a} className="fa-in" fill={INK2}>
          {r.wire}
        </tspan>
      </text>
      <text x={36} y={250} fontSize={10} fill={INK3}>
        name shape → backend
      </text>
      <text x={36} y={266} fontSize={10} fill={INK3}>
        no base_url, no adapter
      </text>

      {/* switchboard */}
      {ROUTES.map((row, i) => {
        const y = rowY(i) + 13;
        const on = i === a;
        const d = `M${hub.x} ${hub.y} C ${hub.x + 34} ${hub.y}, ${250 - 34} ${y}, 250 ${y}`;
        return (
          <g key={row.provider}>
            <path d={d} fill="none" stroke={on ? ACC : HAIR} strokeWidth={on ? 1.25 : 1} className={on ? "fa-flow" : undefined} strokeDasharray={on ? "4 5" : undefined} />
            <rect x={250} y={rowY(i)} width={210} height={26} rx={6} fill={on ? P3 : "transparent"} stroke={on ? "#2e2e2e" : HAIR} />
            <circle cx={264} cy={y} r={3} fill={on ? ACC : "#3a3a3a"} />
            <text x={276} y={y + 3.5} fontSize={11} fill={on ? INK : INK2}>
              {row.provider}
            </text>
            {on && (
              <text x={450} y={y + 3.5} fontSize={9.5} fill={ACC} textAnchor="end" className="fa-in">
                routed
              </text>
            )}
          </g>
        );
      })}
      <circle cx={hub.x} cy={hub.y} r={3} fill={INK2} />
    </Frame>
  );
}

/* ------------------------------------------------------------------- tools */

const LANES = [
  { key: "native", who: "claude-opus-5 · gpt-5.4", how: "API tool_calls" },
  { key: "prompted", who: "gemma3:4b", how: "parsed from text" },
  { key: "constrained", who: "llama.cpp", how: "GBNF-forced JSON" },
];

export function ToolsArt() {
  const a = useCycle(LANES.length, 2200, 0);
  const cy = 135;
  const laneY = [cy - 42, cy, cy + 42];
  const L = 184;
  const R = 296;
  const PY: Seg[][] = [
    [["@tool", INK2]],
    [["def ", INK3], ["read_file", INK], ["(", INK3]],
    [["    path", INK2], [": str,", INK3]],
    [["    limit", INK2], [": int = ", INK3], ["200", INK2]],
    [[") -> ", INK3], ["str", INK2], [":", INK3]],
    [['    """Read a file."""', INK3]],
    [["    ...", INK3]],
  ];
  const JS: Seg[][] = [
    [["{", INK3]],
    [['  "name"', INK2], [": ", INK3], ['"read_file"', INK], [",", INK3]],
    [['  "arguments"', INK2], [": {", INK3]],
    [['    "path"', INK2], [": ", INK3], ['"app.py"', INK], [",", INK3]],
    [['    "limit"', INK2], [": ", INK3], ["200", INK]],
    [["  }", INK3]],
    [["}", INK3]],
  ];
  return (
    <Frame label="A Python tool becomes a JSON tool call through three paths">
      {[
        { x: 16, title: "tools.py", lines: PY },
        { x: R, title: "tool_call", lines: JS },
      ].map((c) => (
        <g key={c.title}>
          <rect x={c.x} y={46} width={168} height={178} rx={9} fill={CODE} stroke={HAIR} />
          <line x1={c.x} y1={70} x2={c.x + 168} y2={70} stroke={HAIR} />
          <text x={c.x + 12} y={62} fontSize={10} fill={INK3}>
            {c.title}
          </text>
          <Code lines={c.lines} x={c.x + 12} y={92} lh={17.5} size={10.5} />
        </g>
      ))}

      {LANES.map((l, i) => {
        const y = laneY[i];
        const on = i === a;
        const d = `M${L} ${cy} C ${L + 16} ${cy}, ${L + 8} ${y}, ${L + 26} ${y} L ${R - 26} ${y} C ${R - 8} ${y}, ${R - 16} ${cy}, ${R} ${cy}`;
        const w = l.key.length * 6 + 16;
        return (
          <g key={l.key}>
            <path d={d} fill="none" stroke={on ? ACC : HAIR} strokeWidth={on ? 1.25 : 1} />
            {on && <path key={a} d={d} pathLength={100} fill="none" stroke={ACC} strokeWidth={2.5} strokeLinecap="round" className="fa-travel" />}
            <rect x={240 - w / 2} y={y - 9} width={w} height={18} rx={9} fill="var(--color-paper-2)" stroke={on ? ACC : HAIR} />
            <text x={240} y={y + 3.5} fontSize={10} textAnchor="middle" fill={on ? ACC : INK3}>
              {l.key}
            </text>
          </g>
        );
      })}
      <circle cx={L} cy={cy} r={2.5} fill={INK2} />
      <circle cx={R} cy={cy} r={2.5} fill={INK2} />

      <text key={a} x={240} y={260} fontSize={10.5} textAnchor="middle" className="fa-in">
        <tspan fill={INK2}>{LANES[a].who}</tspan>
        <tspan fill={INK3}>{"  →  " + LANES[a].how}</tspan>
      </text>
    </Frame>
  );
}

/* --------------------------------------------------------------------- mcp */

const SERVERS = [
  { name: "slack", sub: "sse · 8 tools", x: 180, y: 16, call: "mcp__slack__post_message" },
  { name: "github", sub: "http · 9 tools", x: 342, y: 129, call: "mcp__github__get_issue" },
  { name: "playwright", sub: "stdio · 21 tools", x: 290, y: 238, call: "mcp__playwright__browser_click" },
  { name: "postgres", sub: "stdio · 4 tools", x: 70, y: 238, call: "mcp__postgres__query" },
  { name: "filesystem", sub: "stdio · 11 tools", x: 18, y: 129, call: "mcp__filesystem__read_file" },
];

export function McpArt() {
  const a = useCycle(SERVERS.length, 1700, 1);
  const hx = 240;
  const hy = 150;
  const call = SERVERS[a].call;
  const cw = call.length * 5.75 + 20;
  return (
    <Frame label="MCP servers connected to the agent">
      <circle cx={hx} cy={hy} r={86} fill="none" stroke={HAIR} strokeDasharray="2 5" />
      {SERVERS.map((s, i) => {
        const on = i === a;
        const d = `M${s.x + 60} ${s.y + 21} L ${hx} ${hy}`;
        return (
          <g key={s.name}>
            <path d={d} stroke={on ? ACC : HAIR} strokeOpacity={on ? 0.45 : 1} />
            {on && <path key={a} d={d} pathLength={100} stroke={ACC} strokeWidth={2.5} strokeLinecap="round" className="fa-travel" />}
          </g>
        );
      })}
      {SERVERS.map((s, i) => {
        const on = i === a;
        return (
          <g key={s.name}>
            <rect x={s.x} y={s.y} width={120} height={42} rx={9} fill={P3} stroke={on ? "#3a3a3a" : HAIR} />
            <circle cx={s.x + 14} cy={s.y + 16} r={2.5} fill={on ? ACC : "#3a3a3a"} />
            <text x={s.x + 24} y={s.y + 19.5} fontSize={11} fill={on ? INK : INK2}>
              {s.name}
            </text>
            <text x={s.x + 24} y={s.y + 33} fontSize={9} fill={INK3}>
              {s.sub}
            </text>
          </g>
        );
      })}

      {/* the agent */}
      <rect x={hx - 62} y={hy - 30} width={124} height={60} rx={12} fill="#161616" stroke="#333" />
      <text x={hx} y={hy - 4} fontSize={12.5} textAnchor="middle" fill={INK}>
        mantis
      </text>
      <text x={hx} y={hy + 13} fontSize={9} textAnchor="middle" fill={INK3}>
        53 tools · 5 servers
      </text>

      <g key={a} className="fa-in">
        <rect x={hx - cw / 2} y={196} width={cw} height={18} rx={5} fill="var(--color-paper-2)" stroke={HAIR} />
        <text x={hx} y={208.5} fontSize={9.5} textAnchor="middle" fill={INK2}>
          {call}
        </text>
      </g>
    </Frame>
  );
}

/* ------------------------------------------------------------------ deploy */

const GPUS = [
  { name: "H100 80GB", n: 4, gb: 320, price: 2.49 },
  { name: "A100 80GB", n: 4, gb: 320, price: 1.64 },
  { name: "L40S 48GB", n: 4, gb: 192, price: 0.99 },
];
const NEED = 240;
const STEPS = ["provision", "pull weights", "load", "ready"];
const STATUS = [
  "provisioning 4× H100 on modal …",
  "pulling weights  118 / 470 GB",
  "loading  tensor-parallel 4",
  "https://qwen3-235b.modal.run/v1",
];

export function DeployArt() {
  const p = useCycle(6, 1300, 5);
  const step = Math.min(p, 3);
  const bx = 150;
  const bw = 222;
  const scale = (gb: number) => (gb / 400) * bw;
  const tx = bx + scale(NEED);
  return (
    <Frame label="GPU picker with a VRAM fit check and deploy status">
      <text x={20} y={36} fontSize={12.5} fill={INK}>
        qwen3-235b-a22b<tspan fill={INK3}> · fp8</tspan>
      </text>
      <text x={460} y={36} fontSize={10} textAnchor="end" fill={INK3}>
        needs <tspan fill={INK2}>~{NEED} GB</tspan>
      </text>
      <line x1={20} y1={50} x2={460} y2={50} stroke={HAIR} />

      {GPUS.map((g, i) => {
        const y = 60 + i * 44;
        const sel = i === 0;
        const fits = g.gb * 1 >= NEED;
        return (
          <g key={g.name}>
            {sel && <rect x={12} y={y} width={456} height={40} rx={7} fill={P3} />}
            <circle cx={30} cy={y + 20} r={5} fill="none" stroke={sel ? INK2 : "#3a3a3a"} />
            {sel && <circle cx={30} cy={y + 20} r={2.2} fill={INK} />}
            <text x={44} y={y + 17} fontSize={11.5} fill={sel ? INK : INK2}>
              {g.name}
            </text>
            <text x={44} y={y + 31} fontSize={9} fill={INK3}>
              ×{g.n} · {g.gb} GB
            </text>
            <rect x={bx} y={y + 11} width={bw} height={6} rx={3} fill="#1f1f1f" />
            <rect x={bx} y={y + 11} width={scale(g.gb)} height={6} rx={3} fill={fits ? (sel ? INK2 : "#555") : "#333"} />
            <text x={bx} y={y + 31} fontSize={9} fill={fits ? INK3 : INK2}>
              {fits ? `fits · ${g.gb - NEED} GB headroom` : `short ${NEED - g.gb} GB`}
            </text>
            <text x={460} y={y + 17} fontSize={11} textAnchor="end" fill={sel ? INK : INK2}>
              ${g.price.toFixed(2)}/h
            </text>
            <text x={460} y={y + 31} fontSize={9} textAnchor="end" fill={INK3}>
              ×{g.n} ${(g.price * g.n).toFixed(2)}
            </text>
          </g>
        );
      })}
      <line x1={tx} y1={62} x2={tx} y2={190} stroke={INK3} strokeDasharray="2 3" />
      <text x={tx + 5} y={196} fontSize={8.5} fill={INK3}>
        235B
      </text>

      {/* status */}
      <rect x={12} y={210} width={456} height={78} rx={9} fill={CODE} stroke={HAIR} />
      <line x1={46} y1={232} x2={406} y2={232} stroke={HAIR} />
      <line x1={46} y1={232} x2={46 + step * 120} y2={232} stroke={INK3} />
      {STEPS.map((s, i) => {
        const x = 46 + i * 120;
        const done = i < step;
        const cur = i === step;
        const live = cur && step === 3;
        return (
          <g key={s}>
            {cur && !live && <circle cx={x} cy={232} r={7} fill="none" stroke={INK3} className="fa-breathe" />}
            <circle cx={x} cy={232} r={3.5} fill={live ? ACC : done || cur ? INK2 : "#2c2c2c"} />
            <text x={x} y={251} fontSize={9.5} textAnchor={i === 0 ? "start" : i === 3 ? "end" : "middle"} dx={i === 0 ? -4 : i === 3 ? 4 : 0} fill={cur ? INK : done ? INK2 : INK3}>
              {s}
            </text>
          </g>
        );
      })}
      <text key={step} x={34} y={274} fontSize={10.5} className="fa-in" fill={step === 3 ? ACC : INK2}>
        {step === 3 ? "ready  " : ""}
        <tspan fill={step === 3 ? INK2 : INK2}>{STATUS[step]}</tspan>
      </text>
    </Frame>
  );
}

/* ---------------------------------------------------------------- sessions */

export function SessionsArt() {
  const my = 108;
  const by = 176;
  const main = [
    { x: 176, l: "read" },
    { x: 214, l: "edit" },
    { x: 252, l: "bash" },
    { x: 290, l: "test" },
    { x: 328, l: "fix" },
    { x: 366, l: "test" },
  ];
  const fork = [
    { x: 300, l: "sqlite" },
    { x: 344, l: "migrate" },
    { x: 388, l: "test" },
  ];
  const old = [36, 60, 84, 108, 132];
  return (
    <Frame label="Session timeline with compaction, a fork and a resume point">
      <text x={24} y={40} fontSize={10} fill={INK3}>
        session <tspan fill={INK2}>a3f9</tspan> · main
      </text>
      <text x={456} y={40} fontSize={10} textAnchor="end" fill={INK3}>
        t1 – t20
      </text>

      {/* spine */}
      <line x1={24} y1={my} x2={366} y2={my} stroke="#3a3a3a" />

      {/* compaction: old turns collapse into one summary */}
      {old.map((x) => (
        <circle key={x} cx={x} cy={my} r={4} fill="#1a1a1a" stroke={INK3} className="fa-squeeze" style={{ ["--dx" as string]: `${84 - x}px` }} />
      ))}
      <g className="fa-summary">
        <rect x={22} y={my - 18} width={124} height={36} rx={7} fill={P3} stroke="#2e2e2e" />
        <text x={34} y={my - 3} fontSize={10} fill={INK2}>
          compact t1–t14
        </text>
        <text x={34} y={my + 10} fontSize={9} fill={INK3}>
          52.4k → 6.1k tok
        </text>
      </g>

      {main.map((n, i) =>
        i === 1 ? (
          <g key={n.x}>
            <path d={`M${n.x} ${my - 6} l6 6 l-6 6 l-6 -6 z`} fill="#1a1a1a" stroke={INK2} />
            <text x={n.x} y={my - 14} fontSize={9} textAnchor="middle" fill={INK3}>
              {n.l}
            </text>
            <text x={n.x} y={my + 22} fontSize={9} textAnchor="middle" fill={INK3}>
              checkpoint
            </text>
          </g>
        ) : (
          <g key={n.x}>
            <circle cx={n.x} cy={my} r={4} fill="#1a1a1a" stroke={INK2} />
            <text x={n.x} y={my - 14} fontSize={9} textAnchor="middle" fill={INK3}>
              {n.l}
            </text>
          </g>
        ),
      )}

      {/* fork */}
      <path d={`M252 ${my} C 252 ${by - 20}, 268 ${by}, 300 ${by} L 388 ${by}`} fill="none" stroke="#3a3a3a" />
      {fork.map((n) => (
        <g key={n.x}>
          <circle cx={n.x} cy={by} r={4} fill="#1a1a1a" stroke={INK3} />
          <text x={n.x} y={by + 22} fontSize={9} textAnchor="middle" fill={INK3}>
            {n.l}
          </text>
        </g>
      ))}
      <text x={404} y={by + 3.5} fontSize={10} fill={INK3}>
        fork <tspan fill={INK2}>b71c</tspan>
      </text>

      {/* resume */}
      <line x1={370} y1={my} x2={392} y2={my} stroke={ACC} strokeDasharray="2 3" />
      <rect x={392} y={my - 11} width={66} height={22} rx={11} fill="var(--color-accent-wash)" stroke={ACC} strokeOpacity={0.55} />
      <text x={404} y={my + 3.5} fontSize={10} fill={ACC}>
        resume
      </text>
      <rect x={447} y={my - 5} width={2} height={10} fill={ACC} className="fa-caret" />

      {/* commands */}
      <line x1={24} y1={236} x2={456} y2={236} stroke={HAIR} />
      {[
        { x: 24, t: "--resume a3f9" },
        { x: 142, t: "/fork try-sqlite" },
        { x: 278, t: "/rewind t16" },
        { x: 382, t: "/compact" },
      ].map((c) => (
        <g key={c.t}>
          <rect x={c.x} y={252} width={c.t.length * 6 + 16} height={22} rx={6} fill={P3} />
          <text x={c.x + 8} y={266.5} fontSize={10} fill={INK2}>
            {c.t}
          </text>
        </g>
      ))}
    </Frame>
  );
}

/* ------------------------------------------------------------------ budget */

const SPEND = [0.02, 0.05, 0.09, 0.12, 0.18, 0.22, 0.27, 0.31, 0.36, 0.41, 0.44, 0.47];

export function BudgetArt() {
  const x0 = 56;
  const x1 = 392;
  const top = 26;
  const bot = 138;
  const sx = (i: number) => x0 + ((x1 - x0) / SPEND.length) * i;
  const sy = (v: number) => bot - (v / 0.6) * (bot - top);
  let d = `M${x0} ${bot}`;
  SPEND.forEach((v, i) => {
    d += ` H${sx(i + 1).toFixed(1)} V${sy(v).toFixed(1)}`;
  });
  const area = `${d} V${bot} Z`;
  const ex = sx(SPEND.length);
  const ey = sy(SPEND[SPEND.length - 1]);
  const cap = sy(0.5);

  const bx = 150;
  const bw = 226;
  const ms = (t: number) => (t / 2410) * bw;
  const SPANS = [
    { k: "turn", n: "7", s: 0, e: 2410, dur: "2.41s", usd: "$0.031", fill: "#3a3a3a", ind: 0 },
    { k: "model", n: "gpt-5.4", s: 0, e: 1620, dur: "1.62s", usd: "$0.029", fill: "#6a6a6a", ind: 12 },
    { k: "tool", n: "bash", s: 1640, e: 2280, dur: "640ms", usd: "$0.002", fill: "#4a4a4a", ind: 12 },
    { k: "tool", n: "read_file", s: 2290, e: 2380, dur: "90ms", usd: "—", fill: "#4a4a4a", ind: 12 },
  ];
  return (
    <Frame label="Spend climbing to a max_usd ceiling, with trace spans">
      {/* axes */}
      {[0, 0.25].map((v) => (
        <g key={v}>
          <line x1={x0} y1={sy(v)} x2={456} y2={sy(v)} stroke={HAIR} />
          <text x={x0 - 8} y={sy(v) + 3} fontSize={9} textAnchor="end" fill={INK3}>
            ${v.toFixed(2)}
          </text>
        </g>
      ))}
      <text x={x0 - 8} y={cap + 3} fontSize={9} textAnchor="end" fill={INK2}>
        $0.50
      </text>
      {/* turn 7 band */}
      <rect x={sx(6)} y={top - 6} width={sx(7) - sx(6)} height={bot - top + 6} fill="#ffffff" fillOpacity={0.035} />
      <text x={(sx(6) + sx(7)) / 2} y={bot + 14} fontSize={9} textAnchor="middle" fill={INK3}>
        t7
      </text>
      <text x={x0} y={bot + 14} fontSize={9} fill={INK3}>
        t1
      </text>
      <text x={ex} y={bot + 14} fontSize={9} textAnchor="middle" fill={INK3}>
        t12
      </text>

      {/* ceiling */}
      <line x1={x0} y1={cap} x2={456} y2={cap} stroke={INK3} strokeDasharray="4 4" />
      <text x={456} y={cap - 7} fontSize={9.5} textAnchor="end" fill={INK2}>
        max_usd=0.50
      </text>

      <path d={area} fill="#ffffff" fillOpacity={0.04} className="fa-fadein" />
      <path d={d} fill="none" stroke={INK2} strokeWidth={1.4} strokeLinejoin="round" pathLength={1} className="fa-draw" />
      <g className="fa-stop">
        <line x1={ex} y1={ey} x2={456} y2={ey} stroke={ACC} strokeOpacity={0.5} />
        <circle cx={ex} cy={ey} r={7} fill={ACC} fillOpacity={0.15} />
        <circle cx={ex} cy={ey} r={3.2} fill={ACC} />
        <text x={456} y={ey + 16} fontSize={9.5} textAnchor="end" fill={ACC}>
          stop · $0.47
        </text>
      </g>

      {/* trace */}
      <line x1={20} y1={170} x2={460} y2={170} stroke={HAIR} />
      <text x={20} y={188} fontSize={9.5} fill={INK3}>
        trace
      </text>
      <text x={416} y={188} fontSize={9} textAnchor="end" fill={INK3}>
        time
      </text>
      <text x={460} y={188} fontSize={9} textAnchor="end" fill={INK3}>
        cost
      </text>
      {SPANS.map((s, i) => {
        const y = 204 + i * 22;
        return (
          <g key={s.k + s.n}>
            <text x={20 + s.ind} y={y + 3.5} fontSize={10} fill={INK3}>
              {s.k} <tspan fill={i === 0 ? INK : INK2}>{s.n}</tspan>
            </text>
            <rect x={bx} y={y - 4} width={bw} height={8} rx={2} fill="#161616" />
            <rect x={bx + ms(s.s)} y={y - 4} width={Math.max(ms(s.e - s.s), 3)} height={8} rx={2} fill={s.fill} className="fa-grow" style={{ animationDelay: `${0.15 * i}s` }} />
            <text x={416} y={y + 3.5} fontSize={9.5} textAnchor="end" fill={INK2}>
              {s.dur}
            </text>
            <text x={460} y={y + 3.5} fontSize={9.5} textAnchor="end" fill={s.usd === "—" ? INK3 : INK2}>
              {s.usd}
            </text>
          </g>
        );
      })}
    </Frame>
  );
}

export const FEATURE_ART: Record<"models" | "tools" | "mcp" | "deploy" | "sessions" | "budget", ComponentType> = {
  models: ModelsArt,
  tools: ToolsArt,
  mcp: McpArt,
  deploy: DeployArt,
  sessions: SessionsArt,
  budget: BudgetArt,
};
