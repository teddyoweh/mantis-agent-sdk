import type { Metadata } from "next";
import { Google_Sans, Geist_Mono } from "next/font/google";
import "./globals.css";

const googleSans = Google_Sans({
  variable: "--ff-sans",
  subsets: ["latin"],
  style: ["normal", "italic"],
  display: "swap",
});

const geistMono = Geist_Mono({
  variable: "--ff-mono",
  subsets: ["latin"],
  display: "swap",
});

const SITE = "https://mantisagent.cc";

export const metadata: Metadata = {
  metadataBase: new URL(SITE),
  title: {
    default: "mantis — a coding agent and agent SDK for any model",
    template: "%s · mantis-agent-sdk",
  },
  description:
    "mantis is a terminal coding agent and a Python agent SDK that run on any model: Ollama, vLLM, llama.cpp, your own GPUs, or any hosted API. Drop-in compatible with claude-agent-sdk.",
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
    title: "mantis — a coding agent and agent SDK for any model",
    description:
      "A terminal coding agent and Python agent SDK for any model you can run.",
    url: SITE,
    siteName: "mantis-agent-sdk",
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
    title: "mantis — a coding agent and agent SDK for any model",
    description:
      "A terminal coding agent and Python agent SDK for any model you can run.",
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
      className={`${googleSans.variable} ${geistMono.variable} antialiased`}
    >
      <body>{children}</body>
    </html>
  );
}
