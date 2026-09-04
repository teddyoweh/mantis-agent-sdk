"""Tests for the Skills page — the library of SKILL.md playbooks.

Covers the backend the page reads (tools, raw source, counts, the write path
for each scope) and the page itself (cards, the deterministic identity glyph,
the detail sheet, the editor's slug validation, the crafted empty state).
"""

from __future__ import annotations

import json
import re
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

SKILL = """---
name: Deploy checklist
description: How we ship a release
category: release
allowed-tools: Bash, Read, Grep
always_load: true
---

# Steps

1. run the tests
2. bump the version
"""


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "skills" / "deploy-checklist").mkdir(parents=True)
    (h / "skills" / "deploy-checklist" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(h))
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")
    proj = tmp_path / "proj"
    (proj / ".mantis" / "skills" / "review-rules").mkdir(parents=True)
    (proj / ".mantis" / "skills" / "review-rules" / "SKILL.md").write_text(
        "---\nname: Review rules\ndescription: What we look for\n---\n\nBe kind.\n", encoding="utf-8")
    monkeypatch.chdir(proj)
    from mantis_agent import serve
    return {"home": h, "proj": proj, "serve": serve}


def test_skills_state_carries_scope_tools_raw_and_counts(home):
    serve = home["serve"]
    st = serve.skills_state()
    assert st["counts"] == {"total": 2, "always": 1, "on_demand": 1, "global": 1, "project": 1}
    assert st["tools_seen"] == ["Bash", "Grep", "Read"]
    g = st["global"][0]
    assert g["name"] == "Deploy checklist" and g["scope"] == "global" and g["always_load"] is True
    assert g["tools"] == ["Bash", "Read", "Grep"] and g["category"] == "release"
    assert g["raw"].startswith("---") and "# Steps" in g["body"]
    assert g["meta"]["allowed-tools"] == "Bash, Read, Grep"
    p = st["project"][0]
    assert p["scope"] == "project" and p["always_load"] is False and p["tools"] == []


def test_underscore_spelling_of_allowed_tools_is_accepted(home, tmp_path):
    serve = home["serve"]
    d = home["home"] / "skills" / "other"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: Other\nallowed_tools: Write Edit\n---\nbody\n", encoding="utf-8")
    sk = next(x for x in serve.skills_state()["global"] if x["slug"] == "other")
    assert sk["tools"] == ["Write", "Edit"]


def test_add_skill_writes_tools_and_the_right_scope_path(home):
    serve = home["serve"]
    r = serve.add_skill("project", "New Rules!", "One line", "# Body", tools=["Bash", "Read"])
    assert r["ok"] and r["slug"] == "new-rules" and r["scope"] == "project"
    f = home["proj"] / ".mantis" / "skills" / "new-rules" / "SKILL.md"
    assert f.exists() and r["path"].endswith("new-rules/SKILL.md")
    text = f.read_text()
    assert "allowed-tools: Bash, Read" in text and "always_load" not in text
    # round-trips through the reader
    sk = next(x for x in serve.skills_state()["project"] if x["slug"] == "new-rules")
    assert sk["tools"] == ["Bash", "Read"] and sk["name"] == "New Rules!"
    # no tools → no key at all, so a round trip doesn't sprout empty fields
    serve.add_skill("global", "Bare", "d", "b")
    assert "allowed-tools" not in (home["home"] / "skills" / "bare" / "SKILL.md").read_text()
    assert serve.add_skill("global", "", "d", "b")["ok"] is False


def test_delete_skill_requires_a_real_skill(home):
    serve = home["serve"]
    assert serve.delete_skill("global", "deploy-checklist")["ok"] is True
    assert serve.skills_state()["counts"]["global"] == 0
    assert serve.delete_skill("global", "deploy-checklist")["ok"] is False
    assert serve.delete_skill("global", "../../etc")["ok"] is False


def _boot():
    from mantis_agent import serve

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    httpd.token = "tok"
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_skills_over_http_and_the_page(home):
    httpd, base = _boot()
    try:
        with urllib.request.urlopen(base + "/api/skills", timeout=5) as r:
            st = json.loads(r.read())
        assert st["counts"]["total"] == 2
        req = urllib.request.Request(base + "/api/skill", method="POST",
                                     data=json.dumps({"scope": "global", "name": "Via HTTP", "description": "d",
                                                      "body": "b", "tools": ["Bash"]}).encode(),
                                     headers={"Content-Type": "application/json", "X-Mantis-Token": "tok"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read())["slug"] == "via-http"
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            page = r.read().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()
    for marker in ("skillGlyph", "hashStr", "openSkillSheet", "openSkillEditor", "SKILL_FILTERS", "skillMatches",
                   "sk-grid", "skcard", "sglyph", "sk-split", "sk-prev", "slugify", "SKILL_TOOLS",
                   "Always loaded", "On demand", "This project", "Allowed tools", "New skill",
                   'pageHead(pad, "Skills"'):
        assert marker in page, marker
    js = page.split("<script>")[1]
    # the editor validates the slug and shows the path before writing
    ed = js[js.index("function openSkillEditor("):js.index("function fieldWrap(")]
    assert "has no slug" in ed and "SKILL.md" in ed and "scopeSel.value" in ed
    # the card grid is responsive and the preview stacks on narrow screens
    assert "repeat(auto-fill, minmax(280px, 1fr))" in page
    assert ".sk-split, .sk-frow { grid-template-columns: 1fr; }" in page


def test_identity_glyph_is_deterministic_and_self_contained():
    """Same name → same glyph, everywhere, with nothing fetched."""
    import subprocess

    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    hs = js[js.index("function hashStr("):js.index("function skillMatches(")]
    prog = hs.replace("const w = el(\"span\",\"sglyph\");", "const w = { style: {}, innerHTML: \"\" };") \
              .replace("return w;", "return w.innerHTML;")
    prog += """
const a = skillGlyph("Deploy checklist"), b = skillGlyph("Deploy checklist"), c = skillGlyph("Review rules");
if (a !== b) { console.log("NOT-DETERMINISTIC"); process.exit(1); }
if (a === c) { console.log("NOT-DISTINCT"); process.exit(1); }
for (const bad of ["http://", "https://", "<image", "xlink", "url("]) {
  if (a.includes(bad)) { console.log("EXTERNAL:" + bad); process.exit(1); }
}
if (!a.includes("var(--accent)")) { console.log("NO-ACCENT"); process.exit(1); }
// symmetric: every left cell has its mirror
const xs = [...a.matchAll(/x="(\\d+)"/g)].map(m => +m[1]);
if (!xs.every(x => xs.includes(20 - x))) { console.log("NOT-SYMMETRIC"); process.exit(1); }
console.log("OK " + a.length);
"""
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    assert r.stdout.startswith("OK"), (r.stdout, r.stderr[:400])


def test_skills_empty_state_is_the_crafted_one(home, tmp_path, monkeypatch):
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    assert 'emptyState("skill", "No skills yet"' in js
    art = re.search(r"const ART = \{(.*?)\n\};", js, re.S).group(1)
    skill_art = art[art.index("skill:"):art.index("// a search")]
    assert "<svg viewBox=" in skill_art and "var(--accent)" in skill_art and "currentColor" in skill_art
    for bad in ("http://", "https://", "<image", "xlink"):
        assert bad not in skill_art, bad
