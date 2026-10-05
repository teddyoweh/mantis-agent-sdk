const MANTIS_ART = `            ▄▀▄▀
           ▄█▀
        ▄██▀▀█▀
    ▄█ ▄███▀▀
 ▄▄██▀▀██▀▀▀▀▀
 ▀▀ █  █▀ ▀▄
 ▄▄▀  ▄▀   ▀▄`;

export function Terminal() {
  return (
    <div
      className="mono text-[12.5px] sm:text-[13px] leading-[1.7] rounded-xl overflow-hidden shadow-[0_24px_60px_-30px_rgba(12,19,16,0.45)]"
      style={{ background: "var(--color-code)" }}
    >
      {/* titlebar */}
      <div
        className="flex items-center gap-2 px-4 py-2.5"
        style={{ background: "var(--color-code-2)" }}
      >
        <span className="w-2.5 h-2.5 rounded-full" style={{ background: "#25312a" }} />
        <span className="w-2.5 h-2.5 rounded-full" style={{ background: "#25312a" }} />
        <span className="w-2.5 h-2.5 rounded-full" style={{ background: "#25312a" }} />
        <span className="ml-2 text-[11.5px]" style={{ color: "#7a887f" }}>
          mantis — ~/code/todo-api
        </span>
      </div>

      <div className="px-4 sm:px-5 py-4 overflow-x-auto code-scroll">
        {/* banner */}
        <div className="flex gap-5">
          <pre className="whitespace-pre" style={{ color: "var(--color-mantis-soft)" }}>
            {MANTIS_ART}
          </pre>
          <div className="hidden sm:block pt-1 space-y-0.5" style={{ color: "#7a887f" }}>
            <div style={{ color: "#e2eae4" }}>Mantis Code v2.56.0</div>
            <div>
              qwen2.5-coder:7b · <span style={{ color: "var(--color-mantis-soft)" }}>Ollama (local)</span>
            </div>
            <div>~/code/todo-api</div>
          </div>
        </div>

        {/* prompt */}
        <div className="mt-4 flex gap-2">
          <span style={{ color: "var(--color-mantis-soft)" }}>›</span>
          <span style={{ color: "#e2eae4" }}>build me a fastapi todo app</span>
        </div>

        {/* tool call */}
        <div className="mt-4" style={{ color: "#e3c565" }}>
          ⚒ Edit <span style={{ color: "#e2eae4" }}>app/main.py</span>{" "}
          <span style={{ color: "var(--color-mantis-soft)" }}>+12</span>{" "}
          <span style={{ color: "#e67e80" }}>-0</span>
        </div>
        <div className="mt-1.5 pl-3" style={{ color: "#7a887f" }}>
          <div>
            <span className="pr-3 opacity-50">1</span>
            <span style={{ color: "var(--color-mantis-soft)" }}>+ from fastapi import FastAPI</span>
          </div>
          <div>
            <span className="pr-3 opacity-50">2</span>
            <span style={{ color: "var(--color-mantis-soft)" }}>+ app = FastAPI()</span>
          </div>
          <div>
            <span className="pr-3 opacity-50">3</span>
            <span style={{ color: "var(--color-mantis-soft)" }}>
              + todos: list[str] = []
            </span>
          </div>
          <div className="pl-6 opacity-60">…</div>
        </div>

        {/* result */}
        <div className="mt-3" style={{ color: "#e2eae4" }}>
          <span style={{ color: "var(--color-mantis-soft)" }}>●</span> Done — run it with{" "}
          <span style={{ color: "#e3c565" }}>uvicorn app.main:app --reload</span>.
        </div>

        {/* live spinner line */}
        <div className="mt-4 flex items-center gap-2" style={{ color: "#7a887f" }}>
          <span className="w-1.5 h-1.5 rounded-full animate-pulse" style={{ background: "var(--color-mantis-soft)" }} />
          <span>✻ Undulating…</span>
          <span className="opacity-50">(3s · esc to interrupt)</span>
          <span className="caret" />
        </div>
      </div>
    </div>
  );
}
