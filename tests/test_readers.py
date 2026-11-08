import os
import time

import pytest

from lockview import agent_gate, config, gate, journal, locks, owners, policy, procs, resources


def test_each_slot_lists_the_kinds_that_may_take_it():
    cfg = policy.from_settings(config.load(config.EXAMPLE))
    assert [(s.name, s.kinds) for s in gate.slot_table(cfg, [])] == [
        ("cpu.1", ["cpu", "cpu-short"]), ("cpu.2", ["cpu", "cpu-short"]), ("cpu.3", ["cpu", "cpu-short"]), ("cpu.4", ["cpu-short"]),
        ("gpu.1", ["gpu", "gpu-short", "gpu-solo"]), ("gpu.2", ["gpu", "gpu-short", "gpu-solo"]), ("gpu.3", ["gpu-short", "gpu-solo"])]


def _ticket(pid, kind, state="waiting", **fields):
    return agent_gate.Ticket(pid, pid * 10, kind, 1000.0 + pid, "/w", ["pixi", "run", "sim"], state=state, **fields)


def test_gates_come_from_their_tickets():
    cfg = policy.from_settings(config.load(config.EXAMPLE))
    unit = "agent-gate-2.scope"
    snap = _snap({2: 1, 30: 2}, {2: "/user.slice/app.slice/term.scope", 30: f"/user.slice/app.slice/{unit}"})
    owner = {"project": "rover", "session": "rover-30", "worktree": "pr42"}
    tickets = [_ticket(1, "gpu-short", vram_mb=24000, owner=owner, version="old"),
               _ticket(2, "gpu", "running", slots=["gpu.1"], unit=unit, started=1500.0, owner=owner, version="now"),
               _ticket(3, "gpu", version="now"),  # from before tickets named an owner: found as lockview finds any
               _ticket(4, "nonesuch", version="now")]  # a kind config.toml has since lost
    gates = {g.pid: g for g in gate._gates(snap, cfg, {}, tickets, installed="now")}
    assert sorted(gates) == [1, 2, 3]
    waiting, running = gates[1], gates[2]
    assert (waiting.state, waiting.queued_at, waiting.start_ticks, waiting.command, waiting.cwd) == ("waiting", 1001.0, 10, ["pixi", "run", "sim"], "/w")
    assert (waiting.vram_need_mb, gates[3].vram_need_mb, waiting.old_copy, running.old_copy) == (24000, 7000, True, False)
    assert waiting.owner == owners.Owner("rover", "rover-30", "pr42")
    assert (running.state, running.slots, running.started_at, running.scope) == ("running", ["gpu.1"], 1500.0, f"/user.slice/app.slice/{unit}")
    assert [s.holder for s in gate.slot_table(cfg, list(gates.values())) if s.name.startswith("gpu")] == [2, None, None]


def test_a_program_is_gated_when_it_runs_in_a_gates_scope():
    snap = _snap({5: 1, 6: 1, 7: 1}, {5: "/app.slice/agent-gate-40.scope", 6: "/app.slice/agent-gate-41.scope/sub", 7: "/app.slice/term.scope"})
    assert [bool(agent_gate.IN_SCOPE.search(snap.procs[p].cgroup)) for p in (5, 6, 7)] == [True, True, False]


def _gate(pid, kind, state, ticks, queued_at=0.0, slots=(), needs_idle=False):
    return gate.Gate(pid=pid, kind=kind, state=state, needs_idle=needs_idle, queued_at=queued_at, start_ticks=ticks, command=["x"],
                     cwd="", owner=owners.Owner(""), old_copy=False, vram_need_mb=7000, slots=list(slots))


KINDS = {"gpu": policy.Kind("gpu", 2, "16G", 20), "gpu-solo": policy.Kind("gpu", 2, "16G", 20), "cpu": policy.Kind("cpu", 3, "10G", 14)}
# The gate with gpu-short jobs, without and with their third slot.
SHORT_KINDS_2 = {**KINDS, "gpu-short": policy.Kind("gpu", 2, "16G", 20, 900)}
SHORT_KINDS = {**KINDS, "gpu-short": policy.Kind("gpu", 3, "16G", 20, 900), "gpu-solo": policy.Kind("gpu", 3, "16G", 20),
               "cpu-short": policy.Kind("cpu", 4, "10G", 14, 120)}


def _state(gates, ram=40, free_mb=20000, programs=(), kinds=KINDS):
    cfg = policy.Config(kinds, 7200, 7000, third_ram=36)
    gpu = resources.Gpu(True, "", 24000, 24000 - free_mb, 0)
    return gate.GateState(True, "", cfg, gates, gate.slot_table(cfg, gates), list(programs), ram, gpu)


def test_waiters_get_every_failing_condition():
    run1, run2 = _gate(1, "gpu", "running", 1, slots=["gpu.1"]), _gate(2, "gpu", "running", 2, slots=["gpu.2"])
    older, younger = _gate(3, "gpu", "waiting", 3), _gate(4, "gpu", "waiting", 4)
    ungated = gate.ProgramProc(99, False, owners.Owner("rover", "rover-30"), "python")
    st = _state([run1, run2, older, younger], ram=12, free_mb=5000, programs=[ungated])
    gate.explain_waits(st, now=100)
    assert (older.position, younger.position) == (1, 2)
    assert younger.reasons == ["no free gpu slot (2 of 2 busy)", "RAM 12 GB available, needs 20 GB",
                               "behind 1 older gpu job (first come, first served)", "pid 99 (rover-30) runs outside the gate",
                               "VRAM 4.9 GB free, needs 6.8 GB"]  # in the order the gate checks them
    assert not any("older" in r for r in older.reasons)


def test_a_young_gpu_job_passes_a_patient_solo_job_but_not_an_impatient_one():
    solo, job = _gate(1, "gpu-solo", "waiting", 1, queued_at=0, needs_idle=True), _gate(2, "gpu", "waiting", 2, queued_at=50)
    st = _state([solo, job])
    gate.explain_waits(st, now=100)
    assert job.reasons == []
    solo2, job2 = _gate(1, "gpu-solo", "waiting", 1, queued_at=0, needs_idle=True), _gate(2, "gpu", "waiting", 2, queued_at=50)
    st = _state([solo2, job2])
    gate.explain_waits(st, now=8000)
    assert any(r.startswith("behind 1 older") for r in job2.reasons)
    assert any(r.startswith("GPU draining for idle-GPU job pid 1") for r in job2.reasons)


def test_a_solo_job_needs_both_slots_and_no_gated_program():
    run, solo = _gate(1, "gpu", "running", 1, slots=["gpu.1"]), _gate(2, "gpu-solo", "waiting", 2, needs_idle=True)
    st = _state([run, solo], programs=[gate.ProgramProc(5, True, owners.Owner(""), "python")])
    gate.explain_waits(st, now=10)
    assert "needs every gpu slot free (1 of 2 busy)" in solo.reasons
    assert "needs no [gpu] programs running at all (1 running)" in solo.reasons


def _snap(parents, cgroups=None):
    """A process table: pid -> parent pid, with optional cgroups."""
    cgroups = cgroups or {}
    return procs.Snapshot({pid: procs.Proc(pid, ppid, "x", ("x",), pid, cgroups.get(pid, "")) for pid, ppid in parents.items()}, 0.0)


def test_a_snapshot_reads_each_processs_cpu_time_memory_and_session():
    snap = procs.Snapshot.take()
    me = snap.procs[os.getpid()]
    assert me.ticks >= 0 and me.rss > 2**20 and me.sid == os.getsid(0)
    assert abs(snap.taken - time.time()) < 5


# Processes above the kernel's largest pid (2**22), so none of them is a real process to read.
P = 5_000_000
SCOPE = "/user.slice/user-1000.slice/user@1000.service/app.slice/agent-gate-21.scope"


def _machine(seconds_in=0.0):
    """This machine after `seconds_in` seconds of: a Claude session running a sweep with two workers (3 cores
    in all) outside the gate, a gated build (4 cores), a light pytest run, and a browser no session started."""
    def proc(n, ppid, comm, argv, cores=0.0, rss=0, cgroup=""):
        return procs.Proc(P + n, P + ppid if ppid else 1, comm, tuple(argv), 100 * procs.CLK_TCK, cgroup, {}, P + n,
                          round(cores * seconds_in * procs.CLK_TCK), rss)
    table = [
        proc(10, 0, "claude", ["claude"], rss=3 * 2**30),  # the session itself: heavy, but not a job
        proc(11, 10, "bash", ["/bin/bash", "-c", "python sweep.py"]),
        proc(12, 11, "python", ["python", "sweep.py"], cores=1.0, rss=2**28),
        proc(13, 12, "python", ["python", "sweep.py"], cores=1.0, rss=2**28),
        proc(14, 12, "python", ["python", "sweep.py"], cores=1.0, rss=2**28),
        proc(20, 10, "bash", ["/bin/bash", "-c", "agent-gate cpu -- make"]),
        proc(21, 20, "python3", ["/usr/bin/python3", "-I", "agent-gate", "cpu", "--", "make"]),
        proc(22, 21, "make", ["make"], cores=4.0, rss=2**30, cgroup=SCOPE),
        proc(30, 0, "chrome", ["chrome"], cores=1.0, rss=3 * 2**30),
        proc(40, 10, "bash", ["/bin/bash", "-c", "pytest -q"], cores=0.2),
    ]
    return procs.Snapshot({p.pid: p for p in table}, 1000.0, 1_000_000.0 + seconds_in)


SESSIONS = {P + 10: owners.Session(P + 10, "rover-30", "id", "rover", "", "busy")}


def test_outside_the_gate_is_each_heavy_job_a_claude_session_runs_without_it():
    jobs = gate.outside(_machine(seconds_in=3.0), _machine(), {}, SESSIONS)
    assert [(j["pid"], j["procs"], j["cpu"], j["owner"]["session"], j["via"]) for j in jobs] == [
        (P + 11, 4, 3.0, "rover-30", "Claude session rover-30")]
    assert jobs[0]["start"] == 1100.0 and jobs[0]["rss"] == 3 * 2**28


def test_outside_the_gate_without_a_previous_sample_counts_memory_and_vram_alone():
    assert gate.outside(_machine(seconds_in=3.0), None, {}, SESSIONS) == []
    assert [j["vram_mb"] for j in gate.outside(_machine(seconds_in=3.0), None, {P + 13: 900}, SESSIONS)] == [900]


def _running(pid, kind, slot, vram_mb=0):
    g = _gate(pid, kind, "running", pid, slots=[slot])
    g.scope, g.vram_mb = f"/app.slice/agent-gate-{pid}.scope", vram_mb
    return g


def test_a_gpu_slot_job_with_no_work_on_the_gpu_is_flagged_after_ten_minutes():
    idle = {}

    def flags(now, vram_mb=0, programs=(), gpu_ok=True):
        job, cpu_job = _running(1, "gpu", "gpu.1", vram_mb), _running(2, "cpu", "cpu.1")
        st = _state([job, cpu_job], programs=[gate.ProgramProc(p, True, owners.Owner(""), "python") for p in programs])
        st.gpu.ok = gpu_ok
        cgroups = {50: "/app.slice/agent-gate-1.scope/payload", 51: "/app.slice/agent-gate-12.scope"}
        gate.flag_running(st, [], _snap({1: 1, 2: 1, 50: 1, 51: 1}, cgroups), idle, now)
        assert cpu_job.flags == []
        return job.flags

    assert flags(1000) == [] and idle == {1: 1000}
    assert flags(1599) == []
    assert flags(1600) == ["no work on the GPU for 10 min while holding gpu.1"]
    assert flags(1700, gpu_ok=False) == [] and idle == {1: 1000}  # nvidia-smi failed: unknown, not idle
    assert flags(1800, programs=[50]) == [] and idle == {}  # a gated program in its scope counts as work
    assert flags(1850, programs=[51]) == [] and idle == {1: 1850}  # one in another job's scope does not
    assert flags(2450) == ["no work on the GPU for 10 min while holding gpu.1"]
    assert flags(2600, vram_mb=300) == [] and idle == {}


def test_a_job_holding_a_slot_while_blocked_on_another_lock_is_flagged():
    job = _running(10, "gpu", "gpu.2")
    snap = _snap({10: 1, 11: 10, 12: 11, 20: 1})  # gate 10 -> its job 11 -> a flock 12; 20 is unrelated
    party = lambda pid, since: locks.Party(pid, since, "flock", owners.Owner(""))
    lk = locks.Lock("/x/heavy.lock", waiters=[party(12, 500.0), party(20, 500.0)])
    gate.flag_running(_state([job]), [lk], snap, {}, now=800)
    assert job.flags == ["holding gpu.2 while waiting 5 min for /x/heavy.lock"]
    brief = _running(10, "gpu", "gpu.2")
    gate.flag_running(_state([brief]), [lk], snap, {}, now=530)
    assert brief.flags == []  # a wait under a minute is not worth a flag


def test_journal_pairs_starts_with_ends_and_keeps_outcomes():
    h = journal.History()
    h.feed([
        (100.0, "9", "Started agent-gate-11.scope - /usr/bin/bash /x/render.sh."),
        (100.0, "9", "Started run-u5.scope - /usr/bin/python3 big.py."),
        (100.0, "9", "Started agent-gate-12.scope - /usr/bin/bash -c x agent-gate-job p true."),
        (160.0, "9", "agent-gate-11.scope: Consumed 1h 2min 3.5s CPU time, 15.9G memory peak, 0B memory swap peak."),
        (170.0, "9", "run-u5.scope: A process of this unit has been killed by the OOM killer."),
        (170.0, "9", "run-u5.scope: Failed with result 'oom-kill'."),
        (171.0, "9", "run-u5.scope: Consumed 38min 58.606s CPU time."),
        (180.0, "9", "agent-gate-12.scope: Consumed 4.5s CPU time."),
    ])
    a, b, c = h.done
    assert (a.unit, a.command, a.duration, a.cpu_s, a.gate_pid) == ("agent-gate-11.scope", "/usr/bin/bash /x/render.sh", 60.0, 3723.5, 11)
    assert (b.outcome, b.cpu_s, b.gated, b.gate_pid) == ("oom-kill", 2338.606, False, None)
    assert (c.unit, c.duration, c.gated, c.gate_pid) == ("agent-gate-12.scope", 80.0, True, 12)


def test_only_runs_of_an_earlier_manager_are_lost():
    h = journal.History()
    h.feed([(1.0, "100", "Started agent-gate-1.scope - a."), (2.0, "200", "Started agent-gate-2.scope - b.")])
    assert [r.unit for r in h.lost()] == ["agent-gate-1.scope"]


@pytest.mark.parametrize("text, seconds", [("16.170s", 16.17), ("13min 20.557s", 800.557), ("1d 2h", 93600), ("250ms", 0.25)])
def test_timespans(text, seconds):
    assert journal.parse_timespan(text) == pytest.approx(seconds)


def test_proc_locks_marks_blocked_waiters():
    text = (
        "202: FLOCK  ADVISORY  WRITE 593821 103:05:51700397 0 EOF\n"
        "202: -> FLOCK  ADVISORY  WRITE 625302 103:05:51700397 0 EOF\n"
        "14: POSIX  ADVISORY  WRITE 1646775 103:05:59927025 0 EOF\n"
        "1: FLOCK  ADVISORY  READ 837657 00:1b:2004336 0 EOF\n"
    )
    table = locks.parse_proc_locks(text)
    assert table[(os.makedev(0x103, 5), 51700397)] == {"exclusive": True, "blocked": [625302]}
    assert table[(os.makedev(0, 0x1B), 2004336)] == {"exclusive": False, "blocked": []}
    assert len(table) == 2  # POSIX locks are not flock


def _both_gpu_slots_busy():
    return [_gate(1, "gpu", "running", 1, slots=["gpu.1"]), _gate(2, "gpu", "running", 2, slots=["gpu.2"])]


def test_a_short_job_waits_only_when_the_third_slot_is_no_good_either():
    short = _gate(3, "gpu-short", "waiting", 3)
    st = _state([*_both_gpu_slots_busy(), short], ram=30, kinds=SHORT_KINDS)
    gate.explain_waits(st, now=100)
    assert short.reasons == ["no free gpu slot (2 of 2 busy)", "gpu.3: RAM 30 GB available, needs 36 GB"]
    short2 = _gate(3, "gpu-short", "waiting", 3)
    st = _state([*_both_gpu_slots_busy(), short2], ram=40, kinds=SHORT_KINDS)
    gate.explain_waits(st, now=100)
    assert short2.reasons == []  # it starts in gpu.3 at its next look


def test_a_short_job_waits_behind_older_short_jobs_for_the_third_slot_but_not_gpu_jobs():
    long, older, short = _gate(3, "gpu", "waiting", 3), _gate(4, "gpu-short", "waiting", 4), _gate(5, "gpu-short", "waiting", 5)
    st = _state([*_both_gpu_slots_busy(), long, older, short], kinds=SHORT_KINDS)
    gate.explain_waits(st, now=100)
    assert older.reasons == []
    assert short.reasons == ["no free gpu slot (2 of 2 busy)", "behind 2 older gpu jobs (first come, first served)",
                             "gpu.3: behind 1 older gpu-short job (first come, first served)"]


def _cpu_slots_busy(*slots):
    return [_gate(10 + i, "cpu", "running", 10 + i, slots=[s]) for i, s in enumerate(slots)]


def test_a_cpu_short_job_waits_only_when_the_fourth_slot_is_busy_too():
    long, short = _gate(3, "cpu", "waiting", 3), _gate(4, "cpu-short", "waiting", 4)
    gate.explain_waits(_state([*_cpu_slots_busy("cpu.1", "cpu.2", "cpu.3"), long, short], kinds=SHORT_KINDS), now=100)
    assert long.reasons == ["no free cpu slot (3 of 3 busy)"]  # cpu.4 isn't one of its slots
    assert short.reasons == []  # it starts in cpu.4 at its next look
    short = _gate(4, "cpu-short", "waiting", 4)
    gate.explain_waits(_state([*_cpu_slots_busy("cpu.1", "cpu.2", "cpu.3", "cpu.4"), short], ram=10, kinds=SHORT_KINDS), now=100)
    assert short.reasons == ["no free cpu slot (3 of 3 busy)", "cpu.4: busy", "RAM 10 GB available, needs 14 GB"]


def test_a_gpu_job_counts_only_the_slots_it_may_take():
    job = _gate(3, "gpu", "waiting", 3)
    st = _state([*_both_gpu_slots_busy(), job], kinds=SHORT_KINDS)
    gate.explain_waits(st, now=100)
    assert job.reasons == ["no free gpu slot (2 of 2 busy)"]


@pytest.mark.parametrize("runner,drains", [("gpu", False), ("gpu-short", True)])
def test_a_short_job_starts_during_a_drain_only_while_a_gpu_job_runs(runner, drains):
    running = _gate(1, runner, "running", 1, slots=["gpu.1"])
    solo = _gate(2, "gpu-solo", "waiting", 2, queued_at=0, needs_idle=True)
    long, short = _gate(3, "gpu", "waiting", 3, queued_at=50), _gate(4, "gpu-short", "waiting", 4, queued_at=60)
    st = _state([running, solo, long, short], kinds=SHORT_KINDS_2)  # without gpu.3, which it could take anyway
    gate.explain_waits(st, now=8000)
    assert any(r.startswith("GPU draining") for r in long.reasons)
    assert any(r.startswith("GPU draining") for r in short.reasons) == drains
    assert not any("older" in r for r in short.reasons)  # the waiting gpu job can't start before the solo job
