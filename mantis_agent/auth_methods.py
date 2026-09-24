"""Every way to authenticate each provider family — one contract for the
dashboard, the terminal and the CLI.

A *family* is one of ``anthropic`` · ``openai`` · ``gemini`` · ``xai`` ·
``oss``. Each family offers one or more :class:`AuthMethod`\\ s, e.g. Claude
via an API key, a subscription OAuth login, Vertex AI, Bedrock, or Azure AI
Foundry. Callers render the methods, collect the fields, and hand the values
to :func:`set_method`; the SDK then routes ``model=`` for that family through
the method's backend automatically.

This module is the CONTRACT: signatures and docstrings are fixed; the bodies
are implemented in the providers work. Everything here is sync except the
network probes.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "FAMILIES",
    "AuthField",
    "AuthMethod",
    "auth_methods",
    "clear_method",
    "configured_method",
    "method_status",
    "oauth_finish",
    "oauth_start",
    "set_method",
    "validate_method",
]

FAMILIES: tuple[str, ...] = ("anthropic", "openai", "gemini", "xai", "oss")

MethodKind = Literal["api_key", "oauth", "cloud", "local", "url"]


@dataclass(frozen=True)
class AuthField:
    env: str              # env var we read and persist under (user settings env)
    label: str
    secret: bool = True
    required: bool = True
    help: str = ""        # where to get it / what shape it has
    placeholder: str = ""


@dataclass(frozen=True)
class AuthMethod:
    id: str               # "api_key" | "oauth" | "vertex" | "bedrock" | "azure" | "azure_openai" | "ollama" | "selfhost" | "<hosted provider id>"
    family: str
    label: str            # "API key", "Claude subscription", "Vertex AI", ...
    kind: MethodKind
    description: str      # one line
    fields: tuple[AuthField, ...] = ()
    backend: str | None = None   # backend sentinel/URL template this method implies ("anthropic", "vertex:anthropic", "bedrock:anthropic", "https://{resource}.openai.azure.com/openai/v1", ...)
    docs_url: str = ""
    recommended: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def auth_methods(family: str) -> list[AuthMethod]:
    """All methods for a family, recommended first. Unknown family → ValueError."""
    if family not in FAMILIES:
        raise ValueError(
            f"unknown provider family {family!r}; expected one of {', '.join(FAMILIES)}"
        )
    if family == "oss":
        return list(_oss_methods())
    return list(_STATIC_METHODS[family])


# ---------------------------------------------------------------------------
# The method tables
# ---------------------------------------------------------------------------
#
# Every field names the environment variable the SDK actually reads, so a user
# who already exported it is shown as configured without re-entering anything,
# and a value entered here is persisted under the same name. That symmetry is
# the whole contract: there is no second, private place credentials live.


def _f(env: str, label: str, **kw: Any) -> AuthField:
    return AuthField(env=env, label=label, **kw)


_ANTHROPIC: tuple[AuthMethod, ...] = (
    AuthMethod(
        id="api_key", family="anthropic", label="API key", kind="api_key",
        description="A console API key (sk-ant-api…) billed per token.",
        fields=(_f("ANTHROPIC_API_KEY", "API key",
                   help="sk-ant-api… from console.anthropic.com → Settings → API Keys",
                   placeholder="sk-ant-api03-…"),),
        backend="anthropic",
        docs_url="https://console.anthropic.com/settings/keys",
        recommended=True,
    ),
    AuthMethod(
        id="oauth", family="anthropic", label="Claude subscription", kind="oauth",
        description="Sign in with a Claude Pro/Max account — no API key, no per-token bill.",
        fields=(),
        backend="anthropic",
        docs_url="https://claude.com/product/claude-code",
        extra={"login": "oauth_start", "token_env": "ANTHROPIC_AUTH_TOKEN"},
    ),
    AuthMethod(
        id="vertex", family="anthropic", label="Google Vertex AI", kind="cloud",
        description="Claude billed through your GCP project, via Vertex's Anthropic publisher.",
        fields=(
            _f("GOOGLE_CLOUD_PROJECT", "GCP project id", secret=False,
               help="The project with Vertex AI enabled and Claude model access granted.",
               placeholder="my-gcp-project"),
            _f("CLOUD_ML_REGION", "Region", secret=False, required=False,
               help="Where Claude is served — us-east5 by default.",
               placeholder="us-east5"),
            _f("GOOGLE_APPLICATION_CREDENTIALS", "Service-account key path",
               secret=False, required=False,
               help="Optional: path to a service-account JSON key. Leave blank to use "
                    "`gcloud auth application-default login`.",
               placeholder="/path/to/key.json"),
        ),
        backend="vertex:anthropic",
        docs_url="https://docs.claude.com/en/api/claude-on-vertex-ai",
    ),
    AuthMethod(
        id="bedrock", family="anthropic", label="Amazon Bedrock", kind="cloud",
        description="Claude billed through your AWS account, signed with SigV4.",
        fields=(
            _f("AWS_REGION", "Region", secret=False, required=False,
               help="Bedrock region with Claude model access — us-east-1 by default.",
               placeholder="us-east-1"),
            _f("AWS_ACCESS_KEY_ID", "Access key id", secret=False, required=False,
               help="Leave blank to use AWS_PROFILE or an already-configured ~/.aws.",
               placeholder="AKIA…"),
            _f("AWS_SECRET_ACCESS_KEY", "Secret access key", required=False,
               help="Required when an access key id is given."),
            _f("AWS_SESSION_TOKEN", "Session token", required=False,
               help="Only for temporary (STS/SSO) credentials."),
            _f("AWS_PROFILE", "Profile", secret=False, required=False,
               help="A profile in ~/.aws/credentials to use instead of explicit keys.",
               placeholder="default"),
        ),
        backend="bedrock:anthropic",
        docs_url="https://docs.claude.com/en/api/claude-on-amazon-bedrock",
    ),
    AuthMethod(
        id="azure", family="anthropic", label="Azure AI Foundry", kind="cloud",
        description="Claude through an Azure AI Foundry deployment's Anthropic endpoint.",
        fields=(
            _f("AZURE_ANTHROPIC_ENDPOINT", "Endpoint", secret=False,
               help="Your Foundry resource URL; mantis appends /anthropic/v1.",
               placeholder="https://my-resource.services.ai.azure.com"),
            _f("AZURE_ANTHROPIC_API_KEY", "API key",
               help="The resource key from the Foundry portal."),
        ),
        backend="{AZURE_ANTHROPIC_ENDPOINT}/anthropic/v1",
        docs_url="https://learn.microsoft.com/azure/ai-foundry/",
    ),
)

_OPENAI: tuple[AuthMethod, ...] = (
    AuthMethod(
        id="api_key", family="openai", label="API key", kind="api_key",
        description="A platform.openai.com key (sk-…), billed per token.",
        fields=(_f("OPENAI_API_KEY", "API key",
                   help="From platform.openai.com → API keys.", placeholder="sk-…"),),
        backend="https://api.openai.com/v1",
        docs_url="https://platform.openai.com/api-keys",
        recommended=True,
    ),
    AuthMethod(
        id="chatgpt", family="openai", label="ChatGPT subscription (Codex)", kind="oauth",
        description="Use the ChatGPT plan the Codex CLI is signed in with — no API key, "
                    "no per-token bill.",
        fields=(),
        backend="https://chatgpt.com/backend-api/codex",
        docs_url="https://developers.openai.com/codex/cli",
        extra={"login": "codex login", "detected_from": "codex"},
    ),
    AuthMethod(
        id="azure_openai", family="openai", label="Azure OpenAI", kind="cloud",
        description="GPT models through an Azure OpenAI resource (api-key header).",
        fields=(
            _f("AZURE_OPENAI_ENDPOINT", "Endpoint", secret=False,
               help="Your resource URL. mantis posts to /openai/v1 when the resource "
                    "serves the v1 surface, else /openai/deployments/{model}.",
               placeholder="https://my-resource.openai.azure.com"),
            _f("AZURE_OPENAI_API_KEY", "API key", help="Key 1 or Key 2 from the portal."),
            _f("AZURE_OPENAI_API_VERSION", "API version", secret=False, required=False,
               help="Only for the classic deployment surface; 2024-10-21 by default.",
               placeholder="2024-10-21"),
        ),
        backend="{AZURE_OPENAI_ENDPOINT}/openai/v1",
        docs_url="https://learn.microsoft.com/azure/ai-services/openai/",
        extra={"model_is_deployment": True},
    ),
)

_GEMINI: tuple[AuthMethod, ...] = (
    AuthMethod(
        id="api_key", family="gemini", label="API key", kind="api_key",
        description="A Google AI Studio key (AIza…) — free tier available.",
        fields=(_f("GEMINI_API_KEY", "API key",
                   help="From aistudio.google.com/apikey. GOOGLE_API_KEY is also read.",
                   placeholder="AIza…"),),
        backend="https://generativelanguage.googleapis.com/v1beta/openai",
        docs_url="https://aistudio.google.com/apikey",
        recommended=True,
    ),
    AuthMethod(
        id="vertex", family="gemini", label="Google Vertex AI", kind="cloud",
        description="Gemini billed through your GCP project, via Vertex's OpenAI-compatible endpoint.",
        fields=(
            _f("GOOGLE_CLOUD_PROJECT", "GCP project id", secret=False,
               help="The project with Vertex AI enabled.", placeholder="my-gcp-project"),
            _f("GOOGLE_CLOUD_REGION", "Region", secret=False, required=False,
               help="us-central1 by default.", placeholder="us-central1"),
            _f("GOOGLE_APPLICATION_CREDENTIALS", "Service-account key path",
               secret=False, required=False,
               help="Optional: path to a service-account JSON key. Leave blank to use "
                    "`gcloud auth application-default login`.",
               placeholder="/path/to/key.json"),
        ),
        backend="vertex:gemini",
        docs_url="https://cloud.google.com/vertex-ai/generative-ai/docs/multimodal/call-vertex-using-openai-library",
    ),
)

_XAI: tuple[AuthMethod, ...] = (
    AuthMethod(
        id="api_key", family="xai", label="API key", kind="api_key",
        description="An xAI console key (xai-…).",
        fields=(_f("XAI_API_KEY", "API key",
                   help="From console.x.ai → API Keys. GROK_API_KEY is also read.",
                   placeholder="xai-…"),),
        backend="https://api.x.ai/v1",
        docs_url="https://console.x.ai/",
        recommended=True,
    ),
)

_STATIC_METHODS: dict[str, tuple[AuthMethod, ...]] = {
    "anthropic": _ANTHROPIC,
    "openai": _OPENAI,
    "gemini": _GEMINI,
    "xai": _XAI,
}

#: Methods that never become active on env-detection alone — see
#: :func:`method_status`.
_EXPLICIT_ONLY = frozenset({"vertex", "bedrock"})

#: Catalog providers that are their own family above — excluded from the
#: generated ``oss`` list so a key is never offered in two places.
_OWN_FAMILY = {"openai", "anthropic", "gemini", "xai"}


def _oss_methods() -> tuple[AuthMethod, ...]:
    """Local, self-hosted, and every hosted open-weight provider.

    The hosted entries are generated from ``catalog.CATALOG`` so this list and
    the model picker can never disagree about which providers exist or which
    env var each one reads.
    """

    from .catalog import CATALOG, key_hint  # noqa: PLC0415

    out: list[AuthMethod] = [
        AuthMethod(
            id="ollama", family="oss", label="Ollama (local)", kind="local",
            description="Open-weight models on this machine. No key, no account.",
            backend="http://localhost:11434",
            docs_url="https://ollama.com/download",
            recommended=True,
        ),
        AuthMethod(
            id="selfhost", family="oss", label="Self-hosted endpoint", kind="url",
            description="Any OpenAI-compatible server you run (vLLM, llama.cpp, TGI).",
            fields=(
                _f("MANTIS_AGENT_BASE_URL", "Base URL", secret=False,
                   help="Include the /v1 suffix your server publishes.",
                   placeholder="http://gpu-box:8000/v1"),
                _f("MANTIS_AGENT_API_KEY", "API key", required=False,
                   help="Only if your server requires one."),
            ),
            backend="{MANTIS_AGENT_BASE_URL}",
            docs_url="https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html",
        ),
    ]
    for prov in CATALOG:
        if prov.id in _OWN_FAMILY or not prov.api_key_env:
            continue
        aliases = ", ".join(prov.key_env_aliases)
        out.append(AuthMethod(
            id=prov.id, family="oss", label=prov.label, kind="api_key",
            description=prov.note or f"{prov.label} hosted models.",
            fields=(_f(prov.api_key_env, "API key",
                       help=key_hint(prov.id)
                            + (f" · {aliases} also read" if aliases else "")),),
            backend=prov.base_url,
            docs_url="https://" + (key_hint(prov.id).split("get one at ")[-1] or ""),
            extra={"provider_id": prov.id, "key_env_aliases": list(prov.key_env_aliases)},
        ))
    return tuple(out)


def _method(family: str, method_id: str) -> AuthMethod:
    for m in auth_methods(family):
        if m.id == method_id:
            return m
    raise ValueError(
        f"unknown auth method {method_id!r} for {family!r}; expected one of "
        + ", ".join(m.id for m in auth_methods(family))
    )


def family_of_model(model: str) -> str:
    """Which family serves ``model`` — the routing side of the same table."""

    bare = (model or "").strip().lower().rsplit("/", 1)[-1]
    if bare.startswith("claude"):
        return "anthropic"
    if bare.startswith("gemini"):
        return "gemini"
    if bare.startswith("grok"):
        return "xai"
    if bare.startswith("gpt-oss"):
        return "oss"
    if bare.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return "oss"


# ---------------------------------------------------------------------------
# Status — what is configured, offline
# ---------------------------------------------------------------------------


def _env_value(field_env: str, aliases: tuple[str, ...] = ()) -> tuple[str, str] | None:
    """``(value, source)`` for one field, or None. ``saved`` covers a key the
    user entered in mantis (kept in the key store), ``env`` a shell export."""

    for name in (field_env, *aliases):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, "env"
    return None


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "…" + value[-2:]
    return f"{value[:6]}…{value[-4:]}"


def _method_configured(m: AuthMethod) -> tuple[bool, str | None, str, dict[str, str]]:
    """``(configured, source, hint, masked)`` for one method, without network
    (the Ollama probe is a loopback connect, not a request)."""

    masked: dict[str, str] = {}
    if m.id == "oauth":
        hit = _env_value("ANTHROPIC_AUTH_TOKEN")
        if hit:
            masked["ANTHROPIC_AUTH_TOKEN"] = _mask(hit[0])
            return True, "saved", "signed in with a Claude subscription", masked
        from .cli_logins import detect_claude_code  # noqa: PLC0415

        if detect_claude_code()["signed_in"]:
            return (False, None, "Claude Code is signed in on this machine — run "
                    "`mantis-agent auth login claude` to use the same subscription here", masked)
        return False, None, "run `mantis-agent auth login claude` to sign in", masked

    if m.id == "chatgpt":
        from .cli_logins import detect_codex, has_chatgpt_login  # noqa: PLC0415

        codex = detect_codex()
        if has_chatgpt_login():
            plan = f" ({codex['plan']} plan)" if codex.get("plan") else ""
            return True, "cli", f"signed in to ChatGPT via the Codex CLI{plan}", masked
        if codex["installed"]:
            return False, None, "Codex is installed but not signed in with ChatGPT — `codex login`", masked
        return False, None, "install the Codex CLI and run `codex login`", masked

    if m.id == "ollama":
        if _port_open("127.0.0.1", 11434):
            return True, "cli", "ollama is running on :11434", masked
        import shutil  # noqa: PLC0415

        if shutil.which("ollama"):
            return False, None, "ollama is installed but not running — `ollama serve`", masked
        return False, None, "install from ollama.com/download", masked

    if m.id == "bedrock":
        from .providers.cloud_credentials import aws_credentials, aws_region  # noqa: PLC0415

        creds = aws_credentials()
        if creds is not None:
            masked["AWS_ACCESS_KEY_ID"] = _mask(creds.access_key_id)
            return (True, "env" if creds.source == "env" else "cli",
                    f"{creds.source} credentials, region {aws_region()}", masked)
        return False, None, "export AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or set AWS_PROFILE", masked

    if m.id == "vertex":
        from .providers.cloud_credentials import (  # noqa: PLC0415
            google_project,
            google_token_source,
        )

        source = google_token_source()
        project = google_project()
        if source and project:
            masked["GOOGLE_CLOUD_PROJECT"] = project
            return (True, "env" if source == "env" else "cli",
                    f"{source} credentials, project {project}", masked)
        missing = []
        if not project:
            missing.append("GOOGLE_CLOUD_PROJECT")
        if not source:
            missing.append("Google credentials (`gcloud auth application-default login`)")
        return False, None, "needs " + " and ".join(missing), masked

    # Field-driven methods (api key, azure, selfhost).
    aliases = tuple(m.extra.get("key_env_aliases") or ())
    source: str | None = None
    ok = True
    for f in m.fields:
        hit = _env_value(f.env, aliases if f.env.endswith("API_KEY") else ())
        value = hit[0] if hit else ""
        if not value and m.kind == "api_key":
            value = _saved_key_for(m) or ""
            if value:
                source = source or "saved"
        if value:
            masked[f.env] = _mask(value) if f.secret else value
            source = source or (hit[1] if hit else "saved")
        elif f.required:
            ok = False
    if not ok and m.family == "openai" and m.id == "api_key":
        # A platform key `codex login --with-api-key` stored is a plain OpenAI
        # key; reuse it rather than asking for the same key twice.
        from .cli_logins import codex_api_key  # noqa: PLC0415

        key = codex_api_key()
        if key:
            masked["OPENAI_API_KEY"] = _mask(key)
            return True, "cli", "from the Codex CLI (~/.codex/auth.json)", masked
    if ok and m.fields:
        return True, source or "env", "ready", masked
    missing = [f.env for f in m.fields if f.required and f.env not in masked]
    return False, None, ("set " + ", ".join(missing) if missing else "not configured"), masked


def _saved_key_for(m: AuthMethod) -> str | None:
    """The key mantis itself stored for this method's provider, if any."""

    provider_id = m.extra.get("provider_id") or (
        m.family if m.family in ("anthropic", "openai", "gemini") else None
    )
    if m.family == "xai":
        provider_id = "xai"
    if not provider_id:
        return None
    try:
        from .catalog import saved_key  # noqa: PLC0415

        return saved_key(str(provider_id))
    except Exception:  # noqa: BLE001 — a broken store must not break a status read
        return None


def _port_open(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def configured_method(family: str) -> str | None:
    """Which method is live right now for this family, by precedence
    (explicit selection saved in settings > env vars > CLI logins such as
    gcloud/aws), or None."""
    status = method_status(family)
    for method_id, info in status.items():
        if info["active"]:
            return method_id
    return None


def method_status(family: str) -> dict[str, dict[str, Any]]:
    """``{method_id: {"configured": bool, "source": "env"|"saved"|"cli"|None,
    "active": bool, "hint": str, "masked": {env: "sk-…1234"}}}`` — no network."""
    methods = auth_methods(family)
    try:
        from .catalog import saved_auth_method  # noqa: PLC0415

        chosen = saved_auth_method(family)
    except Exception:  # noqa: BLE001
        chosen = None

    out: dict[str, dict[str, Any]] = {}
    for m in methods:
        configured, source, hint, masked = _method_configured(m)
        out[m.id] = {
            "configured": configured,
            "source": "saved" if (chosen == m.id and source is None and configured) else source,
            "active": False,
            "hint": hint,
            "masked": masked,
            "label": m.label,
            "kind": m.kind,
            "backend": m.backend,
        }
    # Active = the saved selection when it is usable, else the first configured
    # method in table order (which is recommended-first). A saved selection that
    # is no longer configured must NOT win: the credential is gone, and routing
    # to it would fail every request instead of falling back to one that works.
    #
    # Vertex and Bedrock are excluded from that fallback (``_EXPLICIT_ONLY``):
    # a working gcloud login or ~/.aws profile says nothing about wanting Claude
    # billed through that cloud — most machines that have them have them for
    # unrelated reasons — and silently routing there turns "no API key" into a
    # confusing per-region model-access error. They still show as configured,
    # and one `auth use` makes them active.
    active: str | None = None
    if chosen in out and out[chosen]["configured"]:
        active = chosen
    else:
        for m in methods:
            if out[m.id]["configured"] and m.id not in _EXPLICIT_ONLY:
                active = m.id
                break
    if active is not None:
        out[active]["active"] = True
    return out


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def resolved_backend(family: str, method_id: str) -> str | None:
    """The concrete backend for a method — templates filled from the env.

    ``"{AZURE_OPENAI_ENDPOINT}/openai/v1"`` is not a backend until the endpoint
    is known, and handing routing an unsubstituted template is how a request
    ends up at a literal brace-URL.
    """

    m = _method(family, method_id)
    backend = m.backend
    if not backend or "{" not in backend:
        return backend
    out = backend
    for f in m.fields:
        value = (os.environ.get(f.env) or "").strip().rstrip("/")
        if not value:
            return None
        out = out.replace("{" + f.env + "}", value)
    return None if "{" in out else out


def active_backend(family: str) -> str | None:
    """The backend implied by the family's active method, or None."""

    method_id = configured_method(family)
    return resolved_backend(family, method_id) if method_id else None


def set_method(family: str, method_id: str, values: dict[str, str]) -> dict[str, Any]:
    """Persist ``values`` (keyed by AuthField.env) into the user settings env,
    export them into ``os.environ``, and record ``method_id`` as the family's
    active method so routing picks its backend. Returns
    ``{"ok", "message", "backend", "family", "method"}``."""
    m = _method(family, method_id)
    known = {f.env for f in m.fields}
    unknown = sorted(set(values) - known)
    if unknown:
        return {
            "ok": False, "family": family, "method": method_id, "backend": None,
            "message": (
                f"{', '.join(unknown)} is not a field of {family}/{method_id}; "
                f"expected {', '.join(sorted(known)) or 'no fields'}"
            ),
        }

    env: dict[str, str] = {}
    for f in m.fields:
        value = (values.get(f.env) or "").strip()
        if value:
            env[f.env] = value

    # An API key also goes to the key store, which is what the model picker,
    # the dashboard and `validate_provider` read. Writing it in only one of the
    # two places is how a key "saved successfully" and then read as missing.
    key_field = next((f for f in m.fields if f.env.endswith("API_KEY")), None)
    provider_id = m.extra.get("provider_id") or (
        family if family in ("anthropic", "openai", "gemini", "xai") else None
    )
    if m.kind == "api_key" and key_field and provider_id and env.get(key_field.env):
        try:
            from .catalog import BY_ID, set_key  # noqa: PLC0415

            if str(provider_id) in BY_ID:
                set_key(str(provider_id), env[key_field.env])
        except Exception:  # noqa: BLE001 — settings persistence below still applies
            pass

    for k, v in env.items():
        os.environ[k] = v
    if env:
        try:
            from .settings import update_setting_source  # noqa: PLC0415

            update_setting_source("user", {"env": env})
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False, "family": family, "method": method_id, "backend": None,
                "message": f"could not write user settings: {exc}",
            }

    missing = [
        f.env for f in m.fields
        if f.required and not (os.environ.get(f.env) or "").strip()
        and not (m.kind == "api_key" and _saved_key_for(m))
    ]
    if missing and m.kind not in ("oauth", "local", "cloud"):
        return {
            "ok": False, "family": family, "method": method_id,
            "backend": None,
            "message": f"{family}/{method_id} still needs {', '.join(missing)}",
        }

    try:
        from .catalog import set_auth_method  # noqa: PLC0415

        set_auth_method(family, method_id)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False, "family": family, "method": method_id, "backend": None,
            "message": f"could not record the active method: {exc}",
        }

    backend = resolved_backend(family, method_id)
    configured, _source, hint, _masked = _method_configured(m)
    message = f"{m.label} is now the active {family} method"
    if not configured:
        message += f" — {hint}"
    return {
        "ok": True, "family": family, "method": method_id, "backend": backend,
        "message": message,
    }


def clear_method(family: str, method_id: str) -> dict[str, Any]:
    """Forget the saved values for one method (never touches shell env)."""
    m = _method(family, method_id)
    cleared: list[str] = []

    provider_id = m.extra.get("provider_id") or (
        family if family in ("anthropic", "openai", "gemini", "xai") else None
    )
    if m.kind == "api_key" and provider_id:
        try:
            from .catalog import BY_ID, clear_key  # noqa: PLC0415

            if str(provider_id) in BY_ID and clear_key(str(provider_id)):
                cleared.append("saved key")
        except Exception:  # noqa: BLE001
            pass

    envs = [f.env for f in m.fields]
    if m.id == "oauth":
        envs = ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_REFRESH_TOKEN",
                "ANTHROPIC_AUTH_EXPIRES_AT"]
    if envs:
        try:
            from .settings import load_setting_source, update_setting_source  # noqa: PLC0415

            saved_env = (load_setting_source("user") or {}).get("env") or {}
            hit = [e for e in envs if saved_env.get(e)]
            # Empty strings rather than deletion: the settings writer merges, so
            # a removed key is inherited again from the previous file.
            update_setting_source("user", {"env": {e: "" for e in envs}})
            if hit:
                cleared.append(", ".join(hit))
            for e in envs:
                # os.environ is cleared too so the running process stops using
                # the credential now rather than at the next launch. A value the
                # user exported in their shell reappears next launch — which is
                # their standing choice, and this call cannot change it.
                os.environ.pop(e, None)
        except Exception:  # noqa: BLE001
            pass

    active_cleared = False
    try:
        from .catalog import clear_auth_method, saved_auth_method  # noqa: PLC0415

        if saved_auth_method(family) == method_id:
            active_cleared = clear_auth_method(family)
    except Exception:  # noqa: BLE001
        pass

    message = (
        f"cleared {family}/{method_id}: " + "; ".join(cleared)
        if cleared else f"nothing saved for {family}/{method_id}"
    )
    return {
        "ok": True, "family": family, "method": method_id,
        "backend": None, "message": message, "was_active": active_cleared,
        "cleared": cleared,
    }


# ---------------------------------------------------------------------------
# Network probe
# ---------------------------------------------------------------------------


def _explain(status: int, family: str, method_id: str, body: str) -> str:
    """Turn an HTTP failure into the sentence that names the actual fix."""

    snippet = (body or "").strip().replace("\n", " ")[:200]
    if status in (401, 403):
        if method_id == "bedrock":
            return (f"{status}: AWS rejected the request — check the IAM principal "
                    f"can call bedrock:InvokeModel in this region. {snippet}")
        if method_id == "vertex":
            return (f"{status}: Google rejected the credential — check the project has "
                    f"Vertex AI enabled and the account has aiplatform.user. {snippet}")
        if method_id == "oauth":
            return (f"{status}: the subscription token was rejected — run "
                    f"`mantis-agent auth login claude` again. {snippet}")
        return f"{status}: the credential was rejected — check the key is current. {snippet}"
    if status == 404:
        if method_id in ("vertex", "bedrock"):
            return (f"404: no such model in this region — Claude access is granted "
                    f"per-region, and the model id differs per cloud. {snippet}")
        return f"404: endpoint not found — check the base URL. {snippet}"
    if status == 429:
        return f"429: rate limited (the credential is valid). {snippet}"
    return f"HTTP {status}: {snippet}"


async def validate_method(family: str, method_id: str, *, model: str | None = None) -> dict[str, Any]:
    """Network probe of the configured method: list models or send a
    one-token request. ``{"ok", "message", "models": [...], "latency_ms"}``.
    Errors are explained, never raised."""
    import httpx  # noqa: PLC0415

    try:
        m = _method(family, method_id)
    except ValueError as exc:
        return {"ok": False, "message": str(exc), "models": [], "latency_ms": 0}

    started = time.monotonic()

    def done(ok: bool, message: str, models: list[str] | None = None) -> dict[str, Any]:
        return {
            "ok": ok, "message": message, "models": models or [],
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    try:
        plan = _probe_plan(m, model)
    except Exception as exc:  # noqa: BLE001 — a missing credential is an answer
        return done(False, str(exc))
    if plan is None:
        configured, _s, hint, _m = _method_configured(m)
        return done(False, hint if not configured else "no probe available for this method")

    method, url, headers, body, extract = plan
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.request(method, url, headers=headers, content=body)
    except httpx.HTTPError as exc:
        return done(False, f"could not reach {url.split('/')[2]}: {exc}")
    if r.status_code >= 400:
        return done(False, _explain(r.status_code, family, method_id,
                                    r.text if hasattr(r, "text") else ""))
    try:
        models = extract(r.json())
    except Exception:  # noqa: BLE001 — a 200 with an odd body is still a pass
        models = []
    label = f"{len(models)} models available" if models else "credential accepted"
    return done(True, label, models)


def _probe_plan(m: AuthMethod, model: str | None) -> tuple[str, str, dict[str, str], bytes | None, Any] | None:
    """``(method, url, headers, body, extract)`` for the cheapest call that
    proves the credential works — a model list where the route has one, a
    one-token completion where it does not."""

    import json as _json  # noqa: PLC0415

    ids = lambda d: [str(x.get("id") or x.get("name") or "") for x in (d.get("data") or []) if x]  # noqa: E731

    if m.family == "anthropic":
        if m.id in ("api_key", "oauth"):
            from .providers.anthropic_passthrough import ANTHROPIC_DEFAULT_BASE_URL  # noqa: PLC0415

            token = (os.environ.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
            key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip() or (
                _saved_key_for(m) or "")
            headers = {"anthropic-version": "2023-06-01"}
            if m.id == "oauth":
                if not token:
                    raise RuntimeError("no subscription token — run `auth login claude`")
                headers["authorization"] = f"Bearer {token}"
                headers["anthropic-beta"] = "oauth-2025-04-20"
            else:
                if not key:
                    raise RuntimeError("no ANTHROPIC_API_KEY set")
                headers["x-api-key"] = key
            return "GET", f"{ANTHROPIC_DEFAULT_BASE_URL}/models", headers, None, ids

        if m.id == "azure":
            endpoint = (os.environ.get("AZURE_ANTHROPIC_ENDPOINT") or "").strip().rstrip("/")
            key = (os.environ.get("AZURE_ANTHROPIC_API_KEY") or "").strip()
            if not endpoint or not key:
                raise RuntimeError("set AZURE_ANTHROPIC_ENDPOINT and AZURE_ANTHROPIC_API_KEY")
            return ("GET", f"{endpoint}/anthropic/v1/models",
                    {"api-key": key, "anthropic-version": "2023-06-01"}, None, ids)

        if m.id == "vertex":
            from .providers.anthropic_vertex import vertex_region, vertex_url  # noqa: PLC0415
            from .providers.cloud_credentials import (  # noqa: PLC0415
                google_access_token,
                google_project,
            )

            project = google_project()
            token = google_access_token()
            if not project or not token:
                raise RuntimeError(
                    "Vertex needs GOOGLE_CLOUD_PROJECT and Google credentials "
                    "(`gcloud auth application-default login`)"
                )
            region = vertex_region()
            url = vertex_url(project=project, region=region,
                             model=model or "claude-sonnet-4-5", stream=False)
            body = _json.dumps({
                "anthropic_version": "vertex-2023-10-16",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            }).encode()
            return ("POST", url,
                    {"authorization": f"Bearer {token}", "content-type": "application/json"},
                    body, lambda _d: [])

        if m.id == "bedrock":
            from .anthropic_auth import sigv4_headers  # noqa: PLC0415
            from .providers.cloud_credentials import (  # noqa: PLC0415
                aws_credentials,
                aws_region,
            )

            creds = aws_credentials()
            if creds is None:
                raise RuntimeError(
                    "Bedrock needs AWS credentials — export AWS_ACCESS_KEY_ID / "
                    "AWS_SECRET_ACCESS_KEY, or set AWS_PROFILE"
                )
            region = aws_region()
            # The control plane lists what this account can actually invoke, so
            # it proves the credential AND surfaces the per-region model access
            # that is the usual reason a valid key still cannot call Claude —
            # and unlike an invoke, it costs nothing.
            url = f"https://bedrock.{region}.amazonaws.com/foundation-models"
            headers = sigv4_headers(
                method="GET", url=url, body=b"", region=region, service="bedrock",
                access_key_id=creds.access_key_id,
                secret_access_key=creds.secret_access_key,
                session_token=creds.session_token,
            )
            return ("GET", url, headers, None,
                    lambda d: [str(x.get("modelId") or "")
                               for x in (d.get("modelSummaries") or [])
                               if str(x.get("modelId") or "").startswith("anthropic.")])

    if m.family == "openai" and m.id == "chatgpt":
        from .cli_logins import (  # noqa: PLC0415
            CHATGPT_CODEX_URL,
            chatgpt_access_token,
            chatgpt_headers,
            codex_client_version,
        )

        headers = {"authorization": f"Bearer {chatgpt_access_token()}", **chatgpt_headers()}
        return ("GET", f"{CHATGPT_CODEX_URL}/models?client_version={codex_client_version()}",
                headers, None,
                lambda d: [str(x.get("slug") or "") for x in (d.get("models") or [])
                           if x.get("visibility") != "hide"])

    if m.family in ("openai", "gemini", "xai", "oss"):
        base, headers = _openai_compat_probe_target(m)
        if base is None:
            raise RuntimeError(_method_configured(m)[2])
        if m.id == "ollama":
            return "GET", f"{base}/api/tags",  headers, None, (
                lambda d: [str(x.get("name") or "") for x in (d.get("models") or [])])
        return "GET", f"{base.rstrip('/')}/models", headers, None, ids

    return None


def _openai_compat_probe_target(m: AuthMethod) -> tuple[str | None, dict[str, str]]:
    """``(base_url, headers)`` for the OpenAI-compatible families."""

    if m.id == "ollama":
        return "http://localhost:11434", {}
    if m.id == "selfhost":
        base = (os.environ.get("MANTIS_AGENT_BASE_URL") or "").strip().rstrip("/")
        if not base:
            return None, {}
        key = (os.environ.get("MANTIS_AGENT_API_KEY") or "").strip()
        return base, ({"authorization": f"Bearer {key}"} if key else {})
    if m.id == "azure_openai":
        endpoint = (os.environ.get("AZURE_OPENAI_ENDPOINT") or "").strip().rstrip("/")
        key = (os.environ.get("AZURE_OPENAI_API_KEY") or "").strip()
        if not endpoint or not key:
            return None, {}
        return f"{endpoint}/openai/v1", {"api-key": key}
    if m.id == "vertex" and m.family == "gemini":
        from .providers.cloud_credentials import (  # noqa: PLC0415
            google_access_token,
            google_project,
        )

        project = google_project()
        token = google_access_token()
        if not project or not token:
            return None, {}
        region = (os.environ.get("GOOGLE_CLOUD_REGION") or "us-central1").strip()
        return (
            f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}"
            f"/locations/{region}/endpoints/openapi",
            {"authorization": f"Bearer {token}"},
        )

    # Plain API-key methods: the first field is the key, the backend the base.
    key = ""
    aliases = tuple(m.extra.get("key_env_aliases") or ())
    for f in m.fields:
        hit = _env_value(f.env, aliases)
        if hit:
            key = hit[0]
            break
    if not key:
        key = _saved_key_for(m) or ""
    if not key and m.family == "openai" and m.id == "api_key":
        from .cli_logins import codex_api_key  # noqa: PLC0415

        key = codex_api_key() or ""
    if not key or not m.backend:
        return None, {}
    return m.backend, {"authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# OAuth (Claude subscription)
# ---------------------------------------------------------------------------


def _pending_store() -> dict[str, Any]:
    from .catalog import _load_store  # noqa: PLC0415

    return _load_store().get("oauth_pending") or {}


def oauth_start(family: str) -> dict[str, Any]:
    """Begin a browser login (Anthropic subscription today). Returns
    ``{"url", "handle", "instructions"}``; the caller opens ``url`` and later
    passes the pasted code or callback URL to :func:`oauth_finish`."""
    if family != "anthropic":
        raise ValueError(f"{family} has no browser login; use an API key")
    from .anthropic_oauth import build_authorize_url, make_pkce, make_state  # noqa: PLC0415
    from .catalog import _load_store, _save_store  # noqa: PLC0415

    verifier, challenge = make_pkce()
    state = make_state()
    url = build_authorize_url(code_challenge=challenge, state=state)
    # Persisted, not just in memory: the dashboard finishes the login in a
    # different process from the one that started it.
    data = _load_store()
    pending = data.setdefault("oauth_pending", {})
    pending[state] = {"verifier": verifier, "family": family, "ts": time.time()}
    # Keep the store small — an abandoned login should not accumulate forever.
    for handle, rec in list(pending.items()):
        if time.time() - float(rec.get("ts") or 0) > 3600:
            del pending[handle]
    _save_store(data)
    return {
        "url": url,
        "handle": state,
        "instructions": (
            "Open the URL, approve the login, then copy the code shown on the "
            "callback page (it looks like <code>#<state>) and pass it back."
        ),
    }


def oauth_finish(handle: str, code_or_url: str) -> dict[str, Any]:
    """Exchange the code, persist the token, make ``oauth`` the active
    method. Returns the same shape as :func:`set_method`."""
    from .anthropic_auth import Credential, persist_credential  # noqa: PLC0415
    from .anthropic_oauth import exchange_code  # noqa: PLC0415
    from .catalog import _load_store, _save_store, set_auth_method  # noqa: PLC0415

    pending = _pending_store()
    record = pending.get(handle)
    if record is None:
        return {"ok": False, "family": "anthropic", "method": "oauth", "backend": None,
                "message": "that login has expired — start it again"}

    code = (code_or_url or "").strip()
    if code.startswith("http://") or code.startswith("https://"):
        # The user pasted the whole callback URL: pull ?code= (and #state) out.
        from urllib.parse import parse_qs, urlsplit  # noqa: PLC0415

        parts = urlsplit(code)
        query = parse_qs(parts.query)
        code = (query.get("code") or [""])[0] or parts.fragment
    if not code:
        return {"ok": False, "family": "anthropic", "method": "oauth", "backend": None,
                "message": "no authorization code found in what was pasted"}

    try:
        tokens = exchange_code(code, code_verifier=str(record["verifier"]), state=handle)
    except Exception as exc:  # noqa: BLE001 — an expired/mistyped code is normal
        return {"ok": False, "family": "anthropic", "method": "oauth", "backend": None,
                "message": f"the code was not accepted: {exc}"}

    access = str(tokens.get("access_token") or "")
    if not access:
        return {"ok": False, "family": "anthropic", "method": "oauth", "backend": None,
                "message": "the exchange returned no access token"}
    extra: dict[str, Any] = {}
    if tokens.get("refresh_token"):
        extra["refresh_token"] = str(tokens["refresh_token"])
    persist_credential(Credential("oauth", access, extra))
    expires_in = tokens.get("expires_in")
    if expires_in:
        try:
            stamp = str(int(time.time()) + int(expires_in))
            os.environ["ANTHROPIC_AUTH_EXPIRES_AT"] = stamp
            from .settings import update_setting_source  # noqa: PLC0415

            update_setting_source("user", {"env": {"ANTHROPIC_AUTH_EXPIRES_AT": stamp}})
        except Exception:  # noqa: BLE001 — the token still works without it
            pass

    data = _load_store()
    (data.get("oauth_pending") or {}).pop(handle, None)
    _save_store(data)
    try:
        set_auth_method("anthropic", "oauth")
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True, "family": "anthropic", "method": "oauth", "backend": "anthropic",
        "message": "signed in with your Claude subscription",
    }
