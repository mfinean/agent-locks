"""Finished runs from the gate's log and the journal, and the history rows the page draws from them."""

from lockview import estimate, events, gate, journal, locks, owners, policy, procs, resources, runs, server

KINDS = {"gpu": policy.Kind("gpu", 2, "16G", 20), "gpu-short": policy.Kind("gpu", 3, "16G", 20, 900),
         "gpu-solo": policy.Kind("gpu", 3, "16G", 20), "cpu": policy.Kind("cpu", 3, "10G", 14),
         "cpu-short": policy.Kind("cpu", 4, "10G", 14, 120)}
NOW = 6000.0  # within a day of the runs below


def _log(*jobs):
    log = events.EventLog()
    for j in jobs:
        log.jobs[(j.pid, j.tick)] = j
    return log


def _job(pid, kind="gpu", start=1000.0, end=1600.0, slots=("gpu.1",), code=0, cmd="pixi run -e gpu --frozen sim", cwd="/w", owner=None):
    return events.Job(pid, pid * 10, kind, cwd, cmd, "fff64c27", start - 300, [(start - 300, "slots", "")], start, list(slots),
                      f"agent-gate-{pid}.scope", end, code, 7 * 2**30, owner)


def _journal(*entries):
    h = journal.History()
    h.feed(entries)
    return h


def _started(t, unit, cmd="/usr/bin/bash x.sh"):
    return (t, "9", f"Started {unit} - {cmd}.")


def _consumed(t, unit):
    return (t, "9", f"{unit}: Consumed 1min 0.5s CPU time.")


def test_a_logged_run_takes_its_facts_from_the_log_and_cpu_time_and_kills_from_the_journal():
    h = _journal(_started(1000.4, "agent-gate-1.scope"), (1500.0, "9", "agent-gate-1.scope: Failed with result 'oom-kill'."),
                 _consumed(1600.2, "agent-gate-1.scope"))
    [f] = runs.merge(h, _log(_job(1, code=137)))
    assert (f.logged, f.kind, f.slots, f.queued_at, f.code, f.peak) == (True, "gpu", ["gpu.1"], 700, 137, 7 * 2**30)
    assert (f.start, f.end, f.cpu_s, f.outcome) == (1000, 1600, 60.5, "oom-kill")
    assert f.waited == {"slots": 300} and f.keys.task == "pixi:gpu:sim"
    assert f.cut_short


def test_a_reused_pid_is_matched_by_start_time():
    h = _journal(_started(50.0, "agent-gate-1.scope"), _consumed(60.0, "agent-gate-1.scope"),
                 _started(1000.0, "agent-gate-1.scope"), _consumed(1600.0, "agent-gate-1.scope"))
    fs = runs.merge(h, _log(_job(1)))
    assert [(f.start, f.logged, f.cpu_s) for f in fs] == [(50, False, 60.5), (1000, True, 60.5)]


def test_a_run_whose_gate_was_killed_ends_when_the_journal_says():
    h = _journal(_started(1000.0, "agent-gate-1.scope"), _consumed(2000.0, "agent-gate-1.scope"))
    [f] = runs.merge(h, _log(_job(1, end=None, code=None)))
    assert (f.logged, f.end, f.code) == (True, 2000, None)


def test_a_run_with_no_end_yet_is_not_finished():
    h = _journal(_started(1000.0, "agent-gate-1.scope"))
    assert runs.merge(h, _log(_job(1, end=None, code=None))) == []


def test_runs_the_log_lacks_come_from_the_journal_with_a_guessed_kind():
    h = _journal(_started(10.0, "agent-gate-5.scope", "/usr/bin/bash /x/gpu-render.sh"), _consumed(20.0, "agent-gate-5.scope"),
                 _started(10.0, "agent-gate-6.scope", "/usr/bin/make check"), _consumed(20.0, "agent-gate-6.scope"),
                 _started(10.0, "run-u1.scope", "/usr/bin/bash chain.sh"), _consumed(20.0, "run-u1.scope"))
    assert {f.unit: (f.logged, f.kind) for f in runs.merge(h, _log())} == {
        "agent-gate-5.scope": (False, "gpu"), "agent-gate-6.scope": (False, "cpu"), "run-u1.scope": (False, None)}


def test_a_history_row_is_owned_as_the_gate_logged_it_else_as_seen_live_else_by_its_directory():
    queued = {"project": "rover", "session": "rover-30", "worktree": "pr42"}
    seen = {"project": "boards", "session": "s", "worktree": ""}
    jobs = [_job(1, owner=queued), _job(2, start=2000, end=2600, slots=("gpu.2",)), _job(3, start=3000, end=3600, cwd="")]
    observed = {"agent-gate-1.scope": {"owner": seen}, "agent-gate-2.scope": {"owner": seen}}
    rows = {r["unit"]: r["owner"] for r in server.history_rows(runs.merge(_journal(), _log(*jobs)), [], observed, {}, _gs(), NOW)}
    assert rows == {"agent-gate-1.scope": queued, "agent-gate-2.scope": seen, "agent-gate-3.scope": {"project": "", "session": "", "worktree": ""}}


def test_only_a_run_ended_by_a_signal_or_a_kill_is_cut_short():
    def cut(code, outcome=""):
        return runs.Finished("u", "c", None, 0, 1, "gpu", True, code=code, outcome=outcome).cut_short

    assert [cut(0), cut(1), cut(124), cut(135), cut(143), cut(None), cut(None, "killed")] == [False, False, False, True, True, False, True]


def _gs(running=()):
    cfg = policy.Config(KINDS, 7200, 7000, third_ram=36)
    gates = list(running)
    return gate.GateState(True, "", cfg, gates, gate.slot_table(cfg, gates), [], 40, resources.Gpu(True, "", 24000, 0, 0))


def test_history_rows_keep_logged_slots_and_place_the_rest_around_them():
    logged = [_job(1, start=1000, end=5000, slots=["gpu.1"]), _job(2, "gpu-short", start=4000, end=4000 + 905, slots=["gpu.3"], code=143),
              _job(3, "cpu", start=1000, end=1100, slots=["cpu.2"], cmd="make check")]
    h = _journal(_started(1500.0, "agent-gate-9.scope", "/usr/bin/bash /x/gpu-render.sh"), _consumed(3000.0, "agent-gate-9.scope"),
                 _started(1600.0, "agent-gate-8.scope", "/usr/bin/bash /x/gpu-bench.sh"), _consumed(2900.0, "agent-gate-8.scope"),
                 _started(1500.0, "run-u1.scope", "/usr/bin/bash chain.sh"), _consumed(3000.0, "run-u1.scope"),
                 _started(1500.0, "run-u2.scope", "/usr/bin/python3 other.py"), _consumed(3000.0, "run-u2.scope"))
    observed = {"run-u1.scope": {"lock": "/x/heavy.lock"}, "agent-gate-3.scope": {"owner": {"project": "boards", "session": "s", "worktree": ""}}}
    rows = {r["unit"]: r for r in server.history_rows(runs.merge(h, _log(*logged)), [], observed, {}, _gs(), NOW)}
    assert sorted(rows) == ["agent-gate-1.scope", "agent-gate-2.scope", "agent-gate-3.scope", "agent-gate-8.scope", "agent-gate-9.scope", "run-u1.scope"]
    one, short, cpu, guessed = (rows[f"agent-gate-{p}.scope"] for p in (1, 2, 3, 9))
    assert (one["lanes"], one["placed"], one["kind"], one["mem"]) == (["gpu.1"], True, "gpu", "16G")
    assert (short["lanes"], short["kind"], short["code"], short["limit_stop"]) == (["gpu.3"], "gpu-short", 143, True)
    assert cpu["owner"]["session"] == "s"  # who started it, as seen live
    assert one["owner"]["project"] == "" and rows["agent-gate-1.scope"]["cwd"] == "/w"
    # gpu.1 is the logged run's, so a guess goes in gpu.2, and the next one in a cpu slot: never in
    # gpu.3, which is for short jobs, and is free then
    assert (guessed["lanes"], guessed["placed"], guessed["logged"]) == (["gpu.2"], False, False)
    assert (rows["agent-gate-8.scope"]["lanes"], rows["agent-gate-8.scope"]["kind"]) == (["cpu.1"], "cpu")
    assert (rows["run-u1.scope"]["lanes"], rows["run-u1.scope"]["kind"]) == (["lock:/x/heavy.lock"], "lock")


def test_a_short_job_stopped_early_is_not_said_to_have_hit_the_limit():
    rows = server.history_rows(runs.merge(_journal(), _log(_job(2, "gpu-short", start=2000, end=2300, code=143))), [], {}, {}, _gs(), NOW)
    assert rows[0]["limit_stop"] is False


def test_a_cpu_short_job_hits_its_own_limit_and_a_cpu_job_has_none():
    jobs = [_job(2, "cpu-short", start=2000, end=2121, slots=["cpu.4"], code=143), _job(3, "cpu", start=2000, end=2300, slots=["cpu.1"], code=143)]
    rows = {r["unit"]: r for r in server.history_rows(runs.merge(_journal(), _log(*jobs)), [], {}, {}, _gs(), NOW)}
    assert (rows["agent-gate-2.scope"]["lanes"], rows["agent-gate-2.scope"]["limit_stop"]) == (["cpu.4"], True)
    assert rows["agent-gate-3.scope"]["limit_stop"] is False


def _waiting(pid, kind, ticks, slots=(), state="waiting", started_at=None):
    return gate.Gate(pid=pid, kind=kind, state=state, needs_idle=False, queued_at=float(ticks), start_ticks=ticks, command=[f"job{pid}.sh"],
                     cwd="", owner=owners.Owner(""), old_copy=False, vram_need_mb=7000, slots=list(slots), started_at=started_at)


def _cpu_running(*slots):
    return [_waiting(10 + i, "cpu", i, [s], "running", NOW - 10) for i, s in enumerate(slots, 1)]


def test_a_running_short_job_is_predicted_to_end_by_its_limit():
    short = _waiting(20, "cpu-short", 4, ["cpu.4"], "running", NOW - 100)
    rows = {d["pid"]: d for d in server.gate_jobs(_gs([short]), estimate.Estimator([]), NOW)}
    assert rows[20]["remaining"] == 20  # its default estimate is past, which leaves 60 s; the gate stops it at 120 s


def test_a_waiting_cpu_short_job_is_predicted_in_the_fourth_slot_leaving_cpu_slots_to_cpu_jobs():
    short, long = _waiting(30, "cpu-short", 5), _waiting(31, "cpu", 6)  # the short one is older
    rows = {d["pid"]: d for d in server.gate_jobs(_gs([*_cpu_running("cpu.1", "cpu.2"), short, long]), estimate.Estimator([]), NOW)}
    assert (rows[30]["predicted"]["start"], rows[30]["predicted"]["slots"]) == (NOW, ["cpu.4"])
    assert (rows[31]["predicted"]["start"], rows[31]["predicted"]["slots"]) == (NOW, ["cpu.3"])


def test_a_waiting_short_job_is_predicted_to_run_no_longer_than_its_limit():
    first, second = _waiting(30, "cpu-short", 5), _waiting(31, "cpu-short", 6)
    est = estimate.Estimator([(estimate.keys(["job30.sh"]), 300.0, "cpu-short")] * 2)  # it has run 300 s twice
    rows = {d["pid"]: d for d in server.gate_jobs(_gs([*_cpu_running("cpu.1", "cpu.2", "cpu.3"), first, second]), est, NOW)}
    assert (rows[30]["predicted"]["start"], rows[30]["predicted"]["end"]) == (NOW, NOW + 120)
    assert (rows[31]["predicted"]["start"], rows[31]["predicted"]["slots"]) == (NOW + 120, ["cpu.4"])


def test_estimates_leave_out_runs_cut_short_and_count_lock_runs_as_lock_jobs():
    h = _journal(*(e for i, d in ((1, 1000), (2, 2000), (3, 3000), (4, 9000))
                   for e in (_started(10.0, f"run-u{i}.scope", f"/usr/bin/bash chain{i}.sh"), _consumed(10.0 + d, f"run-u{i}.scope"))))
    logged = [_job(1, start=1000, end=1600), _job(2, start=2000, end=2100, code=143), _job(3, start=3000, end=3800)]
    observed = {f"run-u{i}.scope": {"lock": "/x/heavy.lock"} for i in (1, 2, 3)}  # run-u4 ran under no lock it saw
    est = server.estimator(runs.merge(h, _log(*logged)), observed, {})
    e = est.estimate("pixi run -e gpu --frozen sim", "gpu")
    assert (e.basis, e.n, e.median) == ("exact", 2, 700)
    lock = est.estimate("new.sh", "lock")
    assert (lock.basis, lock.n, lock.median) == ("default", 3, 2000)


LOCK = "/srv/run/build.lock"
HOLDER = "/bin/bash /srv/bin/with-lock 0 -- docker exec dev bin/build test //a:b"
ROVER = {"project": "rover", "session": "rover-30", "worktree": ""}


def _hold(first, seen, gone=None, since=None, unit="", at_start=False, command=HOLDER):
    d = {"lock": LOCK, "command": command, "owner": ROVER, "unit": unit, "since": since if since is not None else first - 600,
         "first": first, "seen": seen, "at_start": at_start}
    return {**d, "gone": gone} if gone is not None else d


def test_lockview_records_each_lock_holder_it_sees_and_when_it_first_sees_it_gone(tmp_path):
    holds = server.Observed(tmp_path / "holds.json")
    snap = procs.Snapshot({7: procs.Proc(7, 1, "bash", ("bash", "with-lock"), 4242)}, 0.0)
    held = [locks.Lock(LOCK, holders=[locks.Party(7, 1000.0, "bash with-lock", owners.Owner("rover", "rover-30"), "/u.slice/run-u1.scope")])]
    for now, lks in ((1500.0, held), (1503.0, held), (1506.0, []), (1509.0, [])):
        server.note_holds(holds, lks, snap, now, first_sample=now == 1500.0)
    assert holds.units == {f"{LOCK}:7:4242": {"lock": LOCK, "command": "bash with-lock", "owner": ROVER, "unit": "run-u1.scope",
                                             "since": 1000.0, "first": 1500.0, "seen": 1503.0, "at_start": True, "gone": 1506.0}}


def test_a_hold_lockview_saw_end_is_a_finished_run_in_its_locks_lane():
    holds = {"a": _hold(1000.0, 1600.0, gone=1603.0), "b": _hold(2000.0, 5990.0),  # b still holds
             "c": _hold(3000.0, 3000.0, gone=3003.0)}  # seen once: a queue's brief look at the lock, not a run
    [row] = server.history_rows([], [], {}, holds, _gs(), NOW)
    assert (row["lanes"], row["kind"], row["placed"], row["owner"], row["command"]) == (["lock:" + LOCK], "lock", True, ROVER, HOLDER)
    # With no scope of its own, a hold starts when lockview first saw it: its holder's start includes its time in a queue.
    assert (row["start"], row["end"], row["start_unseen"], row["end_unseen_until"]) == (1000.0, 1600.0, False, None)


def test_a_hold_with_a_scope_starts_with_its_scope():
    [row] = server.history_rows([], [], {}, {"a": _hold(1000.0, 1600.0, gone=1603.0, since=998.5, unit="run-u1.scope")}, _gs(), NOW)
    assert (row["unit"], row["start"]) == ("run-u1.scope", 998.5)


def test_a_hold_lockview_did_not_see_begin_or_end_says_so():
    holds = {"a": _hold(1000.0, 1600.0, gone=4000.0, at_start=True)}  # held when lockview opened; gone by the time it reopened
    [row] = server.history_rows([], [], {}, holds, _gs(), NOW)
    assert (row["start"], row["end"], row["start_unseen"], row["end_unseen_until"]) == (1000.0, 1600.0, True, 4000.0)


def test_a_hold_the_journal_saw_end_is_drawn_once_from_the_journal():
    h = _journal(_started(1000.0, "run-u1.scope", "/usr/bin/make -j8"), _consumed(1600.0, "run-u1.scope"))
    holds = {"a": _hold(1001.0, 1598.0, gone=1601.0, since=1000.0, unit="run-u1.scope")}
    rows = server.history_rows(runs.merge(h, _log()), [], {"run-u1.scope": {"lock": LOCK}}, holds, _gs(), NOW)
    assert [(r["unit"], r["end"], r["cpu_s"]) for r in rows] == [("run-u1.scope", 1600.0, 60.5)]


def test_lock_holders_are_estimated_from_the_holds_lockview_saw_whole():
    holds = {str(i): _hold(1000.0 * i, 1000.0 * i + d, gone=1000.0 * i + d + 3) for i, d in ((1, 600), (2, 800))}
    holds["late"] = _hold(5000.0, 5100.0, gone=9000.0)  # ended while lockview was closed: at least 100 s, not a run time
    holds["early"] = _hold(6000.0, 6100.0, gone=6103.0, at_start=True)  # began before lockview opened
    e = server.estimator([], {}, holds).estimate(HOLDER, "lock")
    assert (e.basis, e.n, e.median) == ("exact", 2, 700)
