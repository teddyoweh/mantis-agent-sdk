"""The CI review core: a finding is structured, sanitized, and fingerprinted.

Three properties are load-bearing and each has its own block below.

**Fingerprints** decide whether a re-run posts a second copy of yesterday's
comment. They must survive everything that moves code without changing it —
rebases, reindentation, an unrelated edit twelve lines up — and must change
the moment the finding itself does. Duplicate-comment spam is the single most
common reason a team turns a review bot off, so these are the tests that keep
the feature usable at all.

**Sanitization** decides whether a review comment can be weaponized. The PR
is written by an attacker; a finding quoting it and rendering ``<img
src=https://evil/x.png>`` is an IP-logging beacon posted under the
repository's own name. Every field is checked, and the ``Finding`` struct
refuses to exist in an unsanitized state so no future caller can skip it.

**Ranking and capping** decide whether the comment is read. Fifteen findings
ranked by severity get read; ninety do not — and a cap that silently drops the
rest teaches reviewers the bot is lying to them, so what was capped is always
reported.
"""

from __future__ import annotations

import ast
import pathlib
import re

import msgspec
import pytest

from mantis_agent.ci.findings import (
    KNOWN_CATEGORIES,
    MAX_BODY,
    MAX_FAILURE_SCENARIO,
    MAX_FINDINGS,
    MAX_PER_FILE,
    MAX_TITLE,
    SEVERITIES,
    CapReport,
    Finding,
    FindingsSchemaError,
    cap_findings,
    dedupe_findings,
    fence_for,
    finding_from_dict,
    make_finding,
    rank_findings,
    sanitize_code,
    sanitize_text,
)
from mantis_agent.ci.fingerprint import (
    FINGERPRINT_VERSION,
    MARKER_RE,
    extract_marker,
    finding_fingerprint,
    marker,
    normalize_context,
    normalize_path,
    normalize_title,
)

# A realistic snippet: the code a "missing timeout" finding would point at.
# Deliberately as long as the identity window, so the "an edit below the
# finding does not re-key it" test is testing the cap and not an accident.
CONTEXT = """\
def fetch(url):
    resp = session.get(url)
    if resp.status_code != 200:
        raise FetchError(resp.text)
    return resp.json()
"""

# A rendered field must contain no live link of any kind: no scheme, no bare
# www host, no markdown link syntax, no HTML. Substring checks are not enough —
# a *defanged* URL still contains "://" inside its brackets, which is the point.
LIVE_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")


def _assert_inert(value: str) -> None:
    assert "<" not in value and ">" not in value, "raw HTML survived"
    assert LIVE_SCHEME_RE.search(value) is None, "a live URL survived"
    assert "](" not in value, "markdown link syntax survived"
    assert "www." not in value, "a bare www host survived"


def _finding(**over: object) -> Finding:
    """A valid finding; keyword overrides for whatever the test is about."""
    kw: dict = dict(
        file="mantis_agent/http.py",
        line=42,
        severity="high",
        category="correctness",
        title="HTTP request has no timeout",
        body="`session.get` is called without `timeout=`, so the call blocks forever.",
        failure_scenario=(
            "A server that accepts the connection and never responds makes "
            "fetch() hang; the CLI never returns and no error is raised."
        ),
        code_context=CONTEXT,
    )
    kw.update(over)
    return make_finding(**kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fingerprints: stable across movement
# ---------------------------------------------------------------------------


def test_fingerprint_survives_a_line_shift() -> None:
    """THE DEFECT this whole module exists to prevent: someone adds an import
    at the top of the file, every line number shifts by one, and the bot posts
    a second copy of all fifteen comments."""
    before = _finding(line=42)
    after = _finding(line=57, end_line=60)
    assert before.id == after.id


def test_fingerprint_survives_reindentation_and_trailing_whitespace() -> None:
    """A reformatter moved the block into a new ``with`` and stripped trailing
    blanks. Nothing about the finding changed."""
    reindented = "\n".join(
        "    " + line.rstrip() + "   " for line in CONTEXT.strip("\n").split("\n")
    )
    assert _finding().id == _finding(code_context=reindented).id


def test_fingerprint_survives_tabs_becoming_spaces() -> None:
    tabbed = CONTEXT.replace("    ", "\t")
    assert _finding().id == _finding(code_context=tabbed).id


def test_fingerprint_survives_blank_lines_inside_the_context() -> None:
    spaced = CONTEXT.replace("\n", "\n\n")
    assert _finding().id == _finding(code_context=spaced).id


def test_fingerprint_survives_an_unrelated_edit_below_the_finding() -> None:
    """A rebase pulled in a change further down the hunk. The context window is
    bounded precisely so distant edits cannot re-key the comment."""
    grown = CONTEXT + "\n".join(f"    log.debug({i})" for i in range(20))
    assert _finding().id == _finding(code_context=grown).id


def test_fingerprint_survives_path_spelling() -> None:
    assert _finding().id == _finding(file="./mantis_agent/http.py").id
    assert _finding().id == _finding(file="mantis_agent\\http.py").id
    assert _finding().id == _finding(file="/mantis_agent//http.py").id


def test_fingerprint_survives_title_case_and_punctuation_drift() -> None:
    """The model rewords its own title cosmetically between runs. That is not
    a new finding."""
    assert _finding().id == _finding(title="HTTP request has no timeout.").id
    assert _finding().id == _finding(title="http  request has no TIMEOUT").id


def test_fingerprint_ignores_the_line_number_entirely() -> None:
    """Stated directly against the primitive, not only through Finding."""
    fp = finding_fingerprint(
        file="a.py", code_context=CONTEXT, category="security", title="t"
    )
    assert fp == finding_fingerprint(
        file="a.py", code_context=CONTEXT, category="security", title="t"
    )
    assert len(fp) == 16 and fp == fp.lower()
    int(fp, 16)  # hex, so it is safe inside an HTML comment marker


# ---------------------------------------------------------------------------
# Fingerprints: unstable when the finding is actually different
# ---------------------------------------------------------------------------


def test_fingerprint_changes_when_the_code_changes() -> None:
    fixed = CONTEXT.replace("session.get(url)", "session.get(url, timeout=5)")
    assert _finding().id != _finding(code_context=fixed).id


def test_fingerprint_changes_with_file_category_or_title() -> None:
    base = _finding().id
    assert base != _finding(file="mantis_agent/serve.py").id
    assert base != _finding(category="security").id
    assert base != _finding(title="HTTP request has no retry").id


def test_fingerprint_fields_cannot_bleed_into_each_other() -> None:
    """Concatenation-hashing would make ("ab", "c") and ("a", "bc") collide.
    Fields are length-framed, so they do not."""
    a = finding_fingerprint(file="ab", code_context="", category="docs", title="c")
    b = finding_fingerprint(file="a", code_context="", category="docs", title="bc")
    assert a != b


def test_normalizers_are_idempotent() -> None:
    for fn, value in (
        (normalize_path, "./a\\b//c.py"),
        (normalize_context, CONTEXT),
        (normalize_title, "  HTTP   Request!! "),
    ):
        once = fn(value)
        assert fn(once) == once


# ---------------------------------------------------------------------------
# The comment marker publication matches on
# ---------------------------------------------------------------------------


def test_marker_round_trips_out_of_a_comment_body() -> None:
    fp = _finding().id
    body = f"**HTTP request has no timeout**\n\nsome prose\n{marker(fp)}"
    assert extract_marker(body) == fp
    # The version is stamped so a future algorithm change migrates old comments
    # instead of double-posting under a new id space.
    assert f"v{FINGERPRINT_VERSION}:" in marker(fp)
    assert MARKER_RE.search(marker(fp)) is not None


def test_marker_is_not_confused_by_other_html_comments() -> None:
    assert extract_marker("<!-- generated by something else -->") is None
    assert extract_marker("no marker here") is None


# ---------------------------------------------------------------------------
# Schema: failure_scenario is the quality lever, so it is required
# ---------------------------------------------------------------------------


def test_failure_scenario_is_a_required_argument() -> None:
    with pytest.raises(TypeError):
        make_finding(  # type: ignore[call-arg]
            file="a.py",
            line=1,
            severity="low",
            category="docs",
            title="t",
            body="b",
            code_context=CONTEXT,
        )


def test_failure_scenario_may_not_be_blank() -> None:
    """A reviewer who cannot state concrete inputs producing a concrete wrong
    output is reporting a style preference, not a bug."""
    with pytest.raises(FindingsSchemaError):
        _finding(failure_scenario="   ")


def test_a_finding_cannot_be_built_without_its_other_substance() -> None:
    for blank in ("file", "title", "body"):
        with pytest.raises(FindingsSchemaError):
            _finding(**{blank: ""})


def test_severity_and_category_are_closed_sets() -> None:
    with pytest.raises(FindingsSchemaError):
        _finding(severity="blocker")
    with pytest.raises(FindingsSchemaError):
        _finding(category="vibes")
    assert set(SEVERITIES) == {"critical", "high", "medium", "low", "nit"}
    assert "correctness" in KNOWN_CATEGORIES and "security" in KNOWN_CATEGORIES


def test_numeric_fields_are_validated() -> None:
    with pytest.raises(FindingsSchemaError):
        _finding(line=-1)
    with pytest.raises(FindingsSchemaError):
        _finding(confidence=1.5)
    with pytest.raises(FindingsSchemaError):
        _finding(line=10, end_line=4)


def test_long_fields_are_capped_with_a_visible_mark() -> None:
    long = _finding(title="T" * 400, body="B" * 4000, failure_scenario="F" * 4000)
    assert len(long.title) <= MAX_TITLE
    assert len(long.body) <= MAX_BODY
    assert len(long.failure_scenario) <= MAX_FAILURE_SCENARIO
    assert long.body.endswith("[…]")


def test_finding_from_dict_validates_model_output() -> None:
    raw = {
        "file": "a.py",
        "line": 3,
        "severity": "medium",
        "category": "tests",
        "title": "t",
        "body": "b",
        "failure_scenario": "f",
    }
    got = finding_from_dict(raw, code_context=CONTEXT)
    assert got.id and got.category == "tests"
    with pytest.raises(FindingsSchemaError):
        finding_from_dict({k: v for k, v in raw.items() if k != "title"},
                          code_context=CONTEXT)
    with pytest.raises(FindingsSchemaError):
        finding_from_dict(dict(raw, exfiltrate="please"), code_context=CONTEXT)
    with pytest.raises(FindingsSchemaError):
        finding_from_dict("not a mapping", code_context=CONTEXT)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Sanitization: a comment must not be a beacon
# ---------------------------------------------------------------------------


BEACONS = (
    '<img src="https://evil.example/pixel.png">',
    "<details><summary>x</summary><img src=https://evil.example/p.png></details>",
    '<a href="https://evil.example">click</a>',
    "![](https://evil.example/pixel.png)",
    "![alt text](https://evil.example/pixel.png)",
    "[click me](https://evil.example/steal)",
    "[![pixel](https://evil.example/p.png)](https://evil.example/click)",
    "![x][ref]\n\n[ref]: https://evil.example/p.png",
    "<https://evil.example/autolink>",
    "https://evil.example/bare",
    "www.evil.example/bare",
    "reply to attacker@evil.example",
    "<script>fetch('https://evil.example')</script>",
)


@pytest.mark.parametrize("hostile", BEACONS)
def test_no_field_can_render_html_an_image_or_an_autolink(hostile: str) -> None:
    got = _finding(body=f"looks fine {hostile} looks fine")
    _assert_inert(got.body)
    assert "@evil" not in got.body, "a live mailto autolink survived"
    # Evidence survives — a reviewer can still see what the PR tried, and the
    # defanged spelling is the standard one, so it reads as deliberate.
    assert "evil.example" in got.body
    assert "looks fine" in got.body


def test_hostile_content_is_neutralized_in_every_field() -> None:
    hostile = '<img src="https://evil.example/p.png">'
    got = _finding(title=hostile, body=hostile, failure_scenario=hostile)
    for value in (got.title, got.body, got.failure_scenario):
        _assert_inert(value)


def test_control_characters_bidi_and_zero_width_are_stripped() -> None:
    got = _finding(body="a\x1b[31mred\x1b[0m b‮reversed‬ c​d\re")
    assert "\x1b" not in got.body
    assert "‮" not in got.body and "​" not in got.body
    assert "\r" not in got.body
    assert "red" in got.body and "reversed" in got.body


def test_title_is_forced_onto_one_line() -> None:
    got = _finding(title="first line\nsecond line")
    assert "\n" not in got.title and "second line" in got.title


def test_sanitization_is_idempotent() -> None:
    """It runs on ingest and again at render time; a second pass must be a
    no-op or the text degrades every time it is touched."""
    for hostile in BEACONS + ("plain `code` & <text>", "x" * (MAX_BODY + 50)):
        once = sanitize_text(hostile, max_chars=MAX_BODY)
        assert sanitize_text(once, max_chars=MAX_BODY) == once


def test_finding_refuses_to_exist_in_an_unsanitized_state() -> None:
    """The struct is the last line of defence: a future caller that builds one
    by hand, or a decoded JSON payload, cannot smuggle live markup in."""
    ok = _finding()
    with pytest.raises(FindingsSchemaError):
        msgspec.structs.replace(ok, body='<img src="https://evil.example/p.png">')
    with pytest.raises((FindingsSchemaError, msgspec.ValidationError)):
        msgspec.json.decode(
            msgspec.json.encode(ok).replace(b'"body":"', b'"body":"<img> '),
            type=Finding,
        )


def test_a_clean_finding_survives_verbatim() -> None:
    """The defense must cost less than the attack: ordinary prose is untouched
    so reviewers do not learn to distrust the rendering."""
    body = "`fetch()` blocks forever - see the retry loop in http.py (line 88)."
    assert _finding(body=body).body == body


def test_suggestions_keep_their_code_but_lose_their_trojans() -> None:
    code = "if resp.status_code != 200:\n\traise FetchError(resp.text)\n"
    got = _finding(suggestion=code + "‮# owned")
    assert "\t" in got.suggestion, "indentation is meaningful in a patch"
    assert "‮" not in got.suggestion
    # A URL inside code is left alone: it renders inside a fence, and defanging
    # it would corrupt the patch that gets applied.
    kept = sanitize_code('URL = "https://api.example/v1"', max_chars=200)
    assert "https://api.example/v1" in kept


def test_fence_for_outgrows_backticks_in_the_content() -> None:
    assert fence_for("plain") == "```"
    assert fence_for("a ``` b") == "````"
    assert len(fence_for("a ````` b")) == 6


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_ranking_is_severity_then_confidence_then_verification() -> None:
    low = _finding(severity="low", title="a", confidence=0.9)
    high_sure = _finding(severity="high", title="b", confidence=0.9, verified_by=2)
    high_unsure = _finding(severity="high", title="c", confidence=0.2, verified_by=2)
    high_tied = _finding(severity="high", title="d", confidence=0.9, verified_by=1)
    crit = _finding(severity="critical", title="e")

    ranked = rank_findings([low, high_unsure, high_tied, crit, high_sure])
    assert [f.title for f in ranked] == ["e", "b", "d", "c", "a"]


def test_ranking_is_a_total_order_so_re_runs_do_not_reshuffle() -> None:
    same = [_finding(title=f"t{i}", file=f"f{i}.py") for i in range(6)]
    assert rank_findings(same) == rank_findings(list(reversed(same)))


def test_dedupe_keeps_the_best_copy_of_a_repeated_finding() -> None:
    weak = _finding(confidence=0.2)
    strong = _finding(confidence=0.9, verified_by=2)
    assert weak.id == strong.id
    kept = dedupe_findings([weak, strong])
    assert len(kept) == 1 and kept[0].confidence == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Volume control: capped, and honest about it
# ---------------------------------------------------------------------------


def _many(n: int, *, severity: str = "medium", file: str = "a.py") -> list[Finding]:
    """``n`` distinct findings. Severity is part of the title and the context
    on purpose: identity ignores severity, so re-using them across severities
    would make these findings deduplicate into each other."""
    return [
        _finding(severity=severity, file=file,
                 title=f"{severity} finding number {i}",
                 code_context=f"{severity} line {i}")
        for i in range(n)
    ]


def test_the_global_cap_reports_what_it_dropped() -> None:
    report = cap_findings(_many(40, file="a.py"), max_per_file=99)
    assert len(report.shown) == MAX_FINDINGS
    assert report.over_max == 40 - MAX_FINDINGS
    assert len(report.dropped) == 40 - MAX_FINDINGS
    assert "25" in report.summary() and "not shown" in report.summary()


def test_the_per_file_cap_reports_per_file() -> None:
    findings = _many(9, file="a.py") + _many(2, file="b.py")
    report = cap_findings(findings)
    assert sum(1 for f in report.shown if f.file == "a.py") == MAX_PER_FILE
    assert report.over_per_file == {"a.py": 9 - MAX_PER_FILE}
    assert "a.py" in report.summary()


def test_nits_are_suppressed_by_default_and_counted() -> None:
    report = cap_findings(_many(3, severity="nit") + _many(1, severity="high"))
    assert [f.severity for f in report.shown] == ["high"]
    assert report.suppressed_nits == 3
    assert "nit" in report.summary()
    kept = cap_findings(_many(3, severity="nit"), suppress_nits=False)
    assert len(kept.shown) == 3


def test_min_severity_filters_below_the_bar_and_says_so() -> None:
    findings = _many(2, severity="low") + _many(1, severity="critical")
    report = cap_findings(findings, min_severity="high")
    assert [f.severity for f in report.shown] == ["critical"]
    assert report.below_min_severity == 2
    assert "high" in report.summary()


def test_duplicates_are_collapsed_and_counted() -> None:
    dupes = [_finding(), _finding(), _finding()]
    report = cap_findings(dupes)
    assert len(report.shown) == 1
    assert report.duplicates == 2


def test_nothing_dropped_means_nothing_claimed() -> None:
    report = cap_findings(_many(3))
    assert report.summary() == ""
    assert report.dropped == ()


def test_capping_keeps_the_most_severe() -> None:
    findings = _many(20, severity="low") + [_finding(severity="critical", title="keep")]
    report = cap_findings(findings, max_per_file=99)
    assert report.shown[0].title == "keep"


def test_the_cap_report_is_json_encodable_for_the_ci_output() -> None:
    report = cap_findings(_many(40))
    payload = msgspec.json.decode(msgspec.json.encode(report))
    assert payload["over_max"] == report.over_max
    assert msgspec.json.decode(msgspec.json.encode(report), type=CapReport).shown


# ---------------------------------------------------------------------------
# The SDK still targets 3.9
# ---------------------------------------------------------------------------


def test_ci_core_parses_as_python_39() -> None:
    root = pathlib.Path(__file__).resolve().parent.parent / "mantis_agent" / "ci"
    for path in sorted(root.glob("*.py")):
        ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 9))
