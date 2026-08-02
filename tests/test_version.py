"""Verify the version contract.

`mantis_agent.__version__` is the SemVer label users see. It must:
  * be a non-empty string,
  * match the version declared in ``pyproject.toml``,
  * parse as semver (MAJOR.MINOR.PATCH with optional pre-release/build),
  * be in the 1.x line (we shipped 1.0.0; downgrades are accidents),
  * be reachable both as an attribute and as a member of __all__.
"""

from __future__ import annotations

import re
import sys
from importlib import metadata as importlib_metadata

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib
from pathlib import Path

import mantis_agent


# Strict SemVer 2.0.0 regex, lifted from https://semver.org/#is-there-a-suggested-regular-expression-regex-to-check-a-semver-string
SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def _pyproject_version() -> str:
    """Read [project].version straight from pyproject.toml."""
    root = Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text())
    return data["project"]["version"]


def test_version_is_non_empty_string() -> None:
    assert isinstance(mantis_agent.__version__, str)
    assert mantis_agent.__version__.strip() == mantis_agent.__version__
    assert mantis_agent.__version__ != ""


def test_version_matches_pyproject() -> None:
    """The shipped version must match the build manifest."""
    assert mantis_agent.__version__ == _pyproject_version(), (
        f"mantis_agent.__version__ ({mantis_agent.__version__!r}) drifted from "
        f"pyproject.toml ({_pyproject_version()!r}). Update both together."
    )


def test_version_matches_installed_metadata() -> None:
    """importlib.metadata is the source of truth at runtime."""
    assert importlib_metadata.version("mantis-agent-sdk") == mantis_agent.__version__


def test_version_is_valid_semver() -> None:
    assert SEMVER_RE.match(mantis_agent.__version__), (
        f"{mantis_agent.__version__!r} is not valid SemVer 2.0.0"
    )


def test_version_is_at_least_1_0_0() -> None:
    """We don't accidentally downgrade past 1.0."""
    m = SEMVER_RE.match(mantis_agent.__version__)
    assert m is not None
    major = int(m.group("major"))
    assert major >= 1, (
        f"Version {mantis_agent.__version__!r} dropped back below 1.x. "
        f"1.0.0 was the stable cut; only forward MAJOR bumps are allowed."
    )


def test_version_is_exported_in_all() -> None:
    assert "__version__" in mantis_agent.__all__


def test_version_detector_has_fallback() -> None:
    """The literal fallback in _detect_version() must itself be valid 1.x SemVer.

    Some environments (zipapps, frozen apps, certain editable installs without
    metadata) hit the fallback path. If someone hand-edits the literal to
    something invalid, the wheel will silently lie about its version. Catch
    that here by parsing the literal source.
    """
    src = Path(mantis_agent.__file__).read_text()
    # Find the fallback literal — it's the one inside _detect_version()'s except.
    fallback_match = re.search(
        r"def _detect_version.*?except.*?return\s+\"([^\"]+)\"",
        src,
        flags=re.DOTALL,
    )
    assert fallback_match is not None, (
        "Could not locate the fallback literal in _detect_version()."
    )
    fallback = fallback_match.group(1)
    assert fallback == _pyproject_version(), (
        f"Fallback version literal {fallback!r} in _detect_version() is stale — it "
        f"must match pyproject [project].version ({_pyproject_version()!r}). Bump it "
        f"whenever you bump the release version."
    )
    assert SEMVER_RE.match(fallback), (
        f"Fallback version literal {fallback!r} in _detect_version() is not valid SemVer."
    )
    m2 = SEMVER_RE.match(fallback)
    assert m2 is not None
    assert int(m2.group("major")) >= 1


def test_python_requires_consistent_with_runtime() -> None:
    """The interpreter running the tests must satisfy pyproject's requires-python."""
    root = Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text())
    requires = data["project"]["requires-python"]
    # Verify the declared minimum by parsing the minor explicitly.
    assert requires.startswith(">="), requires
    declared = tuple(int(p) for p in requires.removeprefix(">=").strip().split("."))
    assert sys.version_info[: len(declared)] >= declared, (
        f"requires-python={requires} but tests are running on "
        f"{sys.version_info[:2]}"
    )
