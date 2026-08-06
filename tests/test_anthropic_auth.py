"""One credential in, one authenticated request out — for all four Anthropic paths.

The failure this prevents is specific and common: an OAuth token and an API key
are both opaque ``sk-ant-`` strings, and sending one the other's way produces an
authentication error that reads exactly like a bad credential. Classification by
shape is what lets a user paste whatever they have.

No real secret appears here. Every value is synthetic and shaped like the thing
it stands in for.
"""

from __future__ import annotations

import datetime as _dt
import json
from datetime import timezone

import pytest

from mantis_agent.anthropic_auth import (
    Credential,
    CredentialError,
    bedrock_headers,
    describe,
    detect_credential,
    resolve_auth,
    sigv4_headers,
)

# Synthetic, correctly-shaped, non-functional.
API_KEY = "sk-ant-api03-" + "A" * 40
OAUTH = "sk-ant-oat01-" + "B" * 60
AWS_ID = "AKIAIOSFODNN7EXAMPLE"
GCP_TOKEN = "ya29." + "c" * 40


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------


def test_api_key_and_oauth_token_are_not_confused() -> None:
    # Both are `sk-ant-`; only the middle segment differs, and getting it wrong
    # is the whole reason this module exists.
    assert detect_credential(API_KEY).mode == "api_key"
    assert detect_credential(OAUTH).mode == "oauth"


def test_aws_and_gcp_credentials_are_recognized() -> None:
    aws = detect_credential(AWS_ID)
    assert aws.mode == "bedrock"
    assert aws.extra["access_key_id"] == AWS_ID
    assert detect_credential(GCP_TOKEN).mode == "vertex"


def test_service_account_json_is_recognized_as_vertex() -> None:
    blob = json.dumps({"type": "service_account", "project_id": "p1",
                       "client_email": "sa@p1.iam.gserviceaccount.com"})
    cred = detect_credential(blob)
    assert cred.mode == "vertex"
    assert cred.extra["project_id"] == "p1"


@pytest.mark.parametrize("wrapper", [
    '{v}', '  {v}  ', '"{v}"', "'{v}'",
    'export ANTHROPIC_API_KEY={v}', 'export ANTHROPIC_API_KEY="{v}"',
    'ANTHROPIC_API_KEY={v}', 'ANTHROPIC_API_KEY: {v}',
])
def test_paste_packaging_is_stripped(wrapper) -> None:
    # People paste what is on the clipboard: a shell export line, a quoted config
    # value, something with a trailing newline. Refusing those is a papercut with
    # no security benefit — the secret is identical either way.
    assert detect_credential(wrapper.format(v=API_KEY)).mode == "api_key"


def test_an_unknown_sk_ant_shape_is_refused_rather_than_guessed() -> None:
    with pytest.raises(CredentialError, match="sk-ant-api"):
        detect_credential("sk-ant-zzz99-" + "Q" * 30)


@pytest.mark.parametrize("bad", ["", "   ", None, "hello", "Bearer abc"])
def test_junk_is_refused_with_an_actionable_message(bad) -> None:
    with pytest.raises(CredentialError):
        detect_credential(bad)


# --------------------------------------------------------------------------
# the secret must never render
# --------------------------------------------------------------------------


def test_repr_and_str_never_leak_the_secret() -> None:
    cred = detect_credential(OAUTH)
    assert OAUTH not in repr(cred)
    assert OAUTH not in str(cred)
    assert OAUTH not in f"{cred}"


def test_hint_identifies_without_disclosing() -> None:
    cred = detect_credential(OAUTH)
    assert cred.hint.startswith("sk-ant-oat")
    assert cred.hint.endswith(OAUTH[-4:])
    assert len(cred.hint) < 24
    assert OAUTH not in cred.hint


def test_describe_and_redacted_headers_never_leak() -> None:
    auth = resolve_auth(detect_credential(OAUTH))
    assert OAUTH not in describe(auth, detect_credential(OAUTH))
    red = auth.redacted()
    assert red["authorization"] == "[redacted]"
    assert OAUTH not in json.dumps(red)


# --------------------------------------------------------------------------
# resolution: the header choice is the point
# --------------------------------------------------------------------------


def test_api_key_uses_x_api_key() -> None:
    auth = resolve_auth(detect_credential(API_KEY))
    assert auth.headers["x-api-key"] == API_KEY
    assert "authorization" not in auth.headers
    assert auth.refreshable is False


def test_oauth_uses_bearer_and_the_beta_header() -> None:
    auth = resolve_auth(detect_credential(OAUTH))
    assert auth.headers["authorization"] == f"Bearer {OAUTH}"
    assert "x-api-key" not in auth.headers
    # Without the beta header the token is rejected — the non-obvious part.
    assert auth.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert auth.refreshable is True


def test_vertex_needs_a_project_and_builds_a_regional_host(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    with pytest.raises(CredentialError, match="project"):
        resolve_auth(detect_credential(GCP_TOKEN))
    auth = resolve_auth(detect_credential(GCP_TOKEN), project="p1", region="us-east5")
    assert auth.base_url == "https://us-east5-aiplatform.googleapis.com/v1"
    assert auth.headers["authorization"].startswith("Bearer ")
    assert auth.project == "p1"


def test_bedrock_needs_both_halves_and_builds_a_regional_host(monkeypatch) -> None:
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    with pytest.raises(CredentialError, match="secret access key"):
        resolve_auth(detect_credential(AWS_ID))
    cred = Credential("bedrock", "s" * 40, {"access_key_id": AWS_ID})
    auth = resolve_auth(cred, region="us-west-2")
    assert auth.base_url == "https://bedrock-runtime.us-west-2.amazonaws.com"
    # SigV4 signs the whole request, so no auth header can exist yet.
    assert "authorization" not in auth.headers


def test_unknown_mode_is_refused() -> None:
    with pytest.raises(CredentialError):
        resolve_auth(Credential("carrier-pigeon", "x"))


# --------------------------------------------------------------------------
# SigV4 — checked against a reference implementation, not by eye
# --------------------------------------------------------------------------

FIXED = _dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc)
AK = "AKIDEXAMPLE"
SK = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
URL = ("https://bedrock-runtime.us-east-1.amazonaws.com"
       "/model/anthropic.claude-3-5-sonnet-20241022-v2:0/invoke")
BODY = b'{"anthropic_version":"bedrock-2023-05-31","max_tokens":16}'


def _mine():
    return sigv4_headers(
        method="POST", url=URL, body=BODY, region="us-east-1", service="bedrock",
        access_key_id=AK, secret_access_key=SK, now=FIXED,
        extra_headers={"content-type": "application/json"},
    )


def test_sigv4_is_deterministic_for_a_fixed_clock() -> None:
    assert _mine()["authorization"] == _mine()["authorization"]


def test_sigv4_matches_botocore() -> None:
    """The algorithm is specified; the risk is transcription. So diff it."""
    botocore_auth = pytest.importorskip("botocore.auth")
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    class FakeDT(_dt.datetime):
        # botocore reads the clock through more than one entry point depending on
        # version; freeze both or the reference signs with a different date and
        # the comparison fails for a reason that has nothing to do with signing.
        @classmethod
        def utcnow(cls):
            return FIXED.replace(tzinfo=None)

        @classmethod
        def now(cls, tz=None):
            return FIXED if tz else FIXED.replace(tzinfo=None)

    original = botocore_auth.datetime.datetime
    botocore_auth.datetime.datetime = FakeDT
    try:
        mine = _mine()
        req = AWSRequest(method="POST", url=URL, data=BODY, headers={
            "content-type": "application/json",
            "x-amz-content-sha256": mine["x-amz-content-sha256"],
        })
        botocore_auth.SigV4Auth(
            Credentials(AK, SK), "bedrock", "us-east-1").add_auth(req)
        ref = req.headers["Authorization"]
    finally:
        botocore_auth.datetime.datetime = original

    assert mine["authorization"].split("Signature=")[1].strip() == \
        ref.split("Signature=")[1].strip()


def test_sigv4_signature_changes_with_the_body() -> None:
    other = sigv4_headers(
        method="POST", url=URL, body=BODY + b" ", region="us-east-1",
        service="bedrock", access_key_id=AK, secret_access_key=SK, now=FIXED,
        extra_headers={"content-type": "application/json"},
    )
    assert other["authorization"] != _mine()["authorization"]


def test_session_token_is_signed_when_present() -> None:
    h = sigv4_headers(
        method="POST", url=URL, body=BODY, region="us-east-1", service="bedrock",
        access_key_id=AK, secret_access_key=SK, session_token="tok", now=FIXED,
    )
    assert h["x-amz-security-token"] == "tok"
    assert "x-amz-security-token" in h["authorization"]  # in SignedHeaders


def test_bedrock_headers_refuses_a_non_bedrock_auth() -> None:
    auth = resolve_auth(detect_credential(API_KEY))
    with pytest.raises(CredentialError):
        bedrock_headers(auth, method="POST", url=URL, body=BODY)


# --------------------------------------------------------------------------
# persistence routing — the bug the /models prompt actually had
# --------------------------------------------------------------------------
#
# `/models` said "paste your ANTHROPIC_API_KEY" and stored whatever arrived
# through the API-key path. An OAuth token therefore landed in the x-api-key
# slot and failed validation with an error that read like a bad key.


def _isolate(monkeypatch, tmp_path):
    """Isolate the process environment — and make monkeypatch OWN each var.

    ``delenv(raising=False)`` on an already-absent variable is a no-op, so
    monkeypatch records nothing and has nothing to restore. ``persist_credential``
    then sets the variable through ``os.environ`` directly and it survives
    teardown, leaking a fake ``ANTHROPIC_API_KEY`` into every later test in the
    process — which is exactly what happened: five unrelated auth/provider tests
    failed in the full suite and passed in isolation.

    ``setenv`` first is what makes monkeypatch record the prior state (absent),
    so teardown removes whatever the code under test wrote.
    """
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_REFRESH_TOKEN", "ANTHROPIC_VERTEX_TOKEN",
                "AWS_ACCESS_KEY_ID"):
        monkeypatch.setenv(var, "__isolated__")
        monkeypatch.delenv(var, raising=False)


def test_an_api_key_and_an_oauth_token_land_in_different_slots(monkeypatch, tmp_path):
    from mantis_agent.anthropic_auth import persist_credential

    _isolate(monkeypatch, tmp_path)
    persist_credential(detect_credential(API_KEY))
    persist_credential(detect_credential(OAUTH))
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == API_KEY
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == OAUTH
    # The whole defect in one assertion.
    assert os.environ["ANTHROPIC_API_KEY"] != os.environ["ANTHROPIC_AUTH_TOKEN"]


def test_bedrock_says_what_is_still_missing(monkeypatch, tmp_path):
    from mantis_agent.anthropic_auth import persist_credential

    _isolate(monkeypatch, tmp_path)
    with pytest.raises(CredentialError, match="AWS_SECRET_ACCESS_KEY"):
        persist_credential(detect_credential(AWS_ID))
    # ...and keeps the half it was given rather than discarding it.
    import os
    assert os.environ["AWS_ACCESS_KEY_ID"] == AWS_ID


def test_service_account_json_is_directed_to_a_file(monkeypatch, tmp_path):
    from mantis_agent.anthropic_auth import persist_credential

    _isolate(monkeypatch, tmp_path)
    blob = json.dumps({"type": "service_account", "project_id": "p1",
                       "client_email": "sa@p1.iam.gserviceaccount.com"})
    with pytest.raises(CredentialError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        persist_credential(detect_credential(blob))


def test_persist_never_echoes_the_secret(monkeypatch, tmp_path):
    from mantis_agent.anthropic_auth import persist_credential

    _isolate(monkeypatch, tmp_path)
    note = persist_credential(detect_credential(OAUTH))
    assert OAUTH not in note
    assert note.startswith("OAuth token saved")


# --------------------------------------------------------------------------
# the picker's per-method guidance
# --------------------------------------------------------------------------


def test_pasteable_methods_report_nothing_missing(monkeypatch, tmp_path) -> None:
    from mantis_agent.anthropic_auth import setup_instructions

    _isolate(monkeypatch, tmp_path)
    # These are one pasted value, so the UI goes straight to an input.
    assert setup_instructions("api_key") == ""
    assert setup_instructions("oauth") == ""


def test_bedrock_guidance_narrows_as_you_configure_it(monkeypatch, tmp_path) -> None:
    from mantis_agent.anthropic_auth import setup_instructions

    _isolate(monkeypatch, tmp_path)
    for v in ("AWS_SECRET_ACCESS_KEY", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.setenv(v, "__isolated__")
        monkeypatch.delenv(v, raising=False)

    both = setup_instructions("bedrock")
    assert "AWS_ACCESS_KEY_ID" in both and "AWS_SECRET_ACCESS_KEY" in both

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", AWS_ID)
    half = setup_instructions("bedrock")
    # Telling someone to redo the half they already did is how guidance stops
    # being read.
    assert "AWS_ACCESS_KEY_ID" not in half
    assert "AWS_SECRET_ACCESS_KEY" in half

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s" * 40)
    assert "AWS_REGION" in setup_instructions("bedrock")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert setup_instructions("bedrock") == ""


def test_vertex_guidance_names_both_halves(monkeypatch, tmp_path) -> None:
    from mantis_agent import anthropic_auth as A

    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    # gcloud may genuinely be installed on the machine running the tests.
    monkeypatch.setattr(A, "_gcloud_access_token", lambda *a, **k: "")

    todo = A.setup_instructions("vertex")
    assert "ANTHROPIC_VERTEX_PROJECT_ID" in todo
    assert "gcloud" in todo or "GOOGLE_APPLICATION_CREDENTIALS" in todo

    monkeypatch.setenv("ANTHROPIC_VERTEX_TOKEN", "ya29." + "c" * 40)
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "p1")
    assert A.setup_instructions("vertex") == ""


def test_an_unknown_method_is_reported_not_silently_accepted() -> None:
    from mantis_agent.anthropic_auth import setup_instructions

    assert "unknown" in setup_instructions("carrier-pigeon")


# --------------------------------------------------------------------------
# the /models probe must understand every credential, not just x-api-key
# --------------------------------------------------------------------------
#
# Observed: "OAuth token saved · sk-ant-oat0…9AAA · validating… ✗ Claude
# (Anthropic): no API key set (not saved)". The token stored fine; the probe
# looked only in the x-api-key store, so a perfectly good OAuth session was
# reported as a missing key — and the message even claimed nothing was saved.


def test_the_models_probe_uses_bearer_for_an_oauth_session(monkeypatch, tmp_path):
    from mantis_agent import catalog

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", OAUTH)
    prov = catalog.BY_ID["anthropic"]

    headers = catalog._models_headers(prov, None)
    assert headers["authorization"] == f"Bearer {OAUTH}"
    assert "x-api-key" not in headers
    # Without the beta header the probe 401s and reads as a bad credential.
    assert headers["anthropic-beta"] == "oauth-2025-04-20"


def test_an_api_key_still_takes_the_x_api_key_path(monkeypatch, tmp_path):
    from mantis_agent import catalog

    _isolate(monkeypatch, tmp_path)
    prov = catalog.BY_ID["anthropic"]
    headers = catalog._models_headers(prov, API_KEY)
    assert headers["x-api-key"] == API_KEY
    assert "authorization" not in headers


def test_no_credential_of_any_kind_yields_no_headers(monkeypatch, tmp_path):
    from mantis_agent import catalog

    _isolate(monkeypatch, tmp_path)
    assert catalog._models_headers(catalog.BY_ID["anthropic"], None) == {}


def test_an_oauth_only_session_counts_as_enabled(monkeypatch, tmp_path):
    from mantis_agent import catalog

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", OAUTH)
    prov = catalog.BY_ID["anthropic"]
    assert catalog.is_enabled(prov) is True
    # ...with no x-api-key, which is exactly why the probe had to change.
    assert catalog.api_key_for(prov) in (None, "")
    # and the switch wires the bearer backend rather than refusing
    assert catalog.anthropic_bearer_backend(prov) == prov.base_url


def test_non_anthropic_providers_are_unaffected(monkeypatch, tmp_path):
    from mantis_agent import catalog

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", OAUTH)
    other = next(p for p in catalog.CATALOG if p.id != "anthropic"
                 and "anthropic.com" not in p.base_url)
    assert catalog._models_headers(other, "k-123") == {"Authorization": "Bearer k-123"}


# --------------------------------------------------------------------------
# changing and resetting a credential — not just first-time setup
# --------------------------------------------------------------------------
#
# The paste prompt only ever appeared for a LOCKED provider, so once a key
# worked there was no way to rotate a leaked one, swap an API key for an OAuth
# token, or revoke locally without hand-editing the key store.


def test_configured_modes_reflects_what_is_actually_set(monkeypatch, tmp_path):
    from mantis_agent.anthropic_auth import configured_modes, persist_credential

    _isolate(monkeypatch, tmp_path)
    assert configured_modes()["oauth"] is False
    persist_credential(detect_credential(OAUTH))
    assert configured_modes()["oauth"] is True
    assert configured_modes()["api_key"] is False


def test_clear_reports_only_what_was_actually_present(monkeypatch, tmp_path):
    from mantis_agent.anthropic_auth import clear_credentials, persist_credential

    _isolate(monkeypatch, tmp_path)
    # Claiming to have cleared a key the user never had reads like the reset
    # did the wrong thing.
    assert clear_credentials() == "nothing to clear"

    persist_credential(detect_credential(OAUTH))
    assert clear_credentials() == "cleared OAuth token"

    persist_credential(detect_credential(API_KEY))
    assert clear_credentials() == "cleared API key"

    persist_credential(detect_credential(API_KEY))
    persist_credential(detect_credential(OAUTH))
    assert clear_credentials() == "cleared API key and OAuth token"


def test_clear_takes_effect_in_the_running_process(monkeypatch, tmp_path):
    import os

    from mantis_agent.anthropic_auth import clear_credentials, persist_credential

    _isolate(monkeypatch, tmp_path)
    persist_credential(detect_credential(OAUTH))
    assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == OAUTH
    clear_credentials()
    # Not "at the next launch" — the session must stop using it immediately.
    assert not os.environ.get("ANTHROPIC_AUTH_TOKEN")


def test_a_new_token_replaces_the_old_one(monkeypatch, tmp_path):
    import os

    from mantis_agent.anthropic_auth import persist_credential

    _isolate(monkeypatch, tmp_path)
    first = "sk-ant-oat01-" + "C" * 60
    persist_credential(detect_credential(first))
    persist_credential(detect_credential(OAUTH))
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == OAUTH


def test_the_settings_file_holding_a_token_is_not_world_readable(monkeypatch, tmp_path):
    """A settings `env` block is where OAuth tokens live — so it is a
    credential file, and it was being written 0644."""
    import os
    import stat

    from mantis_agent.settings import save_setting_source

    _isolate(monkeypatch, tmp_path)
    p = save_setting_source("user", {"env": {"ANTHROPIC_AUTH_TOKEN": OAUTH}})
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600

    # A file that predates the fix must be repaired, not merely left alone.
    os.chmod(p, 0o644)
    save_setting_source("user", {"env": {"ANTHROPIC_AUTH_TOKEN": OAUTH}})
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
