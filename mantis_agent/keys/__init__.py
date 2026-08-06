"""Configurable keybindings — the pure core underneath the action layer.

Three modules land first, and they are pure: no ``prompt_toolkit``, no I/O, no
UI state. They are the part of the plan that has to be right before anything is
rewired, because every later piece (the action registry, the ``default`` preset
that must reproduce today's 77 bindings, help generation, ``mantis keys check``)
is a consumer of exactly this table.

* :mod:`~mantis_agent.keys.parse` — the key syntax (``c-x``, ``s-tab``,
  ``escape``, ``f7``, and space-separated chords like ``c-x c-s``), and the
  normalization that makes two spellings of one keystroke compare equal.
* :mod:`~mantis_agent.keys.keymap` — the document schema, ``extends``
  composition, explicit ``unbind``, the context stack, and resolution.
* :mod:`~mantis_agent.keys.conflicts` — every §7 check, returned as
  diagnostics that name both sides of whatever collided.

Still to come, and deliberately absent here: ``actions.py`` (the registry),
``help.py`` (generation), the shipped preset JSON, the ``prompt_toolkit``
bridge, and configuration loading. Nothing in this package imports the TUI, so
none of that can leak backwards into the core.
"""

from __future__ import annotations

from .conflicts import (
    DEFAULT_INTERCEPTED,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    ActionContextError,
    ActionSpec,
    BindingConflictError,
    ChordPrefixConflictError,
    Diagnostic,
    UnknownActionError,
    check,
    errors,
    has_errors,
    raise_for_errors,
    warnings,
)
from .keymap import (
    GLOBAL_CONTEXT,
    KEYMAP_VERSION,
    Binding,
    ExtendsCycleError,
    Keymap,
    KeymapDoc,
    KeymapSchemaError,
    KeymapVersionError,
    Resolution,
    Unbind,
    UnknownPresetError,
    compose,
    context_stack,
    expand_context,
    is_descendant,
    load_keymap,
    parse_keymap,
    preset_resolver,
)
from .parse import (
    KEY_ALIASES,
    NAMED_KEYS,
    Key,
    KeymapError,
    KeySequence,
    UnknownKeyError,
    format_key,
    format_sequence,
    label_key,
    label_sequence,
    parse_key,
    parse_sequence,
)

__all__ = [
    "DEFAULT_INTERCEPTED",
    "GLOBAL_CONTEXT",
    "KEYMAP_VERSION",
    "KEY_ALIASES",
    "NAMED_KEYS",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "ActionContextError",
    "ActionSpec",
    "Binding",
    "BindingConflictError",
    "ChordPrefixConflictError",
    "Diagnostic",
    "ExtendsCycleError",
    "Key",
    "KeySequence",
    "Keymap",
    "KeymapDoc",
    "KeymapError",
    "KeymapSchemaError",
    "KeymapVersionError",
    "Resolution",
    "Unbind",
    "UnknownActionError",
    "UnknownKeyError",
    "UnknownPresetError",
    "check",
    "compose",
    "context_stack",
    "errors",
    "expand_context",
    "format_key",
    "format_sequence",
    "has_errors",
    "is_descendant",
    "label_key",
    "label_sequence",
    "load_keymap",
    "parse_key",
    "parse_keymap",
    "parse_sequence",
    "preset_resolver",
    "raise_for_errors",
    "warnings",
]
