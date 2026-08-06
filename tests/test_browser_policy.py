"""The browser URL/domain gate — §9 of the browser plan.

This is the layer every navigation passes through, so the tests are written as
an attack corpus rather than as API coverage. Four properties carry the weight:

1. **Label boundaries are real.** ``github.com.evil.net`` is not GitHub, and no
   amount of case, trailing dots, ideographic full stops or IDN homographs
   makes it GitHub. This matcher is shared with sandbox egress and channel
   allowlists, so a suffix bug here is a bug in three features at once.
2. **A host is whatever the browser would dial.** ``http://2852039166/`` and
   ``http://0251.0376.0251.0376/`` are both 169.254.169.254 — a validator that
   only understands dotted quads waves the metadata service straight through.
3. **Metadata is unconditional.** ``allowPrivateNetwork`` defaults to true so
   localhost development works; it must not become an IMDS credential leak.
4. **Every hop is its own decision.** Approving a redirect to ``b.com`` never
   authorizes the final ``c.com``.
"""

from __future__ import annotations

import ipaddress

import pytest

from mantis_agent.browser import (
    ADDRESS_LINK_LOCAL,
    ADDRESS_LOOPBACK,
    ADDRESS_METADATA,
    ADDRESS_MULTICAST,
    ADDRESS_NAME,
    ADDRESS_PRIVATE,
    ADDRESS_PUBLIC,
    ADDRESS_RESERVED,
    ADDRESS_UNSPECIFIED,
    DECISION_ALLOW,
    DECISION_ASK,
    DECISION_BLOCK,
    REASON_BLOCKED_SCHEME,
    REASON_CREDENTIALS,
    REASON_DOMAIN_BLOCKED,
    REASON_DOMAIN_NOT_ALLOWED,
    REASON_INVALID_HOST,
    REASON_METADATA,
    BrowserPolicy,
    DomainPattern,
    DomainPatternError,
    classify_address,
    host_in_patterns,
    host_matches,
    normalize_host,
    origin_of,
    parse_domain_pattern,
    parse_domain_patterns,
    parse_ip_literal,
    redact_url,
)

# ---------------------------------------------------------------------------
# Host normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("github.com", "github.com"),
        ("GITHUB.COM", "github.com"),
        ("GitHub.Com.", "github.com"),
        ("github.com.", "github.com"),
        ("  github.com  ", "github.com"),
        # U+3002 IDEOGRAPHIC FULL STOP is a label separator per IDNA, which is
        # why a naive ``host.split(".")`` sees one label where DNS sees two.
        ("github。com", "github.com"),
        ("github．com", "github.com"),
        ("sub.github.com", "sub.github.com"),
        ("_dmarc.github.com", "_dmarc.github.com"),
        # Punycode passes through; unicode is encoded to the same thing.
        ("xn--mnchen-3ya.de", "xn--mnchen-3ya.de"),
        ("München.de", "xn--mnchen-3ya.de"),
    ],
)
def test_normalize_host_canonicalizes(raw, expected):
    assert normalize_host(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        None,
        ".",
        "..",
        "a..b",
        ".github.com",
        "git hub.com",
        "github.com/path",
        "github.com:8080",  # a port is not part of a host
        "user@github.com",
        "git\\hub.com",
        "gi*hub.com",
        "a" * 64 + ".com",  # label > 63 octets
        ("a" * 60 + ".") * 5 + "com",  # name > 253 octets
    ],
)
def test_normalize_host_rejects_junk(raw):
    assert normalize_host(raw) is None


def test_idn_homograph_does_not_collapse_onto_the_ascii_name():
    # U+0430 CYRILLIC SMALL LETTER A — visually identical to "a".
    homograph = normalize_host("аpple.com")
    assert homograph is not None
    assert homograph != "apple.com"
    assert homograph.startswith("xn--")
    assert not host_matches(homograph, "apple.com")
    assert not host_matches(homograph, "*.apple.com")


# ---------------------------------------------------------------------------
# IP literals, including the spellings a browser accepts and ``ipaddress`` does not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("127.0.0.1", "127.0.0.1"),
        ("169.254.169.254", "169.254.169.254"),
        # inet_aton spellings: decimal int, octal, hex, and short forms.
        ("2852039166", "169.254.169.254"),
        ("0251.0376.0251.0376", "169.254.169.254"),
        ("0xa9.0xfe.0xa9.0xfe", "169.254.169.254"),
        ("0xA9FEA9FE", "169.254.169.254"),
        ("127.1", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),
        ("[::1]", "::1"),
        # 3.9 renders this "::ffff:7f00:1" and 3.14 renders it
        # "::ffff:127.0.0.1", so compare addresses, not their spelling.
        ("[::FFFF:127.0.0.1]", "::ffff:127.0.0.1"),
        ("fe80::1%en0", "fe80::1"),
    ],
)
def test_parse_ip_literal_matches_what_a_resolver_would_dial(raw, expected):
    ip = parse_ip_literal(raw)
    assert ip is not None and ip == ipaddress.ip_address(expected)


@pytest.mark.parametrize("raw", ["example.com", "1.2.3.4.5", "256.1.1.1", "0x1.example.com", ""])
def test_parse_ip_literal_rejects_non_addresses(raw):
    assert parse_ip_literal(raw) is None


def test_normalize_host_canonicalizes_ip_literals():
    assert normalize_host("2852039166") == "169.254.169.254"
    assert normalize_host("[::1]") == "::1"


# ---------------------------------------------------------------------------
# Domain patterns and label-boundary matching
# ---------------------------------------------------------------------------


def test_bare_pattern_is_exact_and_subdomains_need_the_star():
    assert host_matches("github.com", "github.com")
    assert not host_matches("api.github.com", "github.com")
    assert host_matches("api.github.com", "*.github.com")
    # "*.x" covers x itself: a blocklist entry that missed the apex would be a
    # hole, and that failure mode is worse than the mild widening.
    assert host_matches("github.com", "*.github.com")
    assert host_matches("a.b.github.com", "*.github.com")
    # Cookie-style leading dot is accepted as a synonym of "*.".
    assert host_matches("api.github.com", ".github.com")


@pytest.mark.parametrize(
    "host",
    [
        "github.com.evil.net",
        "github.com.evil.net.",
        "GITHUB.COM.EVIL.NET",
        "evilgithub.com",
        "github.como",
        "notgithub.com",
        "github.com.br",
        "xn--github-xyz.com",
    ],
)
@pytest.mark.parametrize("pattern", ["github.com", "*.github.com", ".github.com"])
def test_label_boundary_corpus_never_matches_github(host, pattern):
    assert not host_matches(host, pattern)


def test_trailing_dot_and_case_still_match():
    assert host_matches("GITHUB.COM.", "github.com")
    assert host_matches("API.GitHub.Com.", "*.github.com")


def test_star_matches_everything_and_nothing_else_wildcards():
    assert host_matches("anything.example", "*")
    for bad in ["*github.com", "a.*.com", "**.com", "*.", "*.*", "", "   ", "://"]:
        with pytest.raises(DomainPatternError):
            parse_domain_pattern(bad)


def test_patterns_tolerate_scheme_port_and_path():
    p = parse_domain_pattern("https://GitHub.com:443/some/path")
    assert p.host == "github.com" and not p.include_subdomains
    assert parse_domain_pattern("*.corp.com/").include_subdomains


def test_ip_literal_pattern_is_exact_only():
    assert host_matches("169.254.169.254", "169.254.169.254")
    # A suffix rule must never reach an IP: "1.2.3.4".endswith(".2.3.4").
    assert not host_matches("1.2.3.4", ".2.3.4")
    assert not host_matches("1.2.3.4", "*.2.3.4")
    assert not host_matches("1.2.3.4", "2.3.4")
    with pytest.raises(DomainPatternError):
        parse_domain_pattern("*.169.254.169.254")


def test_parse_domain_patterns_reports_bad_entries_without_dropping_good_ones():
    patterns, errors = parse_domain_patterns(["corp.com", "*bad", "*.corp.com"])
    assert [p.raw for p in patterns] == ["corp.com", "*.corp.com"]
    assert len(errors) == 1 and "*bad" in errors[0]


def test_host_in_patterns_returns_the_matching_rule():
    pats, _ = parse_domain_patterns(["*.corp.com", "example.org"])
    hit = host_in_patterns("api.corp.com", pats)
    assert isinstance(hit, DomainPattern) and hit.raw == "*.corp.com"
    assert host_in_patterns("corp.com.evil.net", pats) is None


# ---------------------------------------------------------------------------
# Address classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,cls",
    [
        ("127.0.0.1", ADDRESS_LOOPBACK),
        ("127.9.9.9", ADDRESS_LOOPBACK),
        ("::1", ADDRESS_LOOPBACK),
        ("::ffff:127.0.0.1", ADDRESS_LOOPBACK),
        ("localhost", ADDRESS_LOOPBACK),
        ("db.localhost", ADDRESS_LOOPBACK),
        ("10.0.0.5", ADDRESS_PRIVATE),
        ("172.16.4.4", ADDRESS_PRIVATE),
        ("192.168.1.10", ADDRESS_PRIVATE),
        ("fd00::1", ADDRESS_PRIVATE),
        ("169.254.10.10", ADDRESS_LINK_LOCAL),
        ("fe80::1", ADDRESS_LINK_LOCAL),
        ("224.0.0.1", ADDRESS_MULTICAST),
        ("ff02::1", ADDRESS_MULTICAST),
        ("240.0.0.1", ADDRESS_RESERVED),
        ("0.0.0.0", ADDRESS_UNSPECIFIED),
        ("::", ADDRESS_UNSPECIFIED),
        ("93.184.216.34", ADDRESS_PUBLIC),
        ("2606:2800:220:1::248", ADDRESS_PUBLIC),
        ("example.com", ADDRESS_NAME),
    ],
)
def test_classify_address(value, cls):
    assert classify_address(value) == cls


@pytest.mark.parametrize(
    "value",
    [
        "169.254.169.254",
        "169.254.170.2",
        "::ffff:169.254.169.254",
        "2002:a9fe:a9fe::",  # 6to4-wrapped 169.254.169.254
        "fd00:ec2::254",
        "100.100.100.200",
        "192.0.0.192",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "2852039166",
        "0251.0376.0251.0376",
    ],
)
def test_metadata_endpoints_classify_as_metadata(value):
    assert classify_address(value) == ADDRESS_METADATA


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def test_defaults_match_the_plan():
    p = BrowserPolicy()
    assert (p.enabled, p.engine, p.headless, p.mode) == (False, "chromium", True, "ephemeral")
    assert p.allowed_domains == () and p.blocked_domains == ()
    assert p.allow_private_network is True
    assert (p.allow_file_urls, p.allow_javascript, p.allow_uploads) == (False, False, False)
    assert p.download_root == ".mantis/artifacts/browser-downloads"
    assert p.persistent_profile is None and p.record_trace == "on-failure"
    assert (p.max_pages, p.navigation_timeout_ms, p.action_timeout_ms) == (8, 30000, 10000)


def test_localhost_development_works_out_of_the_box():
    v = BrowserPolicy().check_url("http://localhost:3000/checkout")
    assert v.allowed and v.address_class == ADDRESS_LOOPBACK and v.port == 3000


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/html,<script>alert(1)</script>",
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "  javascript:alert(1)",
        "java\nscript:alert(1)",  # browsers strip \n before parsing; so do we
        "chrome://settings",
        "chrome-extension://abcdef/page.html",
        "moz-extension://abcdef/page.html",
        "about:config",
        "view-source:https://example.com",
        "blob:https://example.com/uuid",
        "ftp://example.com/x",
        "ws://example.com/socket",
        "filesystem:https://example.com/temporary/x",
    ],
)
def test_blocked_schemes(url):
    v = BrowserPolicy().check_url(url)
    assert not v.allowed and v.reason == REASON_BLOCKED_SCHEME


def test_file_urls_only_when_narrowly_enabled():
    assert not BrowserPolicy().check_url("file:///tmp/x.html").allowed
    p = BrowserPolicy(allow_file_urls=True)
    assert p.check_url("file:///tmp/x.html").allowed
    assert p.check_url("file://localhost/tmp/x.html").allowed
    # ``file://server/share`` is an SMB fetch, not a local file.
    assert not p.check_url("file://evil.example/share/x").allowed
    assert not p.check_url("file://169.254.169.254/share/x").allowed


def test_allow_javascript_is_the_evaluate_tool_not_the_javascript_scheme():
    v = BrowserPolicy(allow_javascript=True).check_url("javascript:alert(1)")
    assert not v.allowed and v.reason == REASON_BLOCKED_SCHEME


def test_embedded_credentials_rejected_and_never_echoed():
    v = BrowserPolicy().check_url("https://alice:hunter2@example.com/x?y=1")
    assert not v.allowed and v.reason == REASON_CREDENTIALS
    assert "hunter2" not in v.url and "hunter2" not in v.display_url
    assert "alice" not in v.url
    assert v.credentials_redacted


def test_embedded_credentials_redacted_when_rejection_is_disabled():
    p = BrowserPolicy(reject_embedded_credentials=False)
    v = p.check_url("https://alice:hunter2@example.com/x")
    assert v.allowed and v.credentials_redacted
    assert v.url == "https://example.com/x"
    assert "hunter2" not in v.display_url


def test_userinfo_cannot_disguise_the_real_host():
    # The classic: everything before "@" is userinfo, the host is the metadata IP.
    v = BrowserPolicy().check_url("http://www.example.com@169.254.169.254/latest/meta-data/")
    assert not v.allowed
    assert v.host == "169.254.169.254"
    assert v.reason in (REASON_CREDENTIALS, REASON_METADATA)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://[::ffff:169.254.169.254]/",
        "http://2852039166/",
        "http://0251.0376.0251.0376/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://100.100.100.200/",
    ],
)
def test_metadata_is_blocked_even_with_private_network_allowed(url):
    p = BrowserPolicy(allow_private_network=True)
    v = p.check_url(url)
    assert not v.allowed and v.reason == REASON_METADATA


def test_private_network_gate():
    allowed = BrowserPolicy(allow_private_network=True)
    denied = BrowserPolicy(allow_private_network=False)
    for url in ["http://10.0.0.5/", "http://127.0.0.1:8080/", "http://[::1]:8080/"]:
        assert allowed.check_url(url).allowed, url
        assert not denied.check_url(url).allowed, url


def test_link_local_multicast_reserved_are_always_blocked():
    p = BrowserPolicy(allow_private_network=True)
    for url in ["http://169.254.10.10/", "http://224.0.0.1/", "http://240.0.0.1/", "http://0.0.0.0/"]:
        assert not p.check_url(url).allowed, url


@pytest.mark.parametrize(
    "url",
    ["", "   ", "not a url", "https://", "http:///path", "https://example.com:99999/"],
)
def test_malformed_urls_fail_closed(url):
    assert not BrowserPolicy().check_url(url).allowed


@pytest.mark.parametrize(
    "url",
    [
        # Percent-encoded host: browsers decode "%31%32%37" to "127". We do not
        # decode, we refuse — the safe direction, and the one that keeps this
        # module from having to reimplement a URL parser.
        "http://%31%32%37.0.0.1/",
        # Tab/CR/LF are stripped before parsing, so this is one host, not two.
        "http://169.254.169\t.254/",
        "http://[::ffff:a9fe:a9fe]/",  # the other spelling of the IPv4-mapped IMDS
    ],
)
def test_encoded_and_split_host_confusion_is_refused(url):
    assert not BrowserPolicy().check_url(url).allowed


def test_invalid_host_reason():
    v = BrowserPolicy().check_url("http://a..b/")
    assert not v.allowed and v.reason == REASON_INVALID_HOST


def test_allowlist_and_blocklist_use_label_boundaries():
    p = BrowserPolicy(allowed_domains=("*.corp.com",))
    assert p.check_url("https://api.corp.com/x").allowed
    assert p.check_url("https://corp.com/x").allowed
    bad = p.check_url("https://corp.com.evil.net/x")
    assert not bad.allowed and bad.reason == REASON_DOMAIN_NOT_ALLOWED

    b = BrowserPolicy(blocked_domains=("*.evil.net",))
    hit = b.check_url("https://a.evil.net/")
    assert not hit.allowed and hit.reason == REASON_DOMAIN_BLOCKED
    assert b.check_url("https://evil.net.example.com/").allowed


def test_blocklist_wins_over_allowlist():
    p = BrowserPolicy(allowed_domains=("*.corp.com",), blocked_domains=("secret.corp.com",))
    assert not p.check_url("https://secret.corp.com/").allowed


def test_verdict_url_is_normalized_and_navigable():
    v = BrowserPolicy().check_url("HTTPS://GitHub.COM.:443/A/b?q=1#frag")
    assert v.allowed
    assert v.host == "github.com"
    assert v.url == "https://github.com/A/b?q=1#frag"
    assert v.scheme == "https"


def test_relative_urls_resolve_against_the_current_page():
    p = BrowserPolicy()
    v = p.check_url("/next", base_url="https://example.com/a/b")
    assert v.allowed and v.url == "https://example.com/next"
    assert not p.check_url("../x", base_url="file:///etc/").allowed


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redact_url_masks_credentials_and_secret_query_params():
    out = redact_url("https://alice:hunter2@example.com/p?api_key=AKIA123&q=hello&token=zz")
    assert "hunter2" not in out and "AKIA123" not in out and "zz" not in out
    assert "q=hello" in out
    assert "example.com" in out


def test_redact_url_is_total():
    assert redact_url("") == ""
    assert redact_url("not a url") == "not a url"


# ---------------------------------------------------------------------------
# Origins and transitions
# ---------------------------------------------------------------------------


def test_origin_of():
    assert origin_of("https://Example.com:443/x") == "https://example.com"
    assert origin_of("http://example.com:8080/x") == "http://example.com:8080"
    assert origin_of("garbage") == ""


def test_same_origin_actions_inherit_authorization():
    p = BrowserPolicy()
    d = p.check_transition("https://example.com/b", from_url="https://example.com/a")
    assert d.decision == DECISION_ALLOW and d.same_origin


def test_https_upgrade_is_not_a_new_origin_but_a_downgrade_is():
    p = BrowserPolicy()
    up = p.check_transition("https://example.com/a", from_url="http://example.com/a")
    assert up.decision == DECISION_ALLOW
    down = p.check_transition("http://example.com/a", from_url="https://example.com/a")
    assert down.decision == DECISION_ASK


def test_new_origin_asks_when_no_allowlist_is_configured():
    d = BrowserPolicy().check_transition("https://other.com/", from_url="https://example.com/")
    assert d.decision == DECISION_ASK and not d.same_origin


def test_initial_navigation_does_not_ask():
    d = BrowserPolicy().check_transition("https://example.com/", kind="initial")
    assert d.decision == DECISION_ALLOW


def test_redirect_to_metadata_is_blocked_not_asked():
    d = BrowserPolicy().check_transition(
        "http://169.254.169.254/latest/", from_url="https://example.com/"
    )
    assert d.decision == DECISION_BLOCK and d.reason == REASON_METADATA


def test_allowlisted_new_origin_is_allowed_without_a_prompt():
    p = BrowserPolicy(allowed_domains=("*.corp.com", "example.com"))
    d = p.check_transition("https://api.corp.com/", from_url="https://example.com/")
    assert d.decision == DECISION_ALLOW


def test_session_approval_is_per_host_and_does_not_carry_to_the_final_target():
    p = BrowserPolicy()
    ok = p.check_transition(
        "https://b.com/", from_url="https://a.com/", session_allowed=("b.com",)
    )
    assert ok.decision == DECISION_ALLOW
    onward = p.check_transition(
        "https://c.com/", from_url="https://b.com/", session_allowed=("b.com",)
    )
    assert onward.decision == DECISION_ASK


def test_check_chain_validates_every_hop_and_stops_at_the_first_refusal():
    p = BrowserPolicy(allowed_domains=("example.com", "cdn.example.com"))
    chain = [
        "https://example.com/start",
        "https://cdn.example.com/next",
        "http://169.254.169.254/latest/",
        "https://example.com/never-reached",
    ]
    decisions = p.check_chain(chain)
    assert [d.decision for d in decisions] == [DECISION_ALLOW, DECISION_ALLOW, DECISION_BLOCK]
    assert decisions[-1].reason == REASON_METADATA


def test_check_chain_is_not_fooled_by_a_friendly_first_hop():
    p = BrowserPolicy()
    decisions = p.check_chain(["https://example.com/", "https://evil.net/"])
    assert [d.decision for d in decisions] == [DECISION_ALLOW, DECISION_ASK]


# ---------------------------------------------------------------------------
# Resolved addresses (the DNS-rebinding seam)
# ---------------------------------------------------------------------------


def test_resolved_addresses_are_checked_and_fail_closed_on_any_bad_answer():
    p = BrowserPolicy(allow_private_network=False)
    ok = p.check_resolved_host("example.com", ["93.184.216.34"])
    assert ok.allowed
    # A rebinding answer set: one public address and one loopback. Any bad
    # answer poisons the set, because the browser picks, not us.
    bad = p.check_resolved_host("example.com", ["93.184.216.34", "127.0.0.1"])
    assert not bad.allowed
    assert not p.check_resolved_host("example.com", []).allowed
    assert not p.check_resolved_host("example.com", ["169.254.169.254"]).allowed


def test_resolved_addresses_respect_the_private_network_setting():
    p = BrowserPolicy(allow_private_network=True)
    assert p.check_resolved_host("dev.local", ["127.0.0.1"]).allowed
    assert not p.check_resolved_host("dev.local", ["169.254.169.254"]).allowed


# ---------------------------------------------------------------------------
# Settings parsing
# ---------------------------------------------------------------------------


def test_from_mapping_reads_the_plan_json():
    p = BrowserPolicy.from_mapping(
        {
            "enabled": True,
            "engine": "firefox",
            "headless": False,
            "mode": "persistent",
            "allowedDomains": ["*.corp.com"],
            "blockedDomains": ["evil.net"],
            "allowPrivateNetwork": False,
            "allowFileUrls": True,
            "maxPages": 3,
            "navigationTimeoutMs": 1234,
        }
    )
    assert p.enabled and p.engine == "firefox" and not p.headless
    assert p.allowed_domains == ("*.corp.com",) and p.blocked_domains == ("evil.net",)
    assert p.allow_private_network is False and p.allow_file_urls is True
    assert p.max_pages == 3 and p.navigation_timeout_ms == 1234


def test_from_mapping_survives_junk_and_clamps():
    p = BrowserPolicy.from_mapping(
        {"maxPages": -5, "navigationTimeoutMs": "nonsense", "allowedDomains": "corp.com",
         "engine": 17, "enabled": "yes"}
    )
    assert p.max_pages >= 1
    assert p.navigation_timeout_ms == BrowserPolicy().navigation_timeout_ms
    assert p.allowed_domains == ("corp.com",)  # a bare string is one domain
    assert p.engine == "chromium"
    assert p.enabled is True
    assert BrowserPolicy.from_mapping(None) == BrowserPolicy()


def test_domain_problems_surfaces_unusable_patterns():
    p = BrowserPolicy(allowed_domains=("corp.com", "*bad"), blocked_domains=("a.*.com",))
    problems = p.domain_problems()
    assert len(problems) == 2
    assert any("*bad" in s for s in problems) and any("a.*.com" in s for s in problems)
    # An unusable entry must not silently widen the allowlist.
    assert not p.check_url("https://anything.example/").allowed


def test_from_env_overrides(monkeypatch):
    monkeypatch.setenv("MANTIS_BROWSER", "1")
    monkeypatch.setenv("MANTIS_BROWSER_HEADLESS", "0")
    monkeypatch.setenv("MANTIS_BROWSER_ENGINE", "webkit")
    monkeypatch.setenv("MANTIS_BROWSER_ALLOWED_DOMAINS", " corp.com , *.corp.com ")
    monkeypatch.setenv("MANTIS_BROWSER_BLOCKED_DOMAINS", "evil.net")
    p = BrowserPolicy.from_env()
    assert p.enabled and not p.headless and p.engine == "webkit"
    assert p.allowed_domains == ("corp.com", "*.corp.com")
    assert p.blocked_domains == ("evil.net",)


def test_policy_is_frozen_and_hashable():
    p = BrowserPolicy()
    with pytest.raises(Exception):
        p.enabled = True  # type: ignore[misc]
    assert isinstance(hash(p), int)
