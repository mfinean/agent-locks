"""agent-gate run as a process against stubs (conftest.AgentGate): first come first served, short
jobs and their slots, the event log, and the contracts its callers rely on: exit codes, `status`,
the pid, and a job that outlives its gate."""

import fcntl
import json
import os
import signal
import subprocess
import time

import pytest

from lockview import agent_gate


def _held(agate, slot: str) -> bool:
    with open(agate.run_dir / f"{slot}.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


# ---- a job's run


def test_a_job_logs_queue_start_and_end_with_its_exit_code(agate):
    p = agate.start("cpu", "bash", "-c", "exit 3")
    assert p.wait(10) == 3
    ev = agate.events()
    assert [e["ev"] for e in ev] == ["queue", "start", "end"]
    q, s, e = ev
    assert {x["pid"] for x in ev} == {p.pid} and len({x["tick"] for x in ev}) == 1 and {x["v"] for x in ev} == {1}
    assert (q["kind"], q["cmd"], q["cwd"]) == ("cpu", "bash -c exit 3", str(agate.root))
    assert len(q["gate"]) == 8
    assert (s["slots"], s["unit"]) == (["cpu.1"], f"agent-gate-{p.pid}.scope")
    assert e["code"] == 3 and (e["peak"] is None or e["peak"] > 0)  # stubbed, the job has no scope: any cgroup's peak
    assert q["t"] <= s["t"] <= e["t"]
    assert list(agate.run_dir.glob("queue/*")) == []  # its ticket goes with it


def test_the_job_gets_the_callers_environment_and_directory_unchanged(agate):
    out = agate.root / "out"
    p = agate.start("cpu", "bash", "-c", f'echo "$PYTHONPATH|$PWD|$MINE" > {out}', env={"PYTHONPATH": "/x", "MINE": "y"})
    assert p.wait(10) == 0
    assert out.read_text() == f"/x|{agate.root}|y\n"


@pytest.mark.parametrize("kind", ["gpu", "gpu-short", "gpu-solo", "cpu", "cpu-short"])
def test_every_job_gives_way_to_the_desktop_on_a_busy_cpu(agate, kind):
    p = agate.start(kind, "true")
    assert p.wait(10) == 0
    assert "CPUWeight=20" in agate.props(p)


@pytest.mark.parametrize(("short", "long", "limit", "cap"), [("gpu-short", "gpu", 900, "16G"), ("cpu-short", "cpu", 120, "10G")])
def test_only_a_short_job_gets_a_time_limit_and_both_get_the_same_cap(agate, short, long, limit, cap):
    s = agate.start(short, "true")
    assert s.wait(10) == 0
    other = agate.start(long, "true")
    assert other.wait(10) == 0
    assert {f"RuntimeMaxSec={limit}", "TimeoutStopSec=30", f"MemoryMax={cap}", "MemorySwapMax=0"} <= set(agate.props(s))
    assert f"MemoryMax={cap}" in agate.props(other)
    assert not [x for x in agate.props(other) if x.startswith(("RuntimeMaxSec", "TimeoutStopSec"))]


@pytest.mark.parametrize(("short", "long"), [("gpu-short", "gpu"), ("cpu-short", "cpu")])
def test_a_short_job_past_its_limit_is_stopped_and_told_why(agate, short, long):
    agate.configure(**{f"{long}__short_limit_min": 1 / 60})
    p = agate.start(short, "/usr/bin/sleep", "30")
    _, err = p.communicate(timeout=10)
    assert p.returncode == 143
    assert f"stopped the job at the {short} limit of 0.0166667 min; rerun it as {long}\n" in err
    assert agate.event(p, "end")["code"] == 143


@pytest.mark.parametrize("short", ["gpu-short", "cpu-short"])
def test_a_short_job_killed_before_its_limit_is_not_blamed_on_it(agate, short):
    p = agate.start(short, "bash", "-c", "kill -TERM $$")
    _, err = p.communicate(timeout=10)
    assert p.returncode == 143
    assert "limit" not in err


def test_a_job_killed_by_a_signal_exits_128_plus_it(agate):
    p = agate.start("cpu", "bash", "-c", "kill -KILL $$")
    assert p.wait(10) == 137
    assert agate.event(p, "end")["code"] == 137


def test_a_job_keeps_its_slot_when_its_gate_is_killed(agate):
    done = agate.root / "done"
    p = agate.start("cpu", "bash", "-c", f"sleep 1; touch {done}")
    agate.wait_for_event(p, "start")
    os.kill(p.pid, signal.SIGKILL)
    p.wait(10)
    assert _held(agate, "cpu.1") and not done.exists()  # the job, orphaned, still holds cpu.1
    end = time.time() + 10
    while not done.exists():
        assert time.time() < end
        time.sleep(0.05)
    time.sleep(0.2)
    assert not _held(agate, "cpu.1")
    assert agate.status() == set()  # its stale ticket is dropped


def test_odd_commands_are_logged_whole_and_valid(agate):
    odd = 'say "hi" \\ back\nslash ünïcode ' + "x" * 3000
    p = agate.start("cpu", "printf", "%s", odd)
    assert p.wait(10) == 0
    assert agate.events()[0]["cmd"] == ("printf %s " + odd)[:2000]


def test_concurrent_gates_write_whole_lines(agate):
    ps = [agate.start("cpu", "true") for _ in range(12)]
    assert [p.wait(30) for p in ps] == [0] * 12
    ev = agate.events()  # every line parses
    for p in ps:
        assert [e["ev"] for e in ev if e["pid"] == p.pid and e["ev"] != "wait"] == ["queue", "start", "end"]


def test_an_unwritable_log_directory_changes_nothing(agate):
    state = agate.root / "state"
    state.mkdir()
    state.chmod(0o500)
    try:
        p = agate.start("cpu", "bash", "-c", "exit 4")
        _, err = p.communicate(timeout=10)
        assert p.returncode == 4
        assert not agate.log_dir.exists()
        assert "denied" not in err and "events" not in err
    finally:
        state.chmod(0o700)


def test_a_writer_stuck_holding_the_log_lock_does_not_hold_up_the_job(agate):
    agate.log_dir.mkdir(parents=True)
    with open(agate.log_dir / time.strftime("events-%Y-%m.jsonl"), "a") as log:
        fcntl.flock(log, fcntl.LOCK_EX)
        p = agate.start("cpu", "bash", "-c", "exit 6")
        assert p.wait(10) == 6
    assert agate.events() == []


# ---- waiting


def test_a_wait_is_logged_once_per_reason_and_a_config_change_applies_to_it(agate):
    agate.configure(cpu__ram_needed_gb=agate.NEVER)
    p = agate.start("cpu", "true")
    agate.wait_for(lambda ev: any(e["ev"] == "wait" for e in ev))
    time.sleep(0.5)  # several more looks, still short of RAM
    agate.configure(cpu__ram_needed_gb=0)
    assert p.wait(10) == 0
    waits = [(e["ev"], e.get("why")) for e in agate.events()]
    assert waits == [("queue", None), ("wait", "ram"), ("start", None), ("end", None)]
    assert agate.event(p, "wait")["detail"].endswith(f"GB available, needs {agate.NEVER} GB")


def test_a_config_broken_while_it_waits_keeps_the_settings_it_had_and_says_so_once(agate):
    agate.configure(cpu__ram_needed_gb=agate.NEVER)
    p = agate.start("cpu", "true")
    agate.wait_for_event(p, "wait")
    agate.config.write_text("[cpu\n")
    time.sleep(0.5)  # several looks at the broken file
    assert p.poll() is None
    agate.configure(cpu__ram_needed_gb=0)
    _, err = p.communicate(timeout=10)
    assert p.returncode == 0
    assert err.count("keeping the settings it had") == 1


def test_full_slots_then_short_vram_are_reasons(agate):
    held = agate.hold("gpu.1", "gpu.2", "gpu.3")
    agate.set_gpu(used=20000)  # 4564 MiB free; a gpu job needs 7000
    p = agate.start("gpu", "true")
    agate.wait_for(lambda ev: any(e.get("why") == "slots" for e in ev))
    for f in held:
        f.close()
    agate.wait_for(lambda ev: any(e.get("why") == "vram" for e in ev))
    agate.set_gpu(used=1000)
    assert p.wait(10) == 0
    ev = agate.events()
    assert [(e["why"], e["detail"]) for e in ev if e["ev"] == "wait"] == [("slots", ""), ("vram", "VRAM 4.5 GB free, needs 6.8 GB")]
    assert next(e for e in ev if e["ev"] == "start")["slots"] == ["gpu.1"]


def test_a_job_can_ask_for_more_vram(agate):
    p = agate.start("gpu", "true", env={"AGENT_GATE_VRAM_MB": "24000"})
    assert agate.wait_for_event(p, "wait")["detail"] == "VRAM 23.0 GB free, needs 23.4 GB"
    agate.set_gpu(used=0)
    assert p.wait(10) == 0


def test_a_solo_job_logs_all_three_gpu_slots(agate):
    p = agate.start("gpu-solo", "true")
    assert p.wait(10) == 0
    assert agate.event(p, "start")["slots"] == ["gpu.1", "gpu.2", "gpu.3"]


@pytest.mark.parametrize(("first", "second"), [("gpu", "gpu-short"), ("gpu-short", "gpu")])
def test_gpu_and_short_jobs_are_first_come_first_served(agate, first, second):
    held = agate.hold("gpu.1", "gpu.2", "gpu.3")
    a = agate.start(first, "true")
    agate.wait_for_event(a, "wait")
    b = agate.start(second, "true")
    agate.wait_for_event(b, "wait")
    assert {(a.pid, first, "waiting"), (b.pid, second, "waiting")} <= agate.status()
    held[0].close()  # one slot frees: the older job takes it
    assert a.wait(10) == 0 and b.wait(10) == 0
    assert agate.event(a, "start")["t"] < agate.event(b, "start")["t"]
    for f in held[1:]:
        f.close()


def test_a_short_job_starts_while_the_gpu_drains_for_a_solo_job(agate):
    agate.configure(gpu__solo_patience_min=1 / 60)
    long = agate.start("gpu", "/usr/bin/sleep", "4")
    agate.wait_for_event(long, "start")
    solo = agate.start("gpu-solo", "true")
    agate.wait_for_event(solo, "wait")
    time.sleep(1.2)  # past its patience: the GPU drains for it
    other = agate.start("gpu", "true")
    agate.wait_for_event(other, "wait")
    short = agate.start("gpu-short", "true")
    assert short.wait(10) == 0  # it passed the waiting gpu job, and the solo job
    assert agate.event(long, "end") is None
    assert [p.wait(10) for p in (long, solo, other)] == [0, 0, 0]
    t = {p: agate.event(p, "start")["t"] for p in (long, short, solo, other)}
    assert t[long] < t[short] < agate.event(long, "end")["t"] <= t[solo] < t[other]


def test_short_jobs_stop_starting_during_a_drain_once_no_gpu_job_runs(agate):
    agate.configure(gpu__solo_patience_min=1 / 60)
    first = agate.start("gpu-short", "/usr/bin/sleep", "3")
    agate.wait_for_event(first, "start")
    solo = agate.start("gpu-solo", "true")
    agate.wait_for_event(solo, "wait")
    time.sleep(1.2)
    second = agate.start("gpu-short", "true")  # only a short job runs: starting would put the solo job off again
    assert [p.wait(10) for p in (first, solo, second)] == [0, 0, 0]
    waits = [(e["why"], e["detail"]) for e in agate.events() if e["pid"] == second.pid and e["ev"] == "wait"]
    assert waits[0][0] == "drain" and f"pid {solo.pid}" in waits[0][1]  # then slots, while the solo job runs
    assert agate.event(solo, "end")["t"] <= agate.event(second, "start")["t"]


def test_a_short_job_takes_a_free_gpu_slot_before_the_third(agate):
    p = agate.start("gpu-short", "true")
    assert p.wait(10) == 0
    assert agate.event(p, "start")["slots"] == ["gpu.1"]


def test_with_both_gpu_slots_taken_a_short_job_runs_as_a_third_job(agate):
    held = agate.hold("gpu.1", "gpu.2")
    long = agate.start("gpu", "true")
    agate.wait_for_event(long, "wait")
    short = agate.start("gpu-short", "true")
    assert short.wait(10) == 0  # past the waiting gpu job, which can't use gpu.3
    assert agate.event(short, "start")["slots"] == ["gpu.3"]
    assert agate.event(long, "start") is None
    held[0].close()  # one at a time: freed together, a look between the two closes can find gpu.2 free first
    assert long.wait(10) == 0
    assert agate.event(long, "start")["slots"] == ["gpu.1"]
    held[1].close()


def test_a_third_job_needs_room_for_itself_at_its_cap(agate):
    held = agate.hold("gpu.1", "gpu.2")
    agate.configure(gpu__short_slot_ram_gb=agate.NEVER)  # enough RAM for a gpu job, not for a third one
    p = agate.start("gpu-short", "true")
    assert agate.wait_for_event(p, "wait")["why"] == "ram"
    agate.configure(gpu__short_slot_ram_gb=0)
    assert p.wait(10) == 0
    assert agate.event(p, "start")["slots"] == ["gpu.3"]
    for f in held:
        f.close()


def test_a_gpu_job_never_runs_as_a_third_job(agate):
    held = agate.hold("gpu.1", "gpu.2")
    p = agate.start("gpu", "true")
    agate.wait_for_event(p, "wait")
    time.sleep(0.5)  # several looks with gpu.3 free
    assert agate.event(p, "start") is None
    held[0].close()
    assert p.wait(10) == 0
    held[1].close()


def test_short_jobs_take_the_third_slot_in_turn(agate):
    held = agate.hold("gpu.1", "gpu.2", "gpu.3")
    a = agate.start("gpu-short", "/usr/bin/sleep", "0.5")
    agate.wait_for_event(a, "wait")
    b = agate.start("gpu-short", "true")
    agate.wait_for_event(b, "wait")
    held[2].close()  # gpu.3 frees: the older short job takes it, and the other waits for it to end
    assert a.wait(10) == 0 and b.wait(10) == 0
    assert agate.event(a, "end")["t"] <= agate.event(b, "start")["t"]
    for f in held[:2]:
        f.close()


def test_a_solo_job_waits_for_a_short_job_in_the_third_slot(agate):
    held = agate.hold("gpu.3")
    p = agate.start("gpu-solo", "true")
    agate.wait_for_event(p, "wait")
    time.sleep(0.3)
    assert agate.event(p, "start") is None
    held[0].close()
    assert p.wait(10) == 0


def test_a_cpu_short_job_skips_the_gpu_checks(agate):
    agate.set_gpu(used=24000)  # too little VRAM for any gpu job
    p = agate.start("cpu-short", "true")
    assert p.wait(10) == 0


def test_a_cpu_short_job_tries_the_fourth_cpu_slot_first(agate):
    p = agate.start("cpu-short", "true")
    assert p.wait(10) == 0
    assert agate.event(p, "start")["slots"] == ["cpu.4"]


def test_with_the_fourth_slot_taken_a_cpu_short_job_takes_a_cpu_slot(agate):
    held = agate.hold("cpu.4")
    p = agate.start("cpu-short", "true")
    assert p.wait(10) == 0
    assert agate.event(p, "start")["slots"] == ["cpu.3"]
    held[0].close()


def test_a_cpu_short_job_passes_waiting_cpu_jobs_in_the_fourth_slot(agate):
    held = agate.hold("cpu.1", "cpu.2", "cpu.3")
    long = agate.start("cpu", "true")
    agate.wait_for_event(long, "wait")
    time.sleep(0.3)  # several looks with cpu.4 free: a cpu job never takes it
    short = agate.start("cpu-short", "true")
    assert short.wait(10) == 0
    assert agate.event(short, "start")["slots"] == ["cpu.4"]
    assert agate.event(long, "start") is None
    assert (long.pid, "cpu", "waiting") in agate.status()
    held[0].close()
    assert long.wait(10) == 0
    assert agate.event(long, "start")["slots"] == ["cpu.1"]
    for f in held[1:]:
        f.close()


def test_a_cpu_short_job_keeps_the_cpu_rule_in_the_fourth_slot(agate):
    held = agate.hold("cpu.1", "cpu.2", "cpu.3")
    agate.configure(cpu__ram_needed_gb=agate.NEVER)
    p = agate.start("cpu-short", "true")
    assert agate.wait_for_event(p, "wait")["why"] == "ram"
    assert (p.pid, "cpu-short", "waiting") in agate.status()
    agate.configure(cpu__ram_needed_gb=0)
    assert p.wait(10) == 0
    assert agate.event(p, "start")["slots"] == ["cpu.4"]
    for f in held:
        f.close()


def test_a_waiting_gate_stopped_with_ctrl_c_leaves_the_queue(agate):
    held = agate.hold("cpu.1", "cpu.2", "cpu.3")
    p = agate.start("cpu", "true")
    agate.wait_for_event(p, "wait")
    p.send_signal(signal.SIGINT)
    assert p.wait(10) == 130
    assert agate.status() == set() and list(agate.run_dir.glob("queue/*")) == []
    for f in held:
        f.close()


# ---- the command line and status


@pytest.mark.parametrize(("args", "message"), [
    (["gpu"], "no command"),
    (["gpus", "--", "true"], "kind must be gpu, gpu-short, gpu-solo, cpu or cpu-short"),
    ([], "usage: agent-gate"),
])
def test_a_bad_command_line_exits_2(agate, args, message):
    r = agate.gate(*args)
    assert r.returncode == 2 and message in r.stderr


def test_a_malformed_vram_need_exits_2(agate):
    r = agate.gate("gpu", "--", "true", env={"AGENT_GATE_VRAM_MB": "8G"})
    assert r.returncode == 2 and "AGENT_GATE_VRAM_MB must be a whole number of MiB, not '8G'" in r.stderr
    assert agate.events() == []


def test_without_a_config_it_says_where_to_get_one(agate):
    agate.config.unlink()
    r = agate.gate("cpu", "--", "true")
    assert r.returncode == 2 and "config.toml is missing: copy" in r.stderr


def test_without_a_gpu_section_gpu_jobs_are_refused(agate):
    text = agate.config.read_text()
    agate.config.write_text(text[text.index("\n[cpu]\n"):])
    r = agate.gate("gpu", "--", "true")
    assert r.returncode == 2 and "has no gpu jobs (no [gpu] section)" in r.stderr
    assert agate.gate("cpu", "--", "true").returncode == 0


def test_status_lists_each_gate_on_a_line_that_starts_with_its_pid(agate):
    held = agate.hold("cpu.1", "cpu.2", "cpu.3", "cpu.4")
    p = agate.start("cpu", "sleep", "0")
    agate.wait_for_event(p, "wait")
    lines = agate.gate("status").stdout.splitlines()
    assert lines[0] == "pid kind state needs-idle-gpu age command"
    assert lines[1].startswith(f"{p.pid} cpu waiting no 00:0") and lines[1].endswith(" sleep 0")
    assert lines[2].startswith("programs running: 0; RAM available: ") and lines[2].endswith("; GPU free: 23564 MiB")
    for f in held:
        f.close()
    assert p.wait(10) == 0


def test_run_through_a_link_it_runs_jobs_and_its_messages_use_the_links_name(agate):
    link = agate.root / "bin" / "lab-gate"
    link.parent.mkdir()
    link.symlink_to(agate.BIN)
    run = lambda *args: subprocess.run([str(link), *args], cwd=agate.root, env=agate.env, capture_output=True, text=True,
                                       timeout=30, check=False)
    assert run("cpu", "--", "sh", "-c", "exit 4").returncode == 4
    r = run()
    assert r.returncode == 2 and "usage: lab-gate gpu|" in r.stderr


def test_a_watcher_reads_the_queue_without_removing_stale_tickets(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_gate, "QUEUE", tmp_path)
    me = os.getpid()
    live, stale = (agent_gate.Ticket(me, agent_gate.start_tick(me) + d, "cpu", 0, "/", ["x"]) for d in (0, 1))
    for t in (live, stale):
        (tmp_path / f"{t.pid}-{t.tick}.json").write_text(json.dumps(agent_gate.asdict(t)))
    assert agent_gate.read_queue(remove_stale=False) == [live] and len(list(tmp_path.iterdir())) == 2
    assert agent_gate.read_queue() == [live] and len(list(tmp_path.iterdir())) == 1


def test_a_stale_ticket_is_ignored_and_removed(agate):
    queue = agate.run_dir / "queue"
    queue.mkdir()
    tick = agent_gate.start_tick(os.getpid())
    for pid, t in ((os.getpid(), tick + 1), (2**22 + 7, 5)):  # a reused pid, and a gone one
        (queue / f"{pid}-{t}.json").write_text(json.dumps({"pid": pid, "tick": t, "kind": "cpu", "queued": 0, "cwd": "/", "command": ["x"]}))
    assert agate.status() == set()
    assert list(queue.iterdir()) == []


# ---- pieces


def test_three_seconds_of_thrashing_in_a_row_kill_the_job():
    t = agent_gate.Thrash()
    stalls = [0, 600_000, 1_200_000, 1_300_000, 1_900_000, 2_500_000, 3_100_000]  # µs, a second apart
    assert [t.observe(s) for s in stalls] == [False, False, False, False, False, False, True]


@pytest.mark.parametrize(("n", "text"), [(512, "512"), (48189440, "46M"), (1610612736, "1.5G"), (17179869184, "16G"), (1576960, "1.6M")])
def test_sizes_read_as_numfmt_writes_them(n, text):
    assert agent_gate.iec(n) == text


@pytest.mark.parametrize(("seconds", "text"), [(5, "00:05"), (3725, "01:02:05"), (90061, "1-01:01:01")])
def test_ages_read_as_ps_writes_them(seconds, text):
    assert agent_gate._age(seconds) == text


# ---- the owner, recorded as it queues


def test_the_queue_event_and_ticket_name_whose_job_it_is(agate):
    repo = agate.root / "rover"
    repo.mkdir()
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"], ["worktree", "add", "-q", "--detach", "../pr42"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True)
    (agate.claude / "sessions").mkdir(parents=True)
    me = os.getpid()  # the gate's parent: its session, as an agent's shell would be under one
    (agate.claude / "sessions" / f"{me}.json").write_text(json.dumps({
        "pid": me, "sessionId": "5fe25d7d-4f92-403c-b77d-1253e8b04e2b", "name": "rover-30", "cwd": str(repo),
        "procStart": str(agent_gate.start_tick(me))}))
    held = agate.hold("cpu.1", "cpu.2", "cpu.3")
    p = agate.start("cpu", "true", cwd=agate.root / "pr42")
    agate.wait_for_event(p, "wait")
    owner = {"project": "rover", "session": "rover-30", "worktree": "pr42"}
    assert agate.event(p, "queue")["owner"] == owner
    [ticket] = (json.loads(f.read_text()) for f in (agate.run_dir / "queue").glob("*.json"))
    assert ticket["owner"] == owner
    assert ticket["version"] == agent_gate.version() == agate.event(p, "queue")["gate"]
    for f in held:
        f.close()
    assert p.wait(10) == 0


def test_without_a_session_the_owner_is_its_directorys_alone(agate):
    p = agate.start("cpu", "true")
    assert p.wait(10) == 0
    assert agate.event(p, "queue")["owner"] == {"project": "", "session": "", "worktree": ""}
