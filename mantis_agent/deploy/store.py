"""Persistence for deployments and provider credentials.

Two files, two rules:

* ``~/.mantis-agent/deployments.json`` holds every :class:`Deployment` we
  created (or adopted with ``deploy ls --refresh``). It never holds a
  secret: ``auth_env`` records the *name* of the variable that authenticates
  inference calls, and any provider ``raw`` object is passed through
  :func:`mask_secrets` before it is written.
* Credentials go where every other saved key goes — the ``env`` block of the
  user ``settings.json`` (``settings.update_setting_source``), which the
  terminal already exports into ``os.environ`` at launch and which is written
  ``0600``. :func:`load_credentials_into_env` does the same export for library
  and CLI callers so a provider adapter can simply read ``os.environ``.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..paths import get_mantis_agent_dir
from ._http import mask_secrets
from .base import DEPLOY_PROVIDERS, DeployError, DeployOpts, Deployment, GpuSpec, utcnow

__all__ = [
    "_import_builtin_providers",
    "credential_env_names",
    "deployment_from_dict",
    "deployment_to_dict",
    "find",
    "get",
    "list_deployments",
    "load_all",
    "load_credentials_into_env",
    "mark_deleted",
    "remove",
    "save_all",
    "save_credentials",
    "save_endpoint_key",
    "store_path",
    "upsert",
]

_VERSION = 1


def store_path() -> Path:
    return get_mantis_agent_dir() / "deployments.json"


# ---------------------------------------------------------------------------
# (De)serialisation
# ---------------------------------------------------------------------------


def _dt_to_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _dt_from_str(s: Any) -> datetime:
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    if isinstance(s, str) and s:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return utcnow()


def deployment_to_dict(dep: Deployment) -> dict[str, Any]:
    """JSON-ready dict. Datetimes become ISO strings, ``raw`` is redacted."""

    d = asdict(dep)
    d["created_at"] = _dt_to_str(dep.created_at)
    d["updated_at"] = _dt_to_str(dep.updated_at)
    d["raw"] = mask_secrets(dep.raw or {})
    d["opts"] = asdict(dep.opts)
    # ``hf_token`` in opts is a secret by definition: never persist it.
    d["opts"]["hf_token"] = None
    d["opts"]["extra"] = mask_secrets(dep.opts.extra or {})
    return d


def _pick(cls: Any, data: dict[str, Any]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def deployment_from_dict(data: dict[str, Any]) -> Deployment:
    gpu_raw = data.get("gpu") or {}
    gpu = GpuSpec(**_pick(GpuSpec, {"provider_id": "unknown", "family": "other", "vram_gb": 0, **gpu_raw}))
    opts = DeployOpts(**_pick(DeployOpts, data.get("opts") or {}))
    body = _pick(Deployment, data)
    body["gpu"] = gpu
    body["opts"] = opts
    body["created_at"] = _dt_from_str(data.get("created_at"))
    body["updated_at"] = _dt_from_str(data.get("updated_at"))
    body.setdefault("status", "unknown")
    body.setdefault("engine", "vllm")
    body.setdefault("served_model_name", body.get("model", ""))
    body["auth_headers"] = dict(data.get("auth_headers") or {})
    body["raw"] = dict(data.get("raw") or {})
    return Deployment(**body)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _read_file() -> dict[str, Any]:
    p = store_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": _VERSION, "deployments": []}
    except (OSError, ValueError) as e:
        raise DeployError(
            f"deployments store is unreadable: {p} ({e})",
            hint="fix or remove the file; it only caches what the providers know",
        ) from e
    if not isinstance(data, dict) or not isinstance(data.get("deployments"), list):
        return {"version": _VERSION, "deployments": []}
    return data


def _write_file(data: dict[str, Any]) -> None:
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=False, default=str)
    # Atomic replace so a crash mid-write can't leave a half-file behind.
    fd, tmp = tempfile.mkstemp(prefix=".deployments-", suffix=".json", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def load_all() -> list[Deployment]:
    out: list[Deployment] = []
    for item in _read_file()["deployments"]:
        if isinstance(item, dict) and item.get("id"):
            try:
                out.append(deployment_from_dict(item))
            except (TypeError, ValueError):
                continue
    return out


def save_all(deps: Iterable[Deployment]) -> None:
    _write_file({"version": _VERSION, "deployments": [deployment_to_dict(d) for d in deps]})


def upsert(dep: Deployment) -> Deployment:
    """Insert or replace by ``(provider, id)``; bumps ``updated_at``."""

    dep.updated_at = utcnow()
    deps = load_all()
    for i, existing in enumerate(deps):
        if existing.id == dep.id and existing.provider == dep.provider:
            deps[i] = dep
            break
    else:
        deps.append(dep)
    save_all(deps)
    return dep


def get(dep_id: str) -> Deployment | None:
    for d in load_all():
        if d.id == dep_id:
            return d
    return None


def find(ref: str) -> Deployment:
    """Resolve a user-typed reference: exact id, exact name, ``provider:id``,
    or a unique id prefix. Raises :class:`DeployError` when ambiguous/missing."""

    ref = (ref or "").strip()
    deps = load_all()
    live = [d for d in deps if d.status != "deleted"]
    for pool in (live, deps):
        exact = [d for d in pool if d.id == ref]
        if len(exact) == 1:
            return exact[0]
        if ":" in ref:
            prov, _, rest = ref.partition(":")
            scoped = [d for d in pool if d.provider == prov and d.id == rest]
            if len(scoped) == 1:
                return scoped[0]
        named = [d for d in pool if d.name and d.name == ref]
        if len(named) == 1:
            return named[0]
        if len(ref) >= 4:
            prefixed = [d for d in pool if d.id.startswith(ref)]
            if len(prefixed) == 1:
                return prefixed[0]
            if len(prefixed) > 1:
                raise DeployError(
                    f"{ref!r} matches {len(prefixed)} deployments",
                    hint="use the full id: " + ", ".join(d.id for d in prefixed[:5]),
                )
    known = ", ".join(f"{d.provider}:{d.id}" for d in live[:8]) or "none"
    raise DeployError(f"no deployment {ref!r}", hint=f"known: {known} (`mantis-agent deploy ls`)")


def mark_deleted(dep_id: str) -> Deployment | None:
    deps = load_all()
    hit: Deployment | None = None
    for d in deps:
        if d.id == dep_id:
            d.status = "deleted"
            d.updated_at = utcnow()
            hit = d
    if hit is not None:
        save_all(deps)
    return hit


def remove(dep_id: str) -> bool:
    deps = load_all()
    kept = [d for d in deps if d.id != dep_id]
    if len(kept) == len(deps):
        return False
    save_all(kept)
    return True


def list_deployments(*, include_deleted: bool = False, provider_id: str | None = None) -> list[Deployment]:
    out = load_all()
    if not include_deleted:
        out = [d for d in out if d.status != "deleted"]
    if provider_id:
        out = [d for d in out if d.provider == provider_id]
    out.sort(key=lambda d: d.created_at, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _import_builtin_providers() -> None:
    """Register the built-in adapters. ``from . import providers`` would hand
    back the *function* ``deploy.providers`` (re-exported by the package
    ``__init__``) instead of the sub-package, so go through importlib."""

    import importlib  # noqa: PLC0415

    importlib.import_module("mantis_agent.deploy.providers")


def credential_env_names(provider_id: str | None = None) -> list[str]:
    """Every env var a registered provider reads (or just one provider's)."""

    _import_builtin_providers()

    names: list[str] = []
    for pid, cls in DEPLOY_PROVIDERS.items():
        if provider_id and pid != provider_id:
            continue
        for f in getattr(cls, "credential_fields", ()):
            if f.env not in names:
                names.append(f.env)
    # The HF token is shared by every provider that pulls gated weights.
    if not provider_id and "HF_TOKEN" not in names:
        names.append("HF_TOKEN")
    return names


def save_credentials(provider_id: str, values: dict[str, str]) -> dict[str, str]:
    """Persist ``{ENV: value}`` into the user settings ``env`` block and
    export into ``os.environ``. Blank values clear the variable. Returns the
    saved (non-blank) mapping."""

    from ..settings import update_setting_source  # noqa: PLC0415

    allowed = set(credential_env_names(provider_id)) | {"HF_TOKEN"}
    saved: dict[str, str] = {}
    patch: dict[str, str] = {}
    for env, value in values.items():
        env = env.strip()
        if env not in allowed:
            raise DeployError(
                f"{provider_id} does not use {env}",
                hint="fields: " + ", ".join(credential_env_names(provider_id)),
                provider=provider_id,
            )
        v = (value or "").strip()
        patch[env] = v
        if v:
            os.environ[env] = v
            saved[env] = v
        else:
            os.environ.pop(env, None)
    if patch:
        update_setting_source("user", {"env": patch})
    return saved


#: Generated per-endpoint keys (``MANTIS_DEPLOY_<SLUG>_KEY``) are saved with the
#: provider credentials, and exported back into the environment with them.
ENDPOINT_KEY_PREFIX = "MANTIS_DEPLOY_"


def save_endpoint_key(env: str, value: str) -> None:
    """Persist a generated endpoint key into the user settings ``env`` block
    and export it, so ``connect`` / ``try`` work after a restart."""
    from ..settings import update_setting_source  # noqa: PLC0415

    env = env.strip()
    if not env.startswith(ENDPOINT_KEY_PREFIX) or not env.endswith("_KEY"):
        raise DeployError(f"{env} is not an endpoint key name")
    os.environ[env] = value
    update_setting_source("user", {"env": {env: value}})


def load_credentials_into_env(*, override: bool = False) -> dict[str, str]:
    """Export saved deploy credentials from settings into ``os.environ``.

    Only the variables a registered provider declares (plus ``HF_TOKEN``)
    are touched; a real shell export wins unless ``override``. Returns what
    was exported."""

    try:
        from ..settings import SETTING_SOURCES, load_settings_env_safe  # noqa: PLC0415

        env = load_settings_env_safe(SETTING_SOURCES) or {}
    except Exception:  # noqa: BLE001 — broken settings must not block a deploy call
        return {}
    wanted = set(credential_env_names())
    out: dict[str, str] = {}
    for k, v in env.items():
        ok = k in wanted or (k.startswith(ENDPOINT_KEY_PREFIX) and k.endswith("_KEY"))
        if ok and isinstance(v, str) and v.strip():
            if override or not (os.environ.get(k) or "").strip():
                os.environ[k] = v.strip()
                out[k] = v.strip()
    return out
