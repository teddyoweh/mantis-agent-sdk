#!/usr/bin/env bash
# Run the test suite across every Python version CI covers.
#
# WHY THIS EXISTS: the obvious one-liner is destructive.
#
#     for py in 3.10 3.11 3.12; do uv run --python "$py" ... ; done   # DON'T
#
# `uv run --python X` rebuilds the PROJECT environment — `.venv/` — in place.
# Run that loop and your dev venv is silently replaced by whichever version
# went last, and anything else using `.venv` at the time (an editor, a watcher,
# another agent session, a suite you are running in parallel) starts failing on
# half-installed site-packages. Those failures look exactly like real test
# failures, which makes the matrix results untrustworthy in both directions.
#
# So: give every version its own environment under `.venvs/`, and never touch
# `.venv/`.
#
# Usage:
#   scripts/test-matrix.sh                    # every version CI runs
#   scripts/test-matrix.sh 3.12 3.13          # just these
#   PYTEST_ARGS="-x -k mcp" scripts/test-matrix.sh 3.12
set -uo pipefail

cd "$(dirname "$0")/.."

# Keep in step with .github/workflows/test.yml.
DEFAULT_VERSIONS=(3.9 3.10 3.11 3.12 3.13 3.14)
VERSIONS=("${@:-}")
[ -z "${VERSIONS[*]}" ] && VERSIONS=("${DEFAULT_VERSIONS[@]}")

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found — install it: https://docs.astral.sh/uv/" >&2
    exit 127
fi

mkdir -p .venvs
export MANTIS_AGENT_MOCK=1

failed=()
passed=()

for py in "${VERSIONS[@]}"; do
    printf '\n\033[1m═══ python %s ═══\033[0m\n' "$py"
    # The isolation that makes this safe. Each version owns its own tree, so
    # nothing here can disturb `.venv` or another version's run.
    export UV_PROJECT_ENVIRONMENT=".venvs/py${py}"
    if uv run --python "$py" --extra dev python -m pytest -q ${PYTEST_ARGS:-}; then
        passed+=("$py")
    else
        failed+=("$py")
    fi
done

printf '\n\033[1m═══ matrix ═══\033[0m\n'
[ ${#passed[@]} -gt 0 ] && printf '\033[32m  pass\033[0m  %s\n' "${passed[*]}"
[ ${#failed[@]} -gt 0 ] && printf '\033[31m  FAIL\033[0m  %s\n' "${failed[*]}"
# `.venv` is deliberately untouched — say so, because the whole point of this
# script is that the naive loop does not leave it that way.
printf '  envs under .venvs/ · your .venv is untouched\n'

[ ${#failed[@]} -eq 0 ] || exit 1
