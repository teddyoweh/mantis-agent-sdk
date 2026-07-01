import Link from "next/link";

/* Shared gradient defs for the mantis mark — rendered once in the root
   layout so multiple <MantisMark/> instances reference one definition
   (avoids duplicate SVG gradient IDs across nav + footer). */
export function MantisDefs() {
  return (
    <svg width="0" height="0" aria-hidden="true" style={{ position: "absolute" }}>
      <defs>
        <linearGradient id="mantis-body" x1="0.2" y1="0" x2="0.85" y2="1">
          <stop offset="0" stopColor="#86c67f" />
          <stop offset="0.5" stopColor="#4c9153" />
          <stop offset="1" stopColor="#2f6339" />
        </linearGradient>
        <linearGradient id="mantis-arm" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#6cad6c" />
          <stop offset="1" stopColor="#295232" />
        </linearGradient>
        <linearGradient id="mantis-head" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#62a75f" />
          <stop offset="1" stopColor="#387042" />
        </linearGradient>
        <radialGradient id="mantis-eye" cx="0.35" cy="0.3" r="0.85">
          <stop offset="0" stopColor="#edb992" />
          <stop offset="0.35" stopColor="#cf6b41" />
          <stop offset="1" stopColor="#8c3d23" />
        </radialGradient>
        <linearGradient id="mantis-sheen" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#ffffff" stopOpacity="0.28" />
          <stop offset="0.5" stopColor="#ffffff" stopOpacity="0" />
        </linearGradient>
      </defs>
    </svg>
  );
}

/* A detailed praying mantis in prayer pose: triangular head with clay
   compound eyes, folded raptorial forelegs framing the head, and a
   glossy segmented abdomen. Full-colour; scales from 16px to hero. */
export function MantisMark({
  size = 22,
  className = "",
}: {
  size?: number;
  className?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      className={className}
      aria-hidden="true"
    >
      {/* segmented abdomen */}
      <path
        fill="url(#mantis-body)"
        stroke="#295232"
        strokeWidth="0.7"
        strokeLinejoin="round"
        d="M32 27.5 C34.4 27.5 35.8 29.4 35.7 33 L34.4 47.5 C34.1 50.4 33.2 51.8 32 51.8 C30.8 51.8 29.9 50.4 29.6 47.5 L28.3 33 C28.2 29.4 29.6 27.5 32 27.5 Z"
      />
      <path
        fill="url(#mantis-sheen)"
        d="M32 28.4 C33.7 28.4 34.7 29.8 34.7 33 L34.1 40 C33.9 41 33.1 41.4 32 41.4 C30.9 41.4 30.1 41 29.9 40 L29.3 33 C29.3 29.8 30.3 28.4 32 28.4 Z"
      />
      <g stroke="#295232" strokeWidth="0.55" opacity="0.5" fill="none" strokeLinecap="round">
        <path d="M29.5 37 H34.5" />
        <path d="M29.8 41.5 H34.2" />
        <path d="M30.1 46 H33.9" />
      </g>
      {/* prothorax */}
      <path
        fill="url(#mantis-body)"
        stroke="#295232"
        strokeWidth="0.7"
        d="M32 20.2 C33.4 20.2 34.1 21.7 34.1 24.1 L33.3 28.8 C33.1 30.1 32.6 30.7 32 30.7 C31.4 30.7 30.9 30.1 30.7 28.8 L29.9 24.1 C29.9 21.7 30.6 20.2 32 20.2 Z"
      />
      {/* raptorial forelegs — folded prayer */}
      <g stroke="url(#mantis-arm)" fill="none" strokeLinecap="round" strokeLinejoin="round">
        <path strokeWidth="3.1" d="M30.8 23 L17 18 L25 11.5" />
        <path strokeWidth="3.1" d="M33.2 23 L47 18 L39 11.5" />
      </g>
      {/* grabber spines */}
      <g stroke="#dc9265" strokeWidth="0.75" strokeLinecap="round" opacity="0.92">
        <path d="M20.6 14 L21.7 15.7" />
        <path d="M23.8 12.9 L24.7 14.6" />
        <path d="M27 12 L27.7 13.7" />
        <path d="M43.4 14 L42.3 15.7" />
        <path d="M40.2 12.9 L39.3 14.6" />
        <path d="M37 12 L36.3 13.7" />
      </g>
      {/* head */}
      <path
        fill="url(#mantis-head)"
        stroke="#295232"
        strokeWidth="0.7"
        strokeLinejoin="round"
        d="M24.3 12.3 C24.3 9.3 27 7.9 32 7.9 C37 7.9 39.7 9.3 39.7 12.3 L35.7 19.6 C34.2 21.8 29.8 21.8 28.3 19.6 Z"
      />
      {/* compound eyes */}
      <ellipse cx="27.3" cy="12.5" rx="2.8" ry="3.4" fill="url(#mantis-eye)" transform="rotate(-13 27.3 12.5)" />
      <ellipse cx="36.7" cy="12.5" rx="2.8" ry="3.4" fill="url(#mantis-eye)" transform="rotate(13 36.7 12.5)" />
      <circle cx="26.4" cy="11.1" r="0.8" fill="#fff" opacity="0.92" />
      <circle cx="35.6" cy="11.1" r="0.8" fill="#fff" opacity="0.92" />
      <circle cx="32" cy="11.2" r="0.7" fill="#295232" />
      {/* antennae */}
      <g fill="none" stroke="#387042" strokeWidth="1.35" strokeLinecap="round">
        <path d="M28.8 8.7 C25.8 5.2 23.8 3.5 21.2 2.4" />
        <path d="M35.2 8.7 C38.2 5.2 40.2 3.5 42.8 2.4" />
      </g>
    </svg>
  );
}

export function Logo() {
  return (
    <Link href="/" className="flex items-center gap-2 group">
      <MantisMark size={24} className="transition-transform duration-300 group-hover:-rotate-6" />
      <span className="font-mono text-[15px] tracking-tight text-ink whitespace-nowrap">
        mantis
        <span className="text-ink-3 hidden sm:inline">-agent-sdk</span>
      </span>
    </Link>
  );
}
