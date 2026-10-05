/* A browser window around a dashboard screenshot: traffic lights, and an
   address bar showing where `mantis serve` lives. */

export function TrafficLights() {
  return (
    <div className="flex gap-2 shrink-0" aria-hidden="true">
      <span className="w-3 h-3 rounded-full bg-[#ff5f57]" />
      <span className="w-3 h-3 rounded-full bg-[#febc2e]" />
      <span className="w-3 h-3 rounded-full bg-[#28c840]" />
    </div>
  );
}

export function BrowserFrame({ url, children }: { url: string; children: React.ReactNode }) {
  return (
    <div className="browser-frame rounded-xl overflow-hidden">
      <div className="relative flex items-center h-10 px-4 bg-paper-3">
        <TrafficLights />
        <div className="absolute left-1/2 -translate-x-1/2 w-[46%] max-w-[380px] min-w-[160px]">
          <div className="flex items-center justify-center gap-1.5 h-6 rounded-md bg-paper-2 text-[12px] text-ink-3 truncate px-3">
            <svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" className="shrink-0 opacity-70">
              <path d="M8 1a4 4 0 0 0-4 4v2H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V8a1 1 0 0 0-1-1h-1V5a4 4 0 0 0-4-4Zm-2 6V5a2 2 0 1 1 4 0v2H6Z" />
            </svg>
            <span className="truncate">{url}</span>
          </div>
        </div>
      </div>
      {children}
    </div>
  );
}
