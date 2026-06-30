"""Lock the 1.0 public API surface.

The set of names in ``mantis_agent.__all__`` is the SemVer-covered
public API per ``SEMVER.md``. This test compares it against the
snapshot in ``tests/public_api_surface.txt``.

A diff here means someone changed the surface — intentionally or not.
If the change is intentional:

  1. Decide MAJOR vs MINOR per SEMVER.md (removed/renamed = MAJOR,
     added = MINOR).
  2. Update ``tests/public_api_surface.txt`` to match.
  3. Update CHANGELOG.md.
  4. In the same commit. The diff in code review is the audit trail.

Other invariants enforced:
  * every name in __all__ resolves to an attribute on the package,
  * no name in __all__ is a bare ``_underscore`` name,
  * __all__ contains no duplicates,
  * every public name is also reachable as ``from mantis_agent import X``.
"""

from __future__ import annotations

from pathlib import Path

import mantis_agent

SURFACE_FILE = Path(__file__).parent / "public_api_surface.txt"


def _load_snapshot() -> set[str]:
    raw = SURFACE_FILE.read_text().splitlines()
    return {line.strip() for line in raw if line.strip() and not line.startswith("#")}


def test_snapshot_file_exists() -> None:
    assert SURFACE_FILE.exists(), (
        f"Missing snapshot at {SURFACE_FILE}. The 1.0 public surface freeze "
        f"depends on this file."
    )


def test_all_matches_snapshot() -> None:
    snapshot = _load_snapshot()
    live = set(mantis_agent.__all__)
    added = sorted(live - snapshot)
    removed = sorted(snapshot - live)
    msg_parts: list[str] = []
    if added:
        msg_parts.append(
            "ADDED to __all__ (MINOR bump candidates):\n  + " + "\n  + ".join(added)
        )
    if removed:
        msg_parts.append(
            "REMOVED from __all__ (MAJOR bump candidates):\n  - " + "\n  - ".join(removed)
        )
    if msg_parts:
        msg_parts.append(
            "\nTo accept this change: update tests/public_api_surface.txt and "
            "CHANGELOG.md in the same commit. See SEMVER.md for the bump rules."
        )
        raise AssertionError("\n\n".join(msg_parts))


def test_all_entries_are_attributes() -> None:
    """Every name we promise must actually be importable."""
    missing = [name for name in mantis_agent.__all__ if not hasattr(mantis_agent, name)]
    assert not missing, (
        f"Names in __all__ that are not attributes of mantis_agent: {missing}"
    )


def test_all_has_no_duplicates() -> None:
    seen: set[str] = set()
    dupes: list[str] = []
    for name in mantis_agent.__all__:
        if name in seen:
            dupes.append(name)
        seen.add(name)
    assert not dupes, f"Duplicate entries in __all__: {dupes}"


def test_all_has_no_private_names() -> None:
    """``_underscore`` names shouldn't appear in __all__ — they're conventionally private."""
    private = [n for n in mantis_agent.__all__ if n.startswith("_") and n != "__version__"]
    assert not private, (
        f"__all__ should not contain private (_underscore) names: {private}"
    )


def test_all_is_importable_via_star() -> None:
    """``from mantis_agent import X`` works for every name in __all__."""
    failures: list[tuple[str, str]] = []
    for name in mantis_agent.__all__:
        try:
            obj = getattr(mantis_agent, name)
            assert obj is not None or name == "__version__"  # __version__ is str, fine
        except Exception as exc:  # pragma: no cover - guarded above
            failures.append((name, repr(exc)))
    assert not failures, f"Failed to resolve some public names: {failures}"


def test_snapshot_is_alphabetized() -> None:
    """Keep the snapshot file alphabetized so diffs are reviewable."""
    raw = [
        line.strip()
        for line in SURFACE_FILE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert raw == sorted(raw), (
        "tests/public_api_surface.txt is not alphabetized. Regenerate with:\n"
        '  python -c "import mantis_agent; '
        "print('\\n'.join(sorted(mantis_agent.__all__)))"
        '" > tests/public_api_surface.txt'
    )
