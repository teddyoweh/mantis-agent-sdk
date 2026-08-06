"""The pure half of MCP OAuth: PKCE, state, discovery, audience, credentials.

Nothing here opens a socket, a browser, or a subprocess — the modules under
test are deliberately network-free so every security property (challenge
derivation, constant-time state comparison, issuer/audience binding, file
modes) is provable in-process. The flow that stitches them together lives in
the wiring step and is tested separately.

Credential tests run against a temp ``MANTIS_MCP_CREDENTIALS_DIR`` so nothing
touches the real ``~/.mantis-agent``.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time

import pytest

from mantis_agent.mcp.credentials import (
    CREDENTIAL_VERSION,
    DEFAULT_REFRESH_FRACTION,
    CredentialStore,
    FileCredentialBackend,
    OAuthCredential,
    credential_key,
    credentials_dir,
)
from mantis_agent.mcp.oauth import (
    CODE_CHALLENGE_METHOD,
    MAX_VERIFIER_LEN,
    MIN_VERIFIER_LEN,
    AuthorizationServerMetadata,
    MCPOAuthAudienceError,
    MCPOAuthDiscoveryError,
    MCPOAuthStateMismatchError,
    PendingAuthorization,
    ProtectedResourceMetadata,
    authorization_server_metadata_urls,
    build_authorization_url,
    build_loopback_redirect_uri,
    build_refresh_body,
    build_token_exchange_body,
    canonical_resource,
    code_challenge_for,
    make_pkce,
    make_state,
    parse_authorization_server_metadata,
    parse_protected_resource_metadata,
    parse_token_response,
    protected_resource_metadata_url,
    redirect_uri_matches,
    resources_match,
    states_equal,
    token_audiences,
    validate_code_verifier,
    validate_https_url,
    validate_token_audience,
)

RESOURCE = "https://mcp.example.com/mcp"
ISSUER = "https://auth.example.com"


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def test_pkce_rfc7636_appendix_b_vector():
    """The one published S256 vector. If this drifts, every server rejects us."""

    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge_for(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_pkce_challenge_is_unpadded_base64url():
    challenge = code_challenge_for("a" * 43)
    assert "=" not in challenge
    assert "+" not in challenge and "/" not in challenge
    # 32 raw SHA-256 bytes → 43 base64 chars without padding.
    assert len(challenge) == 43


def test_make_pkce_within_spec_bounds_and_self_consistent():
    pair = make_pkce()
    assert MIN_VERIFIER_LEN <= len(pair.verifier) <= MAX_VERIFIER_LEN
    assert pair.method == CODE_CHALLENGE_METHOD == "S256"
    assert pair.challenge == code_challenge_for(pair.verifier)
    validate_code_verifier(pair.verifier)


def test_make_pkce_is_unique_per_call():
    seen = {make_pkce().verifier for _ in range(50)}
    assert len(seen) == 50


@pytest.mark.parametrize("bad", [
    "short",                       # < 43
    "a" * 129,                     # > 128
    "a" * 42,                      # one under the floor
    "abc$" + "d" * 40,             # illegal char
    "abc def" + "g" * 40,          # space
    "",
])
def test_validate_code_verifier_rejects_out_of_spec(bad):
    with pytest.raises(ValueError):
        validate_code_verifier(bad)


def test_make_pkce_rejects_entropy_that_would_break_the_bounds():
    with pytest.raises(ValueError):
        make_pkce(nbytes=8)
    with pytest.raises(ValueError):
        make_pkce(nbytes=1024)


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def test_make_state_is_random_and_url_safe():
    states = {make_state() for _ in range(50)}
    assert len(states) == 50
    for s in states:
        assert len(s) >= 32
        assert "=" not in s and "+" not in s and "/" not in s


def test_states_equal_matches_and_rejects():
    s = make_state()
    assert states_equal(s, s) is True
    assert states_equal(s, s[:-1] + ("A" if s[-1] != "A" else "B")) is False
    assert states_equal(s, "") is False
    assert states_equal("", "") is False        # empty is never a valid state
    assert states_equal(s, None) is False       # type: ignore[arg-type]
    assert states_equal(None, s) is False       # type: ignore[arg-type]


def _pending(**kw):
    pair = make_pkce()
    base = dict(
        state=make_state(),
        verifier=pair.verifier,
        resource=RESOURCE,
        redirect_uri="http://127.0.0.1:51234/callback",
        issuer=ISSUER,
        created_at=1000.0,
    )
    base.update(kw)
    return PendingAuthorization(**base)


def test_pending_authorization_consume_returns_verifier():
    p = _pending()
    assert p.consume(p.state, now=1001.0) == p.verifier
    assert p.consumed is True


def test_pending_authorization_rejects_state_mismatch():
    p = _pending()
    with pytest.raises(MCPOAuthStateMismatchError):
        p.consume("not-the-state", now=1001.0)
    # A failed attempt must not burn the pending authorization: the real
    # callback can still arrive.
    assert p.consumed is False
    assert p.consume(p.state, now=1001.0) == p.verifier


def test_pending_authorization_rejects_replay():
    p = _pending()
    p.consume(p.state, now=1001.0)
    with pytest.raises(MCPOAuthStateMismatchError):
        p.consume(p.state, now=1002.0)


def test_pending_authorization_expires():
    p = _pending(ttl_s=300.0)
    with pytest.raises(MCPOAuthStateMismatchError):
        p.consume(p.state, now=1000.0 + 300.1)
    assert p.is_expired(now=1000.0 + 300.1)
    assert not p.is_expired(now=1000.0 + 299.0)


def test_pending_authorization_caps_guessing_attempts():
    p = _pending()
    for _ in range(5):
        with pytest.raises(MCPOAuthStateMismatchError):
            p.consume("wrong", now=1001.0)
    # Budget exhausted — even the *correct* state no longer works.
    with pytest.raises(MCPOAuthStateMismatchError):
        p.consume(p.state, now=1001.0)


# ---------------------------------------------------------------------------
# URL validation and canonicalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://auth.example.com",
    "https://auth.example.com/oauth/token",
    "http://127.0.0.1:8080/token",
    "http://localhost:8080/token",
])
def test_validate_https_url_accepts(url):
    assert validate_https_url(url, field="token_endpoint") == url


@pytest.mark.parametrize("url", [
    "http://auth.example.com/token",        # plaintext, not loopback
    "ftp://auth.example.com/token",
    "https://10.0.0.5/token",               # private
    "https://192.168.1.9/token",
    "https://172.16.4.4/token",
    "https://169.254.169.254/token",        # cloud metadata
    "https://metadata.google.internal/x",
    "https://[fd00::1]/token",              # unique-local v6
    "https://user:pw@auth.example.com/t",   # userinfo
    "https://auth.example.com/t#frag",      # fragment
    "https:///token",                       # no host
    "not a url",
    "",
])
def test_validate_https_url_rejects(url):
    with pytest.raises(MCPOAuthDiscoveryError):
        validate_https_url(url, field="token_endpoint")


def test_validate_https_url_can_forbid_loopback():
    with pytest.raises(MCPOAuthDiscoveryError):
        validate_https_url("http://127.0.0.1:9/x", field="x", allow_loopback=False)


@pytest.mark.parametrize("raw,expected", [
    ("https://MCP.Example.com/mcp", "https://mcp.example.com/mcp"),
    ("https://mcp.example.com:443/mcp", "https://mcp.example.com/mcp"),
    ("https://mcp.example.com/mcp/", "https://mcp.example.com/mcp"),
    ("https://mcp.example.com", "https://mcp.example.com"),
    ("https://mcp.example.com/", "https://mcp.example.com"),
    ("https://mcp.example.com/mcp#frag", "https://mcp.example.com/mcp"),
    ("http://localhost:3000/mcp", "http://localhost:3000/mcp"),
])
def test_canonical_resource(raw, expected):
    assert canonical_resource(raw) == expected


def test_resources_match_is_canonical():
    assert resources_match("https://MCP.Example.com:443/mcp/", RESOURCE)
    assert not resources_match("https://mcp.example.com/other", RESOURCE)
    assert not resources_match("https://evil.example.com/mcp", RESOURCE)


# ---------------------------------------------------------------------------
# Discovery: protected-resource metadata (RFC 9728)
# ---------------------------------------------------------------------------


def test_protected_resource_metadata_url_inserts_well_known_before_path():
    assert (protected_resource_metadata_url(RESOURCE) ==
            "https://mcp.example.com/.well-known/oauth-protected-resource/mcp")
    assert (protected_resource_metadata_url("https://mcp.example.com") ==
            "https://mcp.example.com/.well-known/oauth-protected-resource")


def test_parse_protected_resource_metadata_valid():
    meta = parse_protected_resource_metadata(
        {
            "resource": RESOURCE,
            "authorization_servers": [ISSUER],
            "scopes_supported": ["read", "write"],
            "bearer_methods_supported": ["header"],
        },
        expected_resource=RESOURCE,
    )
    assert isinstance(meta, ProtectedResourceMetadata)
    assert meta.resource == RESOURCE
    assert meta.authorization_servers == (ISSUER,)
    assert meta.scopes_supported == ("read", "write")


def test_parse_protected_resource_metadata_tolerates_canonical_difference():
    meta = parse_protected_resource_metadata(
        {"resource": "https://MCP.Example.com/mcp/", "authorization_servers": [ISSUER]},
        expected_resource=RESOURCE,
    )
    assert meta.resource == "https://MCP.Example.com/mcp/"


@pytest.mark.parametrize("doc", [
    None,
    [],
    "{}",
    {},                                                     # no resource
    {"resource": RESOURCE},                                 # no auth servers
    {"resource": RESOURCE, "authorization_servers": []},    # empty
    {"resource": RESOURCE, "authorization_servers": {}},    # wrong type
    {"resource": RESOURCE, "authorization_servers": [1]},   # non-string entry
    {"resource": "", "authorization_servers": [ISSUER]},
    {"resource": 17, "authorization_servers": [ISSUER]},
    {"resource": RESOURCE, "authorization_servers": ["http://auth.example.com"]},
    {"resource": RESOURCE, "authorization_servers": ["https://169.254.169.254"]},
])
def test_parse_protected_resource_metadata_rejects_malformed(doc):
    with pytest.raises(MCPOAuthDiscoveryError):
        parse_protected_resource_metadata(doc)


def test_parse_protected_resource_metadata_rejects_foreign_resource():
    """The document must describe the server we asked about — otherwise a
    compromised resource can hand us off to an authorization server for a
    different audience."""

    with pytest.raises(MCPOAuthDiscoveryError):
        parse_protected_resource_metadata(
            {"resource": "https://evil.example.com/mcp", "authorization_servers": [ISSUER]},
            expected_resource=RESOURCE,
        )


def test_parse_protected_resource_metadata_bounds_the_server_list():
    doc = {"resource": RESOURCE,
           "authorization_servers": [f"https://as{i}.example.com" for i in range(64)]}
    with pytest.raises(MCPOAuthDiscoveryError):
        parse_protected_resource_metadata(doc)


# ---------------------------------------------------------------------------
# Discovery: authorization-server metadata (RFC 8414)
# ---------------------------------------------------------------------------


def test_authorization_server_metadata_urls_order_and_shape():
    urls = authorization_server_metadata_urls("https://auth.example.com/tenant1")
    assert urls == (
        "https://auth.example.com/.well-known/oauth-authorization-server/tenant1",
        "https://auth.example.com/.well-known/openid-configuration/tenant1",
        "https://auth.example.com/tenant1/.well-known/openid-configuration",
    )
    assert authorization_server_metadata_urls(ISSUER) == (
        "https://auth.example.com/.well-known/oauth-authorization-server",
        "https://auth.example.com/.well-known/openid-configuration",
    )


def _as_doc(**over):
    doc = {
        "issuer": ISSUER,
        "authorization_endpoint": ISSUER + "/authorize",
        "token_endpoint": ISSUER + "/token",
        "code_challenge_methods_supported": ["S256"],
        "response_types_supported": ["code"],
        "registration_endpoint": ISSUER + "/register",
        "revocation_endpoint": ISSUER + "/revoke",
        "scopes_supported": ["read"],
    }
    doc.update(over)
    return doc


def test_parse_authorization_server_metadata_valid():
    meta = parse_authorization_server_metadata(_as_doc(), expected_issuer=ISSUER)
    assert isinstance(meta, AuthorizationServerMetadata)
    assert meta.token_endpoint == ISSUER + "/token"
    assert meta.registration_endpoint == ISSUER + "/register"
    assert meta.revocation_endpoint == ISSUER + "/revoke"
    assert meta.supports_dynamic_registration is True
    assert meta.code_challenge_methods_supported == ("S256",)


def test_parse_authorization_server_metadata_issuer_must_match_exactly():
    with pytest.raises(MCPOAuthDiscoveryError):
        parse_authorization_server_metadata(_as_doc(), expected_issuer="https://other.example.com")
    # RFC 8414 §3.3: identical, not merely equivalent.
    with pytest.raises(MCPOAuthDiscoveryError):
        parse_authorization_server_metadata(_as_doc(issuer=ISSUER + "/"), expected_issuer=ISSUER)


@pytest.mark.parametrize("over", [
    {"issuer": ""},
    {"issuer": "https://auth.example.com?x=1"},              # issuer carries a query
    {"authorization_endpoint": ""},
    {"token_endpoint": ""},
    {"token_endpoint": "http://auth.example.com/token"},     # plaintext
    {"token_endpoint": "https://127.0.0.1.evil.com/token#f"},
    {"registration_endpoint": "http://auth.example.com/r"},
    {"revocation_endpoint": "https://10.1.2.3/revoke"},
    {"code_challenge_methods_supported": ["plain"]},         # PKCE S256 mandatory
    {"code_challenge_methods_supported": []},
    {"response_types_supported": ["token"]},                 # implicit only
    {"authorization_endpoint": 5},
])
def test_parse_authorization_server_metadata_rejects(over):
    with pytest.raises(MCPOAuthDiscoveryError):
        parse_authorization_server_metadata(_as_doc(**over))


def test_parse_authorization_server_metadata_allows_absent_optional_lists():
    doc = {
        "issuer": ISSUER,
        "authorization_endpoint": ISSUER + "/authorize",
        "token_endpoint": ISSUER + "/token",
    }
    meta = parse_authorization_server_metadata(doc)
    assert meta.code_challenge_methods_supported == ()
    assert meta.supports_dynamic_registration is False
    assert meta.revocation_endpoint == ""


# ---------------------------------------------------------------------------
# Authorization request
# ---------------------------------------------------------------------------


def test_build_loopback_redirect_uri_and_exact_match():
    uri = build_loopback_redirect_uri(51234)
    assert uri == "http://127.0.0.1:51234/callback"
    assert redirect_uri_matches(uri, uri)
    assert not redirect_uri_matches(uri, "http://127.0.0.1:51234/callback/")
    assert not redirect_uri_matches(uri, "http://127.0.0.1:51235/callback")
    assert not redirect_uri_matches(uri, "http://localhost:51234/callback")


def test_build_authorization_url_carries_every_required_parameter():
    from urllib.parse import parse_qs, urlsplit

    url = build_authorization_url(
        authorization_endpoint=ISSUER + "/authorize?tenant=acme",
        client_id="client-123",
        redirect_uri="http://127.0.0.1:51234/callback",
        state="the-state",
        code_challenge="the-challenge",
        resource="https://MCP.Example.com:443/mcp/",
        scopes=("read", "write"),
    )
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    assert parts.scheme == "https" and parts.path == "/authorize"
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["client-123"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] == ["the-challenge"]
    assert q["state"] == ["the-state"]
    assert q["scope"] == ["read write"]
    assert q["resource"] == [RESOURCE]           # canonicalized
    assert q["tenant"] == ["acme"]               # endpoint's own query preserved
    assert q["redirect_uri"] == ["http://127.0.0.1:51234/callback"]


def test_build_authorization_url_requires_a_loopback_redirect():
    with pytest.raises(MCPOAuthDiscoveryError):
        build_authorization_url(
            authorization_endpoint=ISSUER + "/authorize",
            client_id="c",
            redirect_uri="https://evil.example.com/callback",
            state="s",
            code_challenge="c",
            resource=RESOURCE,
        )


def test_build_authorization_url_requires_https_endpoint():
    with pytest.raises(MCPOAuthDiscoveryError):
        build_authorization_url(
            authorization_endpoint="http://auth.example.com/authorize",
            client_id="c",
            redirect_uri="http://127.0.0.1:1/callback",
            state="s",
            code_challenge="c",
            resource=RESOURCE,
        )


def test_token_bodies():
    body = build_token_exchange_body(
        code="the-code",
        code_verifier="v" * 43,
        redirect_uri="http://127.0.0.1:51234/callback",
        client_id="client-123",
        resource="https://MCP.Example.com/mcp",
    )
    assert body == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "code_verifier": "v" * 43,
        "redirect_uri": "http://127.0.0.1:51234/callback",
        "client_id": "client-123",
        "resource": RESOURCE,
    }
    refresh = build_refresh_body(
        refresh_token="  rt  ", client_id="client-123", resource=RESOURCE, scope="read write")
    assert refresh == {
        "grant_type": "refresh_token",
        "refresh_token": "rt",
        "client_id": "client-123",
        "resource": RESOURCE,
        "scope": "read write",
    }


# ---------------------------------------------------------------------------
# Token response + audience binding
# ---------------------------------------------------------------------------


def _jwt(payload: dict) -> str:
    def seg(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{seg({'alg': 'none'})}.{seg(payload)}.signature"


def test_token_audiences_reads_string_and_list():
    assert token_audiences(_jwt({"aud": RESOURCE})) == (RESOURCE,)
    assert token_audiences(_jwt({"aud": [RESOURCE, "https://other/mcp"]})) == (
        RESOURCE, "https://other/mcp")
    assert token_audiences("opaque-token") == ()
    assert token_audiences(_jwt({"sub": "x"})) == ()
    assert token_audiences("a.b") == ()
    assert token_audiences("a.!!!.c") == ()


def test_validate_token_audience_accepts_a_match_modulo_canonicalization():
    tok = _jwt({"aud": "https://MCP.Example.com:443/mcp/"})
    assert validate_token_audience(tok, resource=RESOURCE) == (
        "https://MCP.Example.com:443/mcp/",)


def test_validate_token_audience_rejects_a_token_minted_for_another_server():
    """The replay case: server A's token presented to server B."""

    tok = _jwt({"aud": "https://a.example.com/mcp"})
    with pytest.raises(MCPOAuthAudienceError):
        validate_token_audience(tok, resource="https://b.example.com/mcp")


def test_validate_token_audience_absent_is_allowed_unless_required():
    assert validate_token_audience("opaque", resource=RESOURCE) == ()
    with pytest.raises(MCPOAuthAudienceError):
        validate_token_audience("opaque", resource=RESOURCE, require_audience=True)


def test_validate_token_audience_accepts_multi_aud_containing_the_resource():
    tok = _jwt({"aud": ["https://other.example.com/mcp", RESOURCE]})
    assert RESOURCE in validate_token_audience(tok, resource=RESOURCE)


def test_parse_token_response_valid():
    tok = parse_token_response(
        {"access_token": "at", "token_type": "Bearer", "expires_in": 3600,
         "refresh_token": "rt", "scope": "read write"},
        resource=RESOURCE,
    )
    assert tok.access_token == "at"
    assert tok.token_type == "Bearer"
    assert tok.expires_in == 3600
    assert tok.refresh_token == "rt"
    assert tok.scope == "read write"


@pytest.mark.parametrize("doc", [
    None,
    [],
    {},
    {"access_token": ""},
    {"access_token": 5},
    {"access_token": "at", "token_type": "mac"},          # only bearer is usable
    {"access_token": "at", "expires_in": -1},
    {"access_token": "at", "expires_in": "soon"},
    {"access_token": "at", "refresh_token": 9},
])
def test_parse_token_response_rejects(doc):
    with pytest.raises(MCPOAuthDiscoveryError):
        parse_token_response(doc, resource=RESOURCE)


def test_parse_token_response_enforces_audience():
    doc = {"access_token": _jwt({"aud": "https://elsewhere.example.com/mcp"})}
    with pytest.raises(MCPOAuthAudienceError):
        parse_token_response(doc, resource=RESOURCE)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tmp_credentials_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_MCP_CREDENTIALS_DIR", str(tmp_path / "creds"))
    yield


def _cred(**over):
    base = dict(
        server="github",
        issuer=ISSUER,
        resource=RESOURCE,
        access_token="at-secret",
        refresh_token="rt-secret",
        scope="read",
        client_id="client-123",
        obtained_at=1000.0,
        expires_at=1000.0 + 3600.0,
    )
    base.update(over)
    return OAuthCredential(**base)


def test_credential_key_is_stable_and_opaque():
    k1 = credential_key("github", ISSUER, RESOURCE)
    k2 = credential_key("github", ISSUER, RESOURCE)
    assert k1 == k2
    assert len(k1) == 32 and all(c in "0123456789abcdef" for c in k1)
    # Canonicalization applies, so a cosmetic URL difference is the same key —
    # a trailing slash must not strand a credential the user already has.
    assert credential_key("github", ISSUER + "/", "https://MCP.Example.com:443/mcp/") == k1


@pytest.mark.parametrize("kw", [
    {"server": "gitlab"},
    {"issuer": "https://auth.evil.com"},
    {"resource": "https://mcp.example.com/other"},
])
def test_credential_key_changes_with_every_component(kw):
    base = dict(server="github", issuer=ISSUER, resource=RESOURCE)
    base.update(kw)
    assert credential_key(**base) != credential_key("github", ISSUER, RESOURCE)


def test_credential_key_is_not_ambiguous_across_component_boundaries():
    """``("a", "b", "c")`` and ``("ab", "", "c")`` must not collide."""

    assert credential_key("a", "b", "c") != credential_key("ab", "", "c")


def test_store_round_trip():
    store = CredentialStore()
    cred = _cred()
    key = store.save(cred)
    assert key == credential_key("github", ISSUER, RESOURCE)
    loaded = store.load("github", ISSUER, RESOURCE)
    assert loaded is not None
    assert loaded.access_token == "at-secret"
    assert loaded.refresh_token == "rt-secret"
    assert loaded.client_id == "client-123"
    assert loaded.expires_at == 4600.0
    assert loaded.version == CREDENTIAL_VERSION
    assert [c.server for c in store.all()] == ["github"]


def test_store_files_are_private():
    store = CredentialStore()
    store.save(_cred())
    d = credentials_dir()
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    files = list(d.glob("*.json"))
    assert len(files) == 1
    assert stat.S_IMODE(os.stat(files[0]).st_mode) == 0o600


def test_store_miss_and_delete():
    store = CredentialStore()
    assert store.load("github", ISSUER, RESOURCE) is None
    store.save(_cred())
    assert store.delete("github", ISSUER, RESOURCE) is True
    assert store.load("github", ISSUER, RESOURCE) is None
    assert store.delete("github", ISSUER, RESOURCE) is False


def test_store_rejects_a_credential_filed_under_the_wrong_key():
    """A blob whose own identity disagrees with its filename is not a hit.

    Without this, moving one server's file over another's name would replay a
    token at a server it was never minted for — the same threat the audience
    check covers, one layer down."""

    store = CredentialStore()
    store.save(_cred())
    path = next(credentials_dir().glob("*.json"))
    blob = json.loads(path.read_text())
    blob["issuer"] = "https://auth.evil.com"
    path.write_text(json.dumps(blob))
    assert store.load("github", ISSUER, RESOURCE) is None


def test_store_ignores_corrupt_files():
    store = CredentialStore()
    store.save(_cred())
    path = next(credentials_dir().glob("*.json"))
    path.write_text("{not json")
    assert store.load("github", ISSUER, RESOURCE) is None
    assert store.all() == []


def test_store_overwrites_in_place_without_widening_permissions():
    store = CredentialStore()
    store.save(_cred())
    store.save(_cred(access_token="at2"))
    files = list(credentials_dir().glob("*.json"))
    assert len(files) == 1
    assert stat.S_IMODE(os.stat(files[0]).st_mode) == 0o600
    loaded = store.load("github", ISSUER, RESOURCE)
    assert loaded is not None and loaded.access_token == "at2"


def test_store_leaves_no_scratch_files_behind():
    store = CredentialStore()
    store.save(_cred())
    assert [p.name for p in credentials_dir().iterdir() if not p.name.endswith(".json")] == []


def test_backend_directory_is_explicit_when_asked(tmp_path):
    other = tmp_path / "elsewhere"
    store = CredentialStore(backend=FileCredentialBackend(other))
    store.save(_cred())
    assert list(other.glob("*.json"))
    assert not credentials_dir().exists()


def test_secret_values_cover_every_secret_field():
    cred = _cred(client_secret="cs-secret")
    secrets_seen = set(cred.secret_values())
    assert secrets_seen == {"at-secret", "rt-secret", "cs-secret"}
    store = CredentialStore()
    store.save(cred)
    assert set(store.secret_values()) == secrets_seen


def test_repr_never_prints_a_secret():
    """A traceback carrying locals is a 'trace' sink like any other."""

    cred = _cred(client_secret="cs-secret")
    text = repr(cred)
    assert "at-secret" not in text
    assert "rt-secret" not in text
    assert "cs-secret" not in text
    assert "github" in text
    # And the same for the in-flight authorization: verifier and state are
    # both single-use secrets.
    p = _pending()
    ptext = repr(p)
    assert p.verifier not in ptext
    assert p.state not in ptext
    assert RESOURCE in ptext


def test_redacted_view_has_no_secrets():
    text = json.dumps(_cred(client_secret="cs-secret").redacted())
    assert "at-secret" not in text
    assert "rt-secret" not in text
    assert "cs-secret" not in text
    assert "github" in text and ISSUER in text


# ---------------------------------------------------------------------------
# Proactive refresh timing
# ---------------------------------------------------------------------------


def test_refresh_due_at_eighty_percent_of_lifetime():
    cred = _cred(obtained_at=1000.0, expires_at=2000.0)
    assert DEFAULT_REFRESH_FRACTION == 0.8
    assert cred.refresh_at() == 1800.0
    assert cred.is_refresh_due(now=1799.999) is False
    assert cred.is_refresh_due(now=1800.0) is True       # boundary is inclusive
    assert cred.is_refresh_due(now=1800.001) is True
    assert cred.is_refresh_due(now=5000.0) is True


def test_refresh_fraction_is_configurable():
    cred = _cred(obtained_at=0.0, expires_at=100.0)
    assert cred.refresh_at(fraction=0.5) == 50.0
    assert cred.is_refresh_due(now=49.0, fraction=0.5) is False
    assert cred.is_refresh_due(now=50.0, fraction=0.5) is True
    for bad in (0.0, -1.0, 1.5, 2):
        with pytest.raises(ValueError):
            cred.refresh_at(fraction=bad)


def test_refresh_timing_without_an_expiry():
    cred = _cred(expires_at=None)
    assert cred.refresh_at() is None
    assert cred.is_refresh_due(now=time.time() + 10_000) is False
    assert cred.is_expired(now=time.time() + 10_000) is False


def test_expiry_boundary():
    cred = _cred(obtained_at=1000.0, expires_at=2000.0)
    assert cred.is_expired(now=1999.0) is False
    assert cred.is_expired(now=2000.0) is True
    assert cred.lifetime_s == 1000.0


def test_backwards_expiry_is_immediately_due():
    """A server that hands back an already-expired token must not read as fresh."""

    cred = _cred(obtained_at=2000.0, expires_at=1000.0)
    assert cred.is_expired(now=2000.0) is True
    assert cred.is_refresh_due(now=2000.0) is True


def test_credential_from_token_response_sets_the_clock():
    from mantis_agent.mcp.oauth import parse_token_response

    tok = parse_token_response(
        {"access_token": "at", "expires_in": 3600, "refresh_token": "rt", "scope": "read"},
        resource=RESOURCE,
    )
    cred = OAuthCredential.from_token_response(
        tok, server="github", issuer=ISSUER, resource=RESOURCE,
        client_id="client-123", now=1000.0)
    assert cred.obtained_at == 1000.0
    assert cred.expires_at == 4600.0
    assert cred.is_refresh_due(now=3880.0) is True
    assert cred.is_refresh_due(now=3879.0) is False


def test_credential_rotation_keeps_the_old_refresh_token_when_none_is_issued():
    """RFC 6749 §6: the response MAY omit a refresh token; the old one lives on."""

    from mantis_agent.mcp.oauth import parse_token_response

    old = _cred()
    tok = parse_token_response({"access_token": "at2", "expires_in": 60}, resource=RESOURCE)
    rotated = old.rotated(tok, now=5000.0)
    assert rotated.access_token == "at2"
    assert rotated.refresh_token == "rt-secret"
    assert rotated.obtained_at == 5000.0
    assert rotated.expires_at == 5060.0
    assert rotated.key == old.key

    tok2 = parse_token_response(
        {"access_token": "at3", "expires_in": 60, "refresh_token": "rt2"}, resource=RESOURCE)
    assert old.rotated(tok2, now=5000.0).refresh_token == "rt2"
