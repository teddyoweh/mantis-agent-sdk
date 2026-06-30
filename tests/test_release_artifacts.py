"""Verify the 1.0 release artifacts: SEMVER.md, CHANGELOG.md, RELEASING.md,
and the .github/workflows used to ship to PyPI.

These files are part of the release contract. If someone deletes them
or rips out a critical section, the SemVer guarantee weakens and the
publish pipeline breaks silently. Catch that here.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# --- docs --------------------------------------------------------------

def test_semver_md_exists_and_covers_policy() -> None:
    path = ROOT / "SEMVER.md"
    assert path.exists(), "SEMVER.md must exist — it documents the 1.0 guarantee."
    body = path.read_text()
    # Must mention semver, the public surface definition, and the bump rules.
    for needle in (
        "Semantic Versioning",
        "__all__",
        "MAJOR",
        "MINOR",
        "PATCH",
        "Deprecation",
    ):
        assert needle in body, f"SEMVER.md missing required section: {needle!r}"


def test_changelog_md_exists_and_has_release() -> None:
    path = ROOT / "CHANGELOG.md"
    assert path.exists(), "CHANGELOG.md must exist."
    body = path.read_text()
    # Must reference Keep a Changelog and SemVer.
    assert "Keep a Changelog" in body
    assert "Semantic Versioning" in body
    # Must contain a [1.0.0] heading.
    assert re.search(r"^## \[1\.0\.0\]", body, flags=re.MULTILINE), (
        "CHANGELOG.md must contain a `## [1.0.0]` heading."
    )
    # Must contain [Unreleased] for the next dev cycle.
    assert re.search(r"^## \[Unreleased\]", body, flags=re.MULTILINE), (
        "CHANGELOG.md must contain a `## [Unreleased]` heading."
    )


def test_releasing_md_exists() -> None:
    path = ROOT / "RELEASING.md"
    assert path.exists(), "RELEASING.md must exist for maintainers."
    body = path.read_text()
    for needle in ("trusted publishing", "git tag", "pyproject.toml"):
        assert needle in body, f"RELEASING.md missing required guidance: {needle!r}"


# --- workflows ---------------------------------------------------------

def test_release_workflow_exists_and_is_correct() -> None:
    path = ROOT / ".github" / "workflows" / "release.yml"
    assert path.exists(), ".github/workflows/release.yml must exist."
    body = path.read_text()
    # Tag trigger
    assert "tags:" in body
    assert re.search(r"v\[0-9\]\+\.\[0-9\]\+\.\[0-9\]\+", body), (
        "release.yml must trigger on vMAJOR.MINOR.PATCH-shaped tags."
    )
    # Trusted publishing requires id-token: write
    assert "id-token: write" in body, (
        "release.yml must grant 'id-token: write' for PyPI trusted publishing."
    )
    # PyPI publish action
    assert "pypa/gh-action-pypi-publish" in body
    # Both PyPI and TestPyPI environments
    assert "name: pypi" in body
    assert "name: testpypi" in body
    # Pre-publish test gate
    assert "needs: test" in body, (
        "release.yml must gate publish on the test job (needs: test)."
    )


def test_test_workflow_exists_and_matrixes_python() -> None:
    path = ROOT / ".github" / "workflows" / "test.yml"
    assert path.exists(), ".github/workflows/test.yml must exist."
    body = path.read_text()
    # Multi-version matrix
    for v in ("3.11", "3.12", "3.13"):
        assert v in body, f"test.yml must include Python {v} in the matrix."
    # Runs pytest
    assert "pytest" in body


# --- pyproject release-metadata --------------------------------------------

def test_pyproject_has_release_metadata() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    proj = data["project"]
    assert proj["name"] == "mantis-agent-sdk"
    assert proj["version"].startswith("1."), (
        f"pyproject.toml version {proj['version']!r} should be 1.x for the stable line."
    )
    # PyPI display fields
    assert proj.get("readme") == "README.md"
    assert proj.get("authors")
    assert proj.get("keywords")
    # Classifiers — production-ready + license + py versions
    classifiers = " ".join(proj.get("classifiers", []))
    assert "Development Status :: 5 - Production/Stable" in classifiers
    assert "License :: OSI Approved :: Apache Software License" in classifiers
    assert "Programming Language :: Python :: 3.11" in classifiers
    assert "Programming Language :: Python :: 3.12" in classifiers
    # URLs the PyPI page links from
    urls = proj.get("urls", {})
    assert "Homepage" in urls
    assert "Repository" in urls
    assert "Changelog" in urls


def test_hatchling_packages_only_the_sdk() -> None:
    """The wheel should ship mantis_agent and nothing else (no tests, no docs)."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel_pkgs = data.get("tool", {}).get("hatch", {}).get("build", {}).get(
        "targets", {}
    ).get("wheel", {}).get("packages")
    assert wheel_pkgs == ["mantis_agent"], (
        f"Wheel packages should be ['mantis_agent'], got {wheel_pkgs!r}"
    )


@pytest.mark.parametrize(
    "label",
    ["mantis_agent", "tests", "docs", "README.md", "LICENSE", "CHANGELOG.md", "SEMVER.md"],
)
def test_sdist_includes_critical_paths(label: str) -> None:
    """The sdist must carry tests + docs + policy files so source-installs are auditable."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    sdist_include = data.get("tool", {}).get("hatch", {}).get("build", {}).get(
        "targets", {}
    ).get("sdist", {}).get("include", [])
    assert any(label in entry for entry in sdist_include), (
        f"sdist include list missing {label!r}: {sdist_include}"
    )


# --- README -------------------------------------------------------------

def test_readme_roadmap_pypi_item_is_checked() -> None:
    """The PyPI 1.0 roadmap checkbox must be flipped once we ship this work."""
    body = (ROOT / "README.md").read_text()
    # The exact roadmap line — should be checked.
    assert re.search(
        r"^- \[x\] PyPI 1\.0 release with semver guarantee",
        body,
        flags=re.MULTILINE,
    ), (
        "README.md roadmap entry for 'PyPI 1.0 release with semver guarantee' is "
        "still unchecked. Flip it to [x] when shipping the release work."
    )
