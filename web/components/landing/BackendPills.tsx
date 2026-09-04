import { BRAND_ICONS } from "./brandIcons";

// `icon` is a BRAND_ICONS key; `glyph` is the terminal's own family mark for
// vendors the icon set doesn't carry (the same glyphs the mantis footer uses).
const BACKENDS: { name: string; icon?: string; glyph?: string }[] = [
  { name: "Ollama", icon: "ollama" },
  { name: "vLLM", icon: "vllm" },
  { name: "llama.cpp", icon: "llamacpp" },
  { name: "TGI", icon: "huggingface" },
  { name: "Together", icon: "together" },
  { name: "Fireworks", icon: "fireworks" },
  { name: "Groq", icon: "groq" },
  { name: "OpenRouter", icon: "openrouter" },
  { name: "Cerebras", icon: "cerebras" },
  { name: "Claude", glyph: "✦" },
  { name: "OpenAI", icon: "openai" },
  { name: "Gemini", icon: "gemini" },
  { name: "Grok", glyph: "✕" },
  { name: "Modal", icon: "modal" },
];

export function BackendPills() {
  return (
    <div className="flex flex-wrap gap-2">
      {BACKENDS.map((b) => {
        const icon = b.icon ? BRAND_ICONS[b.icon] : undefined;
        return (
          <span key={b.name} className="pill">
            {icon ? (
              <svg
                width="15"
                height="15"
                viewBox={icon.vb}
                aria-hidden="true"
                className="shrink-0"
                dangerouslySetInnerHTML={{ __html: icon.body }}
              />
            ) : (
              <span aria-hidden="true" className="shrink-0 w-[15px] text-center leading-none">
                {b.glyph}
              </span>
            )}
            {b.name}
          </span>
        );
      })}
    </div>
  );
}
