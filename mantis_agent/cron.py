"""Scheduled agent runs — `/loop` that outlives the session.

``/watch`` and ``/loop`` are session-bound: close the terminal and they're
gone. This is the other half — jobs that live on disk and fire whether or not
you're at the keyboard:

    mantis cron add "every 30m" "triage new failures in the test suite"
    mantis cron add "daily 09:00" "summarize yesterday's commits" --godmode
    mantis cron list
    mantis cron tick        # run whatever is due, once (what the OS calls)
    mantis cron install     # register that tick with launchd / systemd

Each job runs through the same headless path as ``mantis -p``, in its own
directory, with its output kept in a per-run log. Two things are deliberate:

* **Nothing runs on import.** A job fires only from an explicit ``tick`` or
  ``daemon``, so a stray `mantis` launch never triggers a schedule.
* **Sandbox on by default.** These runs are unattended, which is exactly where
  "the user will approve it" stops being true. A job can opt out, loudly.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = [
    "CronJob",
    "ScheduleError",
    "add_job",
    "due_jobs",
    "install_scheduler",
    "jobs_path",
    "list_jobs",
    "load_jobs",
    "next_run_after",
    "parse_schedule",
    "remove_job",
    "run_job",
    "tick",
]


class ScheduleError(ValueError):
    """A schedule string we can't make sense of."""


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------
#
# Two dialects. The friendly one ("every 30m", "daily 09:00", "mon 09:00") is
# what people actually type; the 5-field cron expression is there because CI
# people already know it and expect it to work.

_EVERY = re.compile(r"^every\s+(\d+)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|"
                    r"h|hr|hrs|hours?|d|days?)$", re.I)
_DAILY = re.compile(r"^(?:daily|every\s+day)\s+(\d{1,2}):(\d{2})$", re.I)
_WEEKLY = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+(\d{1,2}):(\d{2})$", re.I)
_UNIT_SECONDS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
                 "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
                 "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
                 "d": 86400, "day": 86400, "days": 86400}
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_schedule(text: str) -> dict[str, Any]:
    """A schedule string → a normalized spec. Raises :class:`ScheduleError`.

    Kept as data (not a closure) so it round-trips through the JSON store and
    ``mantis cron list`` can explain a job without re-parsing prose.
    """
    s = (text or "").strip()
    if not s:
        raise ScheduleError("a schedule is required (e.g. \"every 30m\")")

    m = _EVERY.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        secs = n * _UNIT_SECONDS[unit if unit in _UNIT_SECONDS else unit.rstrip("s")]
        if secs < 60:
            raise ScheduleError("the shortest interval is 1 minute")
        return {"kind": "every", "seconds": secs, "text": s}

    m = _DAILY.match(s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        _check_clock(hh, mm)
        return {"kind": "daily", "hour": hh, "minute": mm, "text": s}

    m = _WEEKLY.match(s)
    if m:
        hh, mm = int(m.group(2)), int(m.group(3))
        _check_clock(hh, mm)
        return {"kind": "weekly", "weekday": _WEEKDAYS[m.group(1).lower()],
                "hour": hh, "minute": mm, "text": s}

    parts = s.split()
    if len(parts) == 5:
        for i, (field_text, lo, hi) in enumerate(zip(
                parts, (0, 0, 1, 1, 0), (59, 23, 31, 12, 6))):
            _validate_cron_field(field_text, lo, hi, i)
        return {"kind": "cron", "expr": s, "text": s}

    raise ScheduleError(
        f"can't read the schedule {text!r}. Try \"every 30m\", \"daily 09:00\", "
        "\"mon 09:00\", or a 5-field cron expression like \"*/15 * * * *\"")


def _check_clock(hh: int, mm: int) -> None:
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ScheduleError(f"{hh:02d}:{mm:02d} is not a real time of day")


def _validate_cron_field(text: str, lo: int, hi: int, index: int) -> None:
    for part in text.split(","):
        step = part.split("/")
        base = step[0]
        if len(step) > 1 and not step[1].isdigit():
            raise ScheduleError(f"bad step in cron field {index + 1}: {part!r}")
        if base == "*":
            continue
        for value in base.split("-"):
            if not value.isdigit() or not (lo <= int(value) <= hi):
                raise ScheduleError(
                    f"cron field {index + 1} ({part!r}) must be {lo}-{hi} or '*'")


def _cron_matches(expr: str, when: datetime) -> bool:
    minute, hour, dom, month, dow = expr.split()
    return (_field_matches(minute, when.minute, 0, 59)
            and _field_matches(hour, when.hour, 0, 23)
            and _field_matches(dom, when.day, 1, 31)
            and _field_matches(month, when.month, 1, 12)
            # cron's day-of-week is Sunday=0; Python's weekday() is Monday=0
            and _field_matches(dow, (when.weekday() + 1) % 7, 0, 6))


def _field_matches(text: str, value: int, lo: int, hi: int) -> bool:
    for part in text.split(","):
        base, _, step_s = part.partition("/")
        step = int(step_s) if step_s else 1
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(base)
        if start <= value <= end and (value - start) % max(1, step) == 0:
            return True
    return False


def next_run_after(spec: dict[str, Any], after: float) -> float:
    """The next epoch time this spec fires, strictly after ``after``."""
    kind = spec.get("kind")
    if kind == "every":
        return after + float(spec["seconds"])

    start = datetime.fromtimestamp(after).replace(second=0, microsecond=0)
    if kind == "daily":
        nxt = start.replace(hour=spec["hour"], minute=spec["minute"])
        if nxt.timestamp() <= after:
            nxt += timedelta(days=1)
        return nxt.timestamp()
    if kind == "weekly":
        nxt = start.replace(hour=spec["hour"], minute=spec["minute"])
        delta = (spec["weekday"] - nxt.weekday()) % 7
        nxt += timedelta(days=delta)
        if nxt.timestamp() <= after:
            nxt += timedelta(days=7)
        return nxt.timestamp()
    if kind == "cron":
        probe = start + timedelta(minutes=1)
        for _ in range(60 * 24 * 366):        # a year of minutes, then give up
            if _cron_matches(spec["expr"], probe):
                return probe.timestamp()
            probe += timedelta(minutes=1)
        raise ScheduleError(f"{spec['expr']!r} never fires")
    raise ScheduleError(f"unknown schedule kind {kind!r}")


# ---------------------------------------------------------------------------
# The job store
# ---------------------------------------------------------------------------


@dataclass
class CronJob:
    id: str
    prompt: str
    schedule: dict[str, Any]
    cwd: str
    next_run: float
    created: float = field(default_factory=time.time)
    enabled: bool = True
    godmode: bool = False
    sandbox: bool = True
    model: str | None = None
    last_run: float | None = None
    last_status: str | None = None       # "ok" | "error: …"
    last_log: str | None = None
    runs: int = 0

    def describe(self) -> str:
        when = self.schedule.get("text", "?")
        state = "" if self.enabled else " (paused)"
        return f"{self.id}  {when}{state}  {self.prompt}"


def jobs_path() -> Path:
    from .paths import get_mantis_agent_dir  # noqa: PLC0415

    return get_mantis_agent_dir() / "cron.json"


def log_dir() -> Path:
    from .paths import get_mantis_agent_dir  # noqa: PLC0415

    return get_mantis_agent_dir() / "cron-logs"


def load_jobs() -> list[CronJob]:
    try:
        raw = json.loads(jobs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[CronJob] = []
    for item in raw.get("jobs", []) if isinstance(raw, dict) else []:
        try:
            out.append(CronJob(**item))
        except TypeError:            # a job written by a newer/older build
            continue
    return out


def save_jobs(jobs: list[CronJob]) -> None:
    path = jobs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": [asdict(j) for j in jobs]}, indent=2) + "\n",
                    encoding="utf-8")


def list_jobs() -> list[CronJob]:
    return sorted(load_jobs(), key=lambda j: j.next_run)


def add_job(schedule: str, prompt: str, *, cwd: str | None = None,
            godmode: bool = False, sandbox: bool = True,
            model: str | None = None) -> CronJob:
    spec = parse_schedule(schedule)
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("a scheduled job needs a prompt")
    job = CronJob(
        id=uuid.uuid4().hex[:8],
        prompt=prompt,
        schedule=spec,
        cwd=str(Path(cwd or os.getcwd()).resolve()),
        next_run=next_run_after(spec, time.time()),
        godmode=godmode,
        sandbox=sandbox,
        model=model,
    )
    jobs = load_jobs()
    jobs.append(job)
    save_jobs(jobs)
    return job


def remove_job(job_id: str) -> bool:
    jobs = load_jobs()
    keep = [j for j in jobs if j.id != job_id and not j.id.startswith(job_id)]
    if len(keep) == len(jobs):
        return False
    save_jobs(keep)
    return True


def set_enabled(job_id: str, enabled: bool) -> bool:
    jobs = load_jobs()
    hit = False
    for j in jobs:
        if j.id == job_id or j.id.startswith(job_id):
            j.enabled = enabled
            hit = True
    if hit:
        save_jobs(jobs)
    return hit


def due_jobs(now: float | None = None) -> list[CronJob]:
    now = time.time() if now is None else now
    return [j for j in load_jobs() if j.enabled and j.next_run <= now]


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def run_job(job: CronJob, *, now: float | None = None,
            timeout: float = 3600.0) -> dict[str, Any]:
    """Execute one job through ``mantis -p`` and record the outcome.

    A subprocess rather than an in-process call: a scheduled run must not be
    able to take the scheduler down with it, and each run gets a clean
    interpreter, its own cwd, and a log you can read afterwards.
    """
    now = time.time() if now is None else now
    log_dir().mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
    log_path = log_dir() / f"{job.id}-{stamp}.log"

    argv = [sys.executable, "-m", "mantis_agent.tui", "-p", job.prompt,
            "--output-format", "json"]
    if job.model:
        argv += ["--model", job.model]
    if job.godmode:
        argv.append("--godmode")
    argv.append("--sandbox" if job.sandbox else "--no-sandbox")

    status = "ok"
    try:
        proc = subprocess.run(argv, cwd=job.cwd, capture_output=True, text=True,
                              timeout=timeout, check=False)
        body = proc.stdout or ""
        if proc.returncode != 0:
            status = f"error: exit {proc.returncode}"
        log_path.write_text(
            f"$ {' '.join(argv[:6])} …\ncwd: {job.cwd}\n\n{body}\n"
            + (f"--- stderr ---\n{proc.stderr}\n" if proc.stderr else ""),
            encoding="utf-8")
    except subprocess.TimeoutExpired:
        status = f"error: timed out after {int(timeout)}s"
        log_path.write_text(status + "\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — one bad job must not stop the rest
        status = f"error: {type(e).__name__}: {e}"
        log_path.write_text(status + "\n", encoding="utf-8")

    jobs = load_jobs()
    for j in jobs:
        if j.id == job.id:
            j.last_run = now
            j.last_status = status
            j.last_log = str(log_path)
            j.runs += 1
            j.next_run = next_run_after(j.schedule, now)
    save_jobs(jobs)
    return {"id": job.id, "status": status, "log": str(log_path)}


def tick(now: float | None = None) -> list[dict[str, Any]]:
    """Run everything that's due, once. This is what the OS scheduler calls."""
    return [run_job(job, now=now) for job in due_jobs(now)]


def daemon(interval: float = 30.0, *, iterations: int | None = None) -> int:
    """Foreground loop for `mantis cron daemon` — a tick every ``interval``."""
    count = 0
    while iterations is None or count < iterations:
        for result in tick():
            print(f"[cron] {result['id']}: {result['status']}", flush=True)
        count += 1
        if iterations is not None and count >= iterations:
            break
        time.sleep(interval)
    return 0


# ---------------------------------------------------------------------------
# Handing the schedule to the OS
# ---------------------------------------------------------------------------
#
# A daemon that only runs while a terminal is open isn't a schedule. launchd
# and systemd already solve "run this every minute, survive reboots" — we just
# register a one-minute tick with them.

_LAUNCHD_LABEL = "cc.mantisagent.cron"


def _tick_argv() -> list[str]:
    return [sys.executable, "-m", "mantis_agent.tui", "cron", "tick"]


def launchd_plist() -> str:
    args = "".join(f"    <string>{a}</string>\n" for a in _tick_argv())
    logs = log_dir()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key>\n  <string>{_LAUNCHD_LABEL}</string>\n"
        f"  <key>ProgramArguments</key>\n  <array>\n{args}  </array>\n"
        "  <key>StartInterval</key>\n  <integer>60</integer>\n"
        "  <key>RunAtLoad</key>\n  <false/>\n"
        f"  <key>StandardOutPath</key>\n  <string>{logs / 'launchd.out.log'}</string>\n"
        f"  <key>StandardErrorPath</key>\n  <string>{logs / 'launchd.err.log'}</string>\n"
        "</dict>\n</plist>\n"
    )


def systemd_units() -> tuple[str, str]:
    cmd = " ".join(_tick_argv())
    service = (
        "[Unit]\nDescription=mantis scheduled agent runs\n\n"
        f"[Service]\nType=oneshot\nExecStart={cmd}\n"
    )
    timer = (
        "[Unit]\nDescription=mantis cron tick\n\n"
        "[Timer]\nOnBootSec=1min\nOnUnitActiveSec=1min\nAccuracySec=15s\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def install_scheduler() -> dict[str, Any]:
    """Register the one-minute tick with launchd or systemd."""
    import platform  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    log_dir().mkdir(parents=True, exist_ok=True)
    system = platform.system()
    if system == "Darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(launchd_plist(), encoding="utf-8")
        loaded = False
        if shutil.which("launchctl"):
            subprocess.run(["launchctl", "unload", str(path)],
                           capture_output=True, check=False)
            loaded = subprocess.run(["launchctl", "load", str(path)],
                                    capture_output=True, check=False).returncode == 0
        return {"ok": True, "backend": "launchd", "path": str(path), "loaded": loaded}
    if system == "Linux":
        base = Path.home() / ".config" / "systemd" / "user"
        base.mkdir(parents=True, exist_ok=True)
        service, timer = systemd_units()
        (base / "mantis-cron.service").write_text(service, encoding="utf-8")
        (base / "mantis-cron.timer").write_text(timer, encoding="utf-8")
        started = False
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "daemon-reload"],
                           capture_output=True, check=False)
            started = subprocess.run(
                ["systemctl", "--user", "enable", "--now", "mantis-cron.timer"],
                capture_output=True, check=False).returncode == 0
        return {"ok": True, "backend": "systemd", "path": str(base),
                "loaded": started}
    return {"ok": False,
            "error": f"no supported scheduler for {system or 'this platform'}; "
                     f"run `{' '.join(_tick_argv())}` from your own cron every minute"}
