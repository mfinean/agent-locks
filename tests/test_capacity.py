"""The capacity report, on a scene small enough to work out by hand."""

import pytest

from lockview import capacity, events, gate, journal, owners, policy, resources, runs

KINDS = {"gpu": policy.Kind("gpu", 2, "16G", 20), "gpu-short": policy.Kind("gpu", 3, "16G", 20, 900),
         "gpu-solo": policy.Kind("gpu", 3, "16G", 20), "cpu": policy.Kind("cpu", 3, "10G", 14),
         "cpu-short": policy.Kind("cpu", 4, "10G", 14, 120)}
GB = 2**30


def _job(pid, kind, queued, start=None, end=None, slots=(), code=0, peak=None, waits=()):
    return events.Job(pid, pid, kind, "/w", f"job{pid}.sh", "", queued, list(waits), start, list(slots), f"agent-gate-{pid}.scope",
                      end, code if end is not None else None, peak)


def _live(job, state, slots=(), peak=None):
    g = gate.Gate(pid=job.pid, kind=job.kind, state=state, needs_idle=job.kind == "gpu-solo", queued_at=job.queued, start_ticks=job.tick,
                  command=["x"], cwd="", owner=owners.Owner(""), old_copy=False, vram_need_mb=7000, slots=list(slots), started_at=job.start)
    g.stats = resources.ScopeStats("u", memory_peak=peak) if state == "running" else None
    return g


def _report(jobs, live=(), now=1000.0, hours=24, history=None):
    log = events.EventLog()
    log.jobs = {(j.pid, j.tick): j for j in jobs}
    cfg = policy.Config(KINDS, 7200, 7000, third_ram=36)
    gs = gate.GateState(True, "", cfg, list(live), gate.slot_table(cfg, list(live)), [], 40, resources.Gpu(True, "", 24000, 0, 0))
    return capacity.report(runs.merge(history or journal.History(), log), log, gs, now, hours)


def test_slot_use_and_waiting_on_the_gpu():
    # gpu.1: A 0-600; gpu.2: B 0-400, then C 400-900 (it waited from 100); D, a solo job, waits from 200 on.
    solo = _job(4, "gpu-solo", 200, waits=[(200, "slots", ""), (600, "drain", "")])
    c = _report([_job(1, "gpu", 0, 0, 600, ["gpu.1"]), _job(2, "gpu", 0, 0, 400, ["gpu.2"]),
                 _job(3, "gpu", 100, 400, 900, ["gpu.2"], waits=[(100, "slots", "")]), solo], live=[_live(solo, "waiting")])
    gpu = c["locks"]["gpu"]
    assert (c["from"], gpu["slots"], gpu["slot_s"], gpu["busy_s"]) == (0, 2, 2000, 1500)
    assert (gpu["waiting_s"], gpu["full_while_waiting_s"]) == (900, 500)  # from 100 on; both slots busy until 600
    assert gpu["idle_while_waiting_s"] == 300 * 1 + 100 * 2
    k = c["kinds"]
    assert (k["gpu"]["jobs"], k["gpu"]["wait"]["max"], k["gpu"]["wait"]["total"], k["gpu"]["run"]["p50"]) == (3, 300, 300, 500)
    assert k["gpu"]["by_reason"] == {"slots": 300}
    assert (k["gpu-solo"]["jobs"], k["gpu-solo"]["waiting"], k["gpu-solo"]["by_reason"]) == (1, 1, {"slots": 400, "drain": 400})


def test_the_third_slot_is_counted_apart():
    c = _report([_job(1, "gpu", 0, 0, 1000, ["gpu.1"]), _job(2, "gpu", 0, 0, 1000, ["gpu.2"]), _job(3, "gpu-short", 0, 100, 400, ["gpu.3"])])
    gpu = c["locks"]["gpu"]
    assert (gpu["busy_s"], gpu["extra"]) == (2000, [{"slot": "gpu.3", "busy_s": 300}])


def test_the_fourth_cpu_slot_is_counted_apart():
    c = _report([_job(1, "cpu", 0, 0, 1000, ["cpu.1"]), _job(2, "cpu-short", 0, 0, 50, ["cpu.4"]), _job(3, "cpu-short", 0, 50, 70, ["cpu.2"])])
    cpu = c["locks"]["cpu"]
    assert (cpu["slots"], cpu["slot_s"], cpu["busy_s"], cpu["extra"]) == (3, 3000, 1020, [{"slot": "cpu.4", "busy_s": 50}])


def test_a_window_starts_no_earlier_than_the_log_and_counts_the_jobs_queued_in_it():
    early, late = _job(1, "gpu", 0, 0, 5000, ["gpu.1"]), _job(2, "gpu", 4000, 4500, 5000, ["gpu.2"])
    whole = _report([early, late], now=5000, hours=24)
    assert (whole["from"], whole["limited_by"]) == (0, "log") and whole["kinds"]["gpu"]["jobs"] == 2
    last = _report([early, late], now=5000, hours=1 / 3)  # the last 1,200 s: from 3,800
    assert last["from"] == pytest.approx(3800)
    assert last["kinds"]["gpu"]["jobs"] == 1  # the early job ran in it, but was queued before it
    assert last["locks"]["gpu"]["busy_s"] == pytest.approx(1200 + 500)


def test_exits_kills_and_the_limit_are_counted_apart():
    c = _report([_job(1, "gpu-short", 0, 0, 905, ["gpu.1"], code=143), _job(2, "gpu-short", 0, 0, 300, ["gpu.2"], code=143),
                 _job(3, "gpu-short", 0, 0, 300, ["gpu.1"], code=1), _job(4, "gpu-short", 0, 0, 300, ["gpu.2"], code=0)])
    k = c["kinds"]["gpu-short"]
    assert (k["jobs"], k["failed"], k["cut_short"], k["limit_stops"]) == (4, 1, 2, 1)


def test_each_short_kind_is_stopped_at_its_own_limit():
    c = _report([_job(1, "cpu-short", 0, 0, 121, ["cpu.4"], code=143), _job(2, "cpu", 0, 0, 200, ["cpu.1"], code=143),
                 _job(3, "gpu-short", 0, 0, 200, ["gpu.1"], code=143)])
    assert [c["kinds"][k]["limit_stops"] for k in ("cpu-short", "cpu", "gpu-short")] == [1, 0, 0]


def test_gpu_jobs_memory_together_sums_their_own_peaks_while_they_overlap():
    running = _job(4, "gpu", 0, 700)
    c = _report([_job(1, "gpu", 0, 0, 500, ["gpu.1"], peak=10 * GB), _job(2, "gpu", 0, 300, 800, ["gpu.2"], peak=12 * GB),
                 _job(3, "cpu", 0, 0, 1000, ["cpu.1"], peak=9 * GB), running], live=[_live(running, "running", ["gpu.1"], peak=11 * GB)])
    assert c["gpu_together"] == {"bytes": 23 * GB, "jobs": 2, "at": 700}  # 12 + 11 from 700; 10 + 12 from 300 is less


def test_an_empty_log_is_an_empty_window():
    c = _report([], now=1000)
    assert (c["from"], c["to"], c["kinds"], c["gpu_together"]["at"]) == (1000, 1000, {}, None)


def test_a_window_starts_after_the_last_gate_run_the_log_lacks():
    h = journal.History()
    h.feed([(0.0, "9", "Started agent-gate-9.scope - /usr/bin/bash /x/gpu-render.sh."), (400.0, "9", "agent-gate-9.scope: Consumed 5s CPU time."),
            (0.0, "9", "Started run-u1.scope - /usr/bin/bash chain.sh."), (900.0, "9", "run-u1.scope: Consumed 5s CPU time.")])
    c = _report([_job(1, "gpu", 0, 0, 1000, ["gpu.1"])], history=h)
    # The journal-only gate run's kind and slot would be guesses; a scope under a lock is no gate run.
    assert (c["from"], c["limited_by"], c["locks"]["gpu"]["busy_s"]) == (400, "unlogged run", 600)
    assert _report([_job(1, "gpu", 0, 0, 1000, ["gpu.1"])], hours=0.1)["limited_by"] is None
    late = _report([_job(1, "gpu", 500, 500, 1000, ["gpu.1"])], history=h)  # that run ended before the log began
    assert (late["from"], late["limited_by"]) == (500, "log")
