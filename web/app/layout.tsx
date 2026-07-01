import type { Metadata } from "next";
import { Google_Sans, Geist_Mono, Instrument_Serif } from "next/font/google";
import "./globals.css";

const googleSans = Google_Sans({
  variable: "--ff-sans",
  subsets: ["latin"],
  display: "swap",
});

const geistMono = Geist_Mono({
  variable: "--ff-mono",
  subsets: ["latin"],
  display: "swap",
});

const instrumentSerif = Instrument_Serif({
  variable: "--ff-serif",
  weight: "400",
  style: ["normal", "italic"],
  subsets: ["latin"],
  display: "swap",
});

const SITE = "https://mantis-agent-sdk.vercel.app";

export const metadata: Metadata = {
  metadataBase: new URL(SITE),
  title: {
    default: "mantis — the Claude Agent SDK, for open models",
    template: "%s · mantis-agent-sdk",
  },
  description:
    "Write to Anthropic's claude-agent-sdk API; run the loop against Llama, Qwen, DeepSeek, GLM, or anything you serve. The migration is one import. Plus mantis — a Claude-Code-style terminal for open models.",
  keywords: [
    "claude agent sdk",
    "open source agent sdk",
    "ollama",
    "vllm",
    "mcp",
    "tool use",
    "coding agent",
    "mantis",
  ],
  authors: [{ name: "mantis-agent-sdk" }],
  openGraph: {
    title: "mantis — the Claude Agent SDK, for open models",
    description:
      "Write to Anthropic's claude-agent-sdk API; run it on any model you can serve. One import.",
    url: SITE,
    siteName: "mantis-agent-sdk",
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
    title: "mantis — the Claude Agent SDK, for open models",
    description:
      "Write to Anthropic's claude-agent-sdk API; run it on any model you can serve. One import.",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${googleSans.variable} ${geistMono.variable} ${instrumentSerif.variable} antialiased`}
    >
      <body>{children}</body>
    </html>
  );
}
