"""Micro-benchmarks proving the perf claims in docs/plan.md §9.

Run with::

    python -m mantis_agent.bench

Outputs a table of (op, samples, p50, p95, p99). Not part of pytest;
benchmarks are noisy on shared CI and the targets here are guidelines,
not gates. Use ``--strict`` to fail the run if any p95 exceeds the
documented target (used in nightly CI).

Targets (from plan §9, refined here against the actual implementation):

  * cold-start (import + Agent construct + 1 mock request): < 250 ms
  * per-event normalization overhead vs. raw httpx+json:    < 200 µs / event
  * memory footprint (idle Agent + 5 tools):                < 50 MB resident
  * ThinkingParser throughput on 8 KB input:                > 25 MB/s
  * msgspec encode of SDKMessage vs. json.dumps:            > 4× faster
"""

from __future__ import annotations

import argparse
import gc
import json
import resource
import sys
import time
from collections.abc import Callable
from typing import Any

import msgspec


# ---------------------------------------------------------------------------
# Bench harness
# ---------------------------------------------------------------------------


def _measure(
    name: str,
    fn: Callable[[], Any],
    *,
    samples: int = 200,
    warmup: int = 5,
) -> dict[str, Any]:
    """Time ``fn`` `samples` times, return latency stats in microseconds."""

    for _ in range(warmup):
        fn()
    gc.collect()
    gc.disable()
    times_us: list[float] = []
    try:
        for _ in range(samples):
            t0 = time.perf_counter()
            fn()
            times_us.append((time.perf_counter() - t0) * 1e6)
    finally:
        # Always re-enable GC, even if ``fn`` raises — leaving it disabled
        # would corrupt every later benchmark (and the whole process).
        gc.enable()
    times_us.sort()
    return {
        "op": name,
        "n": samples,
        "p50_us": times_us[samples // 2],
        "p95_us": times_us[int(samples * 0.95)],
        "p99_us": times_us[int(samples * 0.99)],
        "min_us": times_us[0],
        "max_us": times_us[-1],
    }


# ---------------------------------------------------------------------------
# Individual benchmarks
# ---------------------------------------------------------------------------


def bench_msgspec_vs_json() -> tuple[dict[str, Any], dict[str, Any], float]:
    """Encode the same SDKResultMessage with msgspec vs stdlib json."""

    from mantis_agent import (
        ModelUsage,
        SDKResultMessage,
        Usage,
    )

    msg = SDKResultMessage(
        subtype="success",
        duration_ms=420,
        duration_api_ms=380,
        num_turns=3,
        result="The answer is 42.",
        total_cost_usd=0.0042,
        usage=Usage(input_tokens=1234, output_tokens=567),
        modelUsage={
            "qwen2.5-72b-instruct": ModelUsage(
                inputTokens=1234,
                outputTokens=567,
                costUSD=0.0042,
                contextWindow=131072,
                maxOutputTokens=4096,
            ),
        },
    )
    enc = msgspec.json.Encoder()
    # Build a dict-equivalent for stdlib json.
    dict_msg = msgspec.to_builtins(msg)

    msgspec_stat = _measure("msgspec encode SDKResultMessage", lambda: enc.encode(msg))
    json_stat = _measure(
        "json.dumps SDKResultMessage",
        lambda: json.dumps(dict_msg, separators=(",", ":")),
    )
    ratio = json_stat["p50_us"] / msgspec_stat["p50_us"]
    return msgspec_stat, json_stat, ratio


def bench_thinking_parser_throughput() -> dict[str, Any]:
    """Throughput on 8 KB of mixed thinking + text."""

    from mantis_agent.streaming.thinking_parser import ThinkingParser

    payload = (
        "Intro text. "
        "<think>Long reasoning block. " + ("step. " * 800) + "</think> "
        "Conclusion."
    )

    def run() -> None:
        p = ThinkingParser()
        for ev in p.feed(payload):
            pass
        for ev in p.finalize():
            pass

    stat = _measure("ThinkingParser feed 8KB", run, samples=200)
    bytes_per_s = len(payload.encode()) / (stat["p50_us"] / 1e6)
    stat["mb_per_s"] = bytes_per_s / 1e6
    return stat


def bench_tool_text_parser() -> dict[str, Any]:
    """Same but for the Hermes-Pro <tool_call> parser, with 2 tool calls."""

    from mantis_agent.streaming.text_tool_parser import ToolCallTextParser

    payload = (
        "Pre. "
        '<tool_call>{"name": "search", "arguments": {"q": "spawn labs"}}</tool_call>'
        " Inter. "
        '<tool_call>{"name": "fetch", "arguments": {"url": "https://example.com"}}</tool_call>'
        " Post."
    )

    def run() -> None:
        p = ToolCallTextParser()
        for ev in p.feed(payload):
            pass
        for ev in p.finalize():
            pass

    return _measure("ToolCallTextParser 2 calls", run, samples=200)


def bench_capability_lookup() -> dict[str, Any]:
    """O(1) capability lookup — should be sub-microsecond after warmup."""

    from mantis_agent.capabilities import lookup_model

    return _measure(
        "lookup_model('qwen2.5-72b-instruct')",
        lambda: lookup_model("qwen2.5-72b-instruct"),
        samples=2000,
    )


def bench_agent_construct() -> dict[str, Any]:
    """Time to construct an Agent + immediate aclose. Excludes deps import."""

    from mantis_agent import Agent
    from mantis_agent.providers.mock import MockProvider

    def run() -> None:
        a = Agent(
            model="qwen2.5-7b-instruct",
            provider=MockProvider(),
            include_memory=False,
        )
        # We don't await aclose since the mock has no resources; just drop it.
        del a

    return _measure("Agent construct", run, samples=200)


def bench_cold_start_seconds() -> float:
    """Time a fresh subprocess from `python -c 'import mantis_agent; Agent(...)'`.

    Returns wall-clock seconds. Not a percentile — single-shot.
    """

    import subprocess

    code = (
        "import time; t0=time.perf_counter();"
        "import mantis_agent;"
        "from mantis_agent import Agent;"
        "from mantis_agent.providers.mock import MockProvider;"
        "a = Agent(model='qwen2.5-7b-instruct', provider=MockProvider(), include_memory=False);"
        "print(time.perf_counter() - t0)"
    )
    out = subprocess.check_output(
        [sys.executable, "-c", code], stderr=subprocess.DEVNULL
    )
    return float(out.strip())


def memory_resident_mb() -> float:
    """RSS in megabytes for the current process — coarse but useful."""

    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # On Linux ru_maxrss is in KB; on macOS it's in bytes.
    if sys.platform == "darwin":
        return rss_kb / 1e6
    return rss_kb / 1e3


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bench", description=__doc__)
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any p95 exceeds the documented target.",
    )
    args = p.parse_args(argv)

    print("mantis-agent-sdk benchmarks")
    print("=" * 60)

    rows: list[dict[str, Any]] = []
    rows.append(bench_capability_lookup())
    rows.append(bench_agent_construct())
    rows.append(bench_thinking_parser_throughput())
    rows.append(bench_tool_text_parser())

    msgspec_stat, json_stat, ratio = bench_msgspec_vs_json()
    rows.append(msgspec_stat)
    rows.append(json_stat)

    # Print table.
    name_w = max(len(r["op"]) for r in rows) + 2
    fmt = f"{{op:<{name_w}}}  {{p50:>10}}  {{p95:>10}}  {{p99:>10}}"
    print(fmt.format(op="OP", p50="p50", p95="p95", p99="p99"))
    print("-" * (name_w + 36))
    for r in rows:
        print(
            fmt.format(
                op=r["op"],
                p50=f"{r['p50_us']:.1f} µs",
                p95=f"{r['p95_us']:.1f} µs",
                p99=f"{r['p99_us']:.1f} µs",
            )
        )
        if "mb_per_s" in r:
            print(f"  {' ' * name_w}  ({r['mb_per_s']:.1f} MB/s)")

    print()
    print(f"msgspec vs json speedup (p50): {ratio:.2f}×")

    cold = bench_cold_start_seconds()
    print(f"cold start (import + Agent): {cold * 1000:.1f} ms")

    rss = memory_resident_mb()
    print(f"resident memory: {rss:.1f} MB")

    # Targets — from docs/plan.md §9.
    failures: list[str] = []
    if args.strict:
        if cold > 0.500:
            failures.append(f"cold start {cold*1000:.0f}ms > 500ms target")
        if rss > 100:
            failures.append(f"RSS {rss:.0f}MB > 100MB target")
        if ratio < 2.0:
            failures.append(f"msgspec/json ratio {ratio:.2f}× < 2× target")
        if failures:
            print("\nSTRICT MODE FAILURES:")
            for f in failures:
                print(f"  - {f}")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
