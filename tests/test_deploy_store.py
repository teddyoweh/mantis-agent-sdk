"""deployments.json round-trip, lookup, and the credential store."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from mantis_agent.deploy import store
from mantis_agent.deploy.base import DeployError, DeployOpts, Deployment, GpuSpec


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    for v in ("RUNPOD_API_KEY", "HF_TOKEN", "DEEPINFRA_API_KEY", "BASETEN_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    yield tmp_path


def _dep(**kw) -> Deployment:
    base = dict(
        id="ep1", provider="runpod", model="Qwen/Qwen3-8B", engine="vllm", status="running",
        gpu=GpuSpec(provider_id="AMPERE_80", family="A100-80", vram_gb=80, price_per_hour=2.72),
        served_model_name="Qwen/Qwen3-8B", endpoint_url="https://api.runpod.ai/v2/ep1/openai/v1",
        created_at=datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc),
        name="mantis-qwen", opts=DeployOpts(hf_token="hf_secret", max_model_len=8192,
                                             extra={"HF_TOKEN": "hf_secret2", "force": False}),
        auth_env="RUNPOD_API_KEY",
        raw={"endpoint": {"id": "ep1", "env": {"MODEL_NAME": "Qwen/Qwen3-8B", "HF_TOKEN": "hf_secret"}}},
    )
    base.update(kw)
    return Deployment(**base)


def test_round_trip_preserves_datetimes_and_types(_home):
    dep = _dep()
    store.upsert(dep)
    assert store.store_path() == _home / "deployments.json"
    back = store.get("ep1")
    assert back is not None
    assert back.created_at == dep.created_at and back.created_at.tzinfo is not None
    assert isinstance(back.updated_at, datetime)
    assert back.gpu == dep.gpu
    assert back.opts.max_model_len == 8192
    assert back.endpoint_url == dep.endpoint_url and back.auth_env == "RUNPOD_API_KEY"
    assert back.is_live


def test_secrets_never_reach_disk(_home):
    store.upsert(_dep())
    text = (_home / "deployments.json").read_text()
    assert "hf_secret" not in text
    data = json.loads(text)
    d = data["deployments"][0]
    assert d["opts"]["hf_token"] is None
    assert d["raw"]["endpoint"]["env"]["MODEL_NAME"] == "Qwen/Qwen3-8B"  # non-secret kept
    assert d["raw"]["endpoint"]["env"]["HF_TOKEN"] != "hf_secret"
    assert oct(os.stat(_home / "deployments.json").st_mode & 0o777) == "0o600"


def test_upsert_replaces_and_list_sorts_newest_first():
    store.upsert(_dep(id="a", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    store.upsert(_dep(id="b", created_at=datetime(2026, 2, 1, tzinfo=timezone.utc)))
    store.upsert(_dep(id="a", status="failed", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    deps = store.list_deployments()
    assert [d.id for d in deps] == ["b", "a"]
    assert deps[1].status == "failed"


def test_find_by_id_name_prefix_and_scoped():
    store.upsert(_dep(id="abcdef123", name="my-qwen"))
    store.upsert(_dep(id="abcdxyz99", name="other", provider="hf"))
    assert store.find("abcdef123").id == "abcdef123"
    assert store.find("my-qwen").id == "abcdef123"
    assert store.find("abcde").id == "abcdef123"
    assert store.find("hf:abcdxyz99").provider == "hf"
    with pytest.raises(DeployError, match="matches 2"):
        store.find("abcd")
    with pytest.raises(DeployError, match="no deployment"):
        store.find("zzz")


def test_mark_deleted_hides_from_default_list():
    store.upsert(_dep(id="gone"))
    assert store.mark_deleted("gone").status == "deleted"
    assert store.list_deployments() == []
    assert store.list_deployments(include_deleted=True)[0].status == "deleted"
    assert store.remove("gone") is True and store.remove("gone") is False


def test_corrupt_store_raises_a_hinted_error(_home):
    (_home / "deployments.json").write_text("{not json")
    with pytest.raises(DeployError) as ei:
        store.load_all()
    assert ei.value.hint


def test_save_credentials_persists_to_user_settings_and_env(_home):
    saved = store.save_credentials("runpod", {"RUNPOD_API_KEY": " rp_abc \n"})
    assert saved == {"RUNPOD_API_KEY": "rp_abc"}
    assert os.environ["RUNPOD_API_KEY"] == "rp_abc"
    settings = json.loads((_home / "settings.json").read_text())
    assert settings["env"]["RUNPOD_API_KEY"] == "rp_abc"
    # HF_TOKEN is accepted for any provider (gated weights).
    store.save_credentials("runpod", {"HF_TOKEN": "hf_x"})
    with pytest.raises(DeployError, match="does not use"):
        store.save_credentials("runpod", {"OPENAI_API_KEY": "nope"})


def test_load_credentials_into_env_respects_shell(_home, monkeypatch):
    store.save_credentials("deepinfra", {"DEEPINFRA_API_KEY": "di_saved"})
    monkeypatch.delenv("DEEPINFRA_API_KEY")
    exported = store.load_credentials_into_env()
    assert exported.get("DEEPINFRA_API_KEY") == "di_saved"
    monkeypatch.setenv("DEEPINFRA_API_KEY", "di_shell")
    store.load_credentials_into_env()
    assert os.environ["DEEPINFRA_API_KEY"] == "di_shell"
    store.load_credentials_into_env(override=True)
    assert os.environ["DEEPINFRA_API_KEY"] == "di_saved"


def test_credential_env_names_cover_builtins():
    names = store.credential_env_names()
    for env in ("RUNPOD_API_KEY", "HF_TOKEN", "DEEPINFRA_API_KEY", "BASETEN_API_KEY"):
        assert env in names
    assert store.credential_env_names("baseten") == ["BASETEN_API_KEY"]
