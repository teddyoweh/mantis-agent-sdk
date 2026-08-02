"""Scheduled agent runs — the part of `/loop` that survives closing the laptop."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mantis_agent import cron


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return home


# -- schedules ---------------------------------------------------------------


@pytest.mark.parametrize(("text", "kind"), [
    ("every 30m", "every"),
    ("every 2 hours", "every"),
    ("EVERY 1d", "every"),
    ("daily 09:00", "daily"),
    ("every day 23:30", "daily"),
    ("mon 09:00", "weekly"),
    ("friday 17:45", "weekly"),
    ("*/15 * * * *", "cron"),
    ("0 9 * * 1-5", "cron"),
])
def test_schedules_people_actually_type(text: str, kind: str) -> None:
    assert cron.parse_schedule(text)["kind"] == kind


@pytest.mark.parametrize("text", [
    "", "every", "every 10s", "daily 25:00", "mon 09:70",
    "sometimes", "* * * *", "99 * * * *", "nonsense 1 2 3 4",
])
def test_bad_schedules_are_refused_with_a_reason(text: str) -> None:
    with pytest.raises((cron.ScheduleError, ValueError)) as e:
        cron.parse_schedule(text)
    assert str(e.value)


def test_sub_minute_intervals_are_refused() -> None:
    """A 5-second schedule would just be a busy loop wearing a schedule's hat."""
    with pytest.raises(cron.ScheduleError, match="1 minute"):
        cron.parse_schedule("every 5s")


def test_interval_next_run_is_simply_later() -> None:
    spec = cron.parse_schedule("every 30m")
    now = time.time()
    assert cron.next_run_after(spec, now) == pytest.approx(now + 1800)


def test_daily_lands_on_the_next_occurrence_of_that_time() -> None:
    spec = cron.parse_schedule("daily 09:00")
    at_ten = datetime(2026, 8, 1, 10, 0).timestamp()
    nxt = datetime.fromtimestamp(cron.next_run_after(spec, at_ten))
    assert (nxt.hour, nxt.minute) == (9, 0)
    assert nxt.day == 2                      # already past today's 09:00

    at_eight = datetime(2026, 8, 1, 8, 0).timestamp()
    same_day = datetime.fromtimestamp(cron.next_run_after(spec, at_eight))
    assert same_day.day == 1


def test_weekly_lands_on_the_right_weekday() -> None:
    spec = cron.parse_schedule("mon 09:00")
    wednesday = datetime(2026, 7, 29, 12, 0).timestamp()   # a Wednesday
    nxt = datetime.fromtimestamp(cron.next_run_after(spec, wednesday))
    assert nxt.weekday() == 0 and (nxt.hour, nxt.minute) == (9, 0)


def test_cron_expressions_step_and_range() -> None:
    quarter = cron.parse_schedule("*/15 * * * *")
    at = datetime(2026, 8, 1, 10, 7).timestamp()
    nxt = datetime.fromtimestamp(cron.next_run_after(quarter, at))
    assert nxt.minute == 15

    weekdays = cron.parse_schedule("0 9 * * 1-5")
    saturday = datetime(2026, 8, 1, 10, 0).timestamp()     # a Saturday
    nxt2 = datetime.fromtimestamp(cron.next_run_after(weekdays, saturday))
    assert nxt2.weekday() == 0 and nxt2.hour == 9          # skips the weekend


# -- the store ---------------------------------------------------------------


def test_add_list_remove_round_trip(tmp_path) -> None:
    job = cron.add_job("every 30m", "triage failures")
    assert job.id and job.sandbox is True and job.godmode is False
    assert job.cwd == str(tmp_path.resolve())

    listed = cron.list_jobs()
    assert [j.id for j in listed] == [job.id]
    assert listed[0].prompt == "triage failures"

    assert cron.remove_job(job.id) is True
    assert cron.list_jobs() == []
    assert cron.remove_job(job.id) is False


def test_jobs_survive_a_reload() -> None:
    cron.add_job("daily 09:00", "morning triage", godmode=True, sandbox=False)
    reloaded = cron.load_jobs()[0]
    assert reloaded.godmode is True and reloaded.sandbox is False
    assert reloaded.schedule["text"] == "daily 09:00"


def test_a_prompt_is_required() -> None:
    with pytest.raises(ValueError, match="prompt"):
        cron.add_job("every 30m", "   ")


def test_pause_keeps_the_job_but_stops_it_firing() -> None:
    job = cron.add_job("every 30m", "x")
    cron.set_enabled(job.id, False)
    jobs = cron.load_jobs()
    jobs[0].next_run = time.time() - 1        # overdue…
    cron.save_jobs(jobs)
    assert cron.due_jobs() == []              # …but paused, so not due
    cron.set_enabled(job.id, True)
    assert [j.id for j in cron.due_jobs()] == [job.id]


def test_ids_can_be_abbreviated() -> None:
    job = cron.add_job("every 30m", "x")
    assert cron.set_enabled(job.id[:4], False) is True
    assert cron.remove_job(job.id[:4]) is True


def test_a_corrupt_store_is_not_fatal(isolated_home) -> None:
    """A hand-edited cron.json shouldn't wedge every future run."""
    cron.jobs_path().write_text("{not json", encoding="utf-8")
    assert cron.load_jobs() == []
    assert cron.add_job("every 30m", "recovered").id


# -- running -----------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def do_GET(self):
        self._send(json.dumps({"data": [{"id": "gpt-5.4"}]}).encode(),
                   "application/json")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        base = {"id": "1", "object": "chat.completion.chunk", "model": "gpt-5.4"}
        body = "".join([
            "data: " + json.dumps({**base, "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "did the work"},
                 "finish_reason": None}]}) + "\n\n",
            "data: " + json.dumps({**base, "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n",
            "data: [DONE]\n\n",
        ]).encode()
        self._send(body, "text/event-stream")

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # No keep-alive: a reused connection to a stub that's about to be torn
        # down by the next test's fixture shows up as an empty response, which
        # looks exactly like a model failure. One connection per request.
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


@pytest.fixture()
def stub_model(monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("MANTIS_AGENT_MODEL", "gpt-5.4")
    monkeypatch.setenv("MANTIS_AGENT_BASE_URL",
                       f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("MANTIS_AGENT_API_KEY", "k")
    monkeypatch.setenv("MANTIS_AGENT_NO_CONTEXT", "1")
    try:
        yield
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_running_a_job_records_the_outcome_and_reschedules(stub_model) -> None:
    job = cron.add_job("every 30m", "do the scheduled thing")
    before = job.next_run

    result = cron.run_job(job, timeout=120)
    assert result["status"] == "ok"

    stored = cron.load_jobs()[0]
    assert stored.runs == 1 and stored.last_status == "ok"
    assert stored.next_run > before                    # it rearmed itself
    log = open(stored.last_log, encoding="utf-8").read()
    assert "did the work" in log                       # the actual answer is kept


def test_tick_runs_what_is_due_and_leaves_the_rest(stub_model) -> None:
    due = cron.add_job("every 30m", "due now")
    cron.add_job("daily 09:00", "not yet")
    jobs = cron.load_jobs()
    for j in jobs:
        if j.id == due.id:
            j.next_run = time.time() - 1
        else:
            j.next_run = time.time() + 86400
    cron.save_jobs(jobs)

    results = cron.tick()
    assert [r["id"] for r in results] == [due.id]


def test_a_failing_job_is_recorded_not_raised(monkeypatch) -> None:
    """One broken job must not stop the scheduler."""
    job = cron.add_job("every 30m", "will fail")
    monkeypatch.setenv("MANTIS_AGENT_BASE_URL", "http://127.0.0.1:9")  # refused

    result = cron.run_job(job, timeout=120)
    assert result["status"] != "ok"
    stored = cron.load_jobs()[0]
    assert stored.last_status.startswith("error")
    assert stored.next_run > time.time()               # still rearmed


def test_daemon_can_run_a_bounded_number_of_ticks() -> None:
    assert cron.daemon(interval=0.01, iterations=2) == 0


# -- OS scheduler handoff ----------------------------------------------------


def test_launchd_plist_runs_the_tick_every_minute() -> None:
    plist = cron.launchd_plist()
    assert "<integer>60</integer>" in plist
    assert "mantis_agent.tui" in plist and "cron" in plist and "tick" in plist
    assert "<false/>" in plist          # RunAtLoad off: no surprise run on login


def test_systemd_units_are_a_oneshot_plus_a_timer() -> None:
    service, timer = cron.systemd_units()
    assert "Type=oneshot" in service and "cron" in service
    assert "OnUnitActiveSec=1min" in timer and "WantedBy=timers.target" in timer


# -- the CLI -----------------------------------------------------------------


def _cli(*args, home) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ, MANTIS_AGENT_HOME=str(home))
    return subprocess.run([sys.executable, "-m", "mantis_agent.tui", "cron", *args],
                          capture_output=True, text=True, timeout=90, env=env,
                          check=False)


def test_cli_add_list_pause_remove(isolated_home) -> None:
    r = _cli("add", "every 30m", "triage failures", home=isolated_home)
    assert r.returncode == 0 and "Scheduled" in r.stdout

    r = _cli("list", home=isolated_home)
    assert "triage failures" in r.stdout and "every 30m" in r.stdout

    job_id = cron.list_jobs()[0].id
    assert _cli("pause", job_id, home=isolated_home).returncode == 0
    assert "paused" in _cli("list", home=isolated_home).stdout
    assert _cli("resume", job_id, home=isolated_home).returncode == 0

    assert _cli("remove", job_id, home=isolated_home).returncode == 0
    assert "No scheduled jobs" in _cli("list", home=isolated_home).stdout


def test_cli_rejects_a_bad_schedule(isolated_home) -> None:
    r = _cli("add", "whenever", "do a thing", home=isolated_home)
    assert r.returncode == 1
    assert "can't read the schedule" in r.stderr
    assert cron.list_jobs() == []


def test_cli_reports_an_unknown_job(isolated_home) -> None:
    r = _cli("remove", "nope", home=isolated_home)
    assert r.returncode == 1 and "no job" in r.stderr


def test_empty_list_suggests_what_to_do(isolated_home) -> None:
    out = _cli("list", home=isolated_home).stdout
    assert "No scheduled jobs" in out and "mantis cron add" in out
