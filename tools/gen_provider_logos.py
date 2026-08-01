"""Fetch the real provider marks once and inline them into the package."""
import json, re, urllib.request

CDN = "https://cdn.jsdelivr.net/npm/@lobehub/icons-static-svg@latest/icons/{}.svg"
# mantis provider id -> (icon file, tint for mono marks)
SPEC = {
    "openai":     ("openai", "var(--ink)"),
    "anthropic":  ("claude-color", None),
    "gemini":     ("gemini-color", None),
    "deepseek":   ("deepseek-color", None),
    "moonshot":   ("kimi-color", None),
    "glm":        ("zhipu-color", None),
    "qwen":       ("qwen-color", None),
    "groq":       ("groq", "#f55036"),
    "openrouter": ("openrouter-color", None),
    "together":   ("together-color", None),
    "fireworks":  ("fireworks-color", None),
    "cerebras":   ("cerebras-color", None),
    "ollama":     ("ollama", "var(--ink)"),
}

def clean(svg: str) -> str:
    svg = re.sub(r"<\?xml.*?\?>", "", svg, flags=re.S)
    svg = re.sub(r"<!--.*?-->", "", svg, flags=re.S)
    svg = re.sub(r"<title>.*?</title>", "", svg, flags=re.S)
    svg = re.sub(r'\s(width|height)="[^"]*"', "", svg)      # sized by CSS
    svg = re.sub(r'\sstyle="[^"]*"', "", svg)
    svg = re.sub(r'\sxmlns:xlink="[^"]*"', "", svg)
    svg = re.sub(r">\s+<", "><", svg).strip()
    return svg

out = {}
for pid, (name, tint) in SPEC.items():
    raw = urllib.request.urlopen(CDN.format(name), timeout=20).read().decode()
    assert raw.lstrip().startswith("<svg"), (pid, raw[:80])
    out[pid] = {"svg": clean(raw)}
    if tint:
        out[pid]["tint"] = tint

body = ',\n'.join(f'    {json.dumps(k)}: {json.dumps(v)}' for k, v in out.items())
mod = '''"""Provider marks for the ``mantis serve`` dashboard.

The real vendor logos, inlined. A local dashboard has no business fetching
twelve third-party assets at page load — that would leak which providers you
look at to their CDNs and break the page offline — so they ship in the wheel.

Source: the ``@lobehub/icons-static-svg`` set (MIT), which packages each AI
provider's own mark. Colour variants are used where they exist; the monochrome
ones carry a ``tint`` and inherit it through ``currentColor``. Logos remain the
trademarks of their owners and are used here only to identify the provider.

Regenerate with ``tools/gen_provider_logos.py``.
"""

from __future__ import annotations

PROVIDER_LOGOS: dict[str, dict[str, str]] = {
%s,
}
''' % body
open("mantis_agent/serve_logos.py", "w").write(mod)
print("wrote", len(out), "logos,", len(mod), "chars")
