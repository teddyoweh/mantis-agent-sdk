import Link from "next/link";

/* A geometric praying-mantis mark: triangular head, slim thorax,
   and the two folded forelegs in the iconic "prayer" fold. */
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
      viewBox="0 0 32 32"
      fill="none"
      className={className}
      aria-hidden="true"
    >
      {/* thorax / abdomen */}
      <path
        d="M20 6.5 L24.5 20 L21 27"
        stroke="currentColor"
        strokeWidth="2.1"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* head */}
      <path
        d="M20 6.5 L16.4 3.2 L21.4 3.6 Z"
        fill="currentColor"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
      {/* antennae */}
      <path
        d="M17.6 3.6 L13.8 1.4 M19.8 3.4 L18.5 0.6"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
      {/* folded foreleg — the prayer */}
      <path
        d="M20.6 9 L10 12.5 L15.5 15.5 L6.5 18.5"
        stroke="var(--color-mantis)"
        strokeWidth="2.1"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* second foreleg */}
      <path
        d="M21.8 11.4 L12.5 15 L17.5 18 L9 21"
        stroke="var(--color-mantis)"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
        opacity="0.55"
      />
    </svg>
  );
}

export function Logo() {
  return (
    <Link href="/" className="flex items-center gap-2 group">
      <MantisMark size={22} className="text-ink transition-transform duration-300 group-hover:-rotate-6" />
      <span className="font-mono text-[15px] tracking-tight text-ink whitespace-nowrap">
        mantis
        <span className="text-ink-3 hidden sm:inline">-agent-sdk</span>
      </span>
    </Link>
  );
}
