"""Tests for the mkdocs-material documentation site.

Two layers:

1. **Structural** — pure-Python checks that don't need ``mkdocs``
   installed. Verify that ``mkdocs.yml`` is valid YAML with the
   right keys, that every nav entry points to a real file, that
   every internal link resolves, and that page frontmatter (if any)
   parses.

2. **Build** — calls ``mkdocs build --strict`` in a subprocess and
   asserts a non-zero rendering. Skipped automatically if ``mkdocs``
   isn't on PATH (so the suite still passes in CI environments that
   don't install the ``docs`` extra).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
MKDOCS_CONFIG = REPO_ROOT / "mkdocs.yml"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _load_mkdocs_config() -> dict:
    """Load mkdocs.yml. Use the project-pinned mkdocs if available so we
    pick up its custom !!python/name: tag loaders; fall back to a tag-
    tolerant PyYAML loader that ignores anything it doesn't recognise."""

    try:
        from mkdocs.config import load_config  # type: ignore

        return load_config(config_file=str(MKDOCS_CONFIG))
    except Exception:
        pass

    # A missing YAML parser must SKIP, not error. These three structural tests
    # are the only thing in the suite that needs one, and erroring made a bare
    # install look like three real test failures.
    yaml = pytest.importorskip(
        "yaml", reason="needs pyyaml (in the dev extra) or the docs extra")

    class _TolerantLoader(yaml.SafeLoader):
        pass

    def _ignore_unknown(loader, tag_suffix, node):  # noqa: ANN001
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
        return None

    _TolerantLoader.add_multi_constructor("!", _ignore_unknown)
    _TolerantLoader.add_multi_constructor("tag:yaml.org,2002:python/", _ignore_unknown)

    with MKDOCS_CONFIG.open("r", encoding="utf-8") as f:
        return yaml.load(f, Loader=_TolerantLoader)


def _walk_nav_entries(nav) -> list[str]:
    """Collect every ``foo/bar.md`` path referenced by the nav (recursive)."""

    out: list[str] = []

    def visit(item):  # noqa: ANN001
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for v in item.values():
                visit(v)
        elif isinstance(item, list):
            for v in item:
                visit(v)

    visit(nav)
    return out


_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _markdown_links(text: str) -> list[str]:
    return _LINK_RE.findall(text)


# ---------------------------------------------------------------------------
# structural tests
# ---------------------------------------------------------------------------


def test_mkdocs_config_exists() -> None:
    assert MKDOCS_CONFIG.is_file(), "mkdocs.yml missing at repo root"


def test_mkdocs_config_top_level_keys() -> None:
    cfg = _load_mkdocs_config()
    for key in ("site_name", "theme", "nav", "markdown_extensions"):
        assert key in cfg, f"mkdocs.yml missing top-level key {key!r}"
    # Material theme — accept any of: a plain string ('material'), a
    # raw dict {'name': 'material', ...} (when loaded via PyYAML), or
    # an mkdocs ``Theme`` object (when loaded via ``mkdocs.config``).
    theme = cfg["theme"]
    if isinstance(theme, str):
        theme_name = theme
    elif isinstance(theme, dict):
        theme_name = theme.get("name")
    else:
        theme_name = getattr(theme, "name", None)
    assert theme_name == "material", f"expected material theme, got {theme_name!r}"


def test_docs_dir_layout() -> None:
    assert DOCS_DIR.is_dir(), "docs/ directory missing"
    assert (DOCS_DIR / "index.md").is_file(), "docs/index.md missing"
    for subdir in ("getting-started", "guides", "api", "examples", "development"):
        assert (DOCS_DIR / subdir).is_dir(), f"docs/{subdir}/ missing"


def test_every_nav_entry_resolves_to_a_real_file() -> None:
    cfg = _load_mkdocs_config()
    nav = cfg["nav"]
    missing: list[str] = []
    for rel in _walk_nav_entries(nav):
        path = DOCS_DIR / rel
        if not path.is_file():
            missing.append(rel)
    assert not missing, f"nav entries point to missing files: {missing}"


def test_index_page_mentions_quickstart_link() -> None:
    body = (DOCS_DIR / "index.md").read_text(encoding="utf-8")
    # We don't assert on prose, only that the most-important next-step
    # link is present so the page actually wires to the rest of the site.
    assert "getting-started/quickstart.md" in body


def test_all_internal_markdown_links_resolve() -> None:
    """Every relative .md link in a docs page must resolve to an existing file."""

    broken: list[tuple[str, str]] = []
    for md_path in DOCS_DIR.rglob("*.md"):
        text = md_path.read_text(encoding="utf-8")
        for link in _markdown_links(text):
            # skip external, anchors, mailto
            if link.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # split off fragment
            target = link.split("#", 1)[0]
            if not target:
                continue
            # Only audit links that explicitly point at a markdown page —
            # the mkdocs site lives at a stable URL shape and the .md
            # form is the unambiguous internal-reference style.
            if not target.endswith(".md"):
                continue
            resolved = (md_path.parent / target).resolve()
            if not resolved.is_file():
                broken.append((str(md_path.relative_to(REPO_ROOT)), target))
    assert not broken, f"broken internal links: {broken}"


def test_every_md_under_docs_is_reachable_from_nav() -> None:
    """Reject orphan pages — anything under docs/ should be on the nav,
    otherwise a future contributor adds a doc and no one finds it."""

    cfg = _load_mkdocs_config()
    nav_entries = set(_walk_nav_entries(cfg["nav"]))
    orphans: list[str] = []
    for md_path in DOCS_DIR.rglob("*.md"):
        rel = md_path.relative_to(DOCS_DIR).as_posix()
        if rel not in nav_entries:
            orphans.append(rel)
    assert not orphans, f"orphan docs pages (add to nav): {orphans}"


# ---------------------------------------------------------------------------
# build test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("mkdocs") is None,
    reason="mkdocs not installed — install with `pip install mantis-agent-sdk[docs]`",
)
def test_mkdocs_build_clean(tmp_path) -> None:
    """``mkdocs build --strict`` exits 0 and produces an index.html."""

    site_dir = tmp_path / "site"
    proc = subprocess.run(
        [
            "mkdocs",
            "build",
            "--strict",
            "--config-file",
            str(MKDOCS_CONFIG),
            "--site-dir",
            str(site_dir),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )

    # ``--strict`` is the contract — mkdocs treats every warning as fatal,
    # so a 0 exit means no broken links, no missing pages, no untracked
    # files. If this fails, the captured stderr almost always names the
    # offending line.
    assert proc.returncode == 0, (
        f"mkdocs build --strict failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert (site_dir / "index.html").is_file(), "site/index.html not produced"
    # A few representative pages we want to confirm rendered.
    expected_pages = [
        "getting-started/quickstart/index.html",
        "guides/streaming/index.html",
        "api/options/index.html",
    ]
    for rel in expected_pages:
        assert (site_dir / rel).is_file(), f"missing rendered page {rel}"


def test_mkdocs_build_clean_subprocess_module_form() -> None:
    """Even if the ``mkdocs`` entry-point isn't on PATH, ``python -m
    mkdocs`` should work — important on Windows where script shims are
    flaky in CI containers. Skipped only if the mkdocs package itself
    isn't importable."""

    try:
        import mkdocs  # noqa: F401
    except ImportError:
        pytest.skip("mkdocs package not installed")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mkdocs",
            "build",
            "--config-file",
            str(MKDOCS_CONFIG),
            "--site-dir",
            str(REPO_ROOT / "site"),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    try:
        assert proc.returncode == 0, (
            f"python -m mkdocs build failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
        assert (REPO_ROOT / "site" / "index.html").is_file()
    finally:
        # Don't leave the site/ artifact lying around between test runs.
        shutil.rmtree(REPO_ROOT / "site", ignore_errors=True)
