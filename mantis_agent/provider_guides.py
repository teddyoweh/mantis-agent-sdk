"""How to get an API key for each hosted provider, plus the self-host guide.

The single source of truth behind the dashboard's "How to get a key" panels
(``mantis serve``) and the docs pages. Keyed by the same provider ids as
``catalog.CATALOG``. URLs verified 2026-07; providers move their consoles, so
prefer ``keys_url`` (the create-a-key page) over guessing.
"""

from __future__ import annotations

from typing import Any

GUIDES: dict[str, dict[str, Any]] = {
    "openai": {
        "name": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "keys_url": "https://platform.openai.com/api-keys",
        "signup_url": "https://platform.openai.com/signup",
        "pricing_url": "https://openai.com/api/pricing",
        "steps": [
            "Sign in at platform.openai.com",
            "Open the API keys page",
            "Click 'Create new secret key'",
            "Name it and copy the key (shown once)",
            "Add funds under Settings → Billing",
        ],
        "free_note": "No free tier; pay-as-you-go needs a prepaid funded balance.",
    },
    "anthropic": {
        "name": "Anthropic (Claude)",
        "env_var": "ANTHROPIC_API_KEY",
        "keys_url": "https://console.anthropic.com/settings/keys",
        "signup_url": "https://console.anthropic.com/login",
        "pricing_url": "https://www.anthropic.com/pricing#api",
        "steps": [
            "Sign in at console.anthropic.com",
            "Open Settings → API Keys",
            "Click 'Create Key' and name it",
            "Copy the sk-ant- key (shown once)",
            "Add credits under Settings → Billing",
        ],
        "free_note": "No ongoing free tier; buy prepaid credits (small trial credit may apply).",
    },
    "gemini": {
        "name": "Gemini (Google AI Studio)",
        "env_var": "GEMINI_API_KEY",
        "keys_url": "https://aistudio.google.com/apikey",
        "signup_url": "https://aistudio.google.com",
        "pricing_url": "https://ai.google.dev/gemini-api/docs/pricing",
        "steps": [
            "Sign in at aistudio.google.com with a Google account",
            "Open the API keys page, click 'Create API key'",
            "Create in a new or existing Google Cloud project",
            "Copy the key and set GEMINI_API_KEY",
        ],
        "free_note": "Free tier with rate limits; paid tier unlocks higher quotas.",
    },
    "groq": {
        "name": "Groq",
        "env_var": "GROQ_API_KEY",
        "keys_url": "https://console.groq.com/keys",
        "signup_url": "https://console.groq.com",
        "pricing_url": "https://groq.com/pricing",
        "steps": [
            "Sign up at console.groq.com and verify your email",
            "Open 'API Keys', click 'Create API Key'",
            "Name the key and submit",
            "Copy it immediately, then set GROQ_API_KEY",
        ],
        "free_note": "Free tier, no credit card — all models with per-minute/daily limits.",
    },
    "deepseek": {
        "name": "DeepSeek",
        "env_var": "DEEPSEEK_API_KEY",
        "keys_url": "https://platform.deepseek.com/api_keys",
        "signup_url": "https://platform.deepseek.com/sign_up",
        "pricing_url": "https://api-docs.deepseek.com/quick_start/pricing",
        "steps": [
            "Sign up at platform.deepseek.com (email or Google)",
            "Open the API Keys page in the console",
            "Click 'Create API Key' and name it",
            "Copy the key now — it's shown only once",
            "Export it as DEEPSEEK_API_KEY",
        ],
        "free_note": "New accounts get free trial tokens; no credit card to start.",
    },
    "moonshot": {
        "name": "Kimi (Moonshot)",
        "env_var": "MOONSHOT_API_KEY",
        "keys_url": "https://platform.moonshot.ai/console/api-keys",
        "signup_url": "https://platform.moonshot.ai/",
        "pricing_url": "https://platform.moonshot.ai/docs/pricing/chat",
        "steps": [
            "Sign up at platform.moonshot.ai (Google login is easiest)",
            "Open Console → API Keys",
            "Click 'Create API Key', name it, pick a project",
            "Copy the key now — it's shown only once",
            "Export as MOONSHOT_API_KEY (base URL api.moonshot.ai/v1)",
        ],
        "free_note": "Recharge $1 to activate; $5 cumulative recharge grants a $5 bonus.",
    },
    "glm": {
        "name": "GLM (Zhipu)",
        "env_var": "ZHIPUAI_API_KEY",
        "keys_url": "https://z.ai/manage-apikey/apikey-list",
        "signup_url": "https://z.ai/",
        "pricing_url": "https://docs.z.ai/guides/overview/pricing",
        "steps": [
            "Sign up at z.ai (mainland users: open.bigmodel.cn)",
            "Open the API Keys page from your account menu",
            "Click 'Create a new API key'",
            "Copy the key now — it's shown once",
            "Set ZHIPUAI_API_KEY (base URL api.z.ai/api/paas/v4)",
        ],
        "free_note": "Free Flash-tier models; full GLM API is pay-per-token (trial credits for new users).",
    },
    "qwen": {
        "name": "Qwen (DashScope)",
        "env_var": "DASHSCOPE_API_KEY",
        "keys_url": "https://bailian.console.alibabacloud.com/?tab=model#/api-key",
        "signup_url": "https://account.alibabacloud.com/register/intl_register.htm",
        "pricing_url": "https://www.alibabacloud.com/help/en/model-studio/models",
        "steps": [
            "Register an international Alibaba Cloud account, enable Model Studio",
            "Open the Model Studio (Bailian) console, pick an intl region",
            "Select 'API Key' in the left nav, click 'Create API Key'",
            "Set workspace and permission, confirm",
            "Copy the key once (base URL dashscope-intl.aliyuncs.com)",
        ],
        "free_note": "New users get ~1M free tokens/model (valid ~180 days), then pay-as-you-go.",
    },
    "openrouter": {
        "name": "OpenRouter",
        "env_var": "OPENROUTER_API_KEY",
        "keys_url": "https://openrouter.ai/settings/keys",
        "signup_url": "https://openrouter.ai/sign-up",
        "pricing_url": "https://openrouter.ai/pricing",
        "steps": [
            "Sign up at openrouter.ai (Google or email)",
            "Open the Keys page (openrouter.ai/settings/keys)",
            "Click 'Create Key' and name it",
            "Copy the key now — it's shown only once",
            "Set OPENROUTER_API_KEY",
        ],
        "free_note": "Free (:free) models work at $0 balance; add credits for paid models/limits.",
    },
    "together": {
        "name": "Together",
        "env_var": "TOGETHER_API_KEY",
        "keys_url": "https://api.together.xyz/settings/api-keys",
        "signup_url": "https://api.together.xyz",
        "pricing_url": "https://www.together.ai/pricing",
        "steps": [
            "Create an account at api.together.xyz",
            "Open Settings → API Keys",
            "Click 'Create key' and name it",
            "Copy the key value (shown only once)",
            "Set TOGETHER_API_KEY",
        ],
        "free_note": "Small starter credit for new accounts; ~$5 top-up for continued use.",
    },
    "fireworks": {
        "name": "Fireworks",
        "env_var": "FIREWORKS_API_KEY",
        "keys_url": "https://app.fireworks.ai/settings/users/api-keys",
        "signup_url": "https://app.fireworks.ai/login",
        "pricing_url": "https://fireworks.ai/pricing",
        "steps": [
            "Sign up or log in at app.fireworks.ai",
            "Open Settings → API Keys",
            "Click 'Create API Key'",
            "Copy the key and store it securely",
            "Export it as FIREWORKS_API_KEY",
        ],
        "free_note": "New accounts get $1 free credit (10 req/min without a payment method).",
    },
    "cerebras": {
        "name": "Cerebras",
        "env_var": "CEREBRAS_API_KEY",
        "keys_url": "https://cloud.cerebras.ai/platform/apikeys",
        "signup_url": "https://cloud.cerebras.ai/",
        "pricing_url": "https://www.cerebras.ai/pricing",
        "steps": [
            "Sign up or log in at cloud.cerebras.ai",
            "Click 'API Keys' in the left nav",
            "Click 'Create API Key' and name it",
            "Copy the key and store it securely",
            "Export it as CEREBRAS_API_KEY",
        ],
        "free_note": "Free dev tier: 1M tokens/day, no card, ~30 req/min (8k context cap).",
    },
}

SELFHOST: dict[str, Any] = {
    "name": "Self-host",
    "intro": "Run an open-weight model behind any OpenAI-compatible /v1 endpoint and "
             "point mantis at its base URL.",
    "runtimes": [
        {"name": "vLLM", "note": "Fastest for GPUs; serves /v1 out of the box.",
         "command": "vllm serve <model> --host 0.0.0.0 --port 8000",
         "base_url": "http://localhost:8000/v1"},
        {"name": "Ollama", "note": "Easiest locally; built-in OpenAI-compatible /v1.",
         "command": "ollama serve   # then: ollama pull <model>",
         "base_url": "http://localhost:11434/v1"},
        {"name": "llama.cpp", "note": "CPU/Metal friendly; runs GGUF files.",
         "command": "llama-server -m model.gguf --host 0.0.0.0 --port 8080",
         "base_url": "http://localhost:8080/v1"},
        {"name": "TGI (Hugging Face)", "note": "Production server; exposes /v1/chat/completions.",
         "command": "docker run --gpus all -p 8080:80 "
                    "ghcr.io/huggingface/text-generation-inference --model-id <model>",
         "base_url": "http://localhost:8080/v1"},
        {"name": "Modal / RunPod (cloud GPU)",
         "note": "Serverless GPU; deploy a vLLM app and use its public URL.",
         "command": "", "base_url": "https://<your-app>.modal.run/v1"},
    ],
    "steps": [
        "Start your server so it serves /v1",
        "In the Models tab, open 'Self-host / custom endpoint'",
        "Paste the base URL (…/v1) and the model id",
        "Add an API key only if your server requires one",
        "Click Connect — mantis switches to it",
    ],
    "notes": [
        "Most local servers need no API key — leave it blank.",
        "The model id must match what your server reports at /v1/models.",
        "In the TUI: /connect http://localhost:8000/v1 <model>.",
        "Serve on 0.0.0.0 (not just localhost) to reach a server on another machine or the cloud.",
    ],
    # A hand-to-an-agent SKILL.md: drop it into Claude Code / any agent and it
    # deploys the open model on a cloud GPU and hands back the /connect URL.
    "skill": {
        "name": "selfhost-model skill",
        "url": "https://mantisagent.cc/selfhost.md",
        "blurb": "Don't want to do it yourself? Hand this SKILL.md to an AI agent "
                 "(Claude Code, Cursor, etc.) — it spins up a cloud GPU, deploys the "
                 "model, and hands back the ready-to-paste endpoint URL.",
    },
    # Remote GPU / sandbox platforms you can deploy to. Each has a human guide and
    # its own agent skill.
    "platforms": [
        {"name": "Modal", "kind": "Serverless GPU",
         "note": "$30/mo free; deploy a Python file; mantis auths *.modal.run natively.",
         "docs_url": "https://mantisagent.cc/docs/selfhost/modal",
         "skill_url": "https://mantisagent.cc/skills/modal.md"},
        {"name": "RunPod", "kind": "Serverless / pods",
         "note": "vLLM worker template; cheap spot pricing.",
         "docs_url": "https://mantisagent.cc/docs/selfhost/runpod",
         "skill_url": "https://mantisagent.cc/skills/runpod.md"},
        {"name": "Lambda", "kind": "GPU rental",
         "note": "Clean per-hour H100s / B200s; SSH in and run vLLM.",
         "docs_url": "https://mantisagent.cc/docs/selfhost/lambda",
         "skill_url": "https://mantisagent.cc/skills/lambda.md"},
        {"name": "Vast.ai", "kind": "GPU marketplace",
         "note": "Cheapest GPUs anywhere; variable reliability.",
         "docs_url": "https://mantisagent.cc/docs/selfhost/vastai",
         "skill_url": "https://mantisagent.cc/skills/vastai.md"},
        {"name": "HF Endpoints", "kind": "Managed endpoint",
         "note": "One click on any Hugging Face model → OpenAI-compatible URL.",
         "docs_url": "https://mantisagent.cc/docs/selfhost/hf-endpoints",
         "skill_url": "https://mantisagent.cc/skills/hf-endpoints.md"},
    ],
}


def guide_for(provider_id: str) -> dict[str, Any] | None:
    return GUIDES.get(provider_id)
