import pytest

from lockview.estimate import Estimate, Estimator, keys, simulate


def test_pixi_task_through_wrappers():
    k = keys(["env", "PATH=/x", "PYTHONNOUSERSITE=1", "nice", "-n", "10", "/home/u/.pixi/bin/pixi", "run", "-e", "dev", "--frozen", "test",
              "--basetemp=/home/u/.cache/test-tmp-be65f980"])
    assert k.task == "pixi:dev:test"
    assert k.exact == "pixi run -e dev --frozen test --basetemp"


def test_journal_and_live_forms_of_one_command_share_keys():
    live = keys(["bash", "-c", "\n for t in lint test; do\n  pixi run -e dev --frozen $t; done"])
    journal = keys('/usr/bin/bash -c "\\n for t in lint test; do\\n  pixi run -e dev --frozen $t; done"', from_journal=True)
    assert live == journal


def test_a_command_line_joined_with_spaces_keys_like_its_argv():
    # /proc's command line and the gate's log join argv with spaces, which ends a bash -c script's quoting.
    argv = ["timeout", "600", "bash", "-c", "set -e\n cd /x/wt && pixi run -e dev --frozen test; echo \"exit $?\""]
    assert keys(" ".join(argv)) == keys(argv)
    assert keys(" ".join(argv)).task == "pixi:dev:test"


def test_lock_wrappers_are_stripped():
    k = keys("flock /home/u/ev/heavy.lock systemd-run --user --scope -q -p MemoryMax=16G -p MemorySwapMax=0 bash /home/u/ev/w1/tools/chain.sh")
    assert k.task == "tools/chain.sh"
    assert keys("timeout -s KILL -k 10 3600 python3 -s /a/b/run.py").task == "b/run.py"


def test_gate_peak_wrapper_is_stripped():
    # As the journal logs a job that agent-gate runs through its peak-recording wrapper.
    journal = ('/usr/bin/bash -c "peak=\\$1; shift; \\"\\$@\\"; code=\\$?\\ncat \\"/sys/fs/cgroup\\$(cut -d: -f3 /proc/self/cgroup)'
               '/memory.peak\\" > \\"\\$peak\\" 2>/dev/null\\nexit \\$code" agent-gate-job /run/user/1000/agent-gate/peak.1234 '
               '/home/u/.pixi/bin/pixi run -e dev --frozen test')
    assert keys(journal, from_journal=True) == keys(["pixi", "run", "-e", "dev", "--frozen", "test"])


def test_scripts_are_keyed_by_their_directory_and_ids_are_masked():
    k = keys("/usr/bin/bash /home/u/wt/fix-7/run-suite.sh nightly 9c41d2e7 /home/u/wt/fix-7/out OUT=/tmp/x")
    assert k.task == "fix-7/run-suite.sh"
    assert k.exact == "fix-7/run-suite.sh nightly # out OUT="


def test_two_scripts_of_one_name_are_two_jobs():
    est = Estimator(_runs("bash /x/quick/run.sh", 20, 30, 40))
    assert est.estimate("bash /x/slow/run.sh", "gpu").basis == "default"


def test_make_and_cargo_name_their_target():
    assert keys("make -C /x/tree -j6 check OUT=/y").task == "make check"
    assert keys("cargo test -j 2").task == "cargo test"


def test_gpu_hint_ignores_negated_markers():
    assert not keys("pytest -m 'not gpu and not cuda'").gpu_hint
    assert keys("pixi run -e cuda --frozen render").gpu_hint


def _runs(command, *durations, kind=None):
    return [(keys(command), d, kind) for d in durations]


def test_estimate_prefers_exact_then_task_then_longest_part_then_default():
    est = Estimator(
        _runs("pixi run -e dev --frozen test", 600, 700, 800)
        + _runs("pixi run -e dev test --basetemp=/tmp/a", 900)
        + _runs("bash tools/bench.sh", 5000, 5200)
        + _runs("python -m pytest -q", 30, 40)
    )
    e = est.estimate("pixi run -e dev --frozen test", "cpu")
    assert (e.basis, e.median, e.n) == ("exact", 700, 3)
    assert est.estimate("pixi run -e dev test -x", "cpu").basis == "task"
    part = est.estimate(["bash", "-c", "bash setup.sh; bash tools/bench.sh; python -m pytest -q"], "gpu")
    assert (part.basis, part.key, part.median) == ("part", "tools/bench.sh", 5100)
    # pytest says how a job runs, not what it runs: no estimate from it
    assert est.estimate("python -m pytest tests/gpu/test_scene.py", "gpu").basis == "default"


def test_runs_shorter_than_a_start_say_nothing():
    est = Estimator(_runs("bash quick.sh", 1, 2))
    assert est.estimate("bash quick.sh", "cpu").basis == "default"


def test_a_job_with_no_history_takes_the_first_runs_of_new_jobs_of_its_kind():
    est = Estimator(_runs("a.sh", 100, 900, 900, kind="gpu-short") + _runs("b.sh", 200, kind="gpu-short") + _runs("c.sh", 300, kind="gpu-short")
                    + _runs("d.sh", 1000, kind="gpu") + _runs("e.sh", 2000, kind="gpu") + _runs("f.sh", 3000, kind="gpu-solo"))
    short = est.estimate("new.sh", "gpu-short")
    assert (short.median, short.n) == (200, 3)  # a.sh's later runs say how long a.sh takes, not a new job
    assert est.estimate("new.sh", "gpu").median == 2000  # a gpu-solo run counts as a gpu one
    assert est.estimate("new.sh", "gpu-solo").median == 2000
    assert est.estimate("new.sh", "cpu").key == "any cpu job (no history: a guess)"


def test_cpu_short_jobs_have_their_own_default_not_the_cpu_one():
    est = Estimator(_runs("lint.sh", 600, kind="cpu") + _runs("unit.sh", 700, kind="cpu") + _runs("build.sh", 800, kind="cpu"))
    assert est.estimate("git status", "cpu").median == 700
    short = est.estimate("git status", "cpu-short")
    assert (short.median, short.key) == (15, "any cpu-short job (no history: a guess)")
    est = Estimator(_runs("lint.sh", 8, kind="cpu-short") + _runs("fmt.sh", 12, kind="cpu-short") + _runs("q.py", 30, kind="cpu-short"))
    assert est.estimate("git status", "cpu-short").median == 12


def test_remaining_uses_p90_once_past_the_median_then_flags_overrun():
    e = Estimate(median=100, p90=200, n=5, basis="exact", key="k")
    assert e.remaining(40) == (60, False)
    assert e.remaining(150) == (50, False)
    left, over = e.remaining(1000)
    assert over and left == 100


def _w(pid, duration, needs_idle=False, queued_at=0.0, short=False, own_first=False):
    return {"pid": pid, "duration": duration, "needs_idle": needs_idle, "queued_at": queued_at, "short": short, "own_first": own_first}


def test_fifo_on_two_slots():
    plans = simulate({"gpu.1": 100, "gpu.2": 50}, [_w(1, 30), _w(2, 30), _w(3, 30)], now=0, patience=7200)
    assert (plans[1].start, plans[1].slots) == (50, ["gpu.2"])
    assert (plans[2].start, plans[2].slots) == (80, ["gpu.2"])
    assert (plans[3].start, plans[3].slots) == (100, ["gpu.1"])


def test_idle_gpu_job_is_passed_until_its_patience_runs_out():
    # The solo job (queued at 0) waits for both slots; the younger job takes the slot freed at 50.
    plans = simulate({"gpu.1": 100, "gpu.2": 50}, [_w(1, 30, needs_idle=True), _w(2, 200)], now=0, patience=7200)
    assert plans[2].start == 50
    assert plans[1].start == 250 and plans[1].slots == ["gpu.1", "gpu.2"]
    # Past its patience the GPU drains for it: the younger job waits behind it.
    plans = simulate({"gpu.1": 100, "gpu.2": 50}, [_w(1, 30, True, queued_at=-8000), _w(2, 200)], now=0, patience=7200)
    assert plans[1].start == 100 and plans[2].start == 130


def test_free_slot_starts_now():
    plans = simulate({"1": 0}, [_w(9, 60)], now=10, patience=0)
    assert (plans[9].start, plans[9].end) == (10, 70)


THIRD = frozenset({"gpu.3"})


def test_a_short_job_passes_a_waiting_gpu_job_for_the_third_slot():
    plans = simulate({"gpu.1": 100, "gpu.2": 200, "gpu.3": 0}, [_w(1, 50), _w(2, 10, short=True)], 0, 7200, THIRD)
    assert (plans[2].start, plans[2].slots) == (0, ["gpu.3"])
    assert (plans[1].start, plans[1].slots) == (100, ["gpu.1"])


@pytest.mark.parametrize("own_first,short_in,long_at", [(False, "cpu.1", 10), (True, "cpu.4", 0)])
def test_a_short_job_that_tries_its_own_slot_first_leaves_the_shared_one(own_first, short_in, long_at):
    plans = simulate({"cpu.1": 0, "cpu.2": 100, "cpu.3": 100, "cpu.4": 0}, [_w(1, 10, short=True, own_first=own_first), _w(2, 50)], 0, 7200,
                     frozenset({"cpu.4"}))
    assert (plans[1].start, plans[1].slots) == (0, [short_in])
    assert (plans[2].start, plans[2].slots) == (long_at, ["cpu.1"])


def test_short_jobs_take_the_third_slot_in_turn_and_gpu_jobs_never_do():
    plans = simulate({"gpu.1": 100, "gpu.2": 100, "gpu.3": 0}, [_w(1, 10, short=True), _w(2, 10, short=True), _w(3, 5)], 0, 7200, THIRD)
    assert [(plans[p].start, plans[p].slots) for p in (1, 2, 3)] == [(0, ["gpu.3"]), (10, ["gpu.3"]), (100, ["gpu.1"])]


def test_during_a_drain_a_short_job_starts_while_a_gpu_job_runs():
    # A gpu job holds gpu.1 until 100; the solo job is past its patience.
    plans = simulate({"gpu.1": 100, "gpu.2": 0}, [_w(1, 30, True, queued_at=-8000), _w(2, 50), _w(3, 10, short=True)], 0, 7200)
    assert (plans[3].start, plans[3].slots) == (0, ["gpu.2"])
    assert plans[1].start == 100 and plans[2].start == 130


def test_during_a_drain_short_jobs_stop_once_only_short_jobs_run():
    plans = simulate({"gpu.1": 50, "gpu.2": 0}, [_w(1, 30, True, queued_at=-8000), _w(2, 10, short=True)], 0, 7200,
                     held_by_short=frozenset({"gpu.1"}))
    assert plans[1].start == 50 and plans[2].start == 80
