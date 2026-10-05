"use client";

import Image from "next/image";
import { useState } from "react";

/* The launch film, in the hero. A still of "One harness. Every model." until clicked —
   nothing downloads before that — then it plays inline, with sound, on native controls. */

const DURATION = "1:35";

export function Film() {
  const [playing, setPlaying] = useState(false);

  return (
    <div className="rise min-w-0" style={{ animationDelay: "0.15s" }}>
      <div
        className="relative aspect-video rounded-xl overflow-hidden"
        style={{ background: "#0b0d0e" }}
      >
        {playing ? (
          <video
            className="absolute inset-0 w-full h-full"
            controls
            autoPlay
            playsInline
            preload="auto"
            poster="/film/poster.jpg"
          >
            <source src="/film/mantis-film.webm" type="video/webm" />
            <source src="/film/mantis-film.mp4" type="video/mp4" />
            Your browser can&apos;t play this video.{" "}
            <a href="/film/mantis-film.mp4">Download it</a> instead.
          </video>
        ) : (
          <button
            type="button"
            onClick={() => setPlaying(true)}
            aria-label={`Play the mantis launch film (${DURATION})`}
            className="group absolute inset-0 w-full h-full cursor-pointer text-left"
          >
            <Image
              src="/film/poster.jpg"
              alt="mantis — one harness, every model"
              fill
              sizes="(min-width: 1024px) 820px, 100vw"
              loading="eager"
              fetchPriority="high"
              className="object-cover transition-transform duration-700 ease-out group-hover:scale-[1.015]"
            />
            <span
              className="mono absolute left-3.5 bottom-3.5 sm:left-5 sm:bottom-5 flex items-center gap-2.5 rounded-full pl-1.5 pr-4 py-1.5 text-[12.5px] transition-colors"
              style={{
                color: "#ededec",
                background: "rgba(11,13,14,0.78)",
                boxShadow: "inset 0 0 0 1px rgba(255,255,255,0.14)",
                backdropFilter: "blur(6px)",
              }}
            >
              <span
                className="grid place-items-center w-7 h-7 rounded-full transition-transform duration-300 group-hover:scale-110"
                style={{ background: "var(--color-mantis-soft)" }}
              >
                <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true">
                  <path d="M1 0.8 L9.4 6 L1 11.2 Z" fill="#0b0d0e" />
                </svg>
              </span>
              Watch the film
              <span style={{ color: "rgba(237,237,236,0.5)" }}>{DURATION}</span>
            </span>
          </button>
        )}
      </div>
    </div>
  );
}
