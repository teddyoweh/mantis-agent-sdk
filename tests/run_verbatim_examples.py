"""Run each verbatim-ported Claude SDK example against a real local
model and report pass/fail + output snippet.

Not a pytest run — examples take 10-60s each on CPU inference and we
don't want them blocking CI. Run manually::

    ollama pull deepseek-r1:1.5b
    MANTIS_AGENT_MODEL=deepseek-r1:1.5b \\
    MANTIS_AGENT_BASE_URL=http://localhost:11434 \\
    python tests/run_verbatim_examples.py

Exit code is non-zero if any example crashes (NOT if the model gives a
poor answer — small models are flaky and that's not the SDK's fault).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "mantis_agent" / "examples"


# Each entry: (filename, max_seconds, [args]). Args are typically empty
# but mcp_calculator runs through 6 prompts and we want a strict cap.
EXAMPLES_TO_RUN: tuple[tuple[str, int], ...] = (
    ("quick_start.py", 90),
    ("system_prompt.py", 60),
    ("tools_option.py", 90),
    ("max_budget_usd.py", 60),
    ("stderr_callback_example.py", 60),
    ("mcp_calculator.py", 180),
)


def run(name: str, deadline_s: int) -> tuple[str, str]:
    """Run one example. Returns (status, summary)."""

    path = EXAMPLES_DIR / name
    env = dict(os.environ)
    env.setdefault("MANTIS_AGENT_MODEL", "deepseek-r1:1.5b")
    env.setdefault("MANTIS_AGENT_BASE_URL", "http://localhost:11434")
    # The subprocess starts fresh — make sure it can import the in-tree
    # package without ``pip install -e .`` being a prereq.
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{REPO_ROOT}:{existing_path}" if existing_path else str(REPO_ROOT)
    )

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            env=env,
            capture_output=True,
            timeout=deadline_s,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT", f"hit {deadline_s}s wall — model too slow on CPU"

    dur = time.monotonic() - t0
    if proc.returncode != 0:
        # Surface the last 8 lines of stderr — that's where Python tracebacks land.
        tail = "\n".join((proc.stderr or "").splitlines()[-8:])
        return "FAIL", f"exit={proc.returncode} in {dur:.1f}s\n{tail}"

    # Pass: grab a 1-line summary from stdout.
    head = next(
        (line for line in (proc.stdout or "").splitlines() if line.strip()),
        "(no stdout)",
    )
    return "PASS", f"{dur:.1f}s · {head[:140]}"


def main() -> int:
    print(
        f"Running {len(EXAMPLES_TO_RUN)} verbatim Claude SDK examples "
        f"against model={os.environ.get('MANTIS_AGENT_MODEL', 'deepseek-r1:1.5b')}, "
        f"backend={os.environ.get('MANTIS_AGENT_BASE_URL', 'http://localhost:11434')}\n"
    )

    rows: list[tuple[str, str, str]] = []
    fails = 0
    for name, deadline in EXAMPLES_TO_RUN:
        status, summary = run(name, deadline)
        rows.append((name, status, summary))
        marker = "✓" if status == "PASS" else "✗"
        print(f"  {marker} {name:<32} [{status}] {summary[:120]}")
        if status not in ("PASS", "TIMEOUT"):
            fails += 1
            # Print the full stderr tail for fail diagnostic.
            print("    ---")
            for line in summary.splitlines()[1:]:
                print(f"    {line}")
            print("    ---")

    print()
    print(f"Summary: {sum(1 for _, s, _ in rows if s == 'PASS')} pass, "
          f"{sum(1 for _, s, _ in rows if s == 'FAIL')} fail, "
          f"{sum(1 for _, s, _ in rows if s == 'TIMEOUT')} timeout")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
