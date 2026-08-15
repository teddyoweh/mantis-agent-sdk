#!/usr/bin/env python3
"""Doc *coverage* checker — what exists in the code and appears in no doc?

``check_doc_snippets.py`` answers "is what we wrote true?". This answers the
other half, which is the one users actually feel: "is any of it written down?"
A reader who has to open the source to learn that ``fallback_model`` exists, or
that ``MANTIS_HOOKS_FAIL_CLOSED`` is the switch they need, is doing our job.

What counts as documented is deliberately generous — the name appearing
anywhere in either doc tree. This is a floor, not a quality bar: it catches
"nobody has ever mentioned this", not "this is explained well". Making the
floor strict enough to be a real gate would produce noise that gets ignored,
and an ignored gate is worse than none.

Surfaces checked
----------------

``export``
    Every name in ``mantis_agent.__all__``.

``option``
    Every ``MantisAgentOptions`` field.

``env``
    Every environment variable the package actually reads. Extracted by AST
    from ``os.environ.get("X")`` / ``os.environ["X"]`` / ``os.getenv("X")``, so
    f-string fragments like ``f"MANTIS_SUBAGENT_{name}"`` don't produce
    phantom entries.

``setting``
    Every ``settings.json`` key that changes behavior.

``hook_event``
    Every event name the ``hooks={...}`` dict form maps.

Deliberate omissions go in ``ALLOWLIST`` below, each with a reason. That list
is the honest record of what we've decided not to document — much better than
the same information living in nobody's head.

Usage
-----

    python scripts/check_doc_coverage.py            # report + exit 1 if gaps
    python scripts/check_doc_coverage.py --list     # just show the gaps
    python scripts/check_doc_coverage.py --json
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import json
import re
import sys
from pathlib import Path
from typing import Iterator, NamedTuple

REPO = Path(__file__).resolve().parent.parent
for _p in (str(REPO), str(REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DOC_ROOTS = ("docs", "web/content/docs")

#: Names we've decided not to document, and why. Anything here is a choice on
#: the record; anything not here and not documented is a gap.
ALLOWLIST: dict[str, str] = {
    # Internal plumbing users never construct themselves.
    "APIAssistantMessage": "wire-shape internal; users see AssistantMessage",
    "APIUserMessage": "wire-shape internal; users see UserMessage",
    "ClaudeHookContext": "Claude-SDK type alias, re-exported for imports only",
    "ClaudePermissionResult": "Claude-SDK type alias, re-exported for imports only",
    "normalize_response_format": "internal validator; the option is documented",
    "translate_response_format": "internal per-provider translation",
    "BUILTIN_AGENT_TYPES": "internal registry backing the documented agents option",
    # Terminal-only surface, documented in the terminal guide rather than the
    # SDK reference.
    "MANTIS_CLASSIC": "terminal UI switch, not an SDK knob",
    "MANTIS_VIM": "terminal UI switch, not an SDK knob",
    "MANTIS_NO_CLIPBOARD_HINT": "terminal UI switch, not an SDK knob",
    "MANTIS_TERM_WIDTH": "test/CI harness override for terminal measurement",
    "MANTIS_TERM_HEIGHT": "test/CI harness override for terminal measurement",
    "MANTIS_TERM_COLOR": "test/CI harness override for terminal measurement",
    "MANTIS_TERM_UNICODE": "test/CI harness override for terminal measurement",
    "MANTIS_TERM_TTY": "test/CI harness override for terminal measurement",
    "MANTIS_TERM_BOX": "test/CI harness override for terminal measurement",
    "MANTIS_SVG": "internal asset-generation switch",
    "MANTIS_CWD_9": "test fixture variable",
    "MANTIS_FS_SEED_AGENTS": "test fixture variable",
    "MANTIS_FS_SEED_MONITOR": "test fixture variable",
    "MANTIS_CAPABILITY_TIERS": "advisor tier table override, for tests",
    "MANTIS_MODEL": "read only by examples/exa_mcp.py, not by the package",
    "MANTIS_BACKEND": "read only by examples/exa_mcp.py, not by the package",
    # Settings keys for terminal-only affordances.
    "notifChannel": "terminal notification routing, documented in the terminal guide",
    "suggestNext": "terminal next-step suggestions, documented in the terminal guide",
}


class Gap(NamedTuple):
    surface: str
    name: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"[{self.surface}] {self.name}"


# ---------------------------------------------------------------------------
# What the docs mention
# ---------------------------------------------------------------------------


def _doc_blob() -> str:
    parts: list[str] = []
    for root in DOC_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        for pattern in ("*.md", "*.mdx"):
            for p in sorted(base.rglob(pattern)):
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


_BLOB: str | None = None


def mentioned(name: str) -> bool:
    global _BLOB
    if _BLOB is None:
        _BLOB = _doc_blob()
    return bool(re.search(r"\b" + re.escape(name) + r"\b", _BLOB))


# ---------------------------------------------------------------------------
# What the code has
# ---------------------------------------------------------------------------


def public_exports() -> list[str]:
    import mantis_agent

    return sorted(getattr(mantis_agent, "__all__", ()) or ())


def option_fields() -> list[str]:
    from mantis_agent import MantisAgentOptions

    return sorted(f.name for f in dataclasses.fields(MantisAgentOptions))


def env_vars() -> list[str]:
    """Environment variables the package really reads.

    AST-based on purpose: a regex over the source also matches the ``MANTIS_``
    half of an f-string and the prose inside docstrings, which invents
    variables that don't exist and makes the report untrustworthy.
    """

    found: set[str] = set()
    for py in sorted((REPO / "mantis_agent").rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        # Module-level `NAME = "MANTIS_…"` constants, so a variable read as
        # `os.environ.get(_FAIL_CLOSED_ENV)` still counts. Several modules keep
        # their env name in a constant, and missing those understates the
        # surface — which is the one failure mode that makes this check useless.
        consts: dict[str, str] = {}
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant) \
                    and isinstance(stmt.value.value, str) \
                    and stmt.value.value.startswith("MANTIS_"):
                for t in stmt.targets:
                    if isinstance(t, ast.Name):
                        consts[t.id] = stmt.value.value

        for node in ast.walk(tree):
            name: str | None = None
            # Any call whose first argument is a "MANTIS_…" literal (or a
            # constant holding one). Covers os.environ.get / os.getenv directly
            # *and* the package's own env helpers, which a receiver-based match
            # misses entirely. Literals and resolved constants only, so f-string
            # prefixes can't invent a variable.
            if isinstance(node, ast.Call) and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str) \
                        and first.value.startswith("MANTIS_"):
                    name = first.value
                elif isinstance(first, ast.Name) and first.id in consts:
                    name = consts[first.id]
            # os.environ["X"]
            elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                    and isinstance(node.slice.value, str):
                receiver = ast.unparse(node.value) if hasattr(ast, "unparse") else ""
                if receiver.endswith("environ"):
                    name = node.slice.value
            if name and name.startswith("MANTIS_"):
                found.add(name)
    return sorted(found)


def settings_keys() -> list[str]:
    """Settings keys that change behavior, per the snippet checker's probes."""

    import check_doc_snippets as C

    candidates = set(C._direct_settings_keys())
    # Flat keys the merge honors — probe the documented schema plus every
    # options field name, since the two overlap heavily.
    from mantis_agent import MantisAgentOptions

    for f in dataclasses.fields(MantisAgentOptions):
        candidates.add(f.name)
    candidates.update({
        "system_prompt", "permissions", "allowed_tools", "disallowed_tools",
        "mcp_servers", "max_budget_usd", "include_memory", "permission_mode",
        "env", "model", "backend", "max_turns", "max_tokens", "temperature",
    })
    return sorted(k for k in candidates if C._settings_key_is_honored(k))


def hook_events() -> list[str]:
    """Event names the dict form maps (probed, not hardcoded)."""

    import check_doc_snippets as C
    from mantis_agent.hooks import Hooks

    # Candidate spellings: PascalCase of every slot on the Hooks dataclass.
    out: list[str] = []
    for f in dataclasses.fields(Hooks):
        pascal = "".join(part.title() for part in f.name.split("_"))
        if C._hook_event_is_honored(pascal):
            out.append(pascal)
    return sorted(out)


SURFACES = {
    "export": public_exports,
    "option": option_fields,
    "env": env_vars,
    "setting": settings_keys,
    "hook_event": hook_events,
}


def run() -> list[Gap]:
    gaps: list[Gap] = []
    for surface, collect in SURFACES.items():
        for name in collect():
            if name in ALLOWLIST or mentioned(name):
                continue
            gaps.append(Gap(surface, name))
    return gaps


def iter_report(gaps: list[Gap]) -> Iterator[str]:
    by_surface: dict[str, list[str]] = {}
    for g in gaps:
        by_surface.setdefault(g.surface, []).append(g.name)
    for surface, collect in SURFACES.items():
        total = len([n for n in collect() if n not in ALLOWLIST])
        missing = by_surface.get(surface, [])
        covered = total - len(missing)
        pct = 100.0 if not total else 100.0 * covered / total
        yield f"{surface:12} {covered:3}/{total:<3} documented ({pct:5.1f}%)"
        for name in sorted(missing):
            yield f"               · {name}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true", help="gaps only, no summary")
    args = ap.parse_args(argv)

    gaps = run()
    if args.json:
        print(json.dumps([g._asdict() for g in gaps], indent=2))
    elif args.list:
        for g in gaps:
            print(g)
    else:
        for line in iter_report(gaps):
            print(line)
        print()
        print(
            f"{len(gaps)} undocumented name(s). Document them, or add to "
            f"ALLOWLIST in {Path(__file__).name} with a reason."
            if gaps else "every public surface is mentioned somewhere."
        )
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
