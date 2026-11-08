import json
import os
import time

from lockview import events


def _line(ev, t, pid=7, tick=70, **fields):
    return json.dumps({"v": 1, "t": t, "ev": ev, "pid": pid, "tick": tick, **fields}) + "\n"


QUEUE = dict(kind="gpu-short", cwd="/w", cmd="bash -c pytest -q", gate="fff64c27")


def _month(t):
    return time.strftime("%Y-%m", time.localtime(t))


def test_a_job_is_its_queue_waits_start_and_end():
    log = events.EventLog()
    log.feed([
        _line("queue", 100, **QUEUE),
        _line("wait", 101, why="slots", detail=""),
        _line("wait", 400, why="ram", detail="30 GB available, needs 36 GB"),
        _line("start", 700, slots=["gpu.3"], unit="agent-gate-7.scope", extra="ignored"),
        _line("end", 760, code=143, peak=2**30),
    ])
    j = log.jobs[(7, 70)]
    assert (j.kind, j.command, j.cwd, j.gate, j.queued) == ("gpu-short", "bash -c pytest -q", "/w", "fff64c27", 100)
    assert (j.start, j.slots, j.unit, j.end, j.code, j.peak) == (700, ["gpu.3"], "agent-gate-7.scope", 760, 143, 2**30)
    assert j.waited_by(until=9999) == {"slots": 299, "ram": 300}


def test_a_job_keeps_the_owner_its_gate_logged_if_it_is_well_formed():
    owner = {"project": "rover", "session": "rover-30", "worktree": "pr42"}
    bad = [None, "rover", {"project": "rover"}, {**owner, "worktree": 3}]
    log = events.EventLog()
    log.feed([_line("queue", 100, pid=1, owner={**owner, "extra": 1}, **QUEUE), *(_line("queue", 100, pid=2 + i, owner=b, **QUEUE) for i, b in enumerate(bad)),
              _line("queue", 100, pid=9, **QUEUE)])  # logged before gates recorded owners
    assert log.jobs[(1, 70)].owner == owner
    assert [log.jobs[(p, 70)].owner for p in (2, 3, 4, 5, 9)] == [None] * 5


def test_a_waiting_job_has_waited_until_now():
    log = events.EventLog()
    log.feed([_line("queue", 100, **QUEUE), _line("wait", 100, why="fifo", detail="behind gate 3")])
    assert log.jobs[(7, 70)].waited_by(until=250) == {"fifo": 150}


def test_lines_it_does_not_know_are_skipped():
    log = events.EventLog()
    log.feed([
        _line("queue", 100, **QUEUE),
        json.dumps({"v": 2, "t": 101, "ev": "start", "pid": 7, "tick": 70, "slots": ["gpu.1"]}),  # a newer schema
        _line("pause", 102),  # an event it doesn't know
        _line("start", 103, pid=8, slots=["gpu.2"]),  # a job whose queue it never read
        '{"v": 1, "t": 104, "ev": "wa',  # torn
        "[]",
    ])
    assert list(log.jobs) == [(7, 70)] and log.jobs[(7, 70)].start is None


def test_one_pid_run_twice_is_two_jobs():
    log = events.EventLog()
    log.feed([_line("queue", 100, **QUEUE), _line("queue", 900, tick=900, **QUEUE), _line("start", 901, tick=900, slots=["gpu.1"])])
    assert log.jobs[(7, 70)].start is None and log.jobs[(7, 900)].start == 901


def test_each_update_reads_what_was_appended_and_waits_for_a_whole_line(tmp_path):
    now = time.time()
    path = tmp_path / f"events-{_month(now)}.jsonl"
    path.write_text(_line("queue", now, **QUEUE) + _line("wait", now, why="slots", detail="")[:20])
    log = events.EventLog(tmp_path)
    log.update(now)
    assert log.jobs[(7, 70)].waits == []
    with path.open("a") as f:
        f.write(_line("wait", now, why="slots", detail="")[20:] + _line("start", now + 5, slots=["gpu.1"]))
    log.update(now)
    log.update(now)
    j = log.jobs[(7, 70)]
    assert len(j.waits) == 1 and j.start == now + 5


def test_a_rewritten_file_is_read_again_from_the_start(tmp_path):
    now = time.time()
    path = tmp_path / f"events-{_month(now)}.jsonl"
    path.write_text(_line("queue", now, **QUEUE) + _line("wait", now, why="slots", detail="") + _line("queue", now, pid=8, **QUEUE))
    log = events.EventLog(tmp_path)
    log.update(now)
    path.write_text(_line("queue", now, **QUEUE) + _line("wait", now, why="slots", detail=""))
    log.update(now)
    assert list(log.jobs) == [(7, 70)] and len(log.jobs[(7, 70)].waits) == 1


def test_it_reads_the_months_its_window_covers_and_forgets_older_jobs(tmp_path):
    now = time.mktime((2026, 11, 5, 12, 0, 0, 0, 0, -1))  # its window reaches back into October
    old = now - 40 * 86400
    (tmp_path / f"events-{_month(old)}.jsonl").write_text(_line("queue", old, pid=1, **QUEUE))
    (tmp_path / f"events-{_month(now - 10 * 86400)}.jsonl").write_text(_line("queue", now - 10 * 86400, pid=2, **QUEUE))
    (tmp_path / f"events-{_month(now)}.jsonl").write_text(_line("queue", now - 60, pid=3, **QUEUE))
    log = events.EventLog(tmp_path, days=14)
    log.update(now)
    assert sorted(pid for pid, _ in log.jobs) == [2, 3]
    log.update(now + 5 * 86400)
    assert sorted(pid for pid, _ in log.jobs) == [3]


def test_a_missing_directory_is_an_empty_log_and_an_unreadable_one_an_error(tmp_path):
    log = events.EventLog(tmp_path / "none")
    log.update()
    assert (log.jobs, log.error) == ({}, "")
    (tmp_path / f"events-{_month(time.time())}.jsonl").write_text(_line("queue", time.time(), **QUEUE))
    os.chmod(tmp_path / f"events-{_month(time.time())}.jsonl", 0)
    log = events.EventLog(tmp_path)
    log.update()
    assert log.error.startswith(str(tmp_path))
