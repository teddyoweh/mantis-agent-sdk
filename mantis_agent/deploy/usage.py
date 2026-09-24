"""What a deployment actually did, read from its own container log.

The numbers a GPU endpoint's owner wants — how fast it generates, how busy it
is, how long it spent booting versus serving, what that cost per token — are
all in the log vLLM already writes:

* every ~10 s of activity, one stats line (prompt and generation tokens/s,
  running / waiting requests, KV-cache use);
* one access line per request, with its status code;
* the container's life: Modal's tunnel opening (a container started), vLLM's
  "Application startup complete" (it can serve), and the tunnel closing /
  the server shutting down (it scaled away).

Reading the log costs nothing and — unlike scraping ``/metrics`` over HTTP —
never wakes a scaled-to-zero app or resets its idle timer, which on a GPU
billed by the hour is the whole point. Pure functions; no I/O here.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# One stats window vLLM averages over; each stats line stands for this long.
STATS_INTERVAL_S = 10.0

_VLLM_TS = re.compile(r"\b(?:INFO|WARNING|ERROR|DEBUG|CRITICAL) (\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\b")
_MODAL_TS = re.compile(r"^\[modal-client\] (\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)")
_STATS = re.compile(
    r"Avg prompt throughput: ([\d.]+) tokens/s, Avg generation throughput: ([\d.]+) tokens/s, "
    r"Running: (\d+) reqs, Waiting: (\d+) reqs(?:, GPU KV cache usage: ([\d.]+)%)?")
_REQ = re.compile(r'"(?:POST|GET) (/v1/(?:chat/completions|completions|responses|embeddings))[^"]*" (\d{3})')
_OPEN = ("Server tunnel opened",)
_READY = ("Application startup complete",)
_CLOSE = ("Closing tunnel", "Shutting down FastAPI HTTP server")


@dataclass
class Session:
    """One container's life: started → could serve → went away."""

    start: float
    ready: float | None = None
    end: float | None = None
    requests: int = 0


@dataclass
class Parsed:
    sessions: list[Session] = field(default_factory=list)
    # (t, prompt tok/s, generation tok/s, running, waiting, kv %)
    stats: list[tuple[float, float, float, int, int, float | None]] = field(default_factory=list)
    requests: list[tuple[float, int]] = field(default_factory=list)   # (t, status)
    last_t: float | None = None


def _utc(y: int, mo: int, d: int, h: int, mi: int, s: int) -> float | None:
    try:
        return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def parse(lines: list[str], now: float | None = None) -> Parsed:
    """Everything the log says, in order. vLLM stamps ``MM-DD HH:MM:SS`` with
    no year (the container clock is UTC); the year comes from Modal's own
    ISO stamps, or ``now`` — and a date that lands in the future belongs to
    last year. Lines without a stamp (uvicorn's access log) take the time of
    the stamped line before them."""
    now = time.time() if now is None else now
    year = datetime.fromtimestamp(now, timezone.utc).year
    out = Parsed()
    t: float | None = None
    cur: Session | None = None

    def close(at: float | None) -> None:
        nonlocal cur
        if cur is not None and cur.end is None:
            cur.end = at if at is not None else t
        cur = None

    for raw in lines:
        line = str(raw)
        m = _MODAL_TS.search(line)
        if m:
            y, mo, d, h, mi, s = (int(x) for x in m.groups())
            year = y
            t = _utc(y, mo, d, h, mi, s) or t
        else:
            m = _VLLM_TS.search(line)
            if m:
                mo, d, h, mi, s = (int(x) for x in m.groups())
                ts = _utc(year, mo, d, h, mi, s)
                if ts is not None and ts > now + 86400:
                    ts = _utc(year - 1, mo, d, h, mi, s)
                t = ts or t
        if t is None:
            continue
        out.last_t = t if out.last_t is None else max(out.last_t, t)
        if any(k in line for k in _OPEN):
            close(t)
            cur = Session(start=t)
            out.sessions.append(cur)
            continue
        if cur is not None and cur.ready is None and any(k in line for k in _READY):
            cur.ready = t
            continue
        if any(k in line for k in _CLOSE):
            close(t)
            continue
        m = _STATS.search(line)
        if m:
            kv = float(m.group(5)) if m.group(5) is not None else None
            out.stats.append((t, float(m.group(1)), float(m.group(2)), int(m.group(3)), int(m.group(4)), kv))
            continue
        m = _REQ.search(line)
        if m:
            out.requests.append((t, int(m.group(2))))
            if cur is not None:
                cur.requests += 1
    return out


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def summarize(p: Parsed, *, since: float, until: float, live: bool,
              rate_per_hour: float | None) -> dict[str, Any]:
    """The window [since, until], as the page draws it.

    ``live`` says a container is up right now (from the control plane, never
    from the endpoint), so the last session is still open and runs to
    ``until``. A session the log never closed while nothing is up ended at its
    last line — the log is all we know."""
    sessions: list[dict[str, Any]] = []
    gpu_s = serve_s = 0.0
    boots: list[float] = []
    for i, s in enumerate(p.sessions):
        last = i == len(p.sessions) - 1
        end = s.end if s.end is not None else (until if (last and live) else (p.last_t or s.start))
        if last and live and s.end is not None and s.end < s.start:
            end = until
        if end < since or s.start > until:
            continue
        gpu_s += _overlap(s.start, end, since, until)
        if s.ready is not None:
            serve_s += _overlap(s.ready, end, since, until)
            boots.append(s.ready - s.start)
        sessions.append({"start": s.start, "ready": s.ready, "end": end,
                         "open": bool(last and live and s.end is None),
                         "boot_s": (s.ready - s.start) if s.ready is not None else None,
                         "requests": s.requests})
    stats = [x for x in p.stats if since <= x[0] <= until]
    reqs = [x for x in p.requests if since <= x[0] <= until]
    tok_in = sum(x[1] for x in stats) * STATS_INTERVAL_S
    tok_out = sum(x[2] for x in stats) * STATS_INTERVAL_S
    cost = (gpu_s / 3600.0 * rate_per_hour) if rate_per_hour is not None else None
    boots.sort()
    median_boot = boots[len(boots) // 2] if boots else None
    recent = [x for x in stats if x[0] >= until - 60]
    return {
        "since": since, "until": until, "live": live,
        "sessions": sessions,
        "stats": [[round(x[0], 1), x[1], x[2], x[3], x[4], x[5]] for x in stats],
        "requests": [[round(x[0], 1), x[1]] for x in reqs],
        "summary": {
            "gpu_s": round(gpu_s, 1), "serving_s": round(serve_s, 1),
            "booting_s": round(max(0.0, gpu_s - serve_s), 1),
            "cost_usd": round(cost, 2) if cost is not None else None,
            "rate_per_hour": rate_per_hour,
            "tokens_in": int(tok_in), "tokens_out": int(tok_out),
            "usd_per_m_out": round(cost / (tok_out / 1e6), 2) if (cost and tok_out) else None,
            "requests": len(reqs), "errors": sum(1 for x in reqs if x[1] >= 400),
            "wakes": sum(1 for s in sessions if s["start"] >= since),
            "median_boot_s": round(median_boot, 1) if median_boot is not None else None,
            "gen_tps_now": recent[-1][2] if recent else 0.0,
            "gen_tps_peak": max((x[2] for x in stats), default=0.0),
            "running_now": recent[-1][3] if recent else 0,
        },
    }
