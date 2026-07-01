import Link from "next/link";
import Image from "next/image";

/* The mantis mark is an ultra-detailed side-profile praying mantis,
   authored as a standalone SVG in /public/mantis.svg. */
export function MantisMark({
  size = 30,
  className = "",
}: {
  size?: number;
  className?: string;
}) {
  return (
    <Image
      src="/mantis.svg"
      width={size}
      height={size}
      alt="mantis"
      priority
      unoptimized
      className={className}
    />
  );
}

export function Logo() {
  return (
    <Link href="/" className="flex items-center gap-2 group">
      <MantisMark size={30} className="transition-transform duration-300 group-hover:-rotate-6" />
      <span className="font-mono text-[15px] tracking-tight text-ink whitespace-nowrap">
        mantis
        <span className="text-ink-3 hidden sm:inline">-agent-sdk</span>
      </span>
    </Link>
  );
}
