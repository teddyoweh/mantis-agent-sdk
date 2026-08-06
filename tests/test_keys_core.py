"""The keybinding foundation: key syntax, keymap composition, conflicts.

Four contracts, ordered by how much damage getting them wrong does:

1. **A key spelled two ways is one key.** ``s-a`` and ``A`` are the same
   physical keystroke, and so are ``c-A`` and ``c-a``; a keymap that binds both
   to different actions is a conflict, not two bindings. Normalization happens
   in the parser so every downstream table is keyed by the canonical form.
2. **Removal is explicit.** Pure merging can only add, so ``unbind`` is the only
   way an inherited binding goes away, and it applies before the child's own
   bindings so unbind+rebind in one file behaves.
3. **The most specific context wins**, and an exact match anywhere in the stack
   beats a pending chord prefix — which is exactly why binding a chord prefix
   directly is an error rather than a preference.
4. **Every conflict is found at load time with a message naming both sides.**
   A diagnostic that says "conflict in overlay" and stops is not actionable.
"""

from __future__ import annotations

import json

import pytest

from mantis_agent.keys import (
    GLOBAL_CONTEXT,
    ActionSpec,
    BindingConflictError,
    ChordPrefixConflictError,
    ExtendsCycleError,
    Key,
    Keymap,
    KeymapError,
    KeymapSchemaError,
    KeymapVersionError,
    UnknownActionError,
    UnknownKeyError,
    check,
    compose,
    context_stack,
    expand_context,
    format_sequence,
    has_errors,
    is_descendant,
    label_sequence,
    load_keymap,
    parse_key,
    parse_sequence,
    preset_resolver,
    raise_for_errors,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def doc(name: str, bindings: dict, *, extends=None, unbind=None, version: int = 1):
    """Build a KeymapDoc from a literal dict, through the real JSON path."""
    data: dict = {"version": version, "bindings": bindings}
    if extends is not None:
        data["extends"] = extends
    if unbind is not None:
        data["unbind"] = unbind
    return load_keymap(json.dumps(data, indent=2), name=name)


def keymap(bindings: dict, **kw) -> Keymap:
    return compose(doc("test", bindings, **kw))


def codes(diags) -> list:
    return [d.code for d in diags]


# ---------------------------------------------------------------------------
# §6 key syntax — modifiers
# ---------------------------------------------------------------------------


def test_bare_literal_character() -> None:
    k = parse_key("a")
    assert (k.key, k.ctrl, k.shift, k.alt) == ("a", False, False, False)


def test_ctrl_prefix() -> None:
    k = parse_key("c-x")
    assert (k.key, k.ctrl) == ("x", True)
    assert format_sequence((k,)) == "c-x"


def test_alt_prefix_and_meta_alias() -> None:
    assert parse_key("a-b") == parse_key("m-b")
    assert parse_key("a-b").alt is True


def test_shift_on_a_named_key_is_kept() -> None:
    k = parse_key("s-tab")
    assert (k.key, k.shift) == ("tab", True)
    assert format_sequence((k,)) == "s-tab"


def test_shift_plus_letter_folds_to_the_uppercase_literal() -> None:
    # A terminal delivers "A"; it never delivers shift+a separately. Folding is
    # what makes the same-key conflict detectable.
    assert parse_key("s-a") == parse_key("A")
    assert parse_key("s-a").shift is False
    assert format_sequence(parse_sequence("s-a")) == "A"


def test_ctrl_plus_uppercase_folds_to_lowercase() -> None:
    # c-a and c-A are the same byte (0x01) on the wire.
    assert parse_key("c-A") == parse_key("c-a")
    assert format_sequence(parse_sequence("c-A")) == "c-a"


def test_shift_plus_unshiftable_character_is_rejected() -> None:
    with pytest.raises(UnknownKeyError) as e:
        parse_key("s-1")
    assert "!" in str(e.value) or "shifted character" in str(e.value)


def test_modifiers_combine_and_canonicalize_in_a_fixed_order() -> None:
    assert format_sequence(parse_sequence("a-c-x")) == "c-a-x"
    assert format_sequence(parse_sequence("s-a-tab")) == "a-s-tab"


def test_duplicate_modifier_is_rejected() -> None:
    with pytest.raises(UnknownKeyError):
        parse_key("c-c-x")


# ---------------------------------------------------------------------------
# §6 key syntax — named keys and literals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("escape", "escape"),
        ("esc", "escape"),
        ("enter", "enter"),
        ("return", "enter"),
        ("tab", "tab"),
        ("space", "space"),
        ("f1", "f1"),
        ("F12", "f12"),
        ("up", "up"),
        ("pgup", "pageup"),
        ("BackSpace", "backspace"),
    ],
)
def test_named_keys_and_aliases(spec: str, expected: str) -> None:
    assert parse_key(spec).key == expected


def test_hyphen_is_a_literal_key() -> None:
    assert parse_key("-").key == "-"
    assert parse_key("c--") == Key(key="-", ctrl=True)


def test_punctuation_literals_survive() -> None:
    for ch in "?/:*[]\\|":
        assert parse_key(ch).key == ch


def test_case_is_preserved_for_bare_literals() -> None:
    assert parse_key("G").key == "G"
    assert parse_key("g").key == "g"
    assert parse_key("G") != parse_key("g")


@pytest.mark.parametrize("bad", ["", "   ", "c-", "s-", "f13", "ctrl-x", "notakey", "-c-x"])
def test_malformed_keys_are_rejected(bad: str) -> None:
    with pytest.raises(UnknownKeyError):
        parse_key(bad)


def test_unknown_named_key_suggests_a_near_match() -> None:
    with pytest.raises(UnknownKeyError) as e:
        parse_key("escpe")
    assert "escape" in str(e.value)


# ---------------------------------------------------------------------------
# §6 key syntax — chords
# ---------------------------------------------------------------------------


def test_chord_is_space_separated() -> None:
    seq = parse_sequence("c-x c-s")
    assert len(seq) == 2
    assert format_sequence(seq) == "c-x c-s"


def test_chord_tolerates_extra_whitespace() -> None:
    assert parse_sequence("  c-x   c-s  ") == parse_sequence("c-x c-s")


def test_empty_sequence_is_rejected() -> None:
    with pytest.raises(UnknownKeyError):
        parse_sequence("   ")


def test_label_is_human_readable() -> None:
    assert label_sequence(parse_sequence("c-x c-s")) == "ctrl+x ctrl+s"
    assert label_sequence(parse_sequence("s-tab")) == "shift+tab"
    assert label_sequence(parse_sequence("a-b")) == "alt+b"


# ---------------------------------------------------------------------------
# §6 keymap schema
# ---------------------------------------------------------------------------


def test_bindings_load_with_context_key_and_action() -> None:
    km = keymap({"global": {"c-c": "session.interrupt"}})
    b = km.lookup("global", parse_sequence("c-c"))
    assert b is not None
    assert (b.context, b.action) == ("global", "session.interrupt")


def test_malformed_json_reports_the_line() -> None:
    with pytest.raises(KeymapSchemaError) as e:
        load_keymap('{\n  "version": 1,\n  "bindings": {,}\n}', name="user")
    assert e.value.line == 3
    assert "user" in str(e.value)


def test_wrong_version_is_rejected() -> None:
    with pytest.raises(KeymapVersionError):
        doc("user", {"global": {"a": "x"}}, version=2)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"version": 1, "bindings": []},
        {"version": 1, "bindings": {"global": "nope"}},
        {"version": 1, "bindings": {"global": {"a": 3}}},
        {"version": 1, "bindings": {"global": {"a": ""}}},
        {"version": 1, "unbind": "global:a"},
        {"version": 1, "extends": 3},
    ],
)
def test_schema_violations_are_rejected(data) -> None:
    with pytest.raises(KeymapSchemaError):
        load_keymap(json.dumps(data), name="user")


def test_missing_version_defaults_to_current() -> None:
    d = load_keymap('{"bindings": {"global": {"a": "x"}}}', name="user")
    assert d.version == 1


def test_bad_key_syntax_in_a_file_names_the_file_and_context() -> None:
    with pytest.raises(UnknownKeyError) as e:
        doc("user", {"global": {"ctrl-x": "a.b"}})
    assert "user" in str(e.value)
    assert "global" in str(e.value)


def test_binding_records_its_source_and_line() -> None:
    km = keymap({"global": {"c-c": "session.interrupt"}, "input": {"enter": "input.submit"}})
    b = km.lookup("input", parse_sequence("enter"))
    assert b.source == "test"
    assert b.line is not None and b.line > 1


# ---------------------------------------------------------------------------
# §6 extends composition
# ---------------------------------------------------------------------------


def base_doc():
    return doc(
        "default",
        {
            "global": {"c-c": "session.interrupt", "?": "overlay.help"},
            "overlay": {"escape": "overlay.close", "up": "nav.up"},
        },
    )


def test_extends_inherits_parent_bindings() -> None:
    child = doc("user", {"input": {"enter": "input.submit"}}, extends="default")
    km = compose(child, resolve=preset_resolver({"default": base_doc()}))
    assert km.lookup("global", parse_sequence("c-c")).action == "session.interrupt"
    assert km.lookup("input", parse_sequence("enter")).action == "input.submit"


def test_child_overrides_the_same_context_and_key() -> None:
    child = doc("user", {"overlay": {"escape": "overlay.close.all"}}, extends="default")
    km = compose(child, resolve=preset_resolver({"default": base_doc()}))
    b = km.lookup("overlay", parse_sequence("escape"))
    assert b.action == "overlay.close.all"
    assert b.source == "user"


def test_extends_accepts_a_list_and_later_parents_win() -> None:
    a = doc("a", {"global": {"x": "one"}})
    b = doc("b", {"global": {"x": "two"}})
    child = doc("user", {}, extends=["a", "b"])
    km = compose(child, resolve=preset_resolver({"a": a, "b": b}))
    assert km.lookup("global", parse_sequence("x")).action == "two"


def test_lineage_is_recorded_parents_first() -> None:
    child = doc("user", {}, extends="default")
    km = compose(child, resolve=preset_resolver({"default": base_doc()}))
    assert [d.name for d in km.docs] == ["default", "user"]


def test_a_diamond_is_not_a_cycle() -> None:
    root = doc("root", {"global": {"x": "one"}})
    left = doc("left", {}, extends="root")
    right = doc("right", {}, extends="root")
    child = doc("user", {}, extends=["left", "right"])
    km = compose(child, resolve=preset_resolver({"root": root, "left": left, "right": right}))
    assert km.lookup("global", parse_sequence("x")).action == "one"


def test_extends_cycle_is_detected_and_names_the_path() -> None:
    a = doc("a", {}, extends="b")
    b = doc("b", {}, extends="a")
    with pytest.raises(ExtendsCycleError) as e:
        compose(a, resolve=preset_resolver({"a": a, "b": b}))
    assert "a" in str(e.value) and "b" in str(e.value)
    assert "->" in str(e.value)


def test_self_extends_is_a_cycle() -> None:
    a = doc("a", {}, extends="a")
    with pytest.raises(ExtendsCycleError):
        compose(a, resolve=preset_resolver({"a": a}))


def test_unknown_preset_in_extends_raises() -> None:
    child = doc("user", {}, extends="nope")
    with pytest.raises(KeymapError):
        compose(child, resolve=preset_resolver({"default": base_doc()}))


# ---------------------------------------------------------------------------
# §6 unbind
# ---------------------------------------------------------------------------


def test_unbind_removes_an_inherited_binding() -> None:
    child = doc("user", {}, extends="default", unbind=["overlay:up"])
    km = compose(child, resolve=preset_resolver({"default": base_doc()}))
    assert km.lookup("overlay", parse_sequence("up")) is None
    assert km.lookup("overlay", parse_sequence("escape")) is not None


def test_unbind_then_rebind_in_the_same_file_keeps_the_rebind() -> None:
    child = doc(
        "user",
        {"overlay": {"up": "nav.top"}},
        extends="default",
        unbind=["overlay:up"],
    )
    km = compose(child, resolve=preset_resolver({"default": base_doc()}))
    assert km.lookup("overlay", parse_sequence("up")).action == "nav.top"


def test_unbind_normalizes_the_key_spelling() -> None:
    parent = doc("default", {"input": {"A": "input.attach"}})
    child = doc("user", {}, extends="default", unbind=["input:s-a"])
    km = compose(child, resolve=preset_resolver({"default": parent}))
    assert km.lookup("input", parse_sequence("A")) is None


def test_unbind_of_a_chord_works() -> None:
    parent = doc("default", {"global": {"c-x c-s": "session.compact"}})
    child = doc("user", {}, extends="default", unbind=["global:c-x c-s"])
    km = compose(child, resolve=preset_resolver({"default": parent}))
    assert km.bindings == ()


def test_unbind_that_matched_nothing_is_recorded_not_silent() -> None:
    child = doc("user", {}, extends="default", unbind=["overlay:z"])
    km = compose(child, resolve=preset_resolver({"default": base_doc()}))
    assert [u.raw for u in km.unmatched_unbinds] == ["overlay:z"]
    assert "unbind-no-match" in codes(check(km))


@pytest.mark.parametrize("bad", ["noseparator", ":a", "global:"])
def test_malformed_unbind_specs_are_rejected(bad: str) -> None:
    with pytest.raises((KeymapSchemaError, UnknownKeyError)):
        doc("user", {}, unbind=[bad])


# ---------------------------------------------------------------------------
# §6 context stack + resolution precedence
# ---------------------------------------------------------------------------


def test_expand_context_walks_up_the_dotted_path() -> None:
    assert expand_context("overlay.picker") == ("overlay.picker", "overlay")


def test_context_stack_is_most_specific_first_and_ends_at_global() -> None:
    assert context_stack(["overlay.picker", "streaming"]) == (
        "overlay.picker",
        "overlay",
        "streaming",
        GLOBAL_CONTEXT,
    )


def test_context_stack_dedupes_and_never_repeats_global() -> None:
    assert context_stack(["overlay", "overlay.mcp", "global"]) == (
        "overlay",
        "overlay.mcp",
        GLOBAL_CONTEXT,
    )


def test_is_descendant_treats_global_as_the_root() -> None:
    assert is_descendant("overlay.mcp", "overlay")
    assert is_descendant("overlay", GLOBAL_CONTEXT)
    assert is_descendant("overlay", "overlay")
    assert not is_descendant("overlay", "overlay.mcp")
    assert not is_descendant("input", "overlay")
    assert not is_descendant(GLOBAL_CONTEXT, "overlay")


def full_stack_keymap() -> Keymap:
    return keymap(
        {
            "global": {"escape": "session.exit", "?": "overlay.help", "c-c": "session.interrupt"},
            "overlay": {"escape": "overlay.close", "up": "nav.up"},
            "overlay.picker": {"escape": "overlay.close.all", "enter": "list.select"},
        }
    )


def test_most_specific_context_wins() -> None:
    km = full_stack_keymap()
    stack = context_stack(["overlay.picker"])
    assert km.resolve(stack, parse_sequence("escape")).binding.action == "overlay.close.all"


def test_less_specific_contexts_still_resolve() -> None:
    km = full_stack_keymap()
    stack = context_stack(["overlay.picker"])
    assert km.resolve(stack, parse_sequence("up")).binding.context == "overlay"
    assert km.resolve(stack, parse_sequence("c-c")).binding.context == GLOBAL_CONTEXT


def test_a_context_outside_the_stack_is_invisible() -> None:
    km = full_stack_keymap()
    assert km.resolve(context_stack(["input"]), parse_sequence("up")).binding is None


def test_unresolved_key_reports_no_binding_and_no_pending() -> None:
    km = full_stack_keymap()
    r = km.resolve(context_stack(["overlay.picker"]), parse_sequence("z"))
    assert r.binding is None and r.pending == () and not r.is_pending


# ---------------------------------------------------------------------------
# §6 resolution — chords
# ---------------------------------------------------------------------------


def test_chord_prefix_reports_pending_candidates() -> None:
    km = keymap({"global": {"c-x c-s": "session.compact", "c-x c-c": "session.exit"}})
    r = km.resolve(context_stack([]), parse_sequence("c-x"))
    assert r.binding is None
    assert r.is_pending
    assert {b.action for b in r.pending} == {"session.compact", "session.exit"}


def test_completed_chord_resolves() -> None:
    km = keymap({"global": {"c-x c-s": "session.compact"}})
    r = km.resolve(context_stack([]), parse_sequence("c-x c-s"))
    assert r.binding.action == "session.compact"
    assert not r.is_pending


def test_unmatched_continuation_aborts() -> None:
    km = keymap({"global": {"c-x c-s": "session.compact"}})
    r = km.resolve(context_stack([]), parse_sequence("c-x z"))
    assert r.binding is None and r.pending == ()


def test_an_exact_match_beats_a_pending_chord_in_a_deeper_context() -> None:
    # This is the resolution half of the chord-prefix conflict: the direct
    # binding fires, which is precisely why the pair is an error at load.
    km = keymap({"global": {"c-x": "session.new"}, "overlay": {"c-x c-s": "session.compact"}})
    r = km.resolve(context_stack(["overlay"]), parse_sequence("c-x"))
    assert r.binding.action == "session.new"


# ---------------------------------------------------------------------------
# §7 conflict: same key, same context
# ---------------------------------------------------------------------------


def test_two_spellings_of_one_key_in_one_context_is_an_error_naming_both() -> None:
    km = keymap({"input": {"s-a": "input.attach", "A": "input.clear"}})
    diags = [d for d in check(km) if d.code == "duplicate-binding"]
    assert len(diags) == 1
    msg = diags[0].message
    assert "input.attach" in msg and "input.clear" in msg
    assert "s-a" in msg and "A" in msg
    assert "input" in msg
    assert diags[0].severity == "error"
    assert diags[0].line is not None


def test_ctrl_case_spellings_collide_too() -> None:
    km = keymap({"global": {"c-a": "one", "c-A": "two"}})
    assert "duplicate-binding" in codes(check(km))


def test_alias_spellings_collide_too() -> None:
    km = keymap({"overlay": {"esc": "overlay.close", "escape": "session.exit"}})
    assert "duplicate-binding" in codes(check(km))


def test_two_spellings_for_the_same_action_is_only_a_warning() -> None:
    km = keymap({"overlay": {"esc": "overlay.close", "escape": "overlay.close"}})
    diags = [d for d in check(km) if d.code == "duplicate-spelling"]
    assert len(diags) == 1 and diags[0].severity == "warning"
    assert not has_errors(check(km))


def test_the_same_key_in_two_unrelated_contexts_is_not_a_conflict() -> None:
    km = keymap({"input": {"enter": "input.submit"}, "rail": {"enter": "activity.open"}})
    assert check(km) == ()


def test_an_override_through_extends_is_not_a_conflict() -> None:
    child = doc("user", {"overlay": {"escape": "overlay.close.all"}}, extends="default")
    km = compose(child, resolve=preset_resolver({"default": base_doc()}))
    assert "duplicate-binding" not in codes(check(km))


def test_raise_for_errors_raises_the_typed_error() -> None:
    km = keymap({"input": {"s-a": "input.attach", "A": "input.clear"}})
    with pytest.raises(BindingConflictError):
        raise_for_errors(check(km))


def test_raise_for_errors_is_quiet_when_only_warnings() -> None:
    km = keymap({"overlay": {"esc": "overlay.close", "escape": "overlay.close"}})
    raise_for_errors(check(km))


# ---------------------------------------------------------------------------
# §7 conflict: cross-context shadowing
# ---------------------------------------------------------------------------


def test_a_global_binding_shadowed_by_a_child_context_warns() -> None:
    km = keymap({"global": {"escape": "session.exit"}, "overlay": {"escape": "overlay.close"}})
    diags = [d for d in check(km) if d.code == "shadowed-binding"]
    assert len(diags) == 1
    assert diags[0].severity == "warning"
    assert "session.exit" in diags[0].message and "overlay.close" in diags[0].message
    assert "overlay" in diags[0].message


def test_shadowing_between_dotted_relatives_warns() -> None:
    km = keymap({"overlay": {"enter": "list.select"}, "overlay.mcp": {"enter": "mcp.test"}})
    assert "shadowed-binding" in codes(check(km))


def test_shadowing_by_the_same_action_is_not_reported() -> None:
    km = keymap({"global": {"escape": "overlay.close"}, "overlay": {"escape": "overlay.close"}})
    assert "shadowed-binding" not in codes(check(km))


def test_unrelated_contexts_do_not_shadow() -> None:
    km = keymap({"input": {"escape": "input.cancel"}, "rail": {"escape": "focus.input"}})
    assert "shadowed-binding" not in codes(check(km))


# ---------------------------------------------------------------------------
# §7 conflict: chord prefix collision
# ---------------------------------------------------------------------------


def test_direct_binding_of_a_chord_prefix_is_an_error() -> None:
    km = keymap({"global": {"c-x": "session.new", "c-x c-s": "session.compact"}})
    diags = [d for d in check(km) if d.code == "chord-prefix"]
    assert len(diags) == 1 and diags[0].severity == "error"
    assert "c-x" in diags[0].message and "c-x c-s" in diags[0].message
    assert "session.new" in diags[0].message and "session.compact" in diags[0].message


def test_chord_prefix_collision_across_related_contexts_is_an_error() -> None:
    km = keymap({"global": {"c-x": "session.new"}, "overlay": {"c-x c-s": "session.compact"}})
    assert "chord-prefix" in codes(check(km))
    with pytest.raises(ChordPrefixConflictError):
        raise_for_errors(check(km))


def test_chord_prefix_in_unrelated_contexts_is_fine() -> None:
    km = keymap({"input": {"c-x": "input.clear"}, "rail": {"c-x c-s": "session.compact"}})
    assert "chord-prefix" not in codes(check(km))


def test_two_chords_sharing_a_prefix_are_fine() -> None:
    km = keymap({"global": {"c-x c-s": "session.compact", "c-x c-c": "session.exit"}})
    assert not has_errors(check(km))


# ---------------------------------------------------------------------------
# §7 conflict: unknown action / declared contexts
# ---------------------------------------------------------------------------

ACTIONS = (
    ActionSpec("overlay.close", contexts=("overlay",)),
    ActionSpec("overlay.help", contexts=()),
    ActionSpec("session.interrupt", contexts=("global",)),
    ActionSpec("nav.up", contexts=("overlay", "rail")),
    ActionSpec("input.submit", contexts=("input",)),
    ActionSpec("text.insert", contexts=("input",), hidden=True),
)


def test_unknown_action_is_an_error_with_a_near_match_suggestion() -> None:
    km = keymap({"overlay": {"escape": "overlay.clos"}})
    diags = [d for d in check(km, ACTIONS) if d.code == "unknown-action"]
    assert len(diags) == 1 and diags[0].severity == "error"
    assert "overlay.clos" in diags[0].message
    assert "overlay.close" in diags[0].message
    with pytest.raises(UnknownActionError):
        raise_for_errors(check(km, ACTIONS))


def test_unknown_action_without_a_near_match_still_errors() -> None:
    km = keymap({"overlay": {"escape": "zzzzzzz"}})
    diags = [d for d in check(km, ACTIONS) if d.code == "unknown-action"]
    assert len(diags) == 1
    assert "zzzzzzz" in diags[0].message


def test_action_ids_are_not_checked_without_a_registry() -> None:
    km = keymap({"overlay": {"escape": "whatever.at.all"}})
    assert "unknown-action" not in codes(check(km))


def test_binding_an_action_outside_its_declared_contexts_is_an_error() -> None:
    km = keymap({"input": {"escape": "overlay.close"}})
    diags = [d for d in check(km, ACTIONS) if d.code == "action-context"]
    assert len(diags) == 1 and diags[0].severity == "error"
    assert "overlay.close" in diags[0].message
    assert "input" in diags[0].message and "overlay" in diags[0].message


def test_a_declared_context_covers_its_descendants() -> None:
    km = keymap({"overlay.picker": {"escape": "overlay.close"}})
    assert "action-context" not in codes(check(km, ACTIONS))


def test_an_action_declaring_no_contexts_binds_anywhere() -> None:
    km = keymap({"rail": {"?": "overlay.help"}})
    assert "action-context" not in codes(check(km, ACTIONS))


# ---------------------------------------------------------------------------
# §7 conflict: unreachable actions
# ---------------------------------------------------------------------------


def test_a_visible_action_with_no_binding_anywhere_is_reported() -> None:
    km = keymap({"overlay": {"escape": "overlay.close"}})
    diags = [d for d in check(km, ACTIONS) if d.code == "unreachable-action"]
    named = {d.message.split("'")[1] for d in diags}
    # Everything in the registry except the one that is bound, and except the
    # hidden one — a hidden action is bindable but not advertised, so having no
    # binding is its normal state.
    assert named == {"overlay.help", "session.interrupt", "nav.up", "input.submit"}
    assert all(d.severity == "warning" for d in diags)


def test_every_bound_action_is_reachable() -> None:
    km = keymap(
        {
            "global": {"?": "overlay.help", "c-c": "session.interrupt"},
            "overlay": {"escape": "overlay.close", "up": "nav.up"},
            "input": {"enter": "input.submit"},
        }
    )
    assert "unreachable-action" not in codes(check(km, ACTIONS))
    assert not has_errors(check(km, ACTIONS))


# ---------------------------------------------------------------------------
# §7 warning: terminal-intercepted keys
# ---------------------------------------------------------------------------


def test_flow_control_keys_warn_with_a_suggested_remedy() -> None:
    km = keymap({"global": {"c-s": "session.compact"}})
    diags = [d for d in check(km) if d.code == "terminal-intercept"]
    assert len(diags) == 1 and diags[0].severity == "warning"
    assert "c-s" in diags[0].message
    assert "stty" in diags[0].message or "flow control" in diags[0].message


def test_intercept_detection_is_injectable() -> None:
    km = keymap({"global": {"c-g": "session.exit"}})
    assert "terminal-intercept" not in codes(check(km))
    diags = check(km, intercepted={"c-g": "the multiplexer eats it"})
    assert "terminal-intercept" in codes(diags)


def test_intercept_fires_on_any_key_of_a_chord() -> None:
    km = keymap({"global": {"c-x c-s": "session.compact"}})
    assert "terminal-intercept" in codes(check(km))


# ---------------------------------------------------------------------------
# Diagnostics as a whole
# ---------------------------------------------------------------------------


def test_errors_sort_before_warnings() -> None:
    km = keymap(
        {
            "global": {"escape": "session.exit", "c-x": "session.new"},
            "overlay": {"escape": "overlay.close", "c-x c-s": "session.compact"},
        }
    )
    diags = check(km)
    severities = [d.severity for d in diags]
    assert severities == sorted(severities, key=lambda s: 0 if s == "error" else 1)
    assert has_errors(diags)


def test_diagnostic_str_is_greppable() -> None:
    km = keymap({"input": {"s-a": "input.attach", "A": "input.clear"}})
    text = str(check(km)[0])
    assert text.startswith("error")
    assert "duplicate-binding" in text
    assert "test:" in text


def test_a_clean_keymap_has_no_diagnostics() -> None:
    km = keymap(
        {
            "global": {"?": "overlay.help", "c-c": "session.interrupt"},
            "overlay": {"escape": "overlay.close", "up": "nav.up"},
            "input": {"enter": "input.submit"},
        }
    )
    assert check(km, ACTIONS) == ()


def test_module_parses_as_python_39() -> None:
    import ast
    import pathlib

    import mantis_agent.keys as pkg

    root = pathlib.Path(pkg.__file__).parent
    for path in sorted(root.glob("*.py")):
        ast.parse(path.read_text(), feature_version=(3, 9))
