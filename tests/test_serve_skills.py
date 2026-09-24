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
    # The Notion-style workspace (requested redesign): a page list beside the
    # open page, edited in place, autosaved — no modal form, no Save button.
    for marker in ("skillGlyph", "hashStr", "skillMatches", "slugify", "SKILL_TOOLS",
                   "paintSkillList", "paintSkillDoc", "skillProps", "splitBlocks", "joinBlocks", "editBlock",
                   "saveSkillNow", "touchSkill", "newSkill", "Always loaded", "On demand", "This project",
                   'pageHead(pad, "Skills"', "skx-list", "skx-doc"):
        assert marker in page, marker
    js = page.split("<script>")[1]
    for gone in ("function openSkillEditor(", "function openSkillSheet("):
        assert gone not in js, gone
    skills_js = js[js.index("const SKW = {"):js.index("function fieldWrap(")]
    assert '"Save changes"' not in skills_js and "showModal(" not in skills_js
    # title and description are edited where they are read
    doc = js[js.index("function paintSkillDoc("):js.index("function skillProps(")]
    assert 'contentEditable = "plaintext-only"' in doc and 'dataset.ph = "Untitled"' in doc
    # every edit autosaves after a pause; a draft saves only once it has a title
    assert "SKW.timer = setTimeout(saveSkillNow, 700);" in js
    assert 'Give it a title to save' in js
    # a new draft never overwrites an existing skill of the same name
    assert "slug: SKW.draft ? undefined : d.slug" in js and "That name is taken" in js
    # blocks: Enter splits, "/" opens the block menu, Backspace merges, focus is immediate
    ed = js[js.index("function editBlock("):]
    assert "SLASH.filter" in ed and 'e.key === "Backspace" && s === 0' in ed
    assert "grow(); ta.focus();" in ed and "setTimeout(() => {\n    grow(); ta.focus();" not in ed
    # the two panes stack on narrow screens
    assert ".skx { grid-template-columns: minmax(0, 1fr); }" in page


def test_block_split_round_trips_markdown():
    """Cutting a body into blocks and joining it back never loses a line —
    fences stay whole, headings stand alone, lists stay together."""
    import subprocess

    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    fns = js[js.index("function splitBlocks("):js.index("function paintBlocks(")]
    prog = fns + r"""
const src = "# Title\nintro line\n\n- a\n- b\n\n```bash\necho 1\n\necho 2\n```\n\n## Next\n> quote";
const b = splitBlocks(src);
if (JSON.stringify(b) !== JSON.stringify(["# Title","intro line","- a\n- b","```bash\necho 1\n\necho 2\n```","## Next","> quote"])) { console.log("SPLIT " + JSON.stringify(b)); process.exit(1); }
if (joinBlocks(b) !== src.replace("# Title\nintro", "# Title\n\nintro").replace("## Next\n>", "## Next\n\n>")) { console.log("JOIN " + JSON.stringify(joinBlocks(b))); process.exit(1); }
if (blockKind("```x") !== "code" || blockKind("## h") !== "h2" || blockKind("1. x") !== "list") { console.log("KIND"); process.exit(1); }
console.log("OK");
"""
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    assert r.stdout.startswith("OK"), (r.stdout, r.stderr[:400])


def test_creating_a_skill_never_overwrites_one(home):
    from mantis_agent import serve

    assert serve.add_skill("global", "Deploy", "d", "original")["ok"]
    r = serve.add_skill("global", "Deploy", "d", "clobber")
    assert r["ok"] is False and r["exists"] is True
    assert "original" in serve.skills_state()["global"][0]["body"]


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
