"""Skill precedence, shadowing, and stacking.

The behaviour under test is the fix for a real defect: ``discover_skills``
merges skill files into a dict keyed by name, so when two tiers define
``review`` the winner is whatever iteration order produced, the loser vanishes
without a trace, and there is no way to add to a skill rather than replace it.

These tests pin three contracts:

* **precedence is total and deterministic** — every tier pair, plus
  nearest-project-wins in a nested tree, plus a same-tier tie being an *error*
  naming both paths rather than a coin flip;
* **losers survive** — shadowed definitions are retained with their paths and
  ``explain()`` prints exactly what actually won;
* **stacking cannot widen** — ``allowed_tools`` intersects, a child asking for
  anything its parent never granted is rejected, cycles report their chain, and
  the depth cap holds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mantis_agent.skill_resolve import (
    BUILTIN_TIER,
    FAR_DEPTH,
    LOCAL_TIER,
    MANAGED_TIER,
    MAX_STACK_DEPTH,
    PLUGIN_TIER,
    PROJECT_TIER,
    RUNTIME_TIER,
    TIER_ORDER,
    USER_TIER,
    ResolutionSet,
    SkillCandidate,
    SkillCollisionError,
    SkillCycleError,
    SkillDepthExceededError,
    SkillReferenceError,
    SkillToolWideningError,
    candidate_from_skill,
    explain,
    intersect_allowed_tools,
    normalize_allowed_tools,
    project_depth,
    project_skill_dirs,
    resolve,
    stack,
    tier_rank,
)
from mantis_agent.skills import Skill


def cand(name: str, tier: str, path: str | None = None, **kw) -> SkillCandidate:
    """A candidate with a plausible per-tier path unless one is given."""
    if path is None and tier not in (RUNTIME_TIER, BUILTIN_TIER):
        path = f"/fake/{tier}/{name}/SKILL.md"
    return SkillCandidate(
        name=name, tier=tier, path=Path(path) if path else None, **kw
    )


# ---------------------------------------------------------------------------
# Tier precedence
# ---------------------------------------------------------------------------


def test_tier_order_is_the_documented_chain():
    assert TIER_ORDER == (
        MANAGED_TIER, RUNTIME_TIER, USER_TIER, LOCAL_TIER,
        PROJECT_TIER, PLUGIN_TIER, BUILTIN_TIER,
    )
    ranks = [tier_rank(t) for t in TIER_ORDER]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)


def test_unknown_tier_sorts_last():
    """Fail-closed: a made-up tier gets less authority than every real one."""
    assert tier_rank("superuser") > tier_rank(BUILTIN_TIER)
    res = resolve([cand("r", "superuser"), cand("r", BUILTIN_TIER)])["r"]
    assert res.winner.tier == BUILTIN_TIER


@pytest.mark.parametrize(
    ("high", "low"),
    [(a, b) for i, a in enumerate(TIER_ORDER) for b in TIER_ORDER[i + 1:]],
)
def test_every_tier_pair_resolves_to_the_higher_tier(high: str, low: str):
    """All 21 ordered pairs, both discovery orders — order must not matter."""
    for group in ([cand("review", high), cand("review", low)],
                  [cand("review", low), cand("review", high)]):
        res = resolve(group)["review"]
        assert res.winner.tier == high
        assert [s.tier for s in res.shadowed] == [low]


def test_managed_beats_everything_at_once():
    res = resolve([cand("review", t) for t in reversed(TIER_ORDER)])["review"]
    assert res.winner.tier == MANAGED_TIER
    assert [s.tier for s in res.shadowed] == list(TIER_ORDER[1:])


def test_winners_and_shadowed_expose_the_whole_picture():
    r = resolve([cand("a", USER_TIER), cand("a", PROJECT_TIER), cand("b", PLUGIN_TIER)])
    assert isinstance(r, ResolutionSet)
    assert sorted(c.name for c in r.winners()) == ["a", "b"]
    assert [c.tier for c in r.shadowed()] == [PROJECT_TIER]
    assert "a" in r and len(r) == 2 and sorted(iter(r)) == ["a", "b"]
    assert r.get("nope") is None
    assert r["a"].is_shadowing and not r["b"].is_shadowing
    assert r["a"].all_candidates == (r["a"].winner, *r["a"].shadowed)


# ---------------------------------------------------------------------------
# Nearest-project-wins
# ---------------------------------------------------------------------------


@pytest.fixture
def monorepo(tmp_path: Path) -> Path:
    """<root>/.mantis/skills/deploy + <root>/packages/api/.mantis/skills/deploy."""
    for rel in (".mantis/skills/deploy", "packages/api/.mantis/skills/deploy",
                "packages/api/.mantis/skills.local/deploy"):
        d = tmp_path / rel
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("body", encoding="utf-8")
    return tmp_path


def test_project_depth_counts_ancestors(monorepo: Path):
    cwd = monorepo / "packages" / "api"
    assert project_depth(cwd / ".mantis/skills/deploy/SKILL.md", cwd) == 0
    assert project_depth(monorepo / ".mantis/skills/deploy/SKILL.md", cwd) == 2
    assert project_depth(monorepo / ".mantis", monorepo) == 0


def test_project_depth_of_a_sibling_tree_is_farther(monorepo: Path, tmp_path: Path):
    """A sibling tree only meets the cwd at a shared ancestor, so it sorts
    behind everything in the cwd's own branch — which is the property
    nearest-project-wins actually needs."""
    cwd = monorepo / "packages" / "api"
    inside = project_depth(cwd / ".mantis/skills/deploy/SKILL.md", cwd)
    sibling = project_depth(tmp_path.parent / "elsewhere" / "x.md", cwd)
    assert inside < sibling < FAR_DEPTH


def test_nearest_project_wins(monorepo: Path):
    cwd = monorepo / "packages" / "api"
    near = cand("deploy", PROJECT_TIER, str(cwd / ".mantis/skills/deploy/SKILL.md"))
    far = cand("deploy", PROJECT_TIER, str(monorepo / ".mantis/skills/deploy/SKILL.md"))
    # Farther one listed FIRST: a root-then-down directory walk must not win.
    res = resolve([far, near], cwd=cwd)["deploy"]
    assert res.winner.path == near.path
    assert res.winner.depth == 0
    assert [s.path for s in res.shadowed] == [far.path]
    assert res.shadowed[0].depth == 2


def test_local_tier_outranks_project_at_the_same_depth(monorepo: Path):
    cwd = monorepo / "packages" / "api"
    res = resolve(
        [
            cand("deploy", PROJECT_TIER, str(cwd / ".mantis/skills/deploy/SKILL.md")),
            cand("deploy", LOCAL_TIER, str(cwd / ".mantis/skills.local/deploy/SKILL.md")),
        ],
        cwd=cwd,
    )["deploy"]
    assert res.winner.tier == LOCAL_TIER


def test_nearer_project_beats_farther_local(monorepo: Path):
    """Depth only orders *within* a tier — local still outranks project."""
    cwd = monorepo / "packages" / "api"
    res = resolve(
        [
            cand("deploy", PROJECT_TIER, str(cwd / ".mantis/skills/deploy/SKILL.md")),
            cand("deploy", LOCAL_TIER, str(monorepo / ".mantis/skills.local/d/SKILL.md")),
        ],
        cwd=cwd,
    )["deploy"]
    assert res.winner.tier == LOCAL_TIER


def test_project_skill_dirs_walks_up_in_precedence_order(monorepo: Path):
    cwd = monorepo / "packages" / "api"
    found = project_skill_dirs(cwd)
    assert [(d.name, tier, depth) for d, tier, depth in found] == [
        ("skills.local", LOCAL_TIER, 0),
        ("skills", PROJECT_TIER, 0),
        ("skills", PROJECT_TIER, 2),
    ]
    assert [t for _, t, _ in project_skill_dirs(cwd, include_local=False)] == [
        PROJECT_TIER, PROJECT_TIER
    ]


# ---------------------------------------------------------------------------
# Same-tier collisions
# ---------------------------------------------------------------------------


def test_same_tier_collision_is_an_error_naming_both_paths():
    a = cand("review", PLUGIN_TIER, "/plugins/alpha/review/SKILL.md")
    b = cand("review", PLUGIN_TIER, "/plugins/beta/review/SKILL.md")
    with pytest.raises(SkillCollisionError) as ei:
        resolve([a, b])
    err = ei.value
    assert err.name == "review"
    assert set(err.paths) == {a.path, b.path}
    assert "/plugins/alpha/review/SKILL.md" in str(err)
    assert "/plugins/beta/review/SKILL.md" in str(err)


def test_same_depth_project_collision_errors(monorepo: Path):
    cwd = monorepo / "packages" / "api"
    with pytest.raises(SkillCollisionError):
        resolve(
            [
                cand("deploy", PROJECT_TIER, str(cwd / ".mantis/skills/deploy/SKILL.md")),
                cand("deploy", PROJECT_TIER, str(cwd / ".mantis/skills/dup/SKILL.md")),
            ],
            cwd=cwd,
        )


def test_collision_reports_every_tied_candidate():
    tied = [cand("x", PLUGIN_TIER, f"/p/{i}/SKILL.md") for i in range(3)]
    with pytest.raises(SkillCollisionError) as ei:
        resolve([*tied, cand("x", BUILTIN_TIER)])
    assert len(ei.value.candidates) == 3


def test_a_tie_below_the_winner_is_not_an_error():
    """Only an ambiguous *winner* is fatal. Two plugins that tie under a user
    skill change nothing the model sees, so refusing to resolve would be
    strictness without a payoff — they are simply both listed as shadowed."""
    tied = [cand("x", PLUGIN_TIER, f"/p/{i}/SKILL.md") for i in range(2)]
    res = resolve([*tied, cand("x", USER_TIER)])["x"]
    assert res.winner.tier == USER_TIER
    assert {s.path for s in res.shadowed} == {t.path for t in tied}


def test_the_same_file_seen_twice_is_not_a_collision(tmp_path: Path):
    p = tmp_path / "review" / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text("b", encoding="utf-8")
    res = resolve([cand("review", USER_TIER, str(p)), cand("review", USER_TIER, str(p))])
    assert res["review"].shadowed == ()


def test_pathless_runtime_duplicates_still_collide():
    with pytest.raises(SkillCollisionError):
        resolve([cand("x", RUNTIME_TIER), cand("x", RUNTIME_TIER)])


# ---------------------------------------------------------------------------
# explain() / `/skills why`
# ---------------------------------------------------------------------------


def test_explain_matches_the_actual_resolution(monorepo: Path):
    """The plan §13 chain, over a real nearest-project-wins layout.

    (§13's sample shows a project skill beating a user one; §6's normative
    order does not, so the user copy is a *plugin* copy here — the shape of the
    output is what is being pinned, not the doc's typo.)
    """
    cwd = monorepo / "packages" / "api"
    near = cand(
        "review-changes", PROJECT_TIER, str(cwd / ".mantis/skills/deploy/SKILL.md"),
        extends="plugin:review-changes",
        allowed_tools=("read_file", "grep"),
    )
    far = cand("review-changes", PROJECT_TIER, str(monorepo / ".mantis/skills/deploy/SKILL.md"))
    plug = cand("review-changes", PLUGIN_TIER, "/pkgs/rc/SKILL.md")
    resolved = resolve([plug, far, near], cwd=cwd)
    out = explain("review-changes", resolved)

    assert out.splitlines()[0] == "review-changes  → project (nearest)"
    # The winner line names the file that actually won.
    assert f"  ✓ project   {near.path}   trusted" in out
    assert "shadows" in out
    assert f"  · project   {far.path}   (farther)" in out
    assert f"  · plugin    {plug.path}" in out
    assert "extends     plugin:review-changes" in out
    assert "tools       read_file, grep" in out
    # And it is the winner, not merely a nice-looking chain.
    assert resolved["review-changes"].winner.path == near.path


def test_explain_flags_untrusted_and_shell_blocks():
    win = cand("x", USER_TIER, meta={"shell": [{"run": "git diff --stat"}]})
    shadow = cand("x", PROJECT_TIER, trusted=False)
    out = explain("x", resolve([win, shadow]))
    assert "(untrusted)" in out
    assert "shell       1 block(s)" in out
    assert "(nearest)" not in out  # winner tier isn't depth-ordered


def test_explain_unknown_name():
    assert explain("nope", resolve([])) == "nope\n  no definition found"


def test_explain_reports_narrowing_from_the_stack():
    parent = cand("review", PLUGIN_TIER, allowed_tools=("read_file", "grep", "bash"))
    child = cand("review", USER_TIER, extends="plugin:review", allowed_tools=("read_file",))
    resolved = resolve([parent, child])
    stacked = stack(resolved["review"].winner, resolved)
    assert stacked.chain[-1] is child  # the winner is the one that stacks
    out = explain("review", resolved, stacked=stacked)
    assert "tools       read_file   [narrowed from parent]" in out


# ---------------------------------------------------------------------------
# Stacking
# ---------------------------------------------------------------------------


def test_stack_concatenates_parent_then_child_with_a_separator():
    parent = cand("review", USER_TIER, body="USER GUIDANCE")
    child = cand("review", PROJECT_TIER, body="REPO ADDENDUM", extends="user:review")
    st = stack(child, resolve([parent, child]))

    assert st.refs == ("user:review", "project:review")
    assert st.depth == 1
    assert st.body.index("USER GUIDANCE") < st.body.index("REPO ADDENDUM")
    assert "--- project:review extends user:review ---" in st.body


def test_stack_merges_metadata_shallowly_with_the_child_winning():
    parent = cand(
        "review", USER_TIER, description="parent desc",
        meta={"category": "eng", "version": "1.0.0", "author": "teddy"},
    )
    child = cand(
        "review", PROJECT_TIER, description="child desc", extends="user:review",
        meta={"version": "2.0.0", "future_key": "kept"},
    )
    st = stack(child, resolve([parent, child]))
    assert st.meta == {
        "category": "eng", "version": "2.0.0", "author": "teddy", "future_key": "kept",
    }
    assert st.description == "child desc"


def test_stack_without_extends_is_a_one_layer_stack():
    only = cand("solo", USER_TIER, body="B", allowed_tools=("grep",))
    st = stack(only, resolve([only]))
    assert st.chain == (only,) and st.body == "B" and st.depth == 0
    assert st.allowed_tools == ("grep",)


def test_bare_extends_falls_through_to_the_shadowed_definition():
    """``extends: review`` from the winner means "the copy I am shadowing"."""
    parent = cand("review", USER_TIER, body="P")
    child = cand("review", PROJECT_TIER, body="C", extends="review")
    st = stack(child, resolve([parent, child]))
    assert st.refs == ("user:review", "project:review")


def test_extends_a_different_name():
    base = cand("base", USER_TIER, body="BASE")
    child = cand("review", PROJECT_TIER, body="CHILD", extends="base")
    assert stack(child, resolve([base, child])).refs == ("user:base", "project:review")


def test_empty_parent_body_leaves_no_dangling_separator():
    parent = cand("review", USER_TIER, body="   ")
    child = cand("review", PROJECT_TIER, body="C", extends="user:review")
    assert stack(child, resolve([parent, child])).body == "C"


# --- allowed_tools: intersect, never union --------------------------------


def test_child_narrows_parent_grant():
    parent = cand("r", USER_TIER, allowed_tools=("read_file", "grep", "bash"))
    child = cand("r", PROJECT_TIER, extends="user:r", allowed_tools=("read_file", "grep"))
    assert stack(child, resolve([parent, child])).allowed_tools == ("read_file", "grep")


def test_child_widening_parent_grant_is_rejected():
    parent = cand("r", USER_TIER, allowed_tools=("read_file",))
    child = cand("r", PROJECT_TIER, extends="user:r", allowed_tools=("read_file", "write_file"))
    with pytest.raises(SkillToolWideningError) as ei:
        stack(child, resolve([parent, child]))
    err = ei.value
    assert err.widened == ("write_file",)
    assert err.parent_ref == "user:r" and err.child_ref == "project:r"
    assert "write_file" in str(err)


def test_child_cannot_escalate_to_all():
    parent = cand("r", USER_TIER, allowed_tools=("read_file",))
    child = cand("r", PROJECT_TIER, extends="user:r", allowed_tools=("all",))
    with pytest.raises(SkillToolWideningError):
        stack(child, resolve([parent, child]))


def test_intersection_semantics_directly():
    # Child undeclared inherits; parent undeclared imposes nothing.
    assert intersect_allowed_tools(("a", "b"), None) == ("a", "b")
    assert intersect_allowed_tools(None, ("a",)) == ("a",)
    assert intersect_allowed_tools(None, None) is None
    # An unconstrained parent may be narrowed by anything.
    assert intersect_allowed_tools(("all",), ("a",)) == ("a",)
    assert intersect_allowed_tools(("*",), ("all",)) == ("all",)
    # Intersection, never union: parent-only entries are dropped, not kept.
    assert intersect_allowed_tools(("a", "b", "c"), ("b",)) == ("b",)
    # An empty child grant is a real (maximally narrow) declaration.
    assert intersect_allowed_tools(("a",), ()) == ()
    with pytest.raises(SkillToolWideningError):
        intersect_allowed_tools(("a",), ("a", "z"))


def test_parameterised_child_narrows_a_bare_parent_grant():
    """``bash`` covers ``bash(git diff:*)`` — that is a narrowing, not a widening."""
    assert intersect_allowed_tools(("read_file", "bash"), ("bash(git diff:*)",)) == (
        "bash(git diff:*)",
    )
    with pytest.raises(SkillToolWideningError):
        intersect_allowed_tools(("bash(git diff:*)",), ("bash",))


def test_tools_narrow_monotonically_down_a_three_layer_stack():
    a = cand("r", MANAGED_TIER, body="A", allowed_tools=("read_file", "grep", "bash"))
    b = cand("r", USER_TIER, body="B", extends="managed:r", allowed_tools=("read_file", "grep"))
    c = cand("r", PROJECT_TIER, body="C", extends="user:r", allowed_tools=("grep",))
    resolved = resolve([a, b, c])
    st = stack(c, resolved)
    assert st.refs == ("managed:r", "user:r", "project:r")
    assert st.allowed_tools == ("grep",)
    assert st.body.index("A") < st.body.index("B") < st.body.index("C")


def test_grandchild_cannot_widen_past_a_narrowing_ancestor():
    """The accumulated grant is what binds — not the immediate parent's file."""
    a = cand("r", MANAGED_TIER, allowed_tools=("read_file", "bash"))
    b = cand("r", USER_TIER, extends="managed:r", allowed_tools=("read_file",))
    c = cand("r", PROJECT_TIER, extends="user:r", allowed_tools=("read_file", "bash"))
    with pytest.raises(SkillToolWideningError) as ei:
        stack(c, resolve([a, b, c]))
    assert ei.value.widened == ("bash",)


# --- cycles, depth, bad references ----------------------------------------


def test_cycle_is_detected_and_reports_the_chain():
    a = cand("a", USER_TIER, extends="managed:b")
    b = cand("b", MANAGED_TIER, extends="user:a")
    with pytest.raises(SkillCycleError) as ei:
        stack(a, resolve([a, b]))
    assert [c.ref for c in ei.value.chain] == ["user:a", "managed:b", "user:a"]
    assert "user:a -> managed:b -> user:a" in str(ei.value)


def test_self_cycle_is_detected():
    a = cand("a", USER_TIER, extends="user:a")
    with pytest.raises(SkillCycleError) as ei:
        stack(a, resolve([a]))
    assert [c.ref for c in ei.value.chain] == ["user:a", "user:a"]


def test_depth_cap():
    tiers = [MANAGED_TIER, RUNTIME_TIER, USER_TIER, LOCAL_TIER, PROJECT_TIER]
    chain = [
        cand(f"s{i}", t, extends=(f"{tiers[i - 1]}:s{i - 1}" if i else None))
        for i, t in enumerate(tiers)
    ]
    resolved = resolve(chain)
    # 3 hops (4 layers) is the documented cap and must pass.
    assert stack(chain[3], resolved).depth == MAX_STACK_DEPTH == 3
    with pytest.raises(SkillDepthExceededError) as ei:
        stack(chain[4], resolved)
    assert ei.value.max_depth == 3
    assert "project:s4" in str(ei.value)
    # ...and the cap is configurable downward.
    with pytest.raises(SkillDepthExceededError):
        stack(chain[3], resolved, max_depth=2)


def test_missing_extends_target():
    a = cand("a", USER_TIER, extends="ghost")
    with pytest.raises(SkillReferenceError) as ei:
        stack(a, resolve([a]))
    assert "no such skill" in str(ei.value)


def test_extends_wrong_tier():
    a = cand("a", PROJECT_TIER, extends="managed:base")
    base = cand("base", USER_TIER)
    with pytest.raises(SkillReferenceError) as ei:
        stack(a, resolve([a, base]))
    assert "no managed-tier definition" in str(ei.value)


@pytest.mark.parametrize(
    "ref",
    ["../../etc/passwd", "user:../secrets", "a/b", "   ", "~/.ssh/id_rsa", "x:y:z"],
)
def test_extends_cannot_name_a_path(ref: str):
    """``extends`` names a definition, never a file — traversal is refused."""
    a = cand("a", PROJECT_TIER, extends=ref)
    with pytest.raises(SkillReferenceError):
        stack(a, resolve([a]))


def test_extends_unknown_tier_prefix_is_refused():
    a = cand("a", PROJECT_TIER, extends="wherever:b")
    with pytest.raises(SkillReferenceError) as ei:
        stack(a, resolve([a, cand("b", USER_TIER)]))
    assert "unknown tier" in str(ei.value)


# ---------------------------------------------------------------------------
# Bridge to the shipped Skill struct
# ---------------------------------------------------------------------------


def test_candidate_from_skill_uses_the_existing_source_field():
    user = candidate_from_skill(Skill(name="r", description="d", body="B"))
    proj = candidate_from_skill(
        Skill(name="r", description="d", body="P", source="project"),
        path="/repo/.mantis/skills/r/SKILL.md",
        trusted=False,
    )
    assert (user.tier, user.body, user.description) == (USER_TIER, "B", "d")
    assert proj.tier == PROJECT_TIER and proj.trusted is False
    # And the pair resolves the way the tier table says it should.
    assert resolve([proj, user])["r"].winner.tier == USER_TIER


def test_candidate_from_skill_honours_an_explicit_tier():
    c = candidate_from_skill(Skill(name="r", description="", body=""), tier=MANAGED_TIER)
    assert c.tier == MANAGED_TIER


def test_normalize_allowed_tools_accepts_frontmatter_shapes():
    assert normalize_allowed_tools(None) is None
    assert normalize_allowed_tools("read_file, grep") == ("read_file", "grep")
    assert normalize_allowed_tools(["read_file", " grep "]) == ("read_file", "grep")
    # Empty list is a declaration of "nothing", not an absence of one.
    assert normalize_allowed_tools([]) == ()
    assert normalize_allowed_tools(17) is None


def test_candidate_ref_and_location():
    assert cand("r", USER_TIER).ref == "user:r"
    assert cand("r", RUNTIME_TIER).location == "<runtime>"
    assert cand("r", USER_TIER, "/a/SKILL.md").location == "/a/SKILL.md"
    assert cand("r", USER_TIER).effective_depth() == 0
    assert cand("r", PROJECT_TIER).effective_depth() == FAR_DEPTH
