import { BRAND_ICONS } from "./brandIcons";

const BACKENDS: { name: string; icon: string }[] = [
  { name: "Ollama", icon: "ollama" },
  { name: "vLLM", icon: "vllm" },
  { name: "llama.cpp", icon: "llamacpp" },
  { name: "TGI", icon: "huggingface" },
  { name: "Together", icon: "together" },
  { name: "Fireworks", icon: "fireworks" },
  { name: "Groq", icon: "groq" },
  { name: "OpenRouter", icon: "openrouter" },
  { name: "Cerebras", icon: "cerebras" },
  { name: "OpenAI", icon: "openai" },
  { name: "Gemini", icon: "gemini" },
  { name: "Modal", icon: "modal" },
];

export function BackendPills() {
  return (
    <div className="flex flex-wrap gap-2">
      {BACKENDS.map((b) => {
        const icon = BRAND_ICONS[b.icon];
        return (
          <span key={b.name} className="pill">
            <svg
              width="15"
              height="15"
              viewBox={icon.vb}
              aria-hidden="true"
              className="shrink-0"
              dangerouslySetInnerHTML={{ __html: icon.body }}
            />
            {b.name}
          </span>
        );
      })}
    </div>
  );
}
