"use client";

import { useEffect, useRef, useState } from "react";

/* The closing scene, pushed to the edge: a living night meadow.
   Stars twinkle, mist drifts, grass sways in the wind, fireflies
   (this week's models) blink and wander. The mantis breathes, its
   antennae sway, its pseudopupil follows your cursor — and when a
   model drifts close, it telegraphs, cocks the raptorial foreleg,
   and strikes. The log speaks SDK: tools intact, N turns, $cost. */

const PREY = [
  "qwen3:32b",
  "llama4:scout",
  "deepseek-r1:70b",
  "gpt-oss:120b",
  "gemini-2.0-flash",
  "Kimi-K2.6",
  "GLM-5",
  "gpt-4o-mini",
  "mistral:7b",
  "phi4:medium",
  "MiniMax-M2.5",
  "llama3.3:70b",
];

type Fly = {
  id: number;
  name: string;
  baseX: number;
  baseY: number;
  ampX: number;
  ampY: number;
  speed: number;
  phase: number;
  depth: number;
  blinkDur: number;
  caught: boolean;
};

function rnd(id: number, n: number) {
  const x = Math.sin(id * 127.1 + n * 311.7) * 43758.5453;
  return x - Math.floor(x);
}

function makeFly(id: number, name: string, lane: number): Fly {
  return {
    id,
    name,
    baseX: 5 + rnd(id, 1) * 44,
    baseY: 14 + lane * 10.5 + rnd(id, 2) * 4,
    ampX: 2.5 + rnd(id, 3) * 4,
    ampY: 1.5 + rnd(id, 4) * 2.5,
    speed: 0.22 + rnd(id, 5) * 0.35,
    phase: rnd(id, 6) * Math.PI * 2,
    depth: 0.35 + rnd(id, 7) * 0.65,
    blinkDur: 3.2 + rnd(id, 8) * 2.6,
    caught: false,
  };
}

const STARS = Array.from({ length: 26 }, (_, i) => ({
  left: rnd(i + 40, 1) * 96 + 1,
  top: rnd(i + 40, 2) * 46 + 2,
  size: rnd(i + 40, 3) > 0.75 ? 2 : 1,
  delay: rnd(i + 40, 4) * 5,
  dur: 2.5 + rnd(i + 40, 5) * 4,
}));

const MOTES = Array.from({ length: 9 }, (_, i) => ({
  left: rnd(i + 80, 1) * 92 + 2,
  top: 30 + rnd(i + 80, 2) * 60,
  delay: rnd(i + 80, 3) * 12,
  dur: 13 + rnd(i + 80, 4) * 11,
}));

type Arc = { x1: number; y1: number; x2: number; y2: number } | null;
type Phase = "idle" | "cock" | "lunge";

export function HuntScene() {
  const stageRef = useRef<HTMLDivElement>(null);
  const mantisRef = useRef<SVGSVGElement>(null);
  const pupilRef = useRef<SVGEllipseElement>(null);
  const flyRefs = useRef<(HTMLDivElement | null)[]>([]);
  const fliesRef = useRef<Fly[]>(PREY.slice(0, 6).map((n, i) => makeFly(i, n, i)));
  const nextName = useRef(6);
  const [log, setLog] = useState<string[]>([]);
  const [phase, setPhase] = useState<Phase>("idle");
  const [arc, setArc] = useState<Arc>(null);
  const [reduced, setReduced] = useState(false);

  /* pseudopupil tracks the cursor — the mantis stare */
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const onMove = (e: MouseEvent) => {
      const pupil = pupilRef.current;
      const mantis = mantisRef.current;
      if (!pupil || !mantis) return;
      const mr = mantis.getBoundingClientRect();
      const headX = mr.left + mr.width * 0.26;
      const headY = mr.top + mr.height * 0.28;
      const dx = e.clientX - headX;
      const dy = e.clientY - headY;
      const len = Math.hypot(dx, dy) || 1;
      const k = 1.7; // max pupil travel in user units
      pupil.style.transform = `translate(${(dx / len) * k}px, ${(dy / len) * k}px)`;
    };
    stage.addEventListener("mousemove", onMove);
    return () => stage.removeEventListener("mousemove", onMove);
  }, []);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(mq.matches);
    if (mq.matches) return;

    let raf = 0;
    const t0 = performance.now();
    const tick = (now: number) => {
      const t = (now - t0) / 1000;
      fliesRef.current.forEach((f, i) => {
        const el = flyRefs.current[i];
        if (!el || f.caught) return;
        const x =
          f.baseX +
          Math.sin(t * f.speed * 2 + f.phase) * f.ampX +
          Math.sin(t * f.speed * 5.3 + f.phase * 2) * 0.8;
        const y =
          f.baseY +
          Math.cos(t * f.speed * 1.6 + f.phase * 1.3) * f.ampY +
          Math.cos(t * f.speed * 4.1 + f.phase) * 0.6;
        el.style.left = `${x}%`;
        el.style.top = `${y}%`;
      });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    const doStrike = () => {
      const alive = fliesRef.current.filter((f) => !f.caught);
      const stage = stageRef.current;
      const mantis = mantisRef.current;
      if (!alive.length || !stage || !mantis) return;
      const victim = alive.reduce((a, b) => (a.baseX > b.baseX ? a : b));
      const idx = fliesRef.current.indexOf(victim);
      const flyEl = flyRefs.current[idx];
      if (!flyEl) return;

      /* 1 — telegraph: crouch + cock the foreleg */
      setPhase("cock");

      setTimeout(() => {
        /* 2 — the explosive strike */
        const sr = stage.getBoundingClientRect();
        const mr = mantis.getBoundingClientRect();
        const fr = flyEl.getBoundingClientRect();
        const mouth = {
          x: mr.left - sr.left + mr.width * 0.14,
          y: mr.top - sr.top + mr.height * 0.3,
        };
        const target = { x: fr.left - sr.left + 6, y: fr.top - sr.top + 6 };
        setPhase("lunge");
        setArc({ x1: mouth.x, y1: mouth.y, x2: target.x, y2: target.y });

        setTimeout(() => {
          /* 3 — contact */
          victim.caught = true;
          flyEl.classList.add("fly-caught");
          const turns = 1 + Math.floor(Math.random() * 4);
          const cost = (0.0004 + Math.random() * 0.004).toFixed(4);
          setLog((l) =>
            [`✓ caught ${victim.name} · ${turns} turn${turns > 1 ? "s" : ""} · $${cost}`, ...l].slice(0, 3)
          );
        }, 220);
        setTimeout(() => {
          setArc(null);
          setPhase("idle");
        }, 500);
        setTimeout(() => {
          const name = PREY[nextName.current % PREY.length];
          nextName.current += 1;
          fliesRef.current[idx] = makeFly(victim.id + 100 + idx, name, idx);
          const el2 = flyRefs.current[idx];
          if (el2) {
            el2.classList.remove("fly-caught");
            const label = el2.querySelector(".fly-label");
            if (label) label.textContent = name;
          }
        }, 2500);
      }, 300); // cock duration — the wind-up
    };

    const first = setTimeout(doStrike, 2200);
    const strike = setInterval(doStrike, 4600);
    return () => {
      cancelAnimationFrame(raf);
      clearTimeout(first);
      clearInterval(strike);
    };
  }, []);

  const mantisClass =
    reduced ? "" : phase === "cock" ? "hm-cock" : phase === "lunge" ? "mantis-strike" : "mantis-idle";

  return (
    <section
      aria-label="A new model every week; mantis catches it"
      style={{ background: "var(--color-code)" }}
    >
      <div className="wrap py-20">
        <div className="eyebrow" style={{ color: "#8b8577" }}>
          why &ldquo;mantis&rdquo;
        </div>
        <h2
          className="font-display mt-3 text-[clamp(1.5rem,2.8vw,2.1rem)] max-w-[640px]"
          style={{ color: "#e8e2d4" }}
        >
          A new model drops every week. Your code doesn&apos;t move a line.
        </h2>

        <div
          ref={stageRef}
          className="relative mt-10 h-[300px] sm:h-[360px] overflow-hidden rounded-xl"
          style={{
            background:
              "linear-gradient(180deg, #191d17 0%, #20241d 55%, #262a20 100%)",
          }}
        >
          {/* moon + halo */}
          <div
            className="absolute pointer-events-none"
            style={{
              left: "9%",
              top: "10%",
              width: 54,
              height: 54,
              borderRadius: 999,
              background: "radial-gradient(circle at 38% 34%, #f2f0e2, #cfd4b8 70%)",
              boxShadow: "0 0 60px 22px rgba(226,232,200,0.13), 0 0 140px 60px rgba(226,232,200,0.05)",
              opacity: 0.85,
            }}
          />

          {/* stars */}
          {!reduced &&
            STARS.map((s, i) => (
              <span
                key={i}
                className="star"
                style={{
                  left: `${s.left}%`,
                  top: `${s.top}%`,
                  width: s.size,
                  height: s.size,
                  animationDelay: `${s.delay}s`,
                  animationDuration: `${s.dur}s`,
                }}
              />
            ))}

          {/* drifting mist, two depths */}
          <div className="mist mist-a" />
          <div className="mist mist-b" />

          {/* dust motes */}
          {!reduced &&
            MOTES.map((m, i) => (
              <span
                key={i}
                className="mote"
                style={{
                  left: `${m.left}%`,
                  top: `${m.top}%`,
                  animationDelay: `${m.delay}s`,
                  animationDuration: `${m.dur}s`,
                }}
              />
            ))}

          {/* fireflies */}
          {fliesRef.current.map((f, i) => (
            <div
              key={f.id}
              ref={(el) => {
                flyRefs.current[i] = el;
              }}
              className="fly absolute"
              style={{
                left: `${f.baseX}%`,
                top: `${f.baseY}%`,
                opacity: 0.45 + f.depth * 0.55,
                transform: `scale(${0.75 + f.depth * 0.35})`,
                filter: f.depth < 0.55 ? "blur(0.4px)" : "none",
              }}
            >
              <span
                className="fly-dot"
                style={{ animationDuration: `${f.blinkDur}s`, animationDelay: `${(i * 0.9) % 3}s` }}
              />
              <span className="fly-label mono">{f.name}</span>
            </div>
          ))}

          {/* strike arc */}
          {arc && (
            <svg className="absolute inset-0 w-full h-full pointer-events-none">
              <path
                className="strike-arc"
                d={`M ${arc.x1} ${arc.y1} Q ${(arc.x1 + arc.x2) / 2} ${
                  Math.min(arc.y1, arc.y2) - 46
                } ${arc.x2} ${arc.y2}`}
                fill="none"
                stroke="var(--color-mantis-soft)"
                strokeWidth="1.6"
                strokeLinecap="round"
              />
              <circle className="strike-tip" cx={arc.x2} cy={arc.y2} r="4.5" fill="var(--color-mantis-soft)" />
            </svg>
          )}

          {/* far grass — dimmer, shorter, slower wind */}
          <svg
            className="absolute bottom-0 left-0 w-full pointer-events-none grass-sway-b"
            height="34"
            preserveAspectRatio="none"
            viewBox="0 0 1200 34"
            aria-hidden="true"
          >
            <g fill="none" stroke="#1c281c" strokeWidth="2.5" strokeLinecap="round" opacity="0.85">
              <path d="M20 34 C22 24 18 18 24 8" />
              <path d="M90 34 C88 26 94 20 92 12" />
              <path d="M170 34 C174 24 168 16 174 8" />
              <path d="M310 34 C308 26 314 18 310 10" />
              <path d="M470 34 C474 24 468 18 474 6" />
              <path d="M620 34 C618 26 624 18 620 10" />
              <path d="M760 34 C764 24 758 16 764 8" />
              <path d="M900 34 C898 26 904 18 900 12" />
              <path d="M1050 34 C1054 24 1048 16 1054 6" />
              <path d="M1170 34 C1168 26 1174 20 1170 10" />
            </g>
          </svg>

          {/* near grass — taller, gusting */}
          <svg
            className="absolute bottom-0 left-0 w-full pointer-events-none grass-sway-a"
            height="58"
            preserveAspectRatio="none"
            viewBox="0 0 1200 58"
            aria-hidden="true"
          >
            <g fill="none" stroke="#22331f" strokeWidth="3" strokeLinecap="round" opacity="0.95">
              <path d="M40 58 C42 38 38 26 46 12" />
              <path d="M120 58 C118 42 126 32 122 20" />
              <path d="M245 58 C250 40 244 30 252 18" />
              <path d="M400 58 C398 44 406 34 402 26" />
              <path d="M560 58 C565 40 558 28 566 14" />
              <path d="M700 58 C698 44 706 36 702 24" />
              <path d="M840 58 C846 40 838 30 848 16" />
              <path d="M1000 58 C998 42 1006 32 1002 22" />
              <path d="M1140 58 C1144 40 1138 28 1146 12" />
            </g>
          </svg>

          {/* the mantis — inline anatomy so it can live */}
          <svg
            ref={mantisRef}
            viewBox="0 0 128 128"
            className={`hm-svg absolute right-[2%] bottom-[-2%] w-[220px] sm:w-[280px] select-none pointer-events-none ${mantisClass}`}
            aria-hidden="true"
          >
            <defs>
              <linearGradient id="hm-back" x1="0.3" y1="0" x2="0.7" y2="1">
                <stop offset="0" stopColor="#2c5e38" />
                <stop offset="0.45" stopColor="#468950" />
                <stop offset="1" stopColor="#83c87b" />
              </linearGradient>
              <linearGradient id="hm-neck" x1="0" y1="0" x2="0.6" y2="1">
                <stop offset="0" stopColor="#74b96f" />
                <stop offset="1" stopColor="#356f41" />
              </linearGradient>
              <linearGradient id="hm-wing" x1="0.15" y1="0.05" x2="0.85" y2="1">
                <stop offset="0" stopColor="#69b064" />
                <stop offset="0.55" stopColor="#3f7d48" />
                <stop offset="1" stopColor="#274f31" />
              </linearGradient>
              <linearGradient id="hm-wingsheen" x1="0" y1="0" x2="0.3" y2="1">
                <stop offset="0" stopColor="#ffffff" stopOpacity="0.38" />
                <stop offset="0.55" stopColor="#ffffff" stopOpacity="0" />
              </linearGradient>
              <linearGradient id="hm-femur" x1="0" y1="0" x2="0.5" y2="1">
                <stop offset="0" stopColor="#84c67c" />
                <stop offset="1" stopColor="#3a7a49" />
              </linearGradient>
              <linearGradient id="hm-leg" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stopColor="#58995c" />
                <stop offset="1" stopColor="#295232" />
              </linearGradient>
              <linearGradient id="hm-legfar" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stopColor="#3a6f43" />
                <stop offset="1" stopColor="#234b2e" />
              </linearGradient>
              <linearGradient id="hm-headg" x1="0.2" y1="0" x2="0.7" y2="1">
                <stop offset="0" stopColor="#6cb066" />
                <stop offset="1" stopColor="#346e40" />
              </linearGradient>
              <radialGradient id="hm-eye" cx="0.36" cy="0.28" r="0.9">
                <stop offset="0" stopColor="#eaf2d0" />
                <stop offset="0.38" stopColor="#a9d184" />
                <stop offset="0.72" stopColor="#5a9a54" />
                <stop offset="1" stopColor="#31663c" />
              </radialGradient>
              <radialGradient id="hm-gshadow" cx="0.5" cy="0.5" r="0.5">
                <stop offset="0" stopColor="#0c0e0a" stopOpacity="0.5" />
                <stop offset="1" stopColor="#0c0e0a" stopOpacity="0" />
              </radialGradient>
              <linearGradient id="hm-rim" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor="#c7ecb8" stopOpacity="0.9" />
                <stop offset="1" stopColor="#c7ecb8" stopOpacity="0" />
              </linearGradient>
            </defs>

            <g strokeLinecap="round" strokeLinejoin="round">
              <ellipse cx="76" cy="122" rx="46" ry="6.5" fill="url(#hm-gshadow)" />

              {/* far legs */}
              <g fill="none" stroke="url(#hm-legfar)">
                <path strokeWidth="2.3" d="M64 68 L53 92 L64 112 L58 122" />
                <path strokeWidth="2.3" d="M75 76 L91 94 L103 114 L113 122" />
                <g strokeWidth="1.2">
                  <path d="M64 112 L61 121" />
                  <path d="M103 114 L106 122" />
                </g>
              </g>

              {/* abdomen + wing breathe together */}
              <g className="hm-breathe">
                <path
                  fill="url(#hm-back)"
                  stroke="#244d2f"
                  strokeWidth="0.8"
                  d="M58 62 C69 63 85 74 99 92 C108 104 112 111 108 113 C104 114.5 97 108 90 99 C78 86 63 79 56 72 C52 67 53 61 58 62 Z"
                />
                <g stroke="#244d2f" strokeWidth="0.6" opacity="0.5" fill="none">
                  <path d="M64 69 C68 73 70 77 70 81" />
                  <path d="M72 75 C76 80 78 84 78 89" />
                  <path d="M81 83 C85 88 87 92 87 97" />
                  <path d="M90 92 C94 97 96 101 95 105" />
                </g>
                <g fill="none" stroke="#2f6339" strokeWidth="1">
                  <path d="M108 113 L113 116" />
                  <path d="M106 114 L110 119" />
                </g>
                <path
                  fill="url(#hm-wing)"
                  stroke="#244d2f"
                  strokeWidth="0.85"
                  d="M55 57 C74 57 97 70 114 94 C119 101 117 106 112 104 C102 100 89 92 78 82 C65 71 57 68 51 63 C48 60 51 56 55 57 Z"
                />
                <path fill="none" stroke="#1f4429" strokeWidth="1.1" opacity="0.6" d="M55 57.5 C74 57.5 96 70 113 93.5" />
                <path
                  fill="url(#hm-wingsheen)"
                  d="M56 59 C72 59 92 70 107 90 C110 95 109 99 105 97 C96 93 86 86 77 78 C66 68 58 66 53 63 C50 61 52 58 56 59 Z"
                />
                <g fill="none" stroke="#274f31" strokeWidth="0.5" opacity="0.55">
                  <path d="M57 60 C71 63 88 74 103 93" />
                  <path d="M56 64 C69 68 84 79 98 97" />
                  <path d="M56 69 C67 73 80 84 92 100" />
                  <path d="M58 74 C67 78 77 87 87 101" />
                  <path d="M66 63 L64 68" strokeWidth="0.4" />
                  <path d="M78 71 L75 77" strokeWidth="0.4" />
                  <path d="M90 82 L86 89" strokeWidth="0.4" />
                </g>
                <g fill="#254f31" opacity="0.28">
                  <ellipse cx="74" cy="73" rx="2.4" ry="1.5" transform="rotate(38 74 73)" />
                  <ellipse cx="88" cy="86" rx="2" ry="1.2" transform="rotate(40 88 86)" />
                  <ellipse cx="64" cy="67" rx="1.6" ry="1" transform="rotate(35 64 67)" />
                </g>
              </g>

              {/* prothorax */}
              <path
                fill="url(#hm-neck)"
                stroke="#244d2f"
                strokeWidth="0.8"
                d="M36 41 C41 40 47 45 54 54 C58 59 60 63 58 65 C56 67 52 64 48 59 C42 51 37 46 34 44 C32 42 34 41 36 41 Z"
              />
              <path fill="none" stroke="url(#hm-rim)" strokeWidth="1" d="M37 41.5 C42 41 48 46 55 55" />

              {/* near legs */}
              <g fill="none" stroke="url(#hm-leg)">
                <path strokeWidth="2.9" d="M60 64 L48 88 L57 110 L50 121" />
                <path strokeWidth="2.9" d="M71 72 L88 92 L100 114 L110 123" />
                <g strokeWidth="1.5">
                  <path d="M57 110 L47 118" />
                  <path d="M100 114 L112 120" />
                </g>
              </g>
              <g fill="none" stroke="#2c5636" strokeWidth="0.55" opacity="0.75">
                <path d="M53 76 L51 78" />
                <path d="M55 82 L53 84" />
                <path d="M80 84 L82 82" />
                <path d="M84 90 L86 88" />
              </g>

              {/* head */}
              <path
                fill="url(#hm-headg)"
                stroke="#244d2f"
                strokeWidth="0.8"
                d="M35 30 C39.5 30 42.5 33 42.5 38 C42.5 43.4 39 47.4 33 48.4 C26.5 49.4 21.5 46 21.5 42.6 C21.5 38.6 26 32.6 35 30 Z"
              />
              <path fill="none" stroke="url(#hm-rim)" strokeWidth="0.9" d="M35 30.6 C39 30.6 41.8 33.4 42 37.6" />
              <path fill="#2b5a35" d="M22.5 43.6 C20.4 44.6 19.6 46.6 21.6 46.8 C23.6 47 25.6 45.6 25.4 43.8 Z" />
              <path fill="none" stroke="#2b5a35" strokeWidth="0.8" d="M23.5 47 L22 50 M25.5 47.4 L24.8 50.6" />

              {/* compound eye + tracking pseudopupil */}
              <ellipse cx="33" cy="36" rx="5.6" ry="6.4" fill="url(#hm-eye)" transform="rotate(-18 33 36)" />
              <ellipse
                ref={pupilRef}
                className="hm-pupil"
                cx="34.6"
                cy="38.6"
                rx="1.7"
                ry="2.2"
                fill="#20391f"
                opacity="0.92"
                transform="rotate(-18 34.6 38.6)"
              />
              <circle cx="30.5" cy="32.6" r="1.25" fill="#fff" opacity="0.92" />
              <circle cx="35.8" cy="34.4" r="0.5" fill="#fff" opacity="0.6" />

              {/* antennae — their own sway */}
              <g className="hm-antennae" fill="none" stroke="#356f41" strokeWidth="1.25">
                <path d="M34 29 C26 19 18 12 7 8" />
                <path d="M37.5 30 C31 20 24 12 15 5.5" />
              </g>

              {/* raptorial forelegs — cock + extend */}
              <g className="hm-arm">
                <g fill="none" stroke="#3a7047">
                  <path strokeWidth="3" d="M55 60 L45 49 L33 43 L43.5 39.5" />
                </g>
                <path
                  fill="url(#hm-femur)"
                  stroke="#244d2f"
                  strokeWidth="0.7"
                  d="M54 61 C51 57 48 53 44 50 C41 48 39 49 40 51 C42 55 46 59 50 62 C52 63 55 63 54 61 Z"
                />
                <path
                  fill="url(#hm-femur)"
                  stroke="#244d2f"
                  strokeWidth="0.7"
                  d="M44 50 C40 47 33.5 43.6 28.5 42.6 C26 42.1 25 43.8 27 45.3 C31.6 48.8 38 52 42 54 C44.2 55.1 46 51.5 44 50 Z"
                />
                <path fill="none" stroke="#2c6a3c" strokeWidth="0.5" opacity="0.6" d="M30 44.5 C34 47 39 49.5 43 51.5" />
                <path
                  fill="url(#hm-femur)"
                  stroke="#244d2f"
                  strokeWidth="0.7"
                  d="M28.5 42.6 C33 40 39.5 38.4 44.6 38.6 C47.2 38.7 47.6 40.9 45.4 41.9 C41 44 34.5 45.8 30.8 46.2 C28.6 46.4 26 43.9 28.5 42.6 Z"
                />
                <path fill="none" stroke="#244d2f" strokeWidth="1.4" d="M44.8 38.8 C47 38 48 39.5 46.6 40.8" />
                <g fill="none" stroke="#dc9265" strokeWidth="0.75" opacity="0.95">
                  <path d="M31 45.4 L31.9 47.2" />
                  <path d="M34.6 46.9 L35.4 48.7" />
                  <path d="M38 48.4 L38.7 50.2" />
                  <path d="M41 49.9 L41.6 51.7" />
                </g>
                <g fill="none" stroke="#b5d99f" strokeWidth="0.55" opacity="0.8">
                  <path d="M32 43.2 L32.6 41.7" />
                  <path d="M35.4 42.4 L36 40.9" />
                  <path d="M38.6 41.7 L39.2 40.3" />
                </g>
              </g>
            </g>
          </svg>

          {/* the catch log */}
          <div className="absolute left-4 bottom-3 space-y-1">
            {log.map((line, i) => (
              <div
                key={`${line}-${i}`}
                className="mono text-[11.5px]"
                style={{
                  color: i === 0 ? "var(--color-mantis-soft)" : "#8b8577",
                  opacity: 1 - i * 0.35,
                }}
              >
                {line}
              </div>
            ))}
            {log.length === 0 && (
              <div className="mono text-[11.5px]" style={{ color: "#8b8577" }}>
                {reduced ? "✓ any model, same loop" : "waiting…"}
              </div>
            )}
          </div>
        </div>

        <p className="mt-5 text-[13.5px] max-w-[560px] leading-relaxed" style={{ color: "#8b8577" }}>
          The ecosystem keeps shipping; the loop keeps hunting. Same agent, same tools, same
          sessions — whatever lands next.{" "}
          <span className="mono" style={{ color: "#e8e2d4" }}>
            pip install mantis-agent-sdk
          </span>
        </p>
      </div>
    </section>
  );
}
