"""Native Claude OAuth login — PKCE + authorize URL + code parsing. Ported from
Claude Code's services/oauth; these lock the RFC-7636 correctness and the exact
production constants so a Pro/Max subscription login keeps working.
"""

from __future__ import annotations

import base64
import hashlib

from mantis_agent import anthropic_oauth as oa
from mantis_agent.providers.anthropic_passthrough import AnthropicPassthroughProvider


def _lower_headers(p: AnthropicPassthroughProvider) -> dict:
    return {k.lower(): v for k, v in dict(p.client.headers).items()}


def test_pkce_challenge_is_s256_of_verifier() -> None:
    verifier, challenge = oa.make_pkce()
    expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expect
    assert "=" not in challenge  # url-safe, unpadded


def test_authorize_url_has_required_oauth_params() -> None:
    url = oa.build_authorize_url(code_challenge="CH123", state="ST456")
    assert url.startswith("https://claude.com/cai/oauth/authorize?")
    for fragment in (
        f"client_id={oa.CLIENT_ID}",
        "response_type=code",
        "code_challenge=CH123",
        "code_challenge_method=S256",
        "state=ST456",
        "user%3Ainference",  # the inference scope, url-encoded
    ):
        assert fragment in url, fragment


def test_console_flag_switches_authorize_host() -> None:
    url = oa.build_authorize_url(code_challenge="c", state="s", console=True)
    assert url.startswith(oa.CONSOLE_AUTHORIZE_URL)


def test_exchange_code_splits_code_hash_state(monkeypatch) -> None:
    # The manual-redirect code arrives as "<code>#<state>"; exchange_code must
    # split it and post the bare code. We stub httpx to capture the body.
    captured = {}

    class _Resp:
        def raise_for_status(self) -> None:  # noqa: D401
            pass

        def json(self) -> dict:
            return {"access_token": "tok"}

    def _fake_post(url, json, headers, timeout):  # noqa: ANN001
        captured["body"] = json
        return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "post", _fake_post)
    out = oa.exchange_code("ABC#XYZ", code_verifier="v", state="orig")
    assert out["access_token"] == "tok"
    assert captured["body"]["code"] == "ABC"
    assert captured["body"]["state"] == "XYZ"  # state from the code wins
    assert captured["body"]["grant_type"] == "authorization_code"


def test_passthrough_messages_url_preserves_gateway_base() -> None:
    # Gateway support (Bedrock/Vertex/Azure via …/anthropic/v1) relies on the
    # /messages route resolving against the full versioned base — lock it.
    for base in ("https://api.anthropic.com/v1", "https://gw.example.com/anthropic/v1"):
        p = AnthropicPassthroughProvider(auth_token="t", base_url=base)
        url = str(p.client.build_request("POST", "/messages").url)
        assert url == f"{base}/messages", url


def test_oauth_token_sends_beta_header_on_native_only() -> None:
    native = _lower_headers(AnthropicPassthroughProvider(auth_token="t"))
    gateway = _lower_headers(
        AnthropicPassthroughProvider(auth_token="t", base_url="https://gw.example.com/anthropic/v1")
    )
    assert native.get("anthropic-beta") == "oauth-2025-04-20"
    assert gateway.get("anthropic-beta") is None
