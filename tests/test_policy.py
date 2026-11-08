"""The gate's rules, one check at a time, in policy.blockers()'s order. The example config's
numbers: gpu.1-2 plus gpu.3 for short jobs at 36 GB RAM, cpu.1-3 plus cpu.4, 20 and 14 GB RAM, 7000
MiB of VRAM and two hours' solo patience."""

import pytest

from lockview import config, policy

CFG = policy.from_settings(config.load(config.EXAMPLE))
HOUR = 3600
NOW = 10 * HOUR
OK = policy.Machine(ram_available_gb=40, vram_free_mb=20000)


def job(pid, kind, tick=None, waited=60, running=False, vram=7000):
    """A gate that started at tick `tick` (its pid by default) and has waited `waited` seconds."""
    return policy.Job(pid, pid if tick is None else tick, kind, kind == "gpu-solo", NOW - waited, running, vram)


def why(me, slot=1, others=(), m=OK):
    return [r.why for r in policy.blockers(me, slot, [me, *others], m, CFG, NOW)]


def test_each_kind_tries_its_slots_in_the_gates_order():
    assert {k: CFG.order(k) for k in CFG.kinds} == {
        "gpu": [1, 2], "gpu-short": [1, 2, 3], "gpu-solo": [1, 2, 3], "cpu": [1, 2, 3], "cpu-short": [4, 3, 2, 1]}


@pytest.mark.parametrize(("kind", "slot", "ram", "blocked"), [
    ("gpu", 1, 20, False), ("gpu", 1, 19, True),
    ("gpu-short", 2, 20, False), ("gpu-short", 3, 35, True), ("gpu-short", 3, 36, False),  # the short-only GPU slot needs more
    ("gpu-solo", 0, 19, True),
    ("cpu", 1, 14, False), ("cpu", 1, 13, True), ("cpu-short", 4, 14, False),  # cpu.4 needs no more than the others
])
def test_ram(kind, slot, ram, blocked):
    assert why(job(9, kind), slot, m=policy.Machine(ram, 20000)) == (["ram"] if blocked else [])


def test_cpu_jobs_wait_only_for_ram():
    bad_gpu = policy.Machine(40, None, "no driver", (policy.Program(5, False, "x"),))
    assert why(job(9, "cpu"), 1, [job(1, "cpu"), job(2, "gpu-solo", waited=3 * HOUR)], bad_gpu) == []


@pytest.mark.parametrize(("me", "slot", "other", "fifo"), [
    (job(9, "gpu"), 1, job(1, "gpu"), True),
    (job(9, "gpu"), 1, job(1, "gpu", running=True), False),  # only waiting jobs queue
    (job(9, "gpu"), 1, job(10, "gpu"), False),  # a younger one
    (job(9, "gpu", tick=5), 1, job(1, "gpu", tick=5), True),  # same start: the lower pid goes first
    (job(1, "gpu", tick=5), 1, job(9, "gpu", tick=5), False),
    (job(9, "gpu"), 1, job(1, "gpu-short"), True),
    (job(9, "gpu"), 1, job(1, "gpu-solo"), False),  # a solo job is passed until its patience runs out
    (job(9, "gpu"), 1, job(1, "gpu-solo", waited=2 * HOUR), True),
    (job(9, "gpu"), 1, job(1, "cpu"), False),
    (job(9, "gpu-short"), 1, job(1, "gpu"), True),
    (job(9, "gpu-short"), 3, job(1, "gpu"), False),  # gpu jobs can't take gpu.3
    (job(9, "gpu-short"), 3, job(1, "gpu-short"), True),
    (job(9, "gpu-short"), 1, job(1, "gpu-solo", waited=3 * HOUR), False),  # left to the drain check
    (job(9, "gpu-solo"), 0, job(1, "gpu"), False),  # a solo job yields only to solo jobs
    (job(9, "gpu-solo"), 0, job(1, "gpu-solo"), True),
    (job(9, "cpu"), 1, job(1, "cpu"), False),  # cpu jobs take any free slot
])
def test_first_come_first_served(me, slot, other, fifo):
    assert ("fifo" in why(me, slot, [other])) == fifo


def test_a_short_job_passes_gpu_jobs_during_a_drain_while_a_gpu_job_runs():
    solo, long, running = job(1, "gpu-solo", waited=3 * HOUR), job(2, "gpu"), job(3, "gpu", running=True)
    assert why(job(9, "gpu-short"), 1, [solo, long, running]) == []
    assert why(job(9, "gpu-short"), 1, [solo, long, job(3, "gpu-short", running=True)]) == ["drain"]
    assert why(job(9, "gpu"), 1, [solo, long, running]) == ["fifo", "drain"]


def test_the_drain_holds_back_even_older_jobs_but_not_solo_ones():
    solo = job(5, "gpu-solo", waited=2 * HOUR)
    assert why(job(1, "gpu", waited=3 * HOUR), 1, [solo]) == ["drain"]
    assert why(job(1, "gpu-solo", waited=3 * HOUR), 0, [solo]) == []
    assert why(job(1, "gpu", waited=3 * HOUR), 1, [job(5, "gpu-solo", waited=2 * HOUR - 1)]) == []


def test_programs_outside_the_gate_stop_gpu_jobs_and_any_at_all_stop_a_solo_job():
    gated, ungated = policy.Program(5, True, "a"), policy.Program(6, False, "rover-30")
    assert why(job(9, "gpu"), m=policy.Machine(40, 20000, "", (gated,))) == []
    assert why(job(9, "gpu-solo"), 0, m=policy.Machine(40, 20000, "", (gated,))) == ["solo"]
    reasons = policy.blockers(job(9, "gpu"), 1, [], policy.Machine(40, 20000, "", (gated, ungated)), CFG, NOW)
    assert [r.text for r in reasons] == ["pid 6 (rover-30) runs outside the gate"]


@pytest.mark.parametrize(("free", "need", "blocked"), [(7000, 7000, False), (6999, 7000, True), (8000, 9000, True)])
def test_vram(free, need, blocked):
    assert why(job(9, "gpu", vram=need), m=policy.Machine(40, free)) == (["vram"] if blocked else [])


def test_a_failing_nvidia_smi_holds_gpu_jobs():
    reasons = policy.blockers(job(9, "gpu"), 1, [], policy.Machine(40, None, "NVIDIA-SMI has failed"), CFG, NOW)
    assert [(r.why, r.text) for r in reasons] == [("vram", "nvidia-smi fails: NVIDIA-SMI has failed")]


def test_every_failing_check_comes_back_in_the_gates_order_so_the_first_is_what_it_logs():
    bad = policy.Machine(10, 1000, "", (policy.Program(5, False, "x"),))
    queue = [job(1, "gpu"), job(2, "gpu-solo", waited=3 * HOUR)]
    assert why(job(9, "gpu"), 1, queue, bad) == ["ram", "fifo", "program", "drain", "vram"]
    assert why(job(9, "gpu-solo", waited=0), 0, queue, bad) == ["ram", "fifo", "program", "solo", "vram"]
