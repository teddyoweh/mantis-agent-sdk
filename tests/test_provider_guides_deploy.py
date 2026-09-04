"""Deploy-provider key guides and the inlined logo sets.

The guides are what the dashboard's Add-key panel and the docs show; the
JSON files are the marks it draws. Both are hand-maintained data, so the
tests pin the contract: every registered deploy provider has a guide, the
guide's env vars are exactly the adapter's ``credential_fields`` (a guide
that names a variable the adapter never reads is a dead end that looks
authoritative), every URL is https, and every logo entry is drawable.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mantis_agent import provider_guides
from mantis_agent.deploy.providers import baseten, deepinfra, hf_endpoints, modal_deploy, runpod, vastai

DATA = Path(provider_guides.__file__).resolve().parent / "data"

ADAPTERS = {
    "runpod": runpod.RunPodProvider,
    "hf": hf_endpoints.HFEndpointsProvider,
    "modal": modal_deploy.ModalDeployProvider,
    "deepinfra": deepinfra.DeepInfraProvider,
    "baseten": baseten.BasetenProvider,
    "vastai": vastai.VastAIProvider,
}

# Path data: commands + numbers + separators only. Anything else (quotes,
# tags, url(), script) means the JSON was edited by hand into something that
# is not a path.
_PATH_RE = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9.,\-+eE\s]+$")
_TINT_RE = re.compile(r"^(#[0-9A-Fa-f]{6}|var\(--[a-z-]+\))$")



def _is_number(v: str) -> bool:
    try:
        float(v)
    except ValueError:
        return False
    return True

def _registered_deploy_ids() -> set[str]:
    import mantis_agent.deploy.providers  # noqa: F401 - registers the built-ins
    from mantis_agent.deploy.base import DEPLOY_PROVIDERS

    assert DEPLOY_PROVIDERS, "no deploy providers registered"
    # Only the adapters that ship in the package. Other test modules register
    # throwaway providers (id "fake") at import time to drive the manager /
    # CLI / dashboard without a network; those never need a key guide.
    return {
        pid for pid, cls in DEPLOY_PROVIDERS.items()
        if cls.__module__.startswith("mantis_agent.deploy.providers")
    }


# ---------------------------------------------------------------------------
# guides
# ---------------------------------------------------------------------------


def test_every_deploy_provider_has_a_guide() -> None:
    for pid in _registered_deploy_ids():
        assert pid in provider_guides.DEPLOY_GUIDES, f"{pid} has no deploy guide"
        assert provider_guides.deploy_guide(pid) is provider_guides.DEPLOY_GUIDES[pid]
    assert provider_guides.deploy_guide("nope") is None


@pytest.mark.parametrize("pid", sorted(ADAPTERS))
def test_guide_env_vars_match_adapter_credential_fields(pid: str) -> None:
    g = provider_guides.deploy_guide(pid)
    assert g is not None
    fields = [f.env for f in ADAPTERS[pid].credential_fields]
    assert [e["name"] for e in g["env_vars"]] == fields
    required = {f.env: f.required for f in ADAPTERS[pid].credential_fields}
    for e in g["env_vars"]:
        assert e["required"] == required[e["name"]], (pid, e["name"])
        assert e["note"]
    # the primary variable is the first required one, and every name appears
    # in the steps so a reader who only skims the list still sees the export
    assert g["env_var"] == fields[0]
    assert g["env_var"] in " ".join(g["steps"])


@pytest.mark.parametrize("pid", sorted(ADAPTERS))
def test_guide_shape(pid: str) -> None:
    g = provider_guides.deploy_guide(pid)
    assert g is not None
    for key in ("name", "intro", "steps", "free_note", "cost_note", "key_hint"):
        assert g[key], (pid, key)
    assert len(g["steps"]) >= 4
    for key in ("keys_url", "signup_url", "pricing_url", "docs_url"):
        assert g[key].startswith("https://"), (pid, key, g[key])
    # the guide's key page is the adapter's own help text, not a different console
    help_text = " ".join(f.help or "" for f in ADAPTERS[pid].credential_fields)
    host = re.sub(r"^https://(www\.)?", "", g["keys_url"]).split("/")[0]
    assert host.split(".")[-2] in help_text, (pid, host, help_text)


def test_guides_dont_mix_deploy_and_hosted_ids() -> None:
    assert not set(provider_guides.DEPLOY_GUIDES) & set(provider_guides.GUIDES)
    # hosted guides keep working exactly as before
    assert provider_guides.guide_for("openai")["env_var"] == "OPENAI_API_KEY"


# ---------------------------------------------------------------------------
# logos
# ---------------------------------------------------------------------------


def _check_logo_file(name: str, expected_ids: set[str] | None) -> dict:
    data = json.loads((DATA / name).read_text())
    assert isinstance(data, dict) and data
    if expected_ids is not None:
        assert set(data) == expected_ids
    for pid, entry in data.items():
        # A well-formed viewBox, not necessarily the 24-grid: a vendor's own
        # asset (Modal ships a 300-unit favicon) is kept at its native scale
        # rather than re-drawn by hand, and the renderer honours whatever box
        # the entry declares.
        box = entry["viewBox"].split()
        assert len(box) == 4, pid
        assert all(_is_number(v) for v in box), pid
        assert float(box[2]) > 0 and float(box[3]) > 0, pid
        assert entry["paths"] and all(isinstance(p, str) for p in entry["paths"]), pid
        for p in entry["paths"]:
            assert _PATH_RE.match(p), (name, pid, p[:40])
            assert p.lstrip()[0] in "Mm", (name, pid, "path must start with a moveto")
            assert len(p) < 4500, (name, pid, len(p))
        assert _TINT_RE.match(entry["tint"]), (name, pid, entry["tint"])
        assert entry["source"].startswith("https://"), (name, pid)
        assert entry["license"], (name, pid)
        # it must at least be well-formed SVG when wrapped the way the dashboard does
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{entry["viewBox"]}">' + "".join(
            f'<path d="{p}"/>' for p in entry["paths"]) + "</svg>"
        ET.fromstring(svg)
    return data


def test_deploy_logos_json() -> None:
    data = _check_logo_file("deploy_logos.json", set(ADAPTERS))
    for pid in data:
        assert pid in provider_guides.DEPLOY_GUIDES


def test_org_logos_json() -> None:
    data = _check_logo_file("org_logos.json", None)
    seen: dict[str, str] = {}
    for org, entry in data.items():
        assert org == org.lower(), org
        aliases = entry["aliases"]
        assert isinstance(aliases, list) and aliases, org
        # an alias resolves to exactly one org, case-insensitively
        for a in [org, *aliases]:
            key = a.lower()
            assert seen.get(key, org) == org, (a, seen.get(key), org)
            seen[key] = org
    for must in ("qwen", "openai", "meta-llama", "deepseek-ai", "zai-org", "google", "mistralai"):
        assert must in data
