"""Regression tests for the two id defects an adversarial verifier proved.

Both are reachable from *model-authored* text, because a workflow phase is
identified by its title and a title is whatever the model wrote:

D2  Non-Latin titles all normalized to the empty string, so every non-Latin
    phase in a run collapsed onto one node (the rest counted as
    ``dropped_duplicate``, merging their status/activity/usage), and a
    punctuation-only title raised ``InvalidIdError`` into the engine that was
    merely reporting progress.

D3  ``ids.py`` promised "no consumer has to sanitize an id again" while keeping
    both ``/`` and ``.``, so ``make_id('phase', '4f2a/../../../etc/cron.d/pwn')``
    produced an id that ``os.path.normpath`` resolved outside the activity
    directory.

The payload list below is the verifier's, plus emoji-only, RTL, very long and
NFC/NFD pairs. Every assertion here is one of: does not raise, is not empty,
stays distinct, cannot traverse.
"""

from __future__ import annotations

import os
import posixpath
import re
import unicodedata

import pytest

from mantis_agent.activity import ids

# The exact strings from the verifier's report, plus the extra shapes the
# assignment calls for. Every one of these is legal model output.
PAYLOADS = [
    "レビュー",
    "审查代码",
    "Проверка",
    "Review!",
    "Review?",
    "Review",
    "***",
    "!!!",
    "…",
    "#####",
    "   ",
    "\t\n",
    "🐛🔥",
    "🙂",
    "مراجعة الكود",  # RTL
    "עברית",
    "Ελληνικά",
    "z" * 500,
    "レビュー" * 200,
    "../../../../../../etc/cron.d/pwn",
    "..",
    ".",
    "....",
    "a/b/c/d",
    "/etc/passwd",
    "~/.mantis/activity",
    "\x1b[2J\x1b[H",
    "\x00\x00",
    "\ud800",  # lone surrogate from a broken decode
    "Ünïcödé Rëvïëw",
    "Review the code",
]

#: The alphabet a filename may use — ``workflow_store._SAFE_ID`` inverted.
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

ACTIVITY_ROOT = "/home/u/.mantis/activity"


def scoped(title: str) -> str:
    """How an engine builds a phase id: run scope + model-authored title."""

    return "4f2a/" + title


# ---------------------------------------------------------------------------
# D2 — arbitrary model text must never raise and never collapse together
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", PAYLOADS)
def test_phase_title_never_raises_and_never_empty(title: str) -> None:
    nid = ids.make_id("phase", scoped(title))
    kind, local = ids.parse_id(nid)  # round-trips, so the id is well formed
    assert kind == "phase"
    assert local
    assert nid.startswith("wfp:")
    # Both halves are real segments: the run scope survived and the title did
    # not vanish into an empty component.
    scope, _, name = local.partition("/")
    assert scope == "4f2a"
    assert name


@pytest.mark.parametrize("title", PAYLOADS)
def test_unscoped_title_never_raises(title: str) -> None:
    """Even without a run scope, only *blank* input is an error."""

    if title.strip(" \t\r\n\f\v/"):
        assert ids.local_of(ids.make_id("swarm", title))
    else:
        with pytest.raises(ids.InvalidIdError):
            ids.make_id("swarm", title)


def test_blank_local_is_still_a_caller_error() -> None:
    # Unchanged contract: passing nothing is a call-site bug, not model text.
    for blank in ("", "   ", "///", "/ /"):
        with pytest.raises(ids.InvalidIdError):
            ids.make_id("job", blank)


def test_distinct_scripts_do_not_collapse_onto_one_node() -> None:
    # The measured defect: six titles produced three nodes.
    titles = ["レビュー", "审查代码", "Проверка", "Review!", "🐛", "مراجعة"]
    made = [ids.make_id("phase", scoped(t)) for t in titles]
    assert len(set(made)) == len(titles)


@pytest.mark.parametrize(
    "left,right",
    [
        ("レビュー", "审查代码"),
        ("レビュー", "レビュ"),
        ("***", "!!!"),
        ("…", "#####"),
        ("🐛🔥", "🙂"),
        ("עברית", "مراجعة الكود"),
        ("z" * 500, "z" * 499 + "y"),
        ("レビュー" * 200, "レビュー" * 199),
        ("   ", "\t\n"),
    ],
)
def test_distinct_titles_stay_distinct(left: str, right: str) -> None:
    assert ids.make_id("phase", scoped(left)) != ids.make_id("phase", scoped(right))


def test_ascii_titles_stay_readable() -> None:
    # Distinctness must not be bought by hashing everything.
    assert ids.make_id("phase", scoped("Review")) == "wfp:4f2a/Review"
    assert ids.make_id("phase", scoped("Review the code")) == "wfp:4f2a/Review-the-code"
    assert ids.make_id("phase", scoped("Review!")).startswith("wfp:4f2a/Review")
    # A mixed title keeps its readable half *and* gains the distinguishing tail.
    mixed = ids.local_of(ids.make_id("phase", scoped("Review レビュー")))
    assert mixed.startswith("4f2a/Review-")
    assert mixed != ids.local_of(ids.make_id("phase", scoped("Review 审查")))


def test_same_title_is_the_same_node() -> None:
    # Stability matters as much as distinctness: an engine re-emitting a phase
    # must land on the node it created, not a second one.
    for title in PAYLOADS:
        if not scoped(title):  # pragma: no cover - defensive
            continue
        assert ids.make_id("phase", scoped(title)) == ids.make_id("phase", scoped(title))


def test_nfc_and_nfd_spellings_are_one_node() -> None:
    """Canonically equivalent titles render identically, so they are one phase.

    NFD is what a macOS filesystem or a differently-tokenized re-emission hands
    back. Treating the two spellings as two nodes would duplicate the phase in
    the rail; the ids are normalized to NFC so they cannot.
    """

    for base in ("Café Review", "Ünïcödé", "한국어"):
        nfc = unicodedata.normalize("NFC", base)
        nfd = unicodedata.normalize("NFD", base)
        assert ids.make_id("phase", scoped(nfc)) == ids.make_id("phase", scoped(nfd))
        assert ids.safe_path_segment(nfc) == ids.safe_path_segment(nfd)
    # ...but a genuinely different string is still a different id.
    assert ids.make_id("phase", scoped("Café")) != ids.make_id("phase", scoped("Cafe"))


@pytest.mark.parametrize("title", PAYLOADS)
def test_ids_are_bounded(title: str) -> None:
    assert len(ids.local_of(ids.make_id("phase", scoped(title)))) <= 64


# ---------------------------------------------------------------------------
# D3 — path safety, as promised by the module docstring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", PAYLOADS)
def test_no_id_can_escape_the_activity_directory(title: str) -> None:
    local = ids.local_of(ids.make_id("phase", scoped(title)))
    resolved = posixpath.normpath(posixpath.join(ACTIVITY_ROOT, local))
    assert resolved.startswith(ACTIVITY_ROOT + "/")
    assert ".." not in local.split("/")


def test_the_reported_traversal_is_dead() -> None:
    hostile = "4f2a/../../../../../../etc/cron.d/pwn"
    nid = ids.make_id("phase", hostile)
    local = ids.local_of(nid)
    assert local.count("/") == 1  # scope separator only
    assert ".." not in local
    assert posixpath.normpath(posixpath.join(ACTIVITY_ROOT, local)).startswith(
        ACTIVITY_ROOT + "/"
    )


@pytest.mark.parametrize("title", PAYLOADS)
def test_every_segment_is_a_legal_filename(title: str) -> None:
    local = ids.local_of(ids.make_id("phase", scoped(title)))
    assert local.count("/") <= 1  # a title cannot add a path level
    for segment in local.split("/"):
        assert SAFE_SEGMENT.match(segment), segment
        assert segment.strip("-._"), segment  # not '.', '..', '-', ...
        assert os.sep not in segment and (os.altsep or "/") not in segment


@pytest.mark.parametrize("title", PAYLOADS)
def test_safe_path_segment_is_a_total_function(title: str) -> None:
    nid = ids.make_id("phase", scoped(title))
    seg = ids.safe_path_segment(nid)
    assert SAFE_SEGMENT.match(seg), seg
    assert seg.strip("-._")
    assert len(seg) <= 80
    joined = posixpath.normpath(posixpath.join(ACTIVITY_ROOT, seg))
    assert posixpath.dirname(joined) == ACTIVITY_ROOT


def test_safe_path_segment_keeps_distinct_ids_distinct() -> None:
    made = [ids.make_id("phase", scoped(t)) for t in PAYLOADS]
    made += [ids.make_id("job", 3), ids.make_id("subagent", 3), "wfp:a-b", "wfp:a/b"]
    segments = [ids.safe_path_segment(nid) for nid in made]
    assert len(set(segments)) == len(set(made))


def test_safe_path_segment_survives_hostile_input_directly() -> None:
    for hostile in ("../../etc/passwd", "..", ".", "", "   ", "/", "\x00", "🙂"):
        seg = ids.safe_path_segment(hostile)
        assert SAFE_SEGMENT.match(seg), (hostile, seg)
        assert seg not in (".", "..")
        assert posixpath.normpath(posixpath.join(ACTIVITY_ROOT, seg)).startswith(
            ACTIVITY_ROOT + "/"
        )


def test_parse_id_rejects_hand_built_traversal() -> None:
    # make_id can no longer produce these, so parse_id must not bless them
    # either — a journal that trusts parse_id would write outside its root.
    for bad in (
        "wfp:4f2a/../../../etc/cron.d/pwn",
        "wfp:..",
        "wfp:4f2a/..",
        "wfp:4f2a//x",
        "wfp:/x",
        "wfp:4f2a/.",
        "wfp:...",
    ):
        assert ids.try_parse_id(bad) is None
        assert not ids.is_id(bad)
        with pytest.raises(ids.InvalidIdError):
            ids.parse_id(bad)


# ---------------------------------------------------------------------------
# The registry is the consumer that turns an id into a node
# ---------------------------------------------------------------------------


def test_registry_creates_one_node_per_distinct_title() -> None:
    from mantis_agent.activity.registry import ActivityRegistry

    reg = ActivityRegistry()
    titles = ["レビュー", "审查代码", "Проверка", "Review!", "***", "…", "🐛"]
    made = {reg.create_node("phase", scoped(t), title=t) for t in titles}
    assert len(made) == len(titles)
    assert reg.stats.get("dropped_duplicate", 0) == 0
    assert all(reg.node(nid) is not None for nid in made)


# ===========================================================================
# D4 / D5 / D6 — registry defects (registry.py)
# ===========================================================================

from mantis_agent.activity import events as A  # noqa: E402
from mantis_agent.activity.registry import ActivityConfig, ActivityRegistry  # noqa: E402

# ---------------------------------------------------------------------------
# D4 — tree() was the only recursive walk, and blew the stack inside the budget
# ---------------------------------------------------------------------------
# ``max_live_nodes`` defaults to 2000, so a 1500-deep chain is a *legal* tree.
# Rendering one raised RecursionError at a depth of about 997 — and tree() is
# the renderer's entry point, so the failure landed in the middle of a frame.

DEEP = 1500  # comfortably past CPython's ~1000 frames, inside the node budget


def _chain(depth: int) -> tuple:
    reg = ActivityRegistry()
    parent = None
    root = ""
    for i in range(depth):
        parent = reg.create_node("job", i, title="n%d" % i, parent_id=parent)
        root = root or parent
    return reg, root


def test_a_deep_chain_renders_instead_of_blowing_the_stack() -> None:
    reg, root = _chain(DEEP)
    rows = reg.tree()
    assert len(rows) == DEEP == len(reg)
    assert [d for d, _ in rows] == list(range(DEEP))
    assert rows[0][1].id == root


def test_tree_from_an_explicit_root_is_iterative_too() -> None:
    reg, root = _chain(DEEP)
    rows = reg.tree(root)
    assert len(rows) == DEEP
    assert rows[-1][0] == DEEP - 1


def test_a_deep_forest_renders() -> None:
    # Several deep roots at once: one root's depth must not be charged to the
    # next one's walk.
    reg = ActivityRegistry()
    for r in range(3):
        parent = None
        for i in range(600):
            parent = reg.create_node(
                "job", "%d-%d" % (r, i), title="n", parent_id=parent
            )
    assert len(reg.tree()) == len(reg) == 1800


def test_deep_traversal_helpers_agree_with_tree() -> None:
    reg, root = _chain(DEEP)
    assert len(reg.descendants(root)) == DEEP - 1
    assert len(reg.ancestors(reg.tree()[-1][1].id)) == DEEP - 1


def test_tree_still_walks_depth_first_in_declaration_order() -> None:
    # The iterative rewrite must reproduce the recursive traversal exactly.
    reg = ActivityRegistry()
    ses = reg.create_node("session", "s1", title="s")
    a = reg.create_node("phase", "r/a", title="a", parent_id=ses)
    a1 = reg.create_node("agent", "r/a1", title="a1", parent_id=a)
    a2 = reg.create_node("agent", "r/a2", title="a2", parent_id=a)
    b = reg.create_node("phase", "r/b", title="b", parent_id=ses)
    b1 = reg.create_node("agent", "r/b1", title="b1", parent_id=b)
    assert [(d, n.id) for d, n in reg.tree()] == [
        (0, ses), (1, a), (2, a1), (2, a2), (1, b), (2, b1),
    ]


# ---------------------------------------------------------------------------
# D5 — _children grew without bound
# ---------------------------------------------------------------------------
# The plan claims memory is provably capped by the node and event budgets.
# Measured before the fix, at max_live_nodes=50 with 4000 nodes whose parents
# never arrive: len(nodes) = 5, len(_children) = 4000, evicted = 3995.


def _link_bound(reg: ActivityRegistry) -> int:
    return len(reg.nodes) + reg.config.orphan_buffer_size


def test_children_stays_bounded_when_parents_never_arrive() -> None:
    reg = ActivityRegistry(config=ActivityConfig(max_live_nodes=50))
    for i in range(4000):
        nid = reg.create_node("job", i, title="x", parent_id="job:ghost-%d" % i)
        if nid:
            reg.set_status(nid, "done")  # so retention actually evicts
    assert len(reg) <= 50
    assert len(reg._children) <= _link_bound(reg)
    # Tighter, and the real invariant: every surviving key is a parent that at
    # least one *live* node still points at.
    assert len(reg._children) <= len(reg.nodes)
    assert all(reg._children[p] for p in reg._children), "empty child list left behind"


def test_children_stays_bounded_when_parents_are_shared_and_work_churns() -> None:
    import random

    rng = random.Random(11)
    reg = ActivityRegistry(
        config=ActivityConfig(max_live_nodes=50, terminal_node_retention=5)
    )
    for i in range(4000):
        parent = "wfp:ghost-%d" % (i % 3000) if rng.random() < 0.7 else None
        nid = reg.create_node("job", i, title="x", parent_id=parent)
        if nid and rng.random() < 0.9:
            reg.set_status(nid, rng.choice(["done", "error", "running"]))
    assert len(reg._children) <= _link_bound(reg)
    assert len(reg.tree()) == len(reg), "a node became unreachable"


def test_evicting_the_last_child_drops_its_parents_empty_list() -> None:
    reg = ActivityRegistry(config=ActivityConfig(terminal_node_retention=0))
    ses = reg.create_node("session", "s1", title="s")
    kid = reg.create_node("job", 1, title="k", parent_id=ses)
    reg.set_status(kid, "done")  # retention cap 0 -> evicted immediately
    assert reg.node(kid) is None
    assert ses not in reg._children, "an emptied child list was left behind"


def test_a_leaked_children_map_is_reclaimed_and_counted() -> None:
    # The backstop, exercised directly: even if some path leaks entries, the
    # map is pulled back under its bound instead of growing forever.
    reg = ActivityRegistry()
    reg.create_node("session", "s1", title="s")
    for i in range(4000):
        reg._children.setdefault("wfp:leak-%d" % i, ["job:gone-%d" % i])
    kid = reg.create_node("job", 1, title="k", parent_id="wfp:fresh")
    assert len(reg._children) <= _link_bound(reg)
    assert reg.stats["dropped_parent_link"] > 0
    # Dropping a link is not dropping a node.
    assert reg.node(kid) is not None
    assert kid in [n.id for n in reg.roots()]
    assert len(reg.tree()) == len(reg)


def test_the_newest_pending_parent_still_adopts_its_children() -> None:
    # Bounding the map must not break the explicitly supported case: a parent
    # that arrives after its child.
    reg = ActivityRegistry()
    for i in range(4000):
        reg._children.setdefault("wfp:leak-%d" % i, ["job:gone-%d" % i])
    kid = reg.create_node("job", 1, title="k", parent_id="wfp:fresh")
    phase = reg.create_node("phase", "fresh", title="fresh")
    assert [n.id for n in reg.children(phase)] == [kid]


# ---------------------------------------------------------------------------
# D6 — "nothing in here may raise into an engine", except it did
# ---------------------------------------------------------------------------

MALFORMED = [
    "not-an-event",
    None,
    42,
    object(),
    {"type": "node_status", "node_id": "job:1"},
    A.NodeStatus(seq=0, ts=0.0, node_id="", status="done"),
    A.NodeStatus(seq="nope", ts=0.0, node_id="job:1", status="done"),
    A.NodeActivity(seq=0, ts=float("nan"), node_id="job:1", text="hi"),
    A.NodeCreated(seq=0, ts=0.0, node_id="job:1", parent_id=None, kind=["job"], title="t"),
    A.NodeCreated(
        seq=0, ts=0.0, node_id="job:1", parent_id=None, kind="job", title="t", actions=5
    ),
    A.NodeUsage(seq=0, ts=0.0, node_id="job:1", input_tokens="lots"),
    A.NodeUsage(seq=0, ts=0.0, node_id="job:1", cost_usd=float("inf")),
]


@pytest.mark.parametrize("ev", MALFORMED)
def test_a_malformed_event_is_counted_not_raised(ev: object) -> None:
    reg = ActivityRegistry()
    assert reg.apply(ev) is None
    assert reg.stats["dropped_malformed"] == 1
    assert reg.stats["events"] == 1  # it was still seen
    assert len(reg) == 0


def test_a_malformed_event_never_reaches_a_subscriber() -> None:
    reg = ActivityRegistry()
    seen: list = []
    reg.subscribe(seen.append)
    for ev in MALFORMED:
        reg.apply(ev)
    assert seen == []
    assert reg.subscriber_count == 1  # nobody was blamed for the junk


@pytest.mark.parametrize(
    "kind,local",
    [("definitely-not-a-kind", 7), ("job", "   "), ("job", ""), ("job", None)],
)
def test_create_node_never_raises_on_a_bad_kind_or_local(kind: str, local: object) -> None:
    reg = ActivityRegistry()
    node_id = reg.create_node(kind, local, title="t")
    assert isinstance(node_id, str)
    # Either it minted a usable id or it refused — never an exception, and
    # never an unaddressable node.
    assert "" not in reg.nodes
    if not node_id:
        assert len(reg) == 0
        assert reg.stats["dropped_malformed"] == 1
    else:
        assert reg.node(node_id) is not None


def test_the_registry_still_works_after_a_stream_of_junk() -> None:
    reg = ActivityRegistry()
    for ev in MALFORMED:
        reg.apply(ev)
    reg.create_node("definitely-not-a-kind", 1, title="junk")
    nid = reg.create_node("job", 1, title="real")
    assert reg.set_status(nid, "done") is not None
    assert reg.node(nid).status == "done"
    assert reg.counts()["done"] == 1
    assert len(reg.tree()) == len(reg) == 1
