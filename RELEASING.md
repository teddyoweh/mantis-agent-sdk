# Releasing `mantis-agent-sdk`

This file is the maintainer-facing runbook. End users don't need it.

## How releases work

1. Tags shaped `vX.Y.Z` push to GitHub.
2. The `release.yml` workflow runs:
   - the full test suite on Python 3.11/3.12/3.13,
   - `python -m build` (sdist + wheel),
   - upload to PyPI via **trusted publishing** (OIDC), no token in repo.
3. A GitHub Release is drafted with the matching CHANGELOG section.

The version in `pyproject.toml` is the single source of truth.
`mantis_agent.__version__` reads it back via `importlib.metadata`.

## Cutting a release

```bash
# 1. Make sure main is green.
git checkout main && git pull
python -m pytest

# 2. Pick the next version per SEMVER.md.
#    MAJOR: removed/renamed a name in __all__, or broke a signature.
#    MINOR: added to __all__, added optional params, new providers/events.
#    PATCH: bug fixes only, no surface change.
NEXT=1.0.1   # example

# 3. Bump pyproject.toml and move the [Unreleased] CHANGELOG section
#    into a new [X.Y.Z] — YYYY-MM-DD heading, then add the compare link.
$EDITOR pyproject.toml CHANGELOG.md

# 4. Make sure tests still pass after the surface check sees the new __all__.
python -m pytest

# 5. Commit, tag, push.
git add pyproject.toml CHANGELOG.md
git commit -m "release: v$NEXT"
git tag -a "v$NEXT" -m "v$NEXT"
git push origin main --follow-tags
```

Pushing the `v$NEXT` tag triggers the publish workflow. Watch the
**Actions** tab until it goes green; the package will appear on
[PyPI](https://pypi.org/project/mantis-agent-sdk/) within a minute or two.

## PyPI trusted publisher (one-time setup)

On https://pypi.org/manage/project/mantis-agent-sdk/settings/publishing/, add
a trusted publisher with:

- Owner: `teddyoweh`
- Repository name: `mantis-agent-sdk`
- Workflow name: `release.yml`
- Environment name: `pypi`

This grants the workflow short-lived OIDC tokens via `id-token: write`.
No long-lived API key lives in the repo or in GitHub secrets.

## TestPyPI dry runs

To validate the build without publishing for real:

```bash
git tag -a "v1.0.0rc1" -m "v1.0.0rc1"
git push origin v1.0.0rc1
```

The workflow detects the pre-release suffix (`rc`, `a`, `b`, `dev`) and
publishes to TestPyPI instead of PyPI. Install from there to sanity-check:

```bash
pip install --index-url https://test.pypi.org/simple/ mantis-agent-sdk==1.0.0rc1
```

## Yanking a bad release

If you ship something broken:

```bash
# Don't delete — yank.
pip install twine
twine upload --skip-existing  # only needed if re-uploading the same version (forbidden by PyPI)

# Use the PyPI web UI to "yank" the version. It stays resolvable for
# pinned installs but disappears from default `pip install`.
```

Then immediately cut a PATCH release with the fix.

## What the surface freeze means for releases

`tests/test_public_api_surface.py` enforces that `mantis_agent.__all__`
matches `tests/public_api_surface.txt`. If you intend to add or remove
a public name:

1. Edit `__all__`.
2. Run the test — it will fail with a diff.
3. Update the fixture file (or accept the failure as proof the change
   was unintentional and revert).
4. Decide the bump per SEMVER.md.

This keeps API drift visible in code review.
