"use client";

import Image from "next/image";
import { useEffect, useRef, useState } from "react";

/* The launch film, shown in the hero. A still of "One harness. Every model." with a big
   play button under the headline until clicked — nothing downloads before that — then it
   plays inline, with sound, on native controls. The play button comes back whenever the
   film is paused, so it always reads as a video. */

export const FILM_DURATION = "1:35";

function PlayDisc() {
  return (
    <span
      className="film-play relative grid place-items-center w-12 h-12 sm:w-[76px] sm:h-[76px] rounded-full transition-transform duration-300 group-hover:scale-110"
      style={{ background: "var(--color-mantis-soft)", boxShadow: "0 10px 30px rgba(0,0,0,0.5)" }}
    >
      <svg viewBox="0 0 10 12" className="w-3.5 h-4 sm:w-5 sm:h-6 ml-[3px]" aria-hidden="true">
        <path d="M1 0.8 L9.4 6 L1 11.2 Z" fill="#0b0d0e" />
      </svg>
    </span>
  );
}

export function Film() {
  const [started, setStarted] = useState(false);
  const [paused, setPaused] = useState(false);
  const video = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    if (started) video.current?.play().catch(() => setPaused(true));
  }, [started]);

  return (
    <div className="relative aspect-video" style={{ background: "#0b0d0e" }}>
      {started ? (
        <>
          <video
            ref={video}
            className="absolute inset-0 w-full h-full"
            controls
            playsInline
            preload="auto"
            poster="/film/poster.jpg"
            onPlay={() => setPaused(false)}
            onPause={() => setPaused(true)}
          >
            <source src="/film/mantis-film.webm" type="video/webm" />
            <source src="/film/mantis-film.mp4" type="video/mp4" />
            Your browser can&apos;t play this video. <a href="/film/mantis-film.mp4">Download it</a> instead.
          </video>
          {paused && (
            <button
              type="button"
              onClick={() => video.current?.play()}
              aria-label="Play the film"
              className="group absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 cursor-pointer rounded-full"
            >
              <PlayDisc />
            </button>
          )}
        </>
      ) : (
        <button
          type="button"
          onClick={() => setStarted(true)}
          aria-label={`Play the mantis launch film (${FILM_DURATION})`}
          className="group absolute inset-0 w-full h-full cursor-pointer"
        >
          <Image
            src="/film/poster.jpg"
            alt="mantis — one harness, every model"
            fill
            sizes="(min-width: 1280px) 1200px, 100vw"
            loading="eager"
            fetchPriority="high"
            className="object-cover transition-transform duration-700 ease-out group-hover:scale-[1.012]"
          />
          <span
            aria-hidden="true"
            className="absolute inset-0"
            style={{ background: "radial-gradient(ellipse 34% 30% at 50% 71%, rgba(5,6,7,0.7), transparent)" }}
          />
          <span className="absolute left-1/2 top-[74%] sm:top-[71%] -translate-x-1/2 -translate-y-1/2 flex flex-col items-center gap-3">
            <PlayDisc />
            <span
              className="mono hidden sm:flex items-center gap-2 rounded-full px-3.5 py-1 text-[12.5px] whitespace-nowrap"
              style={{
                color: "#ededec",
                background: "rgba(11,13,14,0.78)",
                boxShadow: "inset 0 0 0 1px rgba(255,255,255,0.14)",
                backdropFilter: "blur(6px)",
              }}
            >
              Watch the film
              <span style={{ color: "rgba(237,237,236,0.5)" }}>{FILM_DURATION}</span>
            </span>
          </span>
        </button>
      )}
    </div>
  );
}
